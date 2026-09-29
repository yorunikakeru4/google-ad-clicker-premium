"""Тесты периодической проверки прокси в демоне.

Расписание живёт в уже существующей фоновой механике демона: отдельная нить
ждёт ``stop_event`` — тот же флаг, что гасит супервизора. Здесь проверяется,
что job действительно крутится, что его остановка не ждёт интервала, что
ноль в настройке его выключает и что упавшая проверка не убивает ни
расписание, ни HTTP.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from engine.control_plane.api import TOKEN_ENV_VAR, TOKEN_HEADER
from engine.control_plane.config import SECRET_MASK
from engine.control_plane.config import Config
from engine.control_plane.daemon import (
    DEFAULT_PROXY_CHECK_INTERVAL_SECONDS,
    PROXY_CHECK_INTERVAL_ENV_VAR,
    PROXY_CHECK_THREAD_NAME,
    Daemon,
    build_daemon,
    proxy_check_interval_from_environ,
)
from engine.control_plane.state import StateStore
from engine.control_plane.supervisor import Supervisor, SupervisorSettings
from engine.proxy_health import CheckInProgressError
from engine.proxy_pool import ProxyPool
from tests.engine.control_plane.test_supervisor import FakeClock, FakeProcessRegistry
from tests.engine.test_proxy_health import LoopbackProxy, closed_port

TOKEN = "periodic-test-token"


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


class FakeChecker:
    """Подмена проверяющего: считает запуски и умеет падать по требованию."""

    def __init__(self, error=None):
        self.calls = 0
        self.error = error
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            self.calls += 1
        if self.error is not None:
            raise self.error


def make_daemon(db_path, config_path, registry, **overrides):
    store = StateStore(db_path)
    config = Config.from_dict({"behavior": {"browser_count": 2}})
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
    }
    kwargs.update(overrides)
    return Daemon(**kwargs)


def get_json(daemon, path):
    request = urllib.request.Request(f"http://127.0.0.1:{daemon.port}{path}", method="GET")
    request.add_header(TOKEN_HEADER, TOKEN)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = json.loads(exc.read().decode("utf-8"))
    return status, payload, time.monotonic() - started


def post_json(daemon, path, body):
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{daemon.port}{path}", data=data, method="POST"
    )
    request.add_header(TOKEN_HEADER, TOKEN)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def get_raw(daemon, path):
    request = urllib.request.Request(f"http://127.0.0.1:{daemon.port}{path}", method="GET")
    request.add_header(TOKEN_HEADER, TOKEN)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read().decode("utf-8")


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def proxy_thread_alive():
    return any(
        thread.name == PROXY_CHECK_THREAD_NAME and thread.is_alive()
        for thread in threading.enumerate()
    )


class TestIntervalFromEnviron:
    def test_default_when_unset(self):
        assert proxy_check_interval_from_environ({}) == DEFAULT_PROXY_CHECK_INTERVAL_SECONDS

    def test_default_is_one_hour(self):
        assert DEFAULT_PROXY_CHECK_INTERVAL_SECONDS == 3600.0

    def test_value_is_read_from_environment(self):
        assert proxy_check_interval_from_environ({PROXY_CHECK_INTERVAL_ENV_VAR: "120"}) == 120.0

    def test_zero_disables_the_job(self):
        assert proxy_check_interval_from_environ({PROXY_CHECK_INTERVAL_ENV_VAR: "0"}) == 0.0

    def test_negative_disables_the_job(self):
        assert proxy_check_interval_from_environ({PROXY_CHECK_INTERVAL_ENV_VAR: "-5"}) == 0.0

    def test_blank_value_falls_back_to_default(self):
        assert (
            proxy_check_interval_from_environ({PROXY_CHECK_INTERVAL_ENV_VAR: "   "})
            == DEFAULT_PROXY_CHECK_INTERVAL_SECONDS
        )

    def test_non_numeric_value_raises(self):
        with pytest.raises(ValueError) as excinfo:
            proxy_check_interval_from_environ({PROXY_CHECK_INTERVAL_ENV_VAR: "каждый час"})

        assert PROXY_CHECK_INTERVAL_ENV_VAR in str(excinfo.value)

    def test_env_var_name_is_documented(self):
        assert PROXY_CHECK_INTERVAL_ENV_VAR == "ADCLICKER_PROXY_CHECK_INTERVAL"


class TestBuildDaemonReadsInterval:
    def test_build_takes_interval_from_environment(self, db_path, config_path, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(PROXY_CHECK_INTERVAL_ENV_VAR, "7")

        daemon = build_daemon(db_path=db_path, config_path=config_path, port=0)

        try:
            assert daemon.proxy_check_interval == 7.0
        finally:
            daemon.shutdown()

    def test_build_rejects_non_numeric_interval(self, db_path, config_path, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(PROXY_CHECK_INTERVAL_ENV_VAR, "раз в час")

        with pytest.raises(ValueError) as excinfo:
            build_daemon(db_path=db_path, config_path=config_path, port=0)

        assert PROXY_CHECK_INTERVAL_ENV_VAR in str(excinfo.value)

    def test_explicit_interval_wins_over_environment(
        self, db_path, config_path, monkeypatch
    ):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(PROXY_CHECK_INTERVAL_ENV_VAR, "7")

        daemon = build_daemon(
            db_path=db_path, config_path=config_path, port=0, proxy_check_interval=42.0
        )

        try:
            assert daemon.proxy_check_interval == 42.0
        finally:
            daemon.shutdown()


class TestPeriodicJob:
    def test_job_runs_repeatedly_and_daemon_stays_responsive(
        self, db_path, config_path, registry
    ):
        checker = FakeChecker()
        daemon = make_daemon(
            db_path, config_path, registry, proxy_check_interval=0.05, proxy_checker=checker
        )
        daemon.start()
        try:
            assert wait_until(lambda: checker.calls >= 2, timeout=5), "job не запустился"

            status, _, elapsed = get_json(daemon, "/health")
            assert status == 200
            assert elapsed < 2.0, "периодическая проверка не должна держать HTTP"
            assert get_json(daemon, "/state")[0] == 200
            assert any(
                thread.name == "supervisor" and thread.is_alive()
                for thread in threading.enumerate()
            ), "расписание не должно вытеснять цикл супервизора"
        finally:
            daemon.shutdown()

        settled = checker.calls
        time.sleep(0.2)
        assert checker.calls == settled, "после остановки job продолжает будить проверку"
        assert proxy_thread_alive() is False

    def test_zero_interval_never_starts_the_job(self, db_path, config_path, registry):
        checker = FakeChecker()
        daemon = make_daemon(
            db_path, config_path, registry, proxy_check_interval=0, proxy_checker=checker
        )
        daemon.start()
        try:
            time.sleep(0.3)

            assert checker.calls == 0
            assert proxy_thread_alive() is False
            assert get_json(daemon, "/health")[0] == 200
        finally:
            daemon.shutdown()

    def test_check_failure_is_logged_and_job_survives(
        self, db_path, config_path, registry
    ):
        checker = FakeChecker(CheckInProgressError("проверка прокси уже выполняется"))
        daemon = make_daemon(
            db_path, config_path, registry, proxy_check_interval=0.05, proxy_checker=checker
        )
        daemon.start()
        try:
            assert wait_until(lambda: checker.calls >= 2, timeout=5), "job остановился после ошибки"
            assert get_json(daemon, "/health")[0] == 200
        finally:
            daemon.shutdown()

        store = StateStore(db_path)
        with store._connect() as conn:
            rows = conn.execute(
                "SELECT level, category, message FROM logs WHERE category = 'proxy'"
            ).fetchall()
        warnings = [row for row in rows if row["level"] == "WARNING"]
        assert any("periodic" in row["message"] for row in warnings), "пропуск не должен теряться"
        assert not any(row["level"] == "CRITICAL" for row in rows)

    def test_periodic_job_runs_the_real_check(self, db_path, config_path, registry):
        """Проводка целиком: демон → ProxyHealthChecker → пул → строки в БД."""
        stub = LoopbackProxy()
        try:
            pool = ProxyPool(db_path)
            assert pool.add_lines([f"alice:s3cr3t@127.0.0.1:{stub.port}"])["added"] == 1
            daemon = make_daemon(db_path, config_path, registry, proxy_check_interval=0.05)
            daemon.start()
            try:
                def checked():
                    with StateStore(db_path)._connect() as conn:
                        row = conn.execute("SELECT * FROM proxies").fetchone()
                    return row is not None and row["last_checked_at"] is not None

                assert wait_until(checked, timeout=10), "проверка не записала результат"
            finally:
                daemon.shutdown()
        finally:
            stub.close()

        with StateStore(db_path)._connect() as conn:
            row = conn.execute("SELECT * FROM proxies").fetchone()
        assert row["is_alive"] == 1
        assert row["fail_count"] == 0
        assert row["last_error"] is None

    def test_shutdown_wakes_the_job_instead_of_waiting_out_the_interval(
        self, db_path, config_path, registry
    ):
        """Интервал в часы: остановка обязана быть мгновенной, а не по таймеру."""
        checker = FakeChecker()
        daemon = make_daemon(
            db_path, config_path, registry, proxy_check_interval=3600.0, proxy_checker=checker
        )
        daemon.start()
        assert proxy_thread_alive() is True

        started = time.monotonic()
        daemon.shutdown()

        assert time.monotonic() - started < 5.0
        assert proxy_thread_alive() is False
        assert checker.calls == 0, "первый запуск должен быть через интервал, а не сразу"


class TestCredentialLeakGuard:
    """Сквозная проверка: креды не попадают ни в logs, ни в HTTP-ответы.

    Путь длинный — ручная добавка, фоновая проверка (успех и отказ), список
    в ответе, — потому что утечка обычно появляется не в одном месте, а там,
    где кто-то «для удобства» дописал значение в лог.
    """

    def test_credentials_never_reach_logs_or_responses(self, db_path, config_path, registry):
        stub = LoopbackProxy()
        try:
            daemon = make_daemon(
                db_path, config_path, registry, proxy_check_interval=0.05
            )
            daemon.start()
            status, body = post_json(
                daemon,
                "/control/proxies",
                {
                    "lines": [
                        f"alice:s3cr3t@127.0.0.1:{stub.port}",
                        f"bob:OTHER-SECRET@127.0.0.1:{closed_port()}",
                    ]
                },
            )
            assert status == 200 and body["added"] == 2, body

            def both_checked():
                with StateStore(db_path)._connect() as conn:
                    rows = conn.execute(
                        "SELECT last_checked_at FROM proxies"
                    ).fetchall()
                return len(rows) == 2 and all(row["last_checked_at"] for row in rows)

            assert wait_until(both_checked, timeout=10), "проверка не отработала по обоим"

            listed = get_raw(daemon, "/control/proxies")
        finally:
            daemon.shutdown()
            stub.close()

        with StateStore(db_path)._connect() as conn:
            log_dump = str(conn.execute("SELECT * FROM logs").fetchall())
            proxy_dump = str(conn.execute("SELECT * FROM proxies").fetchall())

        for secret in ("s3cr3t", "OTHER-SECRET", "alice", "bob"):
            assert secret not in log_dump, f"кред {secret!r} утёк в logs"
            assert secret not in proxy_dump, f"кред {secret!r} утёк в колонки proxies"
            assert secret not in listed, f"кред {secret!r} утёк в HTTP-ответ"
        assert SECRET_MASK in listed, "список обязан показывать маску, а не пустоту"
