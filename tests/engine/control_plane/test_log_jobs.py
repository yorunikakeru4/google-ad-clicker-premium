"""Тесты фоновых job'ов ротации логов в демоне (план §5, фаза 9).

Три нити на ``stop_event`` — тот же паттерн, что у proxy-check и
captcha-check:

* **day-close** — в 23:59 локального времени закрывает день: экспорт в
  ``logs/YYYY-MM-DD.log``, затем retention; env-период ускоряет расписание
  в тестах;
* **retention** — чистка старых дней раз в сутки (и после каждого
  закрытия дня);
* **db-size** — защита от роста БД, по умолчанию раз в час.

Проверяется расписание (env-валидация, границы суток, выключение нулём),
работа самих job'ов (файл появился, старые дни ушли) и то, что сбой job'а
логируется и не роняет демон.

Файлы экспорта и чистка идут только в ``tmp_path``: тест меняет cwd и не
имеет права оставить ничего в ``logs/`` репозитория.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest

from engine.control_plane.api import TOKEN_ENV_VAR, TOKEN_HEADER
from engine.control_plane.config import Config
from engine.control_plane.daemon import (
    DAY_CLOSE_INTERVAL_ENV_VAR,
    DB_SIZE_INTERVAL_ENV_VAR,
    DEFAULT_DB_SIZE_INTERVAL_SECONDS,
    DEFAULT_RETENTION_INTERVAL_SECONDS,
    RETENTION_INTERVAL_ENV_VAR,
    Daemon,
    build_daemon,
    day_close_interval_from_environ,
    db_size_interval_from_environ,
    retention_interval_from_environ,
)
from engine.control_plane.state import StateStore
from engine.control_plane.supervisor import Supervisor, SupervisorSettings
from engine.log_rotation import DbSizeResult, local_day, seconds_until_day_close
from tests.engine.control_plane.test_supervisor import FakeClock, FakeProcessRegistry

TOKEN = "log-jobs-token"


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
    """Демон без сетевых job'ов: фокус теста — ротация логов."""
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


def get_health(daemon):
    request = urllib.request.Request(f"http://127.0.0.1:{daemon.port}/health", method="GET")
    request.add_header(TOKEN_HEADER, TOKEN)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def insert_log(db_path, day, message="row", level="INFO", ts=1711954800.0):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO logs (ts, day, level, browser_id, category, message) "
            "VALUES (?, ?, ?, 'br-1', 'click', ?)",
            (ts, day, level, message),
        )
        conn.commit()


def levels_and_messages(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT level, message FROM logs").fetchall()


def log_messages(db_path):
    with sqlite3.connect(db_path) as conn:
        return [row[0] for row in conn.execute("SELECT message FROM logs ORDER BY id")]


def at(year, month, day, hour=0, minute=0, second=0) -> float:
    """Unix-время заданного локального времени."""
    return time.mktime((year, month, day, hour, minute, second, 0, 0, -1))


class TestDayCloseIntervalFromEnviron:
    def test_unset_means_the_2359_schedule(self):
        assert day_close_interval_from_environ({}) is None

    def test_blank_value_falls_back_to_the_schedule(self):
        assert day_close_interval_from_environ({DAY_CLOSE_INTERVAL_ENV_VAR: "   "}) is None

    def test_positive_value_is_a_period_for_tests(self):
        assert day_close_interval_from_environ({DAY_CLOSE_INTERVAL_ENV_VAR: "0.05"}) == 0.05

    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_zero_or_negative_disables_the_job(self, value):
        assert day_close_interval_from_environ({DAY_CLOSE_INTERVAL_ENV_VAR: value}) == 0.0

    def test_non_numeric_value_raises(self):
        with pytest.raises(ValueError) as excinfo:
            day_close_interval_from_environ({DAY_CLOSE_INTERVAL_ENV_VAR: "в полночь"})

        assert DAY_CLOSE_INTERVAL_ENV_VAR in str(excinfo.value)

    def test_env_var_name_is_documented(self):
        assert DAY_CLOSE_INTERVAL_ENV_VAR == "ADCLICKER_DAY_CLOSE_INTERVAL"


class TestRetentionIntervalFromEnviron:
    def test_default_is_one_day(self):
        assert DEFAULT_RETENTION_INTERVAL_SECONDS == 86400.0
        assert retention_interval_from_environ({}) == 86400.0

    def test_value_is_read_from_environment(self):
        assert (
            retention_interval_from_environ({RETENTION_INTERVAL_ENV_VAR: "600"}) == 600.0
        )

    @pytest.mark.parametrize("value", ["0", "-5"])
    def test_zero_or_negative_disables_the_job(self, value):
        assert retention_interval_from_environ({RETENTION_INTERVAL_ENV_VAR: value}) == 0.0

    def test_non_numeric_value_raises(self):
        with pytest.raises(ValueError) as excinfo:
            retention_interval_from_environ({RETENTION_INTERVAL_ENV_VAR: "раз в сутки"})

        assert RETENTION_INTERVAL_ENV_VAR in str(excinfo.value)

    def test_env_var_name_is_documented(self):
        assert RETENTION_INTERVAL_ENV_VAR == "ADCLICKER_RETENTION_INTERVAL"


class TestDbSizeIntervalFromEnviron:
    def test_default_is_one_hour(self):
        assert DEFAULT_DB_SIZE_INTERVAL_SECONDS == 3600.0
        assert db_size_interval_from_environ({}) == 3600.0

    def test_value_is_read_from_environment(self):
        assert db_size_interval_from_environ({DB_SIZE_INTERVAL_ENV_VAR: "60"}) == 60.0

    @pytest.mark.parametrize("value", ["0", "-5"])
    def test_zero_or_negative_disables_the_job(self, value):
        assert db_size_interval_from_environ({DB_SIZE_INTERVAL_ENV_VAR: value}) == 0.0

    def test_non_numeric_value_raises(self):
        with pytest.raises(ValueError) as excinfo:
            db_size_interval_from_environ({DB_SIZE_INTERVAL_ENV_VAR: "час"})

        assert DB_SIZE_INTERVAL_ENV_VAR in str(excinfo.value)

    def test_env_var_name_is_documented(self):
        assert DB_SIZE_INTERVAL_ENV_VAR == "ADCLICKER_DB_SIZE_INTERVAL"


class TestSecondsUntilDayClose:
    """Расписание закрытия дня: цель всегда следующие локальные 23:59."""

    @staticmethod
    def _target(now):
        return now + seconds_until_day_close(now)

    def test_before_the_close_points_to_todays_2359(self):
        now = at(2024, 4, 1, 23, 58, 59)

        assert time.localtime(self._target(now))[:6] == (2024, 4, 1, 23, 59, 0)

    def test_at_the_close_the_target_jumps_to_the_next_day(self):
        """Переход через полночь: ровно в 23:59 закрытие уже позади."""
        now = at(2024, 4, 1, 23, 59, 0)

        target = self._target(now)

        assert time.localtime(target)[:6] == (2024, 4, 2, 23, 59, 0)
        assert seconds_until_day_close(now) >= 23 * 3600

    def test_right_after_midnight_the_close_is_tonight(self):
        now = at(2024, 4, 2, 0, 0, 0)

        assert time.localtime(self._target(now))[:6] == (2024, 4, 2, 23, 59, 0)

    def test_repeats_on_each_following_day(self):
        """Два закрытия подряд живут в разных сутках — job повторяется."""
        first = at(2024, 4, 1, 23, 59, 0)
        first_target = self._target(first)
        second_target = first_target + seconds_until_day_close(first_target)

        assert local_day(first_target) == "2024-04-02"
        assert local_day(second_target) == "2024-04-03"

    @pytest.mark.parametrize(
        "now",
        [
            at(2024, 4, 1, 0, 0, 0),
            at(2024, 4, 1, 12, 0, 0),
            at(2024, 4, 1, 23, 58, 59),
            at(2024, 4, 1, 23, 59, 0),
            at(2024, 4, 1, 23, 59, 59),
        ],
    )
    def test_wait_is_always_positive(self, now):
        assert seconds_until_day_close(now) > 0


class TestDayCloseJob:
    def test_periodic_mode_exports_today_into_cwd_logs(
        self, db_path, config_path, registry, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        today = local_day(time.time())
        insert_log(db_path, today, message="for the export")
        daemon = make_daemon(db_path, config_path, registry, day_close_interval=0.05)

        daemon.start()
        try:
            exported = tmp_path / "logs" / f"{today}.log"
            assert wait_until(exported.exists), "job не закрыл день и не выгрузил файл"
            assert "for the export" in exported.read_text(encoding="utf-8")
            assert get_health(daemon) == 200, "закрытие дня не должно держать HTTP"
        finally:
            daemon.shutdown()

        assert thread_alive("day-close") is False, "shutdown должен гасить нить"

    def test_scheduled_mode_waits_for_the_close_instead_of_firing_now(
        self, db_path, config_path, registry, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        insert_log(db_path, local_day(time.time()))
        daemon = make_daemon(db_path, config_path, registry, day_close_interval=None)

        daemon.start()
        try:
            time.sleep(0.3)
            assert thread_alive("day-close") is True
            assert not (tmp_path / "logs").exists(), (
                "по расписанию первый запуск только в 23:59, а не сразу"
            )
        finally:
            daemon.shutdown()

    def test_zero_interval_never_starts_the_job(
        self, db_path, config_path, registry, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        daemon = make_daemon(db_path, config_path, registry, day_close_interval=0.0)

        daemon.start()
        try:
            time.sleep(0.3)

            assert thread_alive("day-close") is False
            assert not (tmp_path / "logs").exists()
            assert get_health(daemon) == 200
        finally:
            daemon.shutdown()

    def test_failure_is_logged_and_the_job_keeps_ticking(
        self, db_path, config_path, registry, monkeypatch
    ):
        import engine.control_plane.daemon as daemon_module

        calls = []

        def broken_export(*args, **kwargs):
            calls.append(args)
            raise RuntimeError("диск недоступен")

        monkeypatch.setattr(daemon_module, "export_day", broken_export)
        daemon = make_daemon(db_path, config_path, registry, day_close_interval=0.05)

        daemon.start()
        try:
            assert wait_until(lambda: len(calls) >= 2), "job остановился после ошибки"
            assert get_health(daemon) == 200
            assert wait_until(
                lambda: any("day close failed" in m for m in log_messages(db_path))
            ), "сбой job'а обязан попасть в лог"
        finally:
            daemon.shutdown()

    def test_shutdown_wakes_the_job_instead_of_waiting_a_full_day(
        self, db_path, config_path, registry
    ):
        daemon = make_daemon(db_path, config_path, registry, day_close_interval=None)
        daemon.start()
        assert thread_alive("day-close") is True

        started = time.monotonic()
        daemon.shutdown()

        assert time.monotonic() - started < 5.0, "остановка не должна ждать 23:59"
        assert thread_alive("day-close") is False


class TestRetentionJob:
    @pytest.fixture
    def expired(self, db_path, tmp_path):
        """Старая строка и старый файл экспорта — то, что должна убрать чистка."""
        old_day = (datetime.now().date() - timedelta(days=40)).isoformat()
        insert_log(db_path, old_day, message="expired")
        exports = tmp_path / "logs"
        exports.mkdir()
        (exports / f"{old_day}.log").write_text("expired\n", encoding="utf-8")
        return old_day

    def test_periodic_run_purges_expired_rows_and_files(
        self, db_path, config_path, registry, expired, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        daemon = make_daemon(db_path, config_path, registry, retention_interval=0.05)

        daemon.start()
        try:
            def purged():
                with sqlite3.connect(db_path) as conn:
                    rows = conn.execute(
                        "SELECT COUNT(*) FROM logs WHERE day = ?", (expired,)
                    ).fetchone()[0]
                return rows == 0 and not (tmp_path / "logs" / f"{expired}.log").exists()

            assert wait_until(purged), "retention не удалила старые данные"
            assert thread_alive("retention") is True
            assert get_health(daemon) == 200
        finally:
            daemon.shutdown()

        assert thread_alive("retention") is False

    def test_interval_zero_never_starts_the_job(self, db_path, config_path, registry):
        daemon = make_daemon(db_path, config_path, registry, retention_interval=0.0)

        daemon.start()
        try:
            time.sleep(0.3)

            assert thread_alive("retention") is False
            assert get_health(daemon) == 200
        finally:
            daemon.shutdown()

    def test_failure_is_logged_and_the_job_keeps_ticking(
        self, db_path, config_path, registry, monkeypatch
    ):
        import engine.control_plane.daemon as daemon_module

        calls = []

        def broken_retention(*args, **kwargs):
            calls.append(args)
            raise RuntimeError("база занята")

        monkeypatch.setattr(daemon_module, "run_retention", broken_retention)
        daemon = make_daemon(db_path, config_path, registry, retention_interval=0.05)

        daemon.start()
        try:
            assert wait_until(lambda: len(calls) >= 2), "job остановился после ошибки"
            assert get_health(daemon) == 200
            assert wait_until(
                lambda: any("retention failed" in m for m in log_messages(db_path))
            )
        finally:
            daemon.shutdown()


class TestDbSizeJob:
    def test_zero_limit_means_the_guard_is_never_called(
        self, db_path, config_path, registry, monkeypatch
    ):
        import engine.control_plane.daemon as daemon_module

        calls = []

        def guard(*args, **kwargs):
            calls.append(args)
            return DbSizeResult(True, 0, 0, ())

        monkeypatch.setattr(daemon_module, "enforce_db_size_limit", guard)
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            db_size_interval=0.05,
            config_data={"behavior": {"browser_count": 2, "db_size_limit_mb": 0}},
        )

        daemon.start()
        try:
            time.sleep(0.3)

            assert calls == [], "лимит 0 — защита выключена, удаления не должно быть"
            assert thread_alive("db-size") is True, "сама нить при этом работает"
            assert get_health(daemon) == 200
        finally:
            daemon.shutdown()

    def test_guard_receives_the_configured_limit(self, db_path, config_path, registry, monkeypatch):
        import engine.control_plane.daemon as daemon_module

        calls = []

        def guard(db_path_arg, limit_mb, **kwargs):
            calls.append((db_path_arg, limit_mb))
            return DbSizeResult(True, 0, 0, ())

        monkeypatch.setattr(daemon_module, "enforce_db_size_limit", guard)
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            db_size_interval=0.05,
            config_data={"behavior": {"browser_count": 2, "db_size_limit_mb": 128}},
        )

        daemon.start()
        try:
            assert wait_until(lambda: len(calls) >= 1)
            assert calls[0][0] == db_path
            assert calls[0][1] == 128
        finally:
            daemon.shutdown()

    def test_unreachable_limit_is_logged_as_warning_and_daemon_survives(
        self, db_path, config_path, registry, monkeypatch
    ):
        import engine.control_plane.daemon as daemon_module

        def guard(*args, **kwargs):
            return DbSizeResult(
                fits=False, limit_bytes=1024, size_bytes=4096, deleted_days=("2024-01-01",)
            )

        monkeypatch.setattr(daemon_module, "enforce_db_size_limit", guard)
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            db_size_interval=0.05,
            config_data={"behavior": {"browser_count": 2, "db_size_limit_mb": 1}},
        )

        daemon.start()
        try:
            assert wait_until(
                lambda: any(
                    level == "WARNING" and "db size limit" in message
                    for level, message in levels_and_messages(db_path)
                )
            ), "недостижимый лимит обязан быть виден как WARNING"
            assert get_health(daemon) == 200, "защита не имеет права блокировать запись"
        finally:
            daemon.shutdown()

    def test_interval_zero_never_starts_the_job(self, db_path, config_path, registry):
        daemon = make_daemon(db_path, config_path, registry, db_size_interval=0.0)

        daemon.start()
        try:
            time.sleep(0.3)

            assert thread_alive("db-size") is False
            assert get_health(daemon) == 200
        finally:
            daemon.shutdown()


class TestBuildDaemonLogJobs:
    def test_build_takes_log_job_intervals_from_environment(
        self, db_path, config_path, monkeypatch
    ):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(DAY_CLOSE_INTERVAL_ENV_VAR, "3")
        monkeypatch.setenv(RETENTION_INTERVAL_ENV_VAR, "4")
        monkeypatch.setenv(DB_SIZE_INTERVAL_ENV_VAR, "5")

        daemon = build_daemon(db_path=db_path, config_path=config_path, port=0)

        try:
            assert daemon.day_close_interval == 3.0
            assert daemon.retention_interval == 4.0
            assert daemon.db_size_interval == 5.0
        finally:
            daemon.shutdown()

    def test_explicit_intervals_win_over_environment(
        self, db_path, config_path, monkeypatch
    ):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(DAY_CLOSE_INTERVAL_ENV_VAR, "3")
        monkeypatch.setenv(RETENTION_INTERVAL_ENV_VAR, "4")
        monkeypatch.setenv(DB_SIZE_INTERVAL_ENV_VAR, "5")

        daemon = build_daemon(
            db_path=db_path,
            config_path=config_path,
            port=0,
            day_close_interval=42.0,
            retention_interval=43.0,
            db_size_interval=44.0,
        )

        try:
            assert daemon.day_close_interval == 42.0
            assert daemon.retention_interval == 43.0
            assert daemon.db_size_interval == 44.0
        finally:
            daemon.shutdown()

    @pytest.mark.parametrize(
        "env_var", [DAY_CLOSE_INTERVAL_ENV_VAR, RETENTION_INTERVAL_ENV_VAR, DB_SIZE_INTERVAL_ENV_VAR]
    )
    def test_build_rejects_non_numeric_interval(self, db_path, config_path, monkeypatch, env_var):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(env_var, "каждый час")

        with pytest.raises(ValueError) as excinfo:
            build_daemon(db_path=db_path, config_path=config_path, port=0)

        assert env_var in str(excinfo.value)


class TestBuildDaemonFileLevel:
    """Уровень файлового лога применяется там, где демон читает конфиг."""

    def test_build_applies_the_file_level_from_config(
        self, db_path, config_path, monkeypatch
    ):
        import logger as legacy_logger

        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        config_path.write_text(
            json.dumps({"behavior": {"query": "", "log_file_level": "WARNING"}}),
            encoding="utf-8",
        )
        applied = []
        monkeypatch.setattr(legacy_logger, "apply_file_level", lambda name: applied.append(name))

        daemon = build_daemon(db_path=db_path, config_path=config_path, port=0)

        try:
            assert applied == ["WARNING"], "демон обязан применить уровень из конфига"
        finally:
            daemon.shutdown()

    def test_build_without_the_key_applies_the_default_level(
        self, db_path, config_path, monkeypatch
    ):
        import logger as legacy_logger

        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        applied = []
        monkeypatch.setattr(legacy_logger, "apply_file_level", lambda name: applied.append(name))

        daemon = build_daemon(db_path=db_path, config_path=config_path, port=0)

        try:
            assert applied == ["INFO"], "дефолт уровня — INFO, а не «не применять»"
        finally:
            daemon.shutdown()


class TestLevelWiring:
    """Уровень из конфига доводится до экспорта и до файлового логгера."""

    def test_day_close_exports_only_records_at_or_above_the_configured_level(
        self, db_path, config_path, registry, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        today = local_day(time.time())
        insert_log(db_path, today, message="debug detail", level="DEBUG")
        insert_log(db_path, today, message="hard failure", level="ERROR")
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            day_close_interval=0.05,
            config_data={
                "behavior": {"browser_count": 2, "log_file_level": "ERROR"}
            },
        )

        daemon.start()
        try:
            exported = tmp_path / "logs" / f"{today}.log"
            assert wait_until(exported.exists), "закрытие дня не выгрузило файл"
            body = exported.read_text(encoding="utf-8")
        finally:
            daemon.shutdown()

        assert "hard failure" in body
        assert "debug detail" not in body, "DEBUG обязан остаться только в БД"

    def test_db_size_failure_is_logged_and_the_job_keeps_ticking(
        self, db_path, config_path, registry, monkeypatch
    ):
        import engine.control_plane.daemon as daemon_module

        calls = []

        def exploding_guard(*args, **kwargs):
            calls.append(args)
            raise RuntimeError("диск отвалился")

        monkeypatch.setattr(daemon_module, "enforce_db_size_limit", exploding_guard)
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            db_size_interval=0.05,
            config_data={"behavior": {"browser_count": 2, "db_size_limit_mb": 1}},
        )

        daemon.start()
        try:
            assert wait_until(lambda: len(calls) >= 2), "job остановился после ошибки"
            assert get_health(daemon) == 200
            assert wait_until(
                lambda: any("db size check failed" in m for m in log_messages(db_path))
            )
        finally:
            daemon.shutdown()
