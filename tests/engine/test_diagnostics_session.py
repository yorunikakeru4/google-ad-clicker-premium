"""Триггеры диагностики: автосбор за сессию, kv-сигнал, устойчивость к сбоям.

Здесь проверяется «клей» между контрактом API и живым браузером:

- автосбор срабатывает ровно один раз на сессию воркера — при первом
  чекпоинте с драйвером, а не на каждой итерации цикла;
- kv-сигнал ``DIAGNOSTICS_REQUESTED_<browser_id>`` собирается при первом
  живом драйвере и снимается после ЛЮБОЙ попытки (успех или отказ) — иначе
  запрос превращается в вечный ретрай;
- чекпоинт цикла (пауза, между итерациями) сигнал только наблюдает: живого
  браузера там нет, поэтому флаг остаётся до следующего сценария;
- ни один путь сбора не роняет воркер: любой сбой — WARNING в ``browser``.

Сети нет: страница, echo и внутренний IP приходят с заглушек.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import time as clock_time

import pytest

from engine.control_plane.state import StateStore
from engine.db import migrations
from engine.diagnostics import (
    DiagnosticSnapshot,
    EchoResult,
    observe_signal,
    proxy_context,
    read_signal,
    request_signal,
    reset_session_state,
    session_checkpoint,
)
from engine.log import StructuredLogger
from engine.store import StoreWriter
from engine.worker import EXIT_OK, WorkerRunner
from tests.engine.test_worker import FakeSource, default_settings

PAGE = {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.6778.86",
    "accept_language": "de-DE,de;q=0.9",
    "timezone_id": "Europe/Berlin",
    "screen_w": 1920,
    "screen_h": 1080,
    "window_w": 1600,
    "window_h": 900,
    "platform": "Win32",
    "webdriver": True,
    "hardware_concurrency": 8,
    "device_memory": 8,
    "webgl_vendor": "Google Inc. (NVIDIA)",
    "webgl_renderer": "ANGLE (NVIDIA, GeForce)",
}

LOCALES = {"DE": ["de-DE"], "US": ["en-US"]}


@pytest.fixture(autouse=True)
def clean_session_state():
    """Состояние сессии — процессное; тесты не должны видеть чужие хвосты."""
    reset_session_state()
    yield
    reset_session_state()


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


@pytest.fixture
def store(db_path):
    return StateStore(db_path)


@pytest.fixture
def writer(db_path):
    handle = StoreWriter(db_path, flush_interval=60.0)
    yield handle
    handle.close()


class FakeDriver:
    """Драйвер-заглушка: страница отвечает, echo/IP приходят от fetcher'ов."""

    def __init__(self, page=None, *, capabilities=None, page_error=None) -> None:
        self.page = dict(PAGE) if page is None else page
        self.capabilities = {} if capabilities is None else capabilities
        self.page_error = page_error

    def execute_script(self, script, *args):
        if self.page_error is not None:
            raise self.page_error
        return self.page


class RecordingLogger:
    """Логгер-заглушка: пишет вызовы в списки, снимок — в ``diagnostics``."""

    def __init__(self, record_error: Exception | None = None) -> None:
        self.records: list[tuple[str, str, str, dict]] = []
        self.diagnostics: list[dict] = []
        self.record_error = record_error

    def info(self, category, message, **kwargs):
        self.records.append(("INFO", category, message, kwargs))

    def warning(self, category, message, **kwargs):
        self.records.append(("WARNING", category, message, kwargs))

    def record_diagnostic(self, **kwargs):
        if self.record_error is not None:
            raise self.record_error
        self.diagnostics.append(kwargs)

    def messages(self, level: str) -> list[str]:
        return [m for lvl, _cat, m, _kw in self.records if lvl == level]

    @property
    def warnings(self) -> list[str]:
        return self.messages("WARNING")

    def info_fields(self, message: str) -> list[dict]:
        return [
            kwargs.get("fields", {})
            for lvl, _cat, m, kwargs in self.records
            if lvl == "INFO" and m == message
        ]


SNAPSHOT_LOGGED = "diagnostics snapshot recorded"
SIGNAL_PENDING = "diagnostics requested, waiting for a live browser"


def checkpoint(driver, browser_id="br-1", *, store, logger, **overrides):
    params = dict(
        browser_id=browser_id,
        store=store,
        logger=logger,
        echo_fetcher=lambda _driver: EchoResult(
            headers={"Accept": "application/json"}, ip="203.0.113.7", error=None
        ),
        local_ip_fetcher=lambda _driver: "10.0.0.5",
        locales=LOCALES,
    )
    params.update(overrides)
    return session_checkpoint(driver, **params)


def diagnostics_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM diagnostics ORDER BY id").fetchall()


# --- автосбор -----------------------------------------------------------------


class TestAutoCollectionOnce:
    def test_first_checkpoint_records_a_snapshot_without_any_signal(self, store):
        logger = RecordingLogger()

        first = checkpoint(FakeDriver(), store=store, logger=logger)
        second = checkpoint(FakeDriver(), store=store, logger=logger)

        assert first is True
        assert second is False, "повторный чекпоинт за ту же сессию не должен собирать снова"
        assert len(logger.diagnostics) == 1
        assert logger.info_fields(SNAPSHOT_LOGGED) == [{"reason": "auto"}]

    def test_signal_and_auto_collection_share_one_snapshot(self, store):
        logger = RecordingLogger()
        value = request_signal(store, "br-1")

        checkpoint(FakeDriver(), store=store, logger=logger)

        assert len(logger.diagnostics) == 1
        assert logger.info_fields(SNAPSHOT_LOGGED) == [
            {"reason": "auto+signal", "signal": value}
        ]
        assert read_signal(store, "br-1") is None, "сигнал должен сняться вместе со сбором"

    def test_failed_auto_collection_is_not_retried_on_every_scenario(self, store):
        logger = RecordingLogger()
        driver = FakeDriver(page_error=RuntimeError("session deleted"))

        first = checkpoint(driver, store=store, logger=logger)
        second = checkpoint(FakeDriver(), store=store, logger=logger)

        assert first is False and second is False
        assert len(logger.warnings) == 1, "одна попытка за сессию — ровно один WARNING"

    def test_each_browser_id_has_its_own_session_state(self, store):
        logger = RecordingLogger()

        checkpoint(FakeDriver(), browser_id="br-1", store=store, logger=logger)
        checkpoint(FakeDriver(), browser_id="br-2", store=store, logger=logger)

        assert [d["browser_id"] for d in logger.diagnostics] == ["br-1", "br-2"]


# --- kv-сигнал ----------------------------------------------------------------


class TestSignalDrivenCollection:
    def test_fresh_signal_is_collected_and_cleared(self, store):
        logger = RecordingLogger()
        checkpoint(FakeDriver(), store=store, logger=logger)  # автосбор закрыт
        value = request_signal(store, "br-1", now=1700000100.0)

        used = checkpoint(FakeDriver(), store=store, logger=logger)

        assert used is True
        assert logger.info_fields(SNAPSHOT_LOGGED) == [
            {"reason": "signal", "signal": value}
        ]
        assert read_signal(store, "br-1") is None, "флаг обязан сняться после попытки"

    def test_replayed_value_is_stale_and_does_not_collect(self, store):
        logger = RecordingLogger()
        checkpoint(FakeDriver(), store=store, logger=logger)
        value = request_signal(store, "br-1", now=1700000100.0)
        checkpoint(FakeDriver(), store=store, logger=logger)

        # То же значение поставлено в БД заново — это не новый запрос.
        store.set_flag("DIAGNOSTICS_REQUESTED_br-1", value)
        used = checkpoint(FakeDriver(), store=store, logger=logger)

        assert used is False
        assert len(logger.diagnostics) == 2, "две попытки: автосбор и первый сигнал"

    def test_a_newer_value_after_clearing_collects_again(self, store):
        logger = RecordingLogger()
        checkpoint(FakeDriver(), store=store, logger=logger)
        request_signal(store, "br-1", now=1700000100.0)
        checkpoint(FakeDriver(), store=store, logger=logger)

        request_signal(store, "br-1", now=1700000200.0)
        used = checkpoint(FakeDriver(), store=store, logger=logger)

        assert used is True
        assert len(logger.diagnostics) == 3

    def test_signal_is_cleared_even_when_the_collection_fails(self, store):
        logger = RecordingLogger()
        checkpoint(FakeDriver(), store=store, logger=logger)
        request_signal(store, "br-1", now=1700000100.0)

        used = checkpoint(FakeDriver(page_error=RuntimeError("dead")), store=store, logger=logger)

        assert used is False
        assert read_signal(store, "br-1") is None, (
            "снятый при отказе флаг — иначе вечный ретрай на каждом чекпоинте"
        )
        assert logger.warnings, "отказ должен быть виден в логе"
        assert len(logger.diagnostics) == 1, "снимка нет — сбор упал"

    def test_signal_clear_failure_does_not_escape(self, store):
        logger = RecordingLogger()
        checkpoint(FakeDriver(), store=store, logger=logger)
        request_signal(store, "br-1", now=1700000100.0)

        class ClosuredStore(StateStore):
            def set_flag(self, key, value):
                raise sqlite3.OperationalError("db is locked")

        used = checkpoint(FakeDriver(), store=ClosuredStore(store.db_path), logger=logger)

        assert used is True, "снимок записан — упала только попытка снятия флага"
        assert logger.warnings, "невозможное снятие флага обязано уйти в WARNING"

    def test_new_requests_never_come_out_of_order(self, store):
        """Контракт «значение > последнего обработанного»: повторный запрос
        при уже выставленном флаге обязан его продвинуть, а не совпасть."""
        first = request_signal(store, "br-1", now=1700000100.0)
        second = request_signal(store, "br-1", now=1700000100.0)

        assert float(second) > float(first)


# --- чекпоинт цикла (наблюдение сигнала) ---------------------------------------


class TestObserveSignalInLoop:
    def test_pending_signal_is_logged_once_and_left_in_place(self, store):
        logger = RecordingLogger()
        value = request_signal(store, "br-1", now=1700000100.0)

        first = observe_signal("br-1", store, logger)
        second = observe_signal("br-1", store, logger)

        assert first == value
        assert second is None, "повторное наблюдение не должно спамить в лог"
        assert read_signal(store, "br-1") == value, "цикл не снимает сигнал: браузера нет"
        assert logger.records == [
            (
                "INFO",
                "browser",
                SIGNAL_PENDING,
                {"browser_id": "br-1", "fields": {"signal": value}},
            )
        ]

    def test_no_signal_means_no_log(self, store):
        logger = RecordingLogger()

        assert observe_signal("br-1", store, logger) is None
        assert logger.records == []

    def test_a_new_request_is_logged_again(self, store):
        logger = RecordingLogger()
        request_signal(store, "br-1", now=1700000100.0)
        observe_signal("br-1", store, logger)
        store.set_flag("DIAGNOSTICS_REQUESTED_br-1", "")

        request_signal(store, "br-1", now=1700000200.0)
        observed = observe_signal("br-1", store, logger)

        assert observed is not None
        assert len(logger.records) == 2

    def test_works_without_a_logger(self, store):
        request_signal(store, "br-1", now=1700000100.0)

        assert observe_signal("br-1", store, None) is not None

    def test_flag_read_errors_propagate_to_the_calling_checkpoint(self, store):
        """Наблюдение не глотает ошибку сама: её ловит чекпоинт воркера и
        пишет WARNING — глушить здесь значило бы молчать о сломанной БД."""
        class BrokenStore(StateStore):
            def get_flag(self, key, default=None):
                raise sqlite3.OperationalError("disk I/O error")

        with pytest.raises(sqlite3.OperationalError):
            observe_signal("br-1", BrokenStore(store.db_path), RecordingLogger())


# --- изоляция сбоев: сбор не роняет воркер и сценарий ---------------------------


class TestFailureIsolation:
    def test_exploding_driver_is_contained(self, store):
        logger = RecordingLogger()
        driver = FakeDriver(page_error=RuntimeError("chrome crashed"))

        used = checkpoint(driver, store=store, logger=logger)

        assert used is False
        assert logger.warnings
        assert "chrome crashed" in str(logger.records)

    def test_exploding_fetcher_is_contained(self, store):
        logger = RecordingLogger()

        def boom(_driver):
            raise RuntimeError("fetcher exploded")

        used = checkpoint(FakeDriver(), store=store, logger=logger, echo_fetcher=boom)

        assert used is False
        assert logger.warnings

    def test_exploding_store_read_is_contained(self, store):
        logger = RecordingLogger()

        class BrokenStore(StateStore):
            def get_flag(self, key, default=None):
                raise sqlite3.OperationalError("db is locked")

        used = checkpoint(FakeDriver(), store=BrokenStore(store.db_path), logger=logger)

        assert used is False
        assert logger.warnings
        assert "db is locked" in str(logger.records)

    def test_exploding_record_write_is_contained(self, store):
        logger = RecordingLogger(record_error=RuntimeError("disk full"))

        used = checkpoint(FakeDriver(), store=store, logger=logger)

        assert used is False
        assert logger.warnings
        assert "disk full" in str(logger.records)

    def test_missing_logger_is_tolerated(self, store):
        assert checkpoint(FakeDriver(), store=store, logger=None) is True


# --- прокси и страна -----------------------------------------------------------


class TestProxyContext:
    @staticmethod
    def _add_proxy(db_path, *, country=None):
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO proxies (host, port, username, password, country) "
                "VALUES ('10.0.0.1', 8080, 'alice', 's3cr3t', ?)",
                (country,),
            )
            conn.commit()
            return cursor.lastrowid

    @staticmethod
    def _assign(db_path, browser_id, proxy_id):
        StateStore(db_path).register_worker(browser_id, 999)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE workers SET proxy_id = ?, status = 'running' WHERE browser_id = ?",
                (proxy_id, browser_id),
            )
            conn.commit()

    def test_assigned_proxy_gives_id_and_country(self, store, db_path):
        proxy_id = self._add_proxy(db_path, country="DE")
        self._assign(db_path, "br-1", proxy_id)

        assert proxy_context(store, "br-1") == (proxy_id, "DE")

    def test_proxy_without_a_country_gives_none(self, store, db_path):
        proxy_id = self._add_proxy(db_path, country=None)
        self._assign(db_path, "br-1", proxy_id)

        assert proxy_context(store, "br-1") == (proxy_id, None)

    def test_worker_without_a_proxy(self, store, db_path):
        StateStore(db_path).register_worker("br-1", 999)

        assert proxy_context(store, "br-1") == (None, None)

    def test_unknown_worker(self, store):
        assert proxy_context(store, "nope") == (None, None)


# --- строка в БД целиком --------------------------------------------------------


class TestRowInDatabase:
    def test_snapshot_reaches_every_column(self, store, db_path, writer):
        logger = StructuredLogger(writer, browser_id="br-1")

        checkpoint(FakeDriver(), store=store, logger=logger)

        rows = diagnostics_rows(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["browser_id"] == "br-1"
        assert row["ip"] == "203.0.113.7"
        assert row["user_agent"] == PAGE["user_agent"]
        assert row["accept_language"] == PAGE["accept_language"]
        assert row["timezone_id"] == "Europe/Berlin"
        assert (row["screen_w"], row["screen_h"]) == (1920, 1080)
        assert row["platform"] == "Win32"
        assert row["webgl_vendor"] == PAGE["webgl_vendor"]
        assert row["webgl_renderer"] == PAGE["webgl_renderer"]
        assert row["browser_version"] == "131.0.6778.86", "capabilities пустые — версия из UA"
        assert json.loads(row["headers"]) == {"Accept": "application/json"}
        assert json.loads(row["suspicion_flags"]) == []
        assert row["proxy_id"] is None and row["country"] is None

    def test_no_secrets_from_the_proxy_land_in_the_snapshot(self, store, db_path, writer):
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO proxies (host, port, username, password, country) "
                "VALUES ('10.0.0.1', 8080, 'alice', 's3cr3t', 'DE')"
            )
            proxy_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO workers (browser_id, status, proxy_id) VALUES ('br-1', 'running', ?)",
                (proxy_id,),
            )
            conn.commit()

        logger = StructuredLogger(writer, browser_id="br-1")
        checkpoint(FakeDriver(), store=store, logger=logger)

        row = diagnostics_rows(db_path)[0]
        blob = json.dumps({key: row[key] for key in row.keys()})
        assert "s3cr3t" not in blob
        assert "alice" not in blob

    def test_flags_and_errors_land_in_the_json_column(self, store, db_path, writer):
        logger = StructuredLogger(writer, browser_id="br-1")
        page = dict(PAGE, accept_language="ru-RU", platform="MacIntel")

        checkpoint(
            FakeDriver(page),
            store=store,
            logger=logger,
            echo_fetcher=lambda _d: EchoResult(None, None, "timeout after 5000ms"),
        )

        flags = json.loads(diagnostics_rows(db_path)[0]["suspicion_flags"])
        assert len(flags) == 3, flags
        assert any("DE" in flag for flag in flags)
        assert any("echo" in flag for flag in flags)


# --- чекпоинт цикла воркера ------------------------------------------------------


def _read_logs(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT level, category, message FROM logs ORDER BY id").fetchall()


class TestWorkerLoopCheckpoint:
    @pytest.fixture
    def stop_event(self):
        return threading.Event()

    @pytest.fixture
    def logger(self, writer):
        return StructuredLogger(writer)

    def _runner(self, source, store, logger, stop_event, **overrides):
        params = {
            "browser_id": "br-1",
            "store": store,
            "source": source,
            "logger": logger,
            "stop_event": stop_event,
            "now": lambda: clock_time(12, 30),
            "pause_poll_seconds": 0.01,
            "outside_wait_seconds": 0.01,
        }
        params.update(overrides)
        return WorkerRunner(**params)

    @staticmethod
    def _stopping_source(stop_event, **kwargs):
        source = FakeSource(default_settings(**kwargs))
        source.on_scenario = lambda _request: stop_event.set()
        return source

    def test_loop_notices_a_pending_signal_and_keeps_it_for_the_browser(
        self, store, logger, stop_event, db_path, writer
    ):
        request_signal(store, "br-1", now=1700000100.0)
        source = self._stopping_source(stop_event)

        self._runner(source, store, logger, stop_event).run()
        writer.flush()

        assert read_signal(store, "br-1") is not None, (
            "на паузе/между итерациями браузера нет — сигнал ждёт живого драйвера"
        )
        assert diagnostics_rows(db_path) == [], "цикл без браузера снимок не пишет"
        messages = [r["message"] for r in _read_logs(db_path)]
        assert SIGNAL_PENDING in messages
        assert source.requests, "сценарий при этом должен был отработать"

    def test_loop_is_silent_without_a_signal(self, store, logger, stop_event, db_path, writer):
        source = self._stopping_source(stop_event)

        self._runner(source, store, logger, stop_event).run()
        writer.flush()

        messages = [r["message"] for r in _read_logs(db_path)]
        assert SIGNAL_PENDING not in messages

    def test_loop_survives_a_failing_signal_check(
        self, store, logger, stop_event, db_path, writer
    ):
        class BrokenStore(StateStore):
            def get_flag(self, key, default=None):
                if key.startswith("DIAGNOSTICS_REQUESTED_"):
                    raise sqlite3.OperationalError("db is locked")
                return super().get_flag(key, default)

        source = self._stopping_source(stop_event)

        code = self._runner(source, BrokenStore(db_path), logger, stop_event).run()
        writer.flush()

        assert code == EXIT_OK
        assert source.requests, "сбой чтения сигнала не должен был остановить цикл"
        warnings = [r for r in _read_logs(db_path) if r["level"] == "WARNING"]
        assert warnings, "сбой обязан быть виден в логе"
        assert all(r["category"] == "browser" for r in warnings)


# --- снимок как объект -----------------------------------------------------------


class TestSnapshotRecord:
    def test_record_writes_through_the_logger_binding(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")
        snapshot = DiagnosticSnapshot(ts=1700000000.0, browser_id="br-1", ip="203.0.113.7")

        snapshot.record(logger)

        rows = diagnostics_rows(db_path)
        assert [(r["browser_id"], r["ip"], r["ts"]) for r in rows] == [
            ("br-1", "203.0.113.7", 1700000000.0),
        ]

    def test_record_honours_an_explicit_browser_id(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")

        DiagnosticSnapshot(ts=1.0, browser_id="br-2").record(logger)

        assert [r["browser_id"] for r in diagnostics_rows(db_path)] == ["br-2"]
