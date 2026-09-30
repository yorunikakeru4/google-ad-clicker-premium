"""Тесты job'а часовых метрик в демоне (план §2 ``metrics_hourly``, §5 фаза 12).

Нить ``metrics`` на ``stop_event`` — тот же паттерн, что у proxy-check,
captcha-check и трёх job'ов ротации логов:

* **догон при старте** — пересчитывает последние ``METRICS_CATCHUP_HOURS``
  часов, чтобы рестарт не оставлял пустых или устаревших показателей;
* **тик** — пересчитывает только что закрытый час и текущий (неполный);
* период — окружение ``ADCLICKER_METRICS_INTERVAL``: дефолт час, ``0``
  выключает, мусор — ``ValueError`` уже при сборке демона;
* сбой тика (включая догон) уходит в лог и не убивает ни нить, ни демон.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from engine.control_plane.api import TOKEN_ENV_VAR, TOKEN_HEADER
from engine.control_plane.config import Config
from engine.control_plane.daemon import (
    DEFAULT_METRICS_INTERVAL_SECONDS,
    METRICS_CATCHUP_HOURS,
    METRICS_INTERVAL_ENV_VAR,
    METRICS_THREAD_NAME,
    Daemon,
    build_daemon,
    metrics_interval_from_environ,
)
from engine.control_plane.state import StateStore
from engine.control_plane.supervisor import Supervisor, SupervisorSettings
from engine.metrics import bucket_of
from tests.engine.control_plane.test_supervisor import FakeClock, FakeProcessRegistry

TOKEN = "metrics-job-token"


@pytest.fixture
def db_path(tmp_path):
    from engine.db import migrations

    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


@pytest.fixture
def config_path(tmp_path):
    return tmp_path / "config.json"


@pytest.fixture
def registry():
    return FakeProcessRegistry()


def make_daemon(db_path, config_path, registry, **overrides):
    """Демон без сетевых job'ов: фокус теста — нить метрик."""
    store = StateStore(db_path)
    config = Config.from_dict(
        overrides.pop("config_data", None) or {"behavior": {"browser_count": 2}}
    )
    settings = SupervisorSettings(
        heartbeat_interval=0.01,
        shutdown_grace_seconds=0.05,
        restart_backoff_base=0.01,
        restart_backoff_max=0.02,
        max_restarts=2,
        restart_count_reset_after=3600.0,
        max_workers=8,
    )
    kwargs = {
        "db_path": db_path,
        "config_path": config_path,
        "token": TOKEN,
        "port": 0,
        "store": store,
        "supervisor": Supervisor(
            store=store,
            settings=settings,
            spawn=registry,
            clock=FakeClock(),
            browser_ids=lambda n: [f"br-{i + 1}" for i in range(n)],
        ),
        "config": config,
        "proxy_check_interval": 0.0,
        "captcha_check_interval": 0.0,
    }
    kwargs.update(overrides)
    return Daemon(**kwargs)


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def thread_alive(name):
    return any(thread.name == name and thread.is_alive() for thread in threading.enumerate())


def metric_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT bucket, successes, failures, requests, captchas, uptime_seconds "
            "FROM metrics_hourly ORDER BY bucket"
        ).fetchall()


def row_for(db_path, bucket):
    for row in metric_rows(db_path):
        if row[0] == bucket:
            return row
    return None


def log_messages(db_path):
    with sqlite3.connect(db_path) as conn:
        return [row[0] for row in conn.execute("SELECT message FROM logs ORDER BY id")]


def insert_run(db_path, status, *, created_at, ended_at=None):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO runs (status, created_at, ended_at) VALUES (?, ?, ?)",
            (status, created_at, ended_at),
        )
        conn.commit()


def insert_request(db_path, ts):
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO network_requests (ts) VALUES (?)", (ts,))
        conn.commit()


class TestMetricsIntervalFromEnviron:
    def test_default_is_one_hour(self):
        assert DEFAULT_METRICS_INTERVAL_SECONDS == 3600.0
        assert metrics_interval_from_environ({}) == 3600.0

    def test_blank_value_falls_back_to_the_default(self):
        assert metrics_interval_from_environ({METRICS_INTERVAL_ENV_VAR: "  "}) == 3600.0

    def test_value_is_read_from_the_environment(self):
        assert metrics_interval_from_environ({METRICS_INTERVAL_ENV_VAR: "30"}) == 30.0

    @pytest.mark.parametrize("value", ["0", "-5"])
    def test_zero_or_negative_disables_the_job(self, value):
        assert metrics_interval_from_environ({METRICS_INTERVAL_ENV_VAR: value}) == 0.0

    def test_non_numeric_value_raises(self):
        with pytest.raises(ValueError) as excinfo:
            metrics_interval_from_environ({METRICS_INTERVAL_ENV_VAR: "каждый час"})

        assert METRICS_INTERVAL_ENV_VAR in str(excinfo.value)

    def test_env_var_name_is_documented(self):
        assert METRICS_INTERVAL_ENV_VAR == "ADCLICKER_METRICS_INTERVAL"
        assert METRICS_CATCHUP_HOURS == 24
        assert METRICS_THREAD_NAME == "metrics"


class TestBuildDaemonMetricsInterval:
    def test_build_takes_the_interval_from_the_environment(
        self, db_path, config_path, monkeypatch
    ):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(METRICS_INTERVAL_ENV_VAR, "120")

        daemon = build_daemon(db_path=db_path, config_path=config_path, port=0)

        try:
            assert daemon.metrics_interval == 120.0
        finally:
            daemon.shutdown()

    def test_explicit_interval_wins_over_the_environment(
        self, db_path, config_path, monkeypatch
    ):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(METRICS_INTERVAL_ENV_VAR, "120")

        daemon = build_daemon(
            db_path=db_path, config_path=config_path, port=0, metrics_interval=42.0
        )

        try:
            assert daemon.metrics_interval == 42.0
        finally:
            daemon.shutdown()

    def test_build_rejects_a_non_numeric_interval(self, db_path, config_path, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(METRICS_INTERVAL_ENV_VAR, "раз в час")

        with pytest.raises(ValueError) as excinfo:
            build_daemon(db_path=db_path, config_path=config_path, port=0)

        assert METRICS_INTERVAL_ENV_VAR in str(excinfo.value)


class TestMetricsJob:
    def test_startup_catchup_fills_the_last_day(self, db_path, config_path, registry):
        daemon = make_daemon(db_path, config_path, registry, metrics_interval=3600.0)

        daemon.start()
        try:
            assert wait_until(lambda: len(metric_rows(db_path)) > 0), "догон не записал бакеты"
        finally:
            daemon.shutdown()

        stored = metric_rows(db_path)
        assert len(stored) == METRICS_CATCHUP_HOURS + 1, "окно догона — 24 часа плюс текущий"
        assert len({row[0] for row in stored}) == len(stored), "бакеты не должны дублироваться"
        assert stored[-1][0] - stored[0][0] == METRICS_CATCHUP_HOURS * 3600
        assert all(stored[i + 1][0] - stored[i][0] == 3600 for i in range(len(stored) - 1))

    def test_catchup_recomputes_a_stale_row_written_before_the_restart(
        self, db_path, config_path, registry
    ):
        from engine.metrics import BucketMetrics, write_bucket

        current = bucket_of(time.time())
        write_bucket(db_path, BucketMetrics(current, 777, 777, 777, 777, 777))
        insert_run(db_path, "ok", created_at=current, ended_at=current + 1)

        # Интервал больше часа: пересчитать бакет может только догон.
        daemon = make_daemon(db_path, config_path, registry, metrics_interval=3600.0)

        daemon.start()
        try:
            assert wait_until(
                lambda: (row_for(db_path, current) or (0, 0))[1] == 1
            ), "рестарт обязан пересчитать бакет, а не оставить устаревшую строку"
        finally:
            daemon.shutdown()

        assert len(metric_rows(db_path)) == METRICS_CATCHUP_HOURS + 1

    def test_zero_interval_never_starts_the_job(self, db_path, config_path, registry):
        daemon = make_daemon(db_path, config_path, registry, metrics_interval=0.0)

        daemon.start()
        try:
            time.sleep(0.3)

            assert thread_alive(METRICS_THREAD_NAME) is False
            assert metric_rows(db_path) == []
        finally:
            daemon.shutdown()

    def test_tick_recomputes_the_closed_and_the_current_bucket(
        self, db_path, config_path, registry
    ):
        daemon = make_daemon(db_path, config_path, registry, metrics_interval=0.05)

        daemon.start()
        try:
            current = bucket_of(time.time())
            assert wait_until(lambda: row_for(db_path, current) is not None), "догон не записал час"

            insert_request(db_path, time.time())
            assert wait_until(
                lambda: (row_for(db_path, current) or (0, 0, 0, 0))[3] == 1
            ), "тик не пересчитал текущий бакет"

            previous = current - 3600
            assert row_for(db_path, previous) is not None, "тик обязан держать и закрытый час"
        finally:
            daemon.shutdown()

        assert thread_alive(METRICS_THREAD_NAME) is False

    def test_failure_is_logged_and_the_job_keeps_ticking(
        self, db_path, config_path, registry, monkeypatch
    ):
        import engine.control_plane.daemon as daemon_module

        calls = []

        def broken_refresh(*args, **kwargs):
            calls.append(args)
            raise RuntimeError("база занята")

        monkeypatch.setattr(daemon_module, "refresh_range", broken_refresh)
        daemon = make_daemon(db_path, config_path, registry, metrics_interval=0.05)

        daemon.start()
        try:
            assert wait_until(lambda: len(calls) >= 2), "job остановился после ошибки"
            assert thread_alive(METRICS_THREAD_NAME) is True, "нить обязана пережить сбой"
            assert wait_until(
                lambda: any("metrics" in m for m in log_messages(db_path))
            ), "сбой job'а обязан попасть в лог"
        finally:
            daemon.shutdown()

        assert thread_alive(METRICS_THREAD_NAME) is False

    def test_shutdown_wakes_the_job_instead_of_waiting_a_full_hour(
        self, db_path, config_path, registry
    ):
        daemon = make_daemon(db_path, config_path, registry, metrics_interval=3600.0)
        daemon.start()
        assert thread_alive(METRICS_THREAD_NAME) is True

        started = time.monotonic()
        daemon.shutdown()

        assert time.monotonic() - started < 5.0, "остановка не должна ждать интервал"
        assert thread_alive(METRICS_THREAD_NAME) is False

    def test_daemon_enables_the_job_by_default(self, db_path, config_path, registry):
        daemon = make_daemon(db_path, config_path, registry)

        assert daemon.metrics_interval == DEFAULT_METRICS_INTERVAL_SECONDS

        daemon.start()
        try:
            assert thread_alive(METRICS_THREAD_NAME) is True
        finally:
            daemon.shutdown()


def test_health_stays_up_while_the_job_ticks(db_path, config_path, registry):
    """Метрики не имеют права держать HTTP: тик идёт фоном."""
    import urllib.request

    daemon = make_daemon(db_path, config_path, registry, metrics_interval=0.05)
    daemon.start()
    try:
        assert wait_until(lambda: len(metric_rows(db_path)) > 0)
        request = urllib.request.Request(f"http://127.0.0.1:{daemon.port}/health", method="GET")
        request.add_header(TOKEN_HEADER, TOKEN)
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
    finally:
        daemon.shutdown()
