"""Тесты батчевого writer'а SQLite (engine/store.py).

Контракт, который здесь зафиксирован:

- логи, клики и сетевые запросы буферизуются и уходят в БД батчами: по размеру
  или по таймеру (разделяют один общий размер батча);
- конкурентная запись из потоков ничего не теряет, порядок внутри воркера kept;
- close() сбрасывает остаток и безопасен повторно;
- ошибка записи не роняет вызывающего: счётчик потерь + последняя ошибка,
  запись продолжается.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from engine.db import migrations
from engine.store import StoreWriter


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


def _read(db_path, sql, params=()):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


def _wait_for(predicate, timeout_s=5.0):
    """Ожидание условия без фиксированного долгого сна."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestLogBatching:
    def test_log_visible_only_after_flush_while_batch_not_full(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer.log(level="INFO", category="click", message="hello", browser_id="br-1")

            assert _read(db_path, "SELECT * FROM logs") == []

            writer.flush()

            rows = _read(db_path, "SELECT level, browser_id, category, message FROM logs")
            assert [(r["level"], r["browser_id"], r["category"], r["message"]) for r in rows] == [
                ("INFO", "br-1", "click", "hello")
            ]
        finally:
            writer.close()

    def test_log_fields_and_ts_round_trip(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer.log(
                level="WARNING",
                category="proxy",
                message="slow",
                browser_id="br-2",
                fields='{"latency_ms": 900}',
                ts=1700000000.5,
            )
            writer.flush()

            rows = _read(db_path, "SELECT ts, level, browser_id, category, message, fields FROM logs")
            assert len(rows) == 1
            assert rows[0]["ts"] == 1700000000.5
            assert rows[0]["level"] == "WARNING"
            assert rows[0]["browser_id"] == "br-2"
            assert rows[0]["category"] == "proxy"
            assert rows[0]["message"] == "slow"
            assert rows[0]["fields"] == '{"latency_ms": 900}'
        finally:
            writer.close()

    def test_batch_size_triggers_automatic_flush(self, db_path):
        writer = StoreWriter(db_path, batch_size=3, flush_interval=60.0)
        try:
            writer.log(level="INFO", category="click", message="m1", browser_id="br-1")
            writer.log(level="INFO", category="click", message="m2", browser_id="br-1")
            assert _read(db_path, "SELECT COUNT(*) AS n FROM logs")[0]["n"] == 0

            writer.log(level="INFO", category="click", message="m3", browser_id="br-1")

            rows = _read(db_path, "SELECT message FROM logs ORDER BY id")
            assert [r["message"] for r in rows] == ["m1", "m2", "m3"]
        finally:
            writer.close()

    def test_flush_interval_triggers_timed_flush(self, db_path):
        writer = StoreWriter(db_path, batch_size=1000, flush_interval=0.05)
        try:
            writer.log(level="INFO", category="scheduler", message="tick", browser_id="br-1")

            assert _wait_for(
                lambda: len(_read(db_path, "SELECT * FROM logs")) == 1
            ), "таймер не сбросил батч"
        finally:
            writer.close()

    def test_click_values_round_trip(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer.record_click(
                url="https://example.com/a",
                query="shoes",
                category="search",
                browser_id="br-1",
                http_status=200,
                ts=1700000001.0,
            )
            writer.flush()

            rows = _read(
                db_path,
                "SELECT ts, url, query, category, browser_id, http_status FROM clicks",
            )
            assert len(rows) == 1
            assert rows[0]["ts"] == 1700000001.0
            assert rows[0]["url"] == "https://example.com/a"
            assert rows[0]["query"] == "shoes"
            assert rows[0]["category"] == "search"
            assert rows[0]["browser_id"] == "br-1"
            assert rows[0]["http_status"] == 200
        finally:
            writer.close()


class TestConcurrency:
    def test_concurrent_writers_lose_nothing_and_keep_per_worker_order(self, db_path):
        writer = StoreWriter(db_path, batch_size=7, flush_interval=0.02)
        worker_ids = ["br-1", "br-2", "br-3", "br-4"]
        per_worker = 25

        def work(browser_id):
            for i in range(per_worker):
                writer.log(
                    level="INFO",
                    category="click",
                    message=f"{browser_id}-{i:03d}",
                    browser_id=browser_id,
                )
            writer.heartbeat(browser_id)

        threads = [threading.Thread(target=work, args=(bid,)) for bid in worker_ids]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            writer.flush()
        finally:
            writer.close()

        rows = _read(db_path, "SELECT COUNT(*) AS n FROM logs")[0]["n"]
        assert rows == len(worker_ids) * per_worker

        for browser_id in worker_ids:
            messages = [
                r["message"]
                for r in _read(
                    db_path,
                    "SELECT message FROM logs WHERE browser_id = ? ORDER BY id",
                    (browser_id,),
                )
            ]
            assert messages == [f"{browser_id}-{i:03d}" for i in range(per_worker)]

        heartbeats = {
            r["browser_id"]: r["heartbeat_at"]
            for r in _read(db_path, "SELECT browser_id, heartbeat_at FROM workers")
        }
        assert set(heartbeats) == set(worker_ids)
        assert all(value is not None for value in heartbeats.values())


class TestClose:
    def test_close_flushes_remainder_and_second_close_is_safe(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        writer.log(level="INFO", category="cleanup", message="last", browser_id="br-1")
        writer.record_click(url="https://example.com/last", browser_id="br-1")

        writer.close()
        writer.close()

        assert _read(db_path, "SELECT COUNT(*) AS n FROM logs")[0]["n"] == 1
        assert _read(db_path, "SELECT COUNT(*) AS n FROM clicks")[0]["n"] == 1

    def test_write_after_close_does_not_raise_and_counts_dropped(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        writer.close()

        writer.log(level="INFO", category="click", message="late", browser_id="br-1")

        assert writer.dropped == 1
        assert writer.last_error is not None
        assert _read(db_path, "SELECT COUNT(*) AS n FROM logs")[0]["n"] == 0


class TestWriteErrors:
    def test_closed_db_does_not_raise_and_counts_losses(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer.log(level="INFO", category="click", message="m1", browser_id="br-1")
            writer.flush()
            assert writer.dropped == 0
            assert _read(db_path, "SELECT COUNT(*) AS n FROM logs")[0]["n"] == 1

            # Ломаем соединение из-под writer'а: имитация закрытой/битой БД.
            writer._conn.close()

            writer.log(level="INFO", category="click", message="m2", browser_id="br-1")
            writer.flush()

            assert writer.dropped == 1
            assert writer.last_error is not None
        finally:
            writer.close()

    def test_writer_keeps_accepting_records_after_error(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer._conn.close()

            writer.log(level="INFO", category="click", message="m1", browser_id="br-1")
            writer.flush()
            dropped_after_first = writer.dropped

            # Воркер продолжает работать: второй вызов тоже не роняет.
            writer.log(level="INFO", category="click", message="m2", browser_id="br-1")
            writer.flush()

            assert writer.dropped == dropped_after_first + 1
            assert writer.last_error is not None
        finally:
            writer.close()

    def test_heartbeat_error_does_not_raise(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer._conn.close()

            writer.heartbeat("br-1")

            assert writer.dropped >= 1
            assert writer.last_error is not None
        finally:
            writer.close()


class TestRunsAndHeartbeat:
    def test_start_add_finish_run_round_trip(self, db_path):
        from engine.control_plane.state import StateStore

        state = StateStore(db_path)
        worker_id = state.register_worker("br-1", pid=100)

        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            run_id = writer.start_run(worker_id)
            assert isinstance(run_id, int) and run_id > 0

            writer.add_run_counts(run_id, clicks=2, captcha_seen=1, captcha_solved=1)
            writer.finish_run(run_id, status="ok")

            rows = _read(
                db_path,
                "SELECT status, total_clicks, captcha_seen, captcha_solved, ended_at "
                "FROM runs WHERE id = ?",
                (run_id,),
            )
            assert len(rows) == 1
            assert rows[0]["status"] == "ok"
            assert rows[0]["total_clicks"] == 2
            assert rows[0]["captcha_seen"] == 1
            assert rows[0]["captcha_solved"] == 1
            assert rows[0]["ended_at"] is not None
        finally:
            writer.close()

    def test_heartbeat_updates_workers_row(self, db_path):
        from engine.control_plane.state import StateStore

        state = StateStore(db_path)
        state.register_worker("br-1", pid=100)

        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            before = time.time()
            writer.heartbeat("br-1")

            rows = _read(db_path, "SELECT heartbeat_at FROM workers WHERE browser_id = 'br-1'")
            assert len(rows) == 1
            assert rows[0]["heartbeat_at"] >= before
        finally:
            writer.close()

    def test_heartbeat_visible_without_prior_register(self, db_path):
        """Воркер может стукнуть раньше, чем супервизор зарегистрировал строку."""
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer.heartbeat("br-new")

            rows = _read(db_path, "SELECT browser_id, heartbeat_at FROM workers")
            assert [(r["browser_id"], r["heartbeat_at"] is not None) for r in rows] == [
                ("br-new", True)
            ]
        finally:
            writer.close()

    def test_start_run_failure_returns_none_and_counts(self, db_path):
        from engine.control_plane.state import StateStore

        state = StateStore(db_path)
        worker_id = state.register_worker("br-1", pid=100)

        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer._state = None  # ломаем путь runs: имитация недоступности БД

            assert writer.start_run(worker_id) is None
            assert writer.dropped >= 1
            assert writer.last_error is not None
        finally:
            writer._state = StateStore(db_path)
            writer.close()


class TestMarkDegraded:
    """Сигнал деградации: ``status='degraded'`` и ``last_error``, и ничего больше.

    Колонки ``pid``/``started_at``/``restart_count``/``heartbeat_at`` —
    супервизорские: воркер не знает, жив ли его процесс и был ли перезапуск,
    поэтому его запись не должна их перетирать (тот же порядок, что у
    heartbeat-upsert).
    """

    def test_marks_existing_row_without_touching_supervisor_columns(self, db_path):
        from engine.control_plane.state import StateStore, WorkerStatus

        state = StateStore(db_path)
        state.register_worker("br-1", pid=100, now=1000.0)
        state.set_status("br-1", WorkerStatus.RUNNING)
        state.increment_restart_count("br-1")

        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer.mark_degraded("br-1", "cdp connection lost")

            rows = _read(
                db_path,
                "SELECT status, last_error, pid, started_at, restart_count, heartbeat_at "
                "FROM workers WHERE browser_id = 'br-1'",
            )
            assert len(rows) == 1
            row = rows[0]
            assert row["status"] == "degraded"
            assert row["last_error"] == "cdp connection lost"
            assert row["pid"] == 100
            assert row["started_at"] == 1000.0
            assert row["restart_count"] == 1
            assert row["heartbeat_at"] == 1000.0
        finally:
            writer.close()

    def test_creates_the_row_when_worker_was_never_registered(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer.mark_degraded("br-new", "proxy rejected credentials")

            rows = _read(
                db_path,
                "SELECT status, last_error, pid FROM workers WHERE browser_id = 'br-new'",
            )
            assert [(r["status"], r["last_error"], r["pid"]) for r in rows] == [
                ("degraded", "proxy rejected credentials", None),
            ]
        finally:
            writer.close()

    def test_repeated_marks_keep_one_row_and_replace_the_reason(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer.mark_degraded("br-1", "cdp connection lost")
            writer.mark_degraded("br-1", "proxy rejected credentials")

            rows = _read(
                db_path,
                "SELECT status, last_error FROM workers WHERE browser_id = 'br-1'",
            )
            assert [(r["status"], r["last_error"]) for r in rows] == [
                ("degraded", "proxy rejected credentials"),
            ]
        finally:
            writer.close()

    def test_write_error_does_not_raise_and_counts_losses(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer._conn.close()

            writer.mark_degraded("br-1", "cdp connection lost")

            assert writer.dropped >= 1
            assert writer.last_error is not None
        finally:
            writer.close()

    def test_closed_writer_does_not_raise_and_counts_losses(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        writer.close()

        writer.mark_degraded("br-1", "cdp connection lost")

        assert writer.dropped >= 1
        assert writer.last_error is not None


class TestRecordDiagnostic:
    """Снимок диагностики: немедленная запись, JSON-колонки, политика ошибок."""

    def test_writes_every_column_without_an_explicit_flush(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer.record_diagnostic(
                browser_id="br-1",
                ts=1700000000.25,
                proxy_id=3,
                ip="203.0.113.7",
                country="DE",
                user_agent="UA/1.0",
                accept_language="de-DE,de;q=0.9",
                timezone_id="Europe/Berlin",
                screen_w=1920,
                screen_h=1080,
                platform="Win32",
                webgl_vendor="Google Inc. (NVIDIA)",
                webgl_renderer="ANGLE (NVIDIA, GeForce)",
                browser_version="131.0.6778.86",
                headers={"User-Agent": "UA/1.0", "Accept-Language": "de-DE"},
                suspicion_flags=["timezone не соответствует гео"],
            )

            rows = _read(db_path, "SELECT * FROM diagnostics")
        finally:
            writer.close()

        assert len(rows) == 1, "снимок должен попасть в БД сразу, без flush"
        row = rows[0]
        assert row["ts"] == 1700000000.25
        assert row["browser_id"] == "br-1"
        assert row["proxy_id"] == 3
        assert row["ip"] == "203.0.113.7"
        assert row["country"] == "DE"
        assert row["user_agent"] == "UA/1.0"
        assert row["accept_language"] == "de-DE,de;q=0.9"
        assert row["timezone_id"] == "Europe/Berlin"
        assert row["screen_w"] == 1920
        assert row["screen_h"] == 1080
        assert row["platform"] == "Win32"
        assert row["webgl_vendor"] == "Google Inc. (NVIDIA)"
        assert row["webgl_renderer"] == "ANGLE (NVIDIA, GeForce)"
        assert row["browser_version"] == "131.0.6778.86"
        assert json.loads(row["headers"]) == {"User-Agent": "UA/1.0", "Accept-Language": "de-DE"}
        assert json.loads(row["suspicion_flags"]) == ["timezone не соответствует гео"]

    def test_optional_fields_are_null_and_json_columns_stay_json(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer.record_diagnostic(browser_id="br-2")

            rows = _read(db_path, "SELECT * FROM diagnostics")
        finally:
            writer.close()

        row = rows[0]
        assert row["proxy_id"] is None
        assert row["ip"] is None
        assert row["country"] is None
        assert row["headers"] is None
        assert json.loads(row["suspicion_flags"]) == [], "пустой список — это JSON, а не NULL"
        assert row["ts"] > 0

    def test_flag_list_survives_as_a_list_even_when_empty(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer.record_diagnostic(browser_id="br-1", suspicion_flags=[])

            flags = _read(db_path, "SELECT suspicion_flags FROM diagnostics")[0][
                "suspicion_flags"
            ]
        finally:
            writer.close()

        assert json.loads(flags) == []

    @pytest.mark.parametrize("browser_id", ["", "   ", None])
    def test_browser_id_is_required(self, db_path, browser_id):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            with pytest.raises(ValueError):
                writer.record_diagnostic(browser_id=browser_id)
        finally:
            writer.close()

    def test_missing_browser_id_argument_is_a_type_error(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            with pytest.raises(TypeError):
                writer.record_diagnostic()
        finally:
            writer.close()

    def test_closed_writer_counts_loss_and_never_raises(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        writer.close()

        writer.record_diagnostic(browser_id="br-1", ip="203.0.113.7")

        assert writer.dropped >= 1
        assert writer.last_error is not None

    def test_broken_connection_counts_loss_and_never_raises(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer._conn.close()

            writer.record_diagnostic(browser_id="br-1")

            assert writer.dropped >= 1
            assert writer.last_error is not None
        finally:
            writer.close()

    def test_writer_keeps_accepting_records_after_a_failed_diagnostic(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer._conn.close()
            writer.record_diagnostic(browser_id="br-1")
            dropped_after_first = writer.dropped

            writer.record_diagnostic(browser_id="br-1")

            assert writer.dropped == dropped_after_first + 1
        finally:
            writer.close()


class TestConstructor:
    def test_rejects_non_positive_batch_size(self, db_path):
        with pytest.raises(ValueError):
            StoreWriter(db_path, batch_size=0, flush_interval=1.0)

    def test_rejects_non_positive_flush_interval(self, db_path):
        with pytest.raises(ValueError):
            StoreWriter(db_path, batch_size=10, flush_interval=0)


class TestNetworkRequests:
    """Записи network_requests: тот же буфер/батч/таймер и та же политика ошибок."""

    def test_record_round_trip(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer.record_network_request(
                method="GET",
                url="https://site.test/a?x=1",
                resource_type="Document",
                status=200,
                browser_id="br-1",
                ts=1700000010.0,
            )
            writer.flush()

            rows = _read(
                db_path,
                "SELECT ts, browser_id, method, url, resource_type, status FROM network_requests",
            )
            assert [
                (r["ts"], r["browser_id"], r["method"], r["url"], r["resource_type"], r["status"])
                for r in rows
            ] == [(1700000010.0, "br-1", "GET", "https://site.test/a?x=1", "Document", 200)]
        finally:
            writer.close()

    def test_optional_fields_and_default_ts(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer.record_network_request(method="GET", url="https://site.test/bare")
            writer.flush()

            rows = _read(db_path, "SELECT ts, browser_id, resource_type, status FROM network_requests")
            assert len(rows) == 1
            assert rows[0]["ts"] > 0
            assert rows[0]["browser_id"] is None
            assert rows[0]["resource_type"] is None
            assert rows[0]["status"] is None
        finally:
            writer.close()

    def test_records_share_the_batch_with_logs_and_clicks(self, db_path):
        writer = StoreWriter(db_path, batch_size=3, flush_interval=60.0)
        try:
            writer.log(level="INFO", category="click", message="m1", browser_id="br-1")
            writer.record_click(url="https://site.test/c", browser_id="br-1")
            writer.record_network_request(
                method="GET", url="https://site.test/n", browser_id="br-1"
            )

            # Явного flush нет: третья запись добила общий батч.
            assert _read(db_path, "SELECT COUNT(*) AS n FROM logs")[0]["n"] == 1
            assert _read(db_path, "SELECT COUNT(*) AS n FROM clicks")[0]["n"] == 1
            assert _read(db_path, "SELECT COUNT(*) AS n FROM network_requests")[0]["n"] == 1
        finally:
            writer.close()

    def test_timer_flush_includes_network_buffer(self, db_path):
        writer = StoreWriter(db_path, batch_size=1000, flush_interval=0.05)
        try:
            writer.record_network_request(
                method="GET", url="https://site.test/tick", browser_id="br-1"
            )

            assert _wait_for(lambda: len(_read(db_path, "SELECT * FROM network_requests")) == 1), (
                "таймер не сбросил буфер network_requests"
            )
        finally:
            writer.close()

    def test_close_flushes_network_remainder(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        writer.record_network_request(
            method="GET", url="https://site.test/last", browser_id="br-1"
        )

        writer.close()

        assert _read(db_path, "SELECT COUNT(*) AS n FROM network_requests")[0]["n"] == 1

    def test_write_after_close_counts_dropped_and_never_raises(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        writer.close()

        writer.record_network_request(method="GET", url="https://site.test/late")

        assert writer.dropped == 1
        assert writer.last_error is not None
        assert _read(db_path, "SELECT COUNT(*) AS n FROM network_requests")[0]["n"] == 0

    def test_write_error_does_not_raise_and_counts_losses(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            # Ломаем соединение из-под writer'а: имитация закрытой/битой БД.
            writer._conn.close()

            writer.record_network_request(method="GET", url="https://site.test/x")
            writer.flush()

            assert writer.dropped == 1
            assert writer.last_error is not None
        finally:
            writer.close()


class TestCountNetworkRequests:
    """Скользящее окно: границы включительно, фильтр по browser_id, ноль в пустом окне."""

    @pytest.fixture
    def populated(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        rows = [
            ("GET", "https://a.test/1", "Document", 200, "br-1", 100.0),
            ("POST", "https://a.test/2", "XHR", 500, "br-1", 105.0),
            ("GET", "https://a.test/3", "Image", 304, "br-2", 110.0),
        ]
        for method, url, resource_type, status, browser_id, ts in rows:
            writer.record_network_request(
                method=method,
                url=url,
                resource_type=resource_type,
                status=status,
                browser_id=browser_id,
                ts=ts,
            )
        writer.flush()
        yield writer
        writer.close()

    def test_window_bounds_are_inclusive(self, populated):
        assert populated.count_network_requests(100.0, until=110.0) == 3
        assert populated.count_network_requests(100.0, until=109.9) == 2
        assert populated.count_network_requests(105.0, until=110.0) == 2
        assert populated.count_network_requests(101.0, until=109.0) == 1

    def test_filters_by_browser_id(self, populated):
        assert populated.count_network_requests(100.0, until=110.0, browser_id="br-1") == 2
        assert populated.count_network_requests(100.0, until=110.0, browser_id="br-2") == 1
        assert populated.count_network_requests(100.0, until=110.0, browser_id="br-9") == 0

    def test_empty_window_is_zero(self, populated):
        assert populated.count_network_requests(1000.0) == 0
        assert populated.count_network_requests(0.0, until=99.0) == 0

    def test_open_ended_window_counts_everything_from_since(self, populated):
        assert populated.count_network_requests(105.0) == 2
        assert populated.count_network_requests(0.0) == 3

    def test_count_sees_buffered_rows_without_explicit_flush(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer.record_network_request(
                method="GET", url="https://a.test/buffered", browser_id="br-1", ts=50.0
            )

            assert writer.count_network_requests(0.0) == 1
        finally:
            writer.close()

    def test_count_on_closed_writer_returns_zero_and_never_raises(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        writer.close()

        assert writer.count_network_requests(0.0) == 0
        assert writer.last_error is not None

    def test_count_on_broken_connection_returns_zero_and_never_raises(self, db_path):
        writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
        try:
            writer.record_network_request(method="GET", url="https://a.test/x", ts=10.0)
            writer.flush()
            writer._conn.close()

            assert writer.count_network_requests(0.0) == 0
            assert writer.last_error is not None
        finally:
            writer.close()
