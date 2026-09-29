"""Тесты периодической проверки порога CAPTCHA в демоне.

Расписание построено на той же фоновой механике, что и проверка прокси:
отдельная нить ждёт общий ``stop_event``, период задаётся окружением, ``0``
выключает расписание, а любая ошибка тика уходит в лог и не убивает ни
нить, ни HTTP. Отличие только в предмете: здесь считается доля CAPTCHA за
скользящий час и по порогу срабатывает edge-действие (warn/pause/rotate).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from engine.captcha_threshold import CaptchaThresholdPolicy, captcha_share
from engine.control_plane.api import TOKEN_ENV_VAR, TOKEN_HEADER
from engine.control_plane.config import Config
from engine.control_plane.daemon import (
    CAPTCHA_CHECK_INTERVAL_ENV_VAR,
    CAPTCHA_CHECK_THREAD_NAME,
    DEFAULT_CAPTCHA_CHECK_INTERVAL_SECONDS,
    DEFAULT_PROXY_CHECK_INTERVAL_SECONDS,
    Daemon,
    build_daemon,
    captcha_check_interval_from_environ,
)
from engine.control_plane.state import StateStore
from engine.control_plane.supervisor import Supervisor, SupervisorSettings
from engine.db import migrations
from engine.proxy_pool import ProxyPool
from tests.engine.control_plane.test_supervisor import FakeClock, FakeProcessRegistry

TOKEN = "captcha-check-test-token"


class IdleSupervisor:
    """Супервизор-заглушка для политики, которой действия не нужны."""

    def pause(self) -> None:
        raise AssertionError("политика не должна была доходить до действия")

    def rotate_all(self, *, reason: str) -> int:
        raise AssertionError("политика не должна была доходить до действия")


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


@pytest.fixture
def config_path(tmp_path):
    return tmp_path / "config.json"


@pytest.fixture
def registry():
    return FakeProcessRegistry()


class FakePolicy:
    """Подмена политики: считает тики и умеет падать по требованию."""

    def __init__(self, error=None):
        self.calls = 0
        self.checks: list[tuple[float, str]] = []
        self.error = error

    def check(self, threshold_percent, action):
        self.calls += 1
        self.checks.append((threshold_percent, action))
        if self.error is not None:
            raise self.error
        return None


def broken_share(db_path, since):
    """Формула, у которой упала БД. Текст не должен утекать в лог."""
    raise OSError("database is locked")


def make_daemon(db_path, config_path, registry, **overrides):
    store = StateStore(db_path)
    config = Config.from_dict(
        {
            "behavior": {
                "browser_count": 2,
                "captcha_threshold_percent": 5.0,
                "captcha_threshold_action": "warn",
            }
        }
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
    }
    kwargs.update(overrides)
    return Daemon(**kwargs)


def get_json(daemon, path):
    request = urllib.request.Request(f"http://127.0.0.1:{daemon.port}{path}", method="GET")
    request.add_header(TOKEN_HEADER, TOKEN)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def captcha_thread_alive():
    return any(
        thread.name == CAPTCHA_CHECK_THREAD_NAME and thread.is_alive()
        for thread in threading.enumerate()
    )


def captcha_logs(db_path, level=None):
    with StateStore(db_path)._connect() as conn:
        rows = conn.execute(
            "SELECT level, message, fields FROM logs WHERE category = 'captcha' ORDER BY id"
        ).fetchall()
    logs = [dict(row) for row in rows]
    for row in logs:
        row["fields"] = json.loads(row["fields"]) if row["fields"] else {}
    if level is None:
        return logs
    return [row for row in logs if row["level"] == level]


def seed(db_path, *, requests, captchas):
    """Окно в час от текущего времени — ровно то, что читает политика."""
    now = time.time()
    conn = migrations.connect(db_path)
    try:
        for _ in range(requests):
            conn.execute(
                "INSERT INTO network_requests (ts, browser_id, method, url) "
                "VALUES (?, 'br-1', 'GET', 'https://example.test/')",
                (now,),
            )
        for _ in range(captchas):
            conn.execute("INSERT INTO captcha_events (ts, browser_id) VALUES (?, 'br-1')", (now,))
        conn.commit()
    finally:
        conn.close()


class TestCaptchaIntervalFromEnviron:
    def test_default_when_unset(self):
        assert captcha_check_interval_from_environ({}) == DEFAULT_CAPTCHA_CHECK_INTERVAL_SECONDS

    def test_default_is_sixty_seconds(self):
        assert DEFAULT_CAPTCHA_CHECK_INTERVAL_SECONDS == 60.0

    def test_value_is_read_from_environment(self):
        assert captcha_check_interval_from_environ({CAPTCHA_CHECK_INTERVAL_ENV_VAR: "5"}) == 5.0

    def test_zero_disables_the_job(self):
        assert captcha_check_interval_from_environ({CAPTCHA_CHECK_INTERVAL_ENV_VAR: "0"}) == 0.0

    def test_negative_disables_the_job(self):
        assert captcha_check_interval_from_environ({CAPTCHA_CHECK_INTERVAL_ENV_VAR: "-5"}) == 0.0

    def test_blank_value_falls_back_to_default(self):
        assert (
            captcha_check_interval_from_environ({CAPTCHA_CHECK_INTERVAL_ENV_VAR: "   "})
            == DEFAULT_CAPTCHA_CHECK_INTERVAL_SECONDS
        )

    def test_non_numeric_value_raises(self):
        with pytest.raises(ValueError) as excinfo:
            captcha_check_interval_from_environ({CAPTCHA_CHECK_INTERVAL_ENV_VAR: "каждую минуту"})

        assert CAPTCHA_CHECK_INTERVAL_ENV_VAR in str(excinfo.value)

    def test_env_var_name_is_documented(self):
        assert CAPTCHA_CHECK_INTERVAL_ENV_VAR == "ADCLICKER_CAPTCHA_CHECK_INTERVAL"

    def test_does_not_reuse_the_proxy_interval(self):
        """Два job'а — две независимые настройки: выключили один, второй жив."""
        assert CAPTCHA_CHECK_INTERVAL_ENV_VAR != "ADCLICKER_PROXY_CHECK_INTERVAL"
        assert DEFAULT_CAPTCHA_CHECK_INTERVAL_SECONDS != DEFAULT_PROXY_CHECK_INTERVAL_SECONDS


class TestBuildDaemonReadsInterval:
    def test_build_takes_interval_from_environment(self, db_path, config_path, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(CAPTCHA_CHECK_INTERVAL_ENV_VAR, "3")

        daemon = build_daemon(db_path=db_path, config_path=config_path, port=0)

        try:
            assert daemon.captcha_check_interval == 3.0
        finally:
            daemon.shutdown()

    def test_build_rejects_non_numeric_interval(self, db_path, config_path, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(CAPTCHA_CHECK_INTERVAL_ENV_VAR, "раз в минуту")

        with pytest.raises(ValueError) as excinfo:
            build_daemon(db_path=db_path, config_path=config_path, port=0)

        assert CAPTCHA_CHECK_INTERVAL_ENV_VAR in str(excinfo.value)

    def test_explicit_interval_wins_over_environment(self, db_path, config_path, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(CAPTCHA_CHECK_INTERVAL_ENV_VAR, "3")

        daemon = build_daemon(
            db_path=db_path, config_path=config_path, port=0, captcha_check_interval=17.0
        )

        try:
            assert daemon.captcha_check_interval == 17.0
        finally:
            daemon.shutdown()


class TestCaptchaJob:
    def test_job_runs_repeatedly_and_daemon_stays_responsive(self, db_path, config_path, registry):
        policy = FakePolicy()
        daemon = make_daemon(
            db_path, config_path, registry, captcha_check_interval=0.05, captcha_policy=policy
        )
        daemon.start()
        try:
            assert wait_until(lambda: policy.calls >= 2, timeout=5), "job не запустился"

            assert get_json(daemon, "/health")[0] == 200
            assert get_json(daemon, "/state")[0] == 200
            assert any(
                thread.name == "supervisor" and thread.is_alive()
                for thread in threading.enumerate()
            ), "расписание не должно вытеснять цикл супервизора"
        finally:
            daemon.shutdown()

        settled = policy.calls
        time.sleep(0.2)
        assert policy.calls == settled, "после остановки job продолжает будить политику"
        assert captcha_thread_alive() is False

    def test_zero_interval_never_starts_the_job(self, db_path, config_path, registry):
        policy = FakePolicy()
        daemon = make_daemon(
            db_path, config_path, registry, captcha_check_interval=0, captcha_policy=policy
        )
        daemon.start()
        try:
            time.sleep(0.3)

            assert policy.calls == 0
            assert captcha_thread_alive() is False
            assert get_json(daemon, "/health")[0] == 200
        finally:
            daemon.shutdown()

    def test_job_passes_threshold_and_action_from_the_config(self, db_path, config_path, registry):
        policy = FakePolicy()
        config = Config.from_dict(
            {"behavior": {"captcha_threshold_percent": 12.5, "captcha_threshold_action": "rotate"}}
        )
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            captcha_check_interval=0.05,
            captcha_policy=policy,
            config=config,
        )
        daemon.start()
        try:
            assert wait_until(lambda: policy.calls >= 2, timeout=5)
        finally:
            daemon.shutdown()

        assert policy.checks, "ни один тик не дошёл до политики"
        assert set(policy.checks) == {(12.5, "rotate")}

    def test_formula_failure_is_logged_and_job_survives(self, db_path, config_path, registry):
        """Сбой формулы/БД — ошибка тика, а не смерть расписания."""
        policy = CaptchaThresholdPolicy(
            StateStore(db_path), IdleSupervisor(), share_fn=broken_share
        )
        daemon = make_daemon(
            db_path, config_path, registry, captcha_check_interval=0.05, captcha_policy=policy
        )
        daemon.start()
        try:
            assert wait_until(
                lambda: len(captcha_logs(db_path, level="ERROR")) >= 2, timeout=5
            ), "упавшая формула должна логироваться и не останавливать расписание"
            assert captcha_thread_alive() is True, "нить job'а обязана жить после сбоя"
            assert get_json(daemon, "/health")[0] == 200
        finally:
            daemon.shutdown()

        dumped = json.dumps([row["fields"] for row in captcha_logs(db_path)])
        assert "database is locked" not in dumped, (
            "текст исключения БД не должен утекать в лог целиком"
        )
        errors = captcha_logs(db_path, level="ERROR")
        assert all(
            row["message"] == "periodic captcha threshold check failed" for row in errors
        )
        assert all(row["fields"] == {"error": "OSError"} for row in errors)

    def test_shutdown_wakes_the_job_instead_of_waiting_out_the_interval(
        self, db_path, config_path, registry
    ):
        policy = FakePolicy()
        daemon = make_daemon(
            db_path, config_path, registry, captcha_check_interval=3600.0, captcha_policy=policy
        )
        daemon.start()
        assert captcha_thread_alive() is True

        started = time.monotonic()
        daemon.shutdown()

        assert time.monotonic() - started < 5.0
        assert captcha_thread_alive() is False
        assert policy.calls == 0, "первый запуск должен быть через интервал, а не сразу"


class TestJobEndToEnd:
    """Проводка целиком: БД → формула → edge-действие → лог/kv/ротация."""

    def test_exceeding_share_is_logged_once_by_the_running_job(
        self, db_path, config_path, registry
    ):
        seed(db_path, requests=20, captchas=2)  # 10% > 5%
        daemon = make_daemon(db_path, config_path, registry, captcha_check_interval=0.05)
        daemon.start()
        try:
            assert wait_until(lambda: captcha_logs(db_path, level="WARNING"), timeout=5), (
                "превышение порога обязано попасть в лог"
            )
            time.sleep(0.2)
        finally:
            daemon.shutdown()

        logs = captcha_logs(db_path)
        assert len(logs) == 1, "edge-trigger: одно превышение — одно предупреждение"
        assert logs[0]["message"] == "captcha threshold exceeded"
        assert logs[0]["fields"]["action"] == "warn"
        assert logs[0]["fields"]["threshold_percent"] == 5.0
        assert logs[0]["fields"]["share"] == pytest.approx(0.1)

    def test_share_below_the_threshold_stays_quiet(self, db_path, config_path, registry):
        seed(db_path, requests=100, captchas=1)  # 1% < 5%
        daemon = make_daemon(db_path, config_path, registry, captcha_check_interval=0.05)
        daemon.start()
        try:
            time.sleep(0.3)
        finally:
            daemon.shutdown()

        assert captcha_logs(db_path) == [], "доля ниже порога не должна логироваться"

    def test_missing_requests_stay_quiet(self, db_path, config_path, registry):
        """Пустое окно — ``None``: ни триггера, ни ложного превышения."""
        daemon = make_daemon(db_path, config_path, registry, captcha_check_interval=0.05)
        daemon.start()
        try:
            time.sleep(0.3)
        finally:
            daemon.shutdown()

        assert captcha_logs(db_path) == []

    def test_pause_action_sets_the_kv_flag(self, db_path, config_path, registry):
        """Действие pause доходит до kv-флага тем же путём, что кнопка в UI."""
        seed(db_path, requests=20, captchas=2)
        config = Config.from_dict({"behavior": {"captcha_threshold_action": "pause"}})
        daemon = make_daemon(
            db_path, config_path, registry, captcha_check_interval=0.05, config=config
        )
        # Воркеры поднимаются ДО запуска расписания: иначе первый тик мог бы
        # увидеть пул остановленным и израсходовать edge-переход впустую.
        daemon.supervisor.start(1)
        daemon.start()
        try:
            # Ждём ОБЕ записи: request_pause() и set_run_state() идут двумя
            # транзакциями, и тик между ними читал бы флаг со старым run_state.
            assert wait_until(
                lambda: daemon.store.is_pause_requested()
                and daemon.store.get_run_state() == "paused",
                timeout=5,
            ), "pause обязан поставить kv-флаг и состояние paused"
            assert registry.created[0].poll() is None, "пауза не убивает процессы"
            assert len(captcha_logs(db_path, level="WARNING")) == 1, (
                "edge: пауза запрашивается один раз, а не на каждом тике"
            )
        finally:
            daemon.shutdown()

    def test_rotate_action_replaces_the_process_with_a_reserve_proxy(
        self, db_path, config_path, registry
    ):
        seed(db_path, requests=20, captchas=2)
        config = Config.from_dict({"behavior": {"captcha_threshold_action": "rotate"}})
        pool = ProxyPool(db_path)
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080", "bob:hunter2@10.0.0.2:9090"])
        ids = [row["id"] for row in pool.list_proxies()]
        daemon = make_daemon(
            db_path, config_path, registry, captcha_check_interval=0.05, config=config
        )
        daemon.supervisor.start(1)
        assert daemon.store.get_worker("br-1")["proxy_id"] == ids[0]
        daemon.start()
        try:
            assert wait_until(lambda: len(registry.created) >= 2, timeout=5), (
                "rotate обязан подменить прокси живого воркера"
            )
            assert wait_until(
                lambda: daemon.store.get_worker("br-1")["proxy_id"] == ids[1], timeout=5
            )
            assert daemon.store.get_worker("br-1")["restart_count"] == 0, (
                "ротация не должна крутить restart_count"
            )
            assert registry.created[0].poll() is not None, "старый процесс должен быть погашен"
            assert len(captcha_logs(db_path, level="WARNING")) == 1, (
                "действие — один раз на переход"
            )
        finally:
            daemon.shutdown()

    def test_formula_reads_the_same_window_as_the_policy(self, db_path):
        """Проверка формулы на живых строках — та же, что у политики."""
        seed(db_path, requests=4, captchas=1)

        assert captcha_share(db_path, time.time() - 3600.0) == pytest.approx(0.25)
