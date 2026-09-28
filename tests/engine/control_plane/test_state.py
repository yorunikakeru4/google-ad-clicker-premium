"""Тесты хранилища состояния control plane.

Здесь проверяется контракт с UI: демон пишет в таблицы ``workers``/``logs``/
``kv``, UI (rusqlite) читает их параллельно. Поэтому важны точные значения,
а не «запись какая-то есть».
"""

import sqlite3
import time

import pytest

from engine.control_plane.state import (
    PAUSE_REQUESTED_KEY,
    RUN_STATE_KEY,
    WORKER_STATUSES,
    StateStore,
    WorkerStatus,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "adclicker.db"
    from engine.db import migrations

    migrations.migrate(path)
    return path


@pytest.fixture
def store(db_path):
    return StateStore(db_path)


def _read(db_path, sql, params=()):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


class TestWorkerRegistry:
    """Регистрация воркеров: одна строка на воркер, PID сохраняется."""

    def test_register_worker_creates_row_with_status_starting(self, store, db_path):
        worker_id = store.register_worker("br-1", pid=4242)

        rows = _read(db_path, "SELECT * FROM workers WHERE id = ?", (worker_id,))
        assert len(rows) == 1
        assert rows[0]["browser_id"] == "br-1"
        assert rows[0]["pid"] == 4242
        assert rows[0]["status"] == WorkerStatus.STARTING.value
        assert rows[0]["restart_count"] == 0
        assert rows[0]["last_error"] is None

    def test_register_worker_sets_started_at_and_heartbeat(self, store, db_path):
        before = time.time()

        store.register_worker("br-1", pid=1)

        rows = _read(db_path, "SELECT started_at, heartbeat_at FROM workers")
        assert rows[0]["started_at"] is not None
        assert rows[0]["heartbeat_at"] is not None
        assert rows[0]["started_at"] >= before

    def test_register_two_workers_keeps_them_separate(self, store, db_path):
        store.register_worker("br-1", pid=1)
        store.register_worker("br-2", pid=2)

        rows = _read(db_path, "SELECT browser_id, pid FROM workers ORDER BY browser_id")
        assert [(r["browser_id"], r["pid"]) for r in rows] == [("br-1", 1), ("br-2", 2)]

    def test_register_same_browser_id_twice_updates_existing_row(self, store, db_path):
        first = store.register_worker("br-1", pid=1)
        second = store.register_worker("br-1", pid=2)

        rows = _read(db_path, "SELECT id, pid, status FROM workers")
        assert second == first, "перезапуск не должен плодить дубли воркера"
        assert len(rows) == 1
        assert rows[0]["pid"] == 2
        assert rows[0]["status"] == WorkerStatus.STARTING.value

    def test_get_worker_returns_record(self, store):
        worker_id = store.register_worker("br-7", pid=99)

        worker = store.get_worker("br-7")

        assert worker["id"] == worker_id
        assert worker["browser_id"] == "br-7"
        assert worker["pid"] == 99

    def test_get_missing_worker_returns_none(self, store):
        assert store.get_worker("nope") is None

    def test_list_workers_sorted_by_browser_id(self, store):
        store.register_worker("br-3", pid=3)
        store.register_worker("br-1", pid=1)
        store.register_worker("br-2", pid=2)

        assert [w["browser_id"] for w in store.list_workers()] == ["br-1", "br-2", "br-3"]

    def test_list_workers_empty_on_fresh_database(self, store):
        assert store.list_workers() == []


class TestWorkerStatusTransitions:
    """Смена статуса и учёт рестартов."""

    def test_set_status_updates_row(self, store, db_path):
        store.register_worker("br-1", pid=1)

        store.set_status("br-1", WorkerStatus.RUNNING)

        rows = _read(db_path, "SELECT status FROM workers WHERE browser_id = 'br-1'")
        assert rows[0]["status"] == WorkerStatus.RUNNING.value

    def test_set_status_with_error_records_last_error(self, store, db_path):
        store.register_worker("br-1", pid=1)

        store.set_status("br-1", WorkerStatus.CIRCUIT_OPEN, error="restart limit reached")

        rows = _read(db_path, "SELECT status, last_error FROM workers")
        assert rows[0]["status"] == WorkerStatus.CIRCUIT_OPEN.value
        assert rows[0]["last_error"] == "restart limit reached"

    def test_increment_restart_count_returns_new_value(self, store):
        store.register_worker("br-1", pid=1)

        assert store.increment_restart_count("br-1") == 1
        assert store.increment_restart_count("br-1") == 2
        assert store.increment_restart_count("br-1") == 3

    def test_increment_restart_count_persists(self, store, db_path):
        store.register_worker("br-1", pid=1)
        store.increment_restart_count("br-1")
        store.increment_restart_count("br-1")

        rows = _read(db_path, "SELECT restart_count FROM workers")
        assert rows[0]["restart_count"] == 2

    def test_reset_restart_count_on_successful_run(self, store, db_path):
        store.register_worker("br-1", pid=1)
        store.increment_restart_count("br-1")
        store.increment_restart_count("br-1")

        store.reset_restart_count("br-1")

        rows = _read(db_path, "SELECT restart_count FROM workers")
        assert rows[0]["restart_count"] == 0

    def test_heartbeat_updates_timestamp_only(self, store, db_path):
        store.register_worker("br-1", pid=1)
        before = _read(db_path, "SELECT started_at, status FROM workers")[0]
        store.set_status("br-1", WorkerStatus.RUNNING)
        time.sleep(0.01)

        store.heartbeat("br-1", now=1_000_000.5)

        after = _read(db_path, "SELECT started_at, heartbeat_at, status FROM workers")[0]
        assert after["heartbeat_at"] == 1_000_000.5
        assert after["started_at"] == before["started_at"], "heartbeat не должен трогать started_at"
        assert after["status"] == WorkerStatus.RUNNING.value

    def test_clear_pid_after_exit(self, store, db_path):
        store.register_worker("br-1", pid=1234)

        store.set_status("br-1", WorkerStatus.STOPPED)

        rows = _read(db_path, "SELECT pid, status FROM workers")
        assert rows[0]["pid"] is None, "мёртвый PID не должен оставаться в БД"
        assert rows[0]["status"] == WorkerStatus.STOPPED.value

    def test_worker_statuses_cover_documented_lifecycle(self):
        assert {s.value for s in WorkerStatus} == {
            "starting",
            "running",
            "backoff",
            "stopped",
            "circuit_open",
        }
        assert WORKER_STATUSES == {s.value for s in WorkerStatus}


class TestPauseFlag:
    """Пауза живёт в kv, а не в памяти: воркеры — отдельные процессы."""

    def test_pause_not_requested_by_default(self, store):
        assert store.is_pause_requested() is False

    def test_request_pause_then_check(self, store, db_path):
        store.request_pause()

        assert store.is_pause_requested() is True
        rows = _read(db_path, "SELECT value FROM kv WHERE key = ?", (PAUSE_REQUESTED_KEY,))
        assert rows[0]["value"] == "1"

    def test_clear_pause(self, store):
        store.request_pause()

        store.clear_pause()

        assert store.is_pause_requested() is False

    def test_request_pause_twice_is_idempotent(self, store):
        store.request_pause()
        store.request_pause()

        assert store.is_pause_requested() is True

    def test_pause_flag_readable_by_a_second_connection(self, store, db_path):
        """Ровно тот сценарий, ради которого флаг и живёт в kv."""
        store.request_pause()

        with sqlite3.connect(db_path) as conn:
            value = conn.execute(
                "SELECT value FROM kv WHERE key = ?", (PAUSE_REQUESTED_KEY,)
            ).fetchone()

        assert value[0] == "1"


class TestRunState:
    """Глобальное состояние демона тоже в kv: UI читает его без демона."""

    def test_initial_state_is_stopped(self, store):
        assert store.get_run_state() == "stopped"

    def test_set_run_state_round_trip(self, store, db_path):
        store.set_run_state("running")

        assert store.get_run_state() == "running"
        rows = _read(db_path, "SELECT key, value FROM kv WHERE key = ?", (RUN_STATE_KEY,))
        assert rows[0]["value"] == "running"

    def test_unknown_stored_state_falls_back_to_stopped(self, store, db_path):
        with sqlite3.connect(db_path) as conn:
            conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)", (RUN_STATE_KEY, "banana"))
            conn.commit()

        assert store.get_run_state() == "stopped"

    def test_paused_is_a_known_state(self, store):
        store.set_run_state("paused")

        assert store.get_run_state() == "paused"

    def test_stopping_is_a_known_state(self, store):
        store.set_run_state("stopping")

        assert store.get_run_state() == "stopping"


class TestLogging:
    """Структурированные логи демона: UI читает их из таблицы logs."""

    def test_log_writes_row_with_level_and_category(self, store, db_path):
        store.log("INFO", "supervisor", "worker spawned", {"pid": 42})

        rows = _read(db_path, "SELECT level, category, message, fields FROM logs")
        assert rows[0]["level"] == "INFO"
        assert rows[0]["category"] == "supervisor"
        assert rows[0]["message"] == "worker spawned"
        assert '"pid": 42' in rows[0]["fields"]

    def test_log_records_browser_id_when_given(self, store, db_path):
        store.log("WARN", "supervisor", "worker crashed", browser_id="br-1")

        rows = _read(db_path, "SELECT browser_id FROM logs")
        assert rows[0]["browser_id"] == "br-1"

    def test_log_without_fields_stores_null(self, store, db_path):
        store.log("INFO", "api", "started")

        rows = _read(db_path, "SELECT fields FROM logs")
        assert rows[0]["fields"] is None

    def test_log_timestamp_is_recent(self, store, db_path):
        before = time.time()

        store.log("INFO", "api", "tick")

        rows = _read(db_path, "SELECT ts FROM logs")
        assert rows[0]["ts"] >= before

    def test_default_level_is_info(self, store, db_path):
        store.log(category="api", message="no level")

        rows = _read(db_path, "SELECT level FROM logs")
        assert rows[0]["level"] == "INFO"


class TestRunRecords:
    """Записи о запусках сценария: по одной строке на запуск воркера."""

    def test_start_run_creates_running_row(self, store, db_path):
        worker_id = store.register_worker("br-1", pid=1)

        run_id = store.start_run(worker_id)

        rows = _read(db_path, "SELECT worker_id, status, started_at, ended_at FROM runs WHERE id = ?", (run_id,))
        assert rows[0]["worker_id"] == worker_id
        assert rows[0]["status"] == "running"
        assert rows[0]["started_at"] is not None
        assert rows[0]["ended_at"] is None

    def test_finish_run_sets_ended_at_and_status(self, store, db_path):
        worker_id = store.register_worker("br-1", pid=1)
        run_id = store.start_run(worker_id)

        store.finish_run(run_id, "completed")

        rows = _read(db_path, "SELECT status, ended_at FROM runs WHERE id = ?", (run_id,))
        assert rows[0]["status"] == "completed"
        assert rows[0]["ended_at"] is not None

    def test_finish_run_records_error(self, store, db_path):
        worker_id = store.register_worker("br-1", pid=1)
        run_id = store.start_run(worker_id)

        store.finish_run(run_id, "failed", error="selenium timeout")

        rows = _read(db_path, "SELECT status, error FROM runs WHERE id = ?", (run_id,))
        assert rows[0]["status"] == "failed"
        assert rows[0]["error"] == "selenium timeout"

    def test_add_run_counters(self, store, db_path):
        worker_id = store.register_worker("br-1", pid=1)
        run_id = store.start_run(worker_id)

        store.add_run_counts(run_id, clicks=7, captcha_seen=1, captcha_solved=0)

        rows = _read(db_path, "SELECT total_clicks, captcha_seen, captcha_solved FROM runs WHERE id = ?", (run_id,))
        assert rows[0]["total_clicks"] == 7
        assert rows[0]["captcha_seen"] == 1
        assert rows[0]["captcha_solved"] == 0

    def test_latest_run_for_worker(self, store):
        worker_id = store.register_worker("br-1", pid=1)
        first = store.start_run(worker_id)
        store.finish_run(first, "completed")
        second = store.start_run(worker_id)

        assert store.latest_run_id(worker_id) == second

    def test_latest_run_is_none_without_runs(self, store):
        worker_id = store.register_worker("br-1", pid=1)

        assert store.latest_run_id(worker_id) is None


class TestSnapshot:
    """Снимок состояния для /state: один консистентный срез."""

    def test_snapshot_reports_stopped_with_no_workers(self, store):
        snapshot = store.snapshot()

        assert snapshot["state"] == "stopped"
        assert snapshot["paused"] is False
        assert snapshot["workers"] == []
        assert snapshot["worker_count"] == 0
        assert snapshot["alive_count"] == 0

    def test_snapshot_counts_only_running_workers_as_alive(self, store):
        store.register_worker("br-1", pid=1)
        store.set_status("br-1", WorkerStatus.RUNNING)
        store.register_worker("br-2", pid=2)
        store.set_status("br-2", WorkerStatus.STOPPED)

        snapshot = store.snapshot()

        assert snapshot["worker_count"] == 2
        assert snapshot["alive_count"] == 1

    def test_snapshot_reflects_pause_flag(self, store):
        store.set_run_state("running")
        store.request_pause()

        snapshot = store.snapshot()

        assert snapshot["state"] == "running"
        assert snapshot["paused"] is True

    def test_snapshot_includes_restart_count_and_last_error(self, store):
        store.register_worker("br-1", pid=1)
        store.increment_restart_count("br-1")
        store.set_status("br-1", WorkerStatus.CIRCUIT_OPEN, error="too many restarts")

        worker = store.snapshot()["workers"][0]

        assert worker["restart_count"] == 1
        assert worker["last_error"] == "too many restarts"
        assert worker["status"] == "circuit_open"

    def test_snapshot_worker_has_no_secret_or_internal_columns(self, store):
        """Снимок идёт наружу по HTTP: лишних колонок быть не должно."""
        store.register_worker("br-1", pid=1)

        worker = store.snapshot()["workers"][0]

        assert set(worker) == {
            "browser_id",
            "pid",
            "status",
            "restart_count",
            "started_at",
            "heartbeat_at",
            "last_error",
        }

    def test_snapshot_has_monotonic_timestamp(self, store):
        snapshot = store.snapshot()

        assert snapshot["updated_at"] >= 0.0


class TestThreadSafety:
    """Супервизор пишет из своего потока, HTTP — из своих: блокировки обязательны."""

    def test_concurrent_heartbeats_from_many_threads_all_land(self, store, db_path):
        import threading

        store.register_worker("br-1", pid=1)
        errors = []

        def beat(index):
            try:
                for _ in range(10):
                    store.heartbeat("br-1", now=1000.0 + index)
            except Exception as exc:  # noqa: BLE001 - тест обязан увидеть причину
                errors.append(exc)

        threads = [threading.Thread(target=beat, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        rows = _read(db_path, "SELECT heartbeat_at FROM workers")
        assert rows[0]["heartbeat_at"] in {1000.0, 1001.0, 1002.0}

    def test_concurrent_worker_registration_keeps_unique_browser_ids(self, store):
        import threading

        errors = []

        def register(index):
            try:
                store.register_worker(f"br-{index}", pid=100 + index)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=register, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert sorted(w["browser_id"] for w in store.list_workers()) == ["br-0", "br-1", "br-2"]
