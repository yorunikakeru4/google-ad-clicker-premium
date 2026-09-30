"""Тесты фонового job'а очистки профилей в демоне (план §5, фаза 10).

Нить на ``stop_event`` по образцу day-close, но с двумя своими решениями:

* **первый прогон при старте** — через короткий grace: сироты после
  аварийного Kill должны убираться сразу, а не копиться до ближайшего
  ``cleanup_time``. Grace нужен, чтобы зачистка не конкурировала со спавном
  воркеров в первые секунды работы;
* **интервал ``cleanup_interval_days``** считается на тике в штатном
  (расписательном) режиме: ручной запуск незадолго до планового не должен
  давать второй прогон в тот же день. Env-период (``ADCLICKER_CLEANUP_INTERVAL``)
  заменяет и расписание, и интервал — это ускоренный режим для тестов и стенда.

Все каталоги-цели теста лежат в ``tmp_path`` и передаются демону явно:
боевой сервис (системный tempdir) в тестах не используется.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import engine.cleanup as cleanup_module
from engine.cleanup import (
    CLEANUP_LAST_TS_KEY,
    CLEANUP_NEXT_RUN_KEY,
    CleanupService,
    default_targets,
)
from engine.control_plane.api import TOKEN_ENV_VAR, TOKEN_HEADER
from engine.control_plane.config import Config
from engine.control_plane.daemon import (
    CLEANUP_INTERVAL_ENV_VAR,
    Daemon,
    build_daemon,
    cleanup_interval_from_environ,
)
from engine.control_plane.state import StateStore
from engine.control_plane.supervisor import Supervisor, SupervisorSettings
from tests.engine.control_plane.test_supervisor import FakeClock, FakeProcessRegistry

TOKEN = "cleanup-job-token"

# Возраст «осиротевшего» кандидата: больше грейса по умолчанию (10 минут).
OLD_SECONDS = 7200


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


@pytest.fixture
def roots(tmp_path):
    """Корни целей очистки в tmp: как у модуля, но не системный tempdir."""
    temp_root = tmp_path / "temp"
    (temp_root / "uc_profiles").mkdir(parents=True)
    (temp_root / "sb_profiles").mkdir(parents=True)
    screenshots_root = tmp_path / "shots"
    screenshots_root.mkdir()
    return temp_root, screenshots_root


def age_path(path: Path, seconds: float = OLD_SECONDS) -> Path:
    """Состарить mtime — иначе грейс по умолчанию (10 минут) не пропустит."""
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def orphan(roots):
    """Осиротевший профиль, готовый к удалению."""
    temp_root, _ = roots
    path = temp_root / "uc_profiles" / "profile_1234"
    path.mkdir()
    (path / "Cookies").write_bytes(b"C" * 100)
    return age_path(path)


def make_service(store, roots):
    temp_root, screenshots_root = roots
    return CleanupService(
        store, targets=default_targets(temp_root, screenshots_root)
    )


def make_daemon(db_path, config_path, registry, roots, **overrides):
    """Демон без сетевых job'ов; корни очистки — только из tmp_path."""
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
        "cleanup_service": make_service(store, roots),
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


def get_health(daemon):
    request = urllib.request.Request(f"http://127.0.0.1:{daemon.port}/health", method="GET")
    request.add_header(TOKEN_HEADER, TOKEN)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def log_messages(db_path):
    with sqlite3.connect(db_path) as conn:
        return [
            (level, message, json.loads(fields) if fields else {})
            for level, message, fields in conn.execute(
                "SELECT level, message, fields FROM logs WHERE category = 'cleanup' ORDER BY id"
            )
        ]


def kv(store, key):
    return store.get_flag(key)


class TestCleanupIntervalFromEnviron:
    """Env-период: тот же контракт, что у закрытия дня."""

    def test_unset_means_the_schedule(self):
        assert cleanup_interval_from_environ({}) is None

    def test_blank_value_falls_back_to_the_schedule(self):
        assert cleanup_interval_from_environ({CLEANUP_INTERVAL_ENV_VAR: "  "}) is None

    def test_positive_value_is_a_period_for_tests(self):
        assert cleanup_interval_from_environ({CLEANUP_INTERVAL_ENV_VAR: "0.05"}) == 0.05

    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_zero_or_negative_disables_the_job(self, value):
        assert cleanup_interval_from_environ({CLEANUP_INTERVAL_ENV_VAR: value}) == 0.0

    def test_non_numeric_value_raises(self):
        with pytest.raises(ValueError) as excinfo:
            cleanup_interval_from_environ({CLEANUP_INTERVAL_ENV_VAR: "по расписанию"})

        assert CLEANUP_INTERVAL_ENV_VAR in str(excinfo.value)

    def test_env_var_name_is_documented(self):
        assert CLEANUP_INTERVAL_ENV_VAR == "ADCLICKER_CLEANUP_INTERVAL"


class TestBuildDaemonCleanupInterval:
    """build_daemon — боевой путь: без окружения расписание включено."""

    def test_build_takes_the_interval_from_environment(self, db_path, config_path, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(CLEANUP_INTERVAL_ENV_VAR, "3")

        daemon = build_daemon(db_path=db_path, config_path=config_path, port=0)

        try:
            assert daemon.cleanup_interval == 3.0
        finally:
            daemon.shutdown()

    def test_build_defaults_to_the_schedule(self, db_path, config_path, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.delenv(CLEANUP_INTERVAL_ENV_VAR, raising=False)

        daemon = build_daemon(db_path=db_path, config_path=config_path, port=0)

        try:
            assert daemon.cleanup_interval is None, "без окружения — расписание, а не выключено"
        finally:
            daemon.shutdown()

    def test_explicit_interval_wins_over_environment(self, db_path, config_path, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(CLEANUP_INTERVAL_ENV_VAR, "3")

        daemon = build_daemon(
            db_path=db_path, config_path=config_path, port=0, cleanup_interval=42.0
        )

        try:
            assert daemon.cleanup_interval == 42.0
        finally:
            daemon.shutdown()

    def test_build_rejects_non_numeric_interval(self, db_path, config_path, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(CLEANUP_INTERVAL_ENV_VAR, "каждую ночь")

        with pytest.raises(ValueError) as excinfo:
            build_daemon(db_path=db_path, config_path=config_path, port=0)

        assert CLEANUP_INTERVAL_ENV_VAR in str(excinfo.value)


class TestCleanupStartupRun:
    """Первый прогон при старте: сироты не копятся до ближайшего времени."""

    def test_scheduled_mode_waits_for_the_grace_before_the_first_run(
        self, db_path, config_path, registry, roots, orphan
    ):
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            roots,
            cleanup_interval=None,
            cleanup_startup_grace=0.3,
        )

        daemon.start()
        try:
            time.sleep(0.1)
            assert orphan.exists(), "до grace зачистка не должна стартовать"

            assert wait_until(lambda: not orphan.exists()), "стартовая зачистка не сработала"
            assert thread_alive("cleanup") is True
            assert get_health(daemon) == 200
        finally:
            daemon.shutdown()

    def test_startup_run_is_recorded_in_kv(self, db_path, config_path, registry, roots, orphan):
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            roots,
            cleanup_interval=None,
            cleanup_startup_grace=0.05,
        )
        daemon.start()
        try:
            assert wait_until(lambda: kv(daemon.store, CLEANUP_LAST_TS_KEY) is not None), (
                "стартовый прогон обязан записать метку в kv"
            )
            report = json.loads(kv(daemon.store, "CLEANUP_LAST_REPORT"))
            assert report["removed"] == 1
        finally:
            daemon.shutdown()

    def test_next_run_after_the_startup_points_to_the_configured_time(
        self, db_path, config_path, registry, roots, orphan
    ):
        """Нить пишет в kv цель ближайшего запуска из cleanup_time конфига."""
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            roots,
            cleanup_interval=None,
            cleanup_startup_grace=0.05,
            config_data={
                "behavior": {"browser_count": 2, "cleanup_time": "05:30"},
            },
        )
        daemon.start()
        try:
            def target_written():
                raw = kv(daemon.store, CLEANUP_NEXT_RUN_KEY)
                if not raw:
                    return False
                stamp = time.localtime(float(raw))
                return (stamp.tm_hour, stamp.tm_min) == (5, 30)

            assert wait_until(target_written), "цель в kv обязана совпасть с cleanup_time"
            assert float(kv(daemon.store, CLEANUP_NEXT_RUN_KEY)) > time.time()
        finally:
            daemon.shutdown()

    def test_scheduled_mode_does_not_fire_before_the_next_day(
        self, db_path, config_path, registry, roots, orphan
    ):
        """После стартового прогона нить ждёт следующего cleanup_time, не тикает."""
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            roots,
            cleanup_interval=None,
            cleanup_startup_grace=0.05,
        )
        daemon.start()
        try:
            assert wait_until(lambda: kv(daemon.store, CLEANUP_LAST_TS_KEY) is not None)
            first_run = kv(daemon.store, CLEANUP_LAST_TS_KEY)
            second = roots[0] / "uc_profiles" / "profile_2222"
            second.mkdir()
            make_orphan_after = age_path(second)

            time.sleep(0.3)
            assert make_orphan_after.exists(), "второго прогона до времени быть не должно"
            assert kv(daemon.store, CLEANUP_LAST_TS_KEY) == first_run
            assert thread_alive("cleanup") is True
        finally:
            daemon.shutdown()


class TestCleanupPeriodicJob:
    """Env-период: ускоренный режим, интервал дней ему не мешает."""

    def test_periodic_mode_keeps_cleaning(
        self, db_path, config_path, registry, roots, orphan
    ):
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            roots,
            cleanup_interval=0.05,
            cleanup_startup_grace=0.02,
        )

        daemon.start()
        try:
            assert wait_until(lambda: not orphan.exists()), "первый прогон не сработал"

            second = roots[0] / "uc_profiles" / "profile_2222"
            second.mkdir()
            age_path(second)

            assert wait_until(lambda: not second.exists()), "периодический тик не сработал"
            assert thread_alive("cleanup") is True
            assert get_health(daemon) == 200
        finally:
            daemon.shutdown()

        assert thread_alive("cleanup") is False, "shutdown должен гасить нить"

    def test_failure_is_logged_and_the_job_keeps_ticking(
        self, db_path, config_path, registry, roots, orphan, monkeypatch
    ):
        calls = []

        def broken_run(self, config, **kwargs):
            calls.append(config)
            raise RuntimeError("диск недоступен")

        monkeypatch.setattr(cleanup_module.CleanupService, "run", broken_run)
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            roots,
            cleanup_interval=0.05,
            cleanup_startup_grace=0.02,
        )

        daemon.start()
        try:
            assert wait_until(lambda: len(calls) >= 2), "job остановился после ошибки"
            assert orphan.exists(), "упавший прогон ничего не удаляет"
            assert get_health(daemon) == 200, "сбой тика не должен ронять демон"
            assert wait_until(
                lambda: any("cleanup failed" in row[1] for row in log_messages(db_path))
            ), "сбой job'а обязан попасть в лог"
        finally:
            daemon.shutdown()

    def test_schedule_error_is_logged_and_the_loop_survives(
        self, db_path, config_path, registry, roots, monkeypatch
    ):
        """Нечисловое время из конфига не убивает нить расписания."""
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            roots,
            cleanup_interval=None,
            cleanup_startup_grace=0.02,
        )
        daemon.start()
        try:
            assert wait_until(lambda: kv(daemon.store, CLEANUP_LAST_TS_KEY) is not None)
            def broken_schedule(now):
                raise ValueError("нет")

            monkeypatch.setattr(daemon, "_cleanup_next_run_at", broken_schedule)
            time.sleep(0.3)
            assert thread_alive("cleanup") is True, "нить обязана пережить ошибку расписания"
            assert get_health(daemon) == 200
            assert any(
                "cleanup schedule failed" in row[1] for row in log_messages(db_path)
            ), "ошибка расписания обязана быть видна в логе"
        finally:
            daemon.shutdown()


class TestCleanupIntervalGate:
    """Интервал ``cleanup_interval_days`` — только в штатном режиме."""

    def test_tick_before_the_interval_is_skipped(
        self, db_path, config_path, registry, roots, orphan
    ):
        daemon = make_daemon(db_path, config_path, registry, roots, cleanup_interval=None)
        daemon.store.set_flag(CLEANUP_LAST_TS_KEY, str(time.time()))

        daemon._cleanup_tick()

        assert orphan.exists(), "повтор в тот же день запрещён интервалом"
        skipped = [
            row
            for row in log_messages(db_path)
            if row[1] == "cleanup run skipped" and row[2].get("reason") == "interval"
        ]
        assert skipped, "пропуск по интервалу обязан попасть в лог"

    def test_tick_after_the_interval_runs(
        self, db_path, config_path, registry, roots, orphan
    ):
        daemon = make_daemon(db_path, config_path, registry, roots, cleanup_interval=None)
        daemon.store.set_flag(CLEANUP_LAST_TS_KEY, str(time.time() - 2 * 86400))

        daemon._cleanup_tick()

        assert not orphan.exists(), "интервал прошёл — сирота обязана уйти"
        assert float(kv(daemon.store, CLEANUP_LAST_TS_KEY)) > time.time() - 60

    def test_periodic_mode_ignores_the_interval(self, db_path, config_path, registry, roots):
        """Env-период заменяет интервал: иначе ускоренные тесты не тикали бы."""
        daemon = make_daemon(
            db_path, config_path, registry, roots, cleanup_interval=0.05
        )
        before = str(time.time())
        daemon.store.set_flag(CLEANUP_LAST_TS_KEY, before)

        daemon._cleanup_tick()

        assert kv(daemon.store, CLEANUP_LAST_TS_KEY) != before, (
            "в периодическом режиме прогон выполняется, а не пропускается"
        )


class TestCleanupJobDisabled:
    """Прямая сборка ``Daemon``: расписание выключено, тесты не ходят в temp."""

    def test_default_daemon_never_starts_the_cleanup_thread(
        self, db_path, config_path, registry, roots, orphan
    ):
        daemon = make_daemon(db_path, config_path, registry, roots)

        daemon.start()
        try:
            time.sleep(0.3)

            assert thread_alive("cleanup") is False
            assert orphan.exists(), "выключенный job ничего не удаляет"
            assert kv(daemon.store, CLEANUP_NEXT_RUN_KEY) == "", (
                "next_run обязан сказать null"
            )
            assert get_health(daemon) == 200
        finally:
            daemon.shutdown()

    def test_zero_interval_never_starts_the_job(
        self, db_path, config_path, registry, roots, orphan
    ):
        daemon = make_daemon(
            db_path, config_path, registry, roots, cleanup_interval=0.0
        )

        daemon.start()
        try:
            time.sleep(0.3)

            assert thread_alive("cleanup") is False
            assert orphan.exists()
        finally:
            daemon.shutdown()


class TestCleanupShutdown:
    """Остановка не ждёт расписания — как у остальных нитей демона."""

    def test_shutdown_wakes_the_job_instead_of_waiting_for_the_schedule(
        self, db_path, config_path, registry, roots
    ):
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            roots,
            cleanup_interval=None,
            cleanup_startup_grace=0.05,
        )
        daemon.start()
        assert thread_alive("cleanup") is True

        started = time.monotonic()
        daemon.shutdown()

        assert time.monotonic() - started < 5.0, "остановка не должна ждать 04:00"
        assert thread_alive("cleanup") is False
