"""Тесты job'а постоянного экспорта в PostgreSQL (план §9, контракт §13.3).

Нить ``export`` на ``stop_event`` — тот же паттерн, что у metrics и
proxy-check, но сам тик устроен иначе: настройки берутся из секции
``export`` config.json на каждом проходе, ``enabled=false`` — полный no-op
без открытого соединения, а неудачный проход уходит в лог через
``_run_tick`` (``category="export"``) и не убивает ни нить, ни демон.

Psycopg в тестах не нужен: экземпляр ``Exporter`` подменяется фейковым
классом на уровне модуля демона — здесь проверяется связка «конфиг →
настройки → проход → лог», а сама выгрузка разобрана в
``tests/engine/test_exporter.py``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from engine.control_plane import config as config_module
from engine.control_plane.api import TOKEN_ENV_VAR
from engine.control_plane.daemon import (
    DEFAULT_EXPORT_INTERVAL_SECONDS,
    EXPORT_INTERVAL_ENV_VAR,
    EXPORT_THREAD_NAME,
    Daemon,
    build_daemon,
    export_interval_from_environ,
)
from engine.control_plane.state import StateStore
from engine.control_plane.supervisor import Supervisor, SupervisorSettings
from engine.exporter import ExportError
from tests.engine.control_plane.test_supervisor import FakeClock, FakeProcessRegistry

TOKEN = "export-job-token"


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


def config_with(**export_overrides):
    """Конфиг теста: валидная секция export поверх минимального поведения."""
    export = {"enabled": True}
    export.update(export_overrides)
    return {"behavior": {"browser_count": 2}, "export": export}


def make_daemon(db_path, config_path, registry, **overrides):
    """Демон без чужих job'ов: фокус теста — нить экспорта."""
    store = StateStore(db_path)
    raw_config = overrides.pop("config_data", None)
    config = config_module.Config.from_dict(
        raw_config if raw_config is not None else {"behavior": {"browser_count": 2}}
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
        "day_close_interval": 0.0,
        "retention_interval": 0.0,
        "db_size_interval": 0.0,
        "cleanup_interval": 0.0,
        "metrics_interval": 0.0,
        "export_interval": 0.0,
    }
    kwargs.update(overrides)
    return Daemon(**kwargs)


class FakeExporter:
    """Подмена ``engine.control_plane.daemon.Exporter``: записывает вызовы."""

    instances: list[FakeExporter] = []
    raise_on_pass = False

    def __init__(self, db_path, settings):
        self.db_path = db_path
        self.settings = settings
        self.passes: list[float | None] = []
        self.closed = 0
        FakeExporter.instances.append(self)

    def export_pass(self, *, now=None):
        if FakeExporter.raise_on_pass:
            raise ExportError("PostgreSQL недоступен")
        self.passes.append(now)
        return {"logs": 3, "clicks": 1}

    def close(self):
        self.closed += 1


@pytest.fixture
def fake_exporter(monkeypatch):
    import engine.control_plane.daemon as daemon_module

    FakeExporter.instances = []
    FakeExporter.raise_on_pass = False
    monkeypatch.setattr(daemon_module, "Exporter", FakeExporter)
    return FakeExporter


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def thread_alive(name):
    return any(thread.name == name and thread.is_alive() for thread in threading.enumerate())


def log_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return [
            {"message": row[0], "category": row[1], "fields": row[2]}
            for row in conn.execute("SELECT message, category, fields FROM logs ORDER BY id").fetchall()
        ]


def messages(db_path):
    return [row["message"] for row in log_rows(db_path)]


class TestExportIntervalFromEnviron:
    def test_default_is_one_minute(self):
        assert DEFAULT_EXPORT_INTERVAL_SECONDS == 60.0
        assert export_interval_from_environ({}) == 60.0

    def test_blank_value_falls_back_to_the_default(self):
        assert export_interval_from_environ({EXPORT_INTERVAL_ENV_VAR: "  "}) == 60.0

    def test_value_is_read_from_the_environment(self):
        assert export_interval_from_environ({EXPORT_INTERVAL_ENV_VAR: "15"}) == 15.0

    @pytest.mark.parametrize("value", ["0", "-5"])
    def test_zero_or_negative_disables_the_job(self, value):
        assert export_interval_from_environ({EXPORT_INTERVAL_ENV_VAR: value}) == 0.0

    def test_non_numeric_value_raises(self):
        with pytest.raises(ValueError) as excinfo:
            export_interval_from_environ({EXPORT_INTERVAL_ENV_VAR: "каждую минуту"})

        assert EXPORT_INTERVAL_ENV_VAR in str(excinfo.value)

    def test_env_var_name_is_documented(self):
        assert EXPORT_INTERVAL_ENV_VAR == "ADCLICKER_EXPORT_INTERVAL"
        assert EXPORT_THREAD_NAME == "export"


class TestBuildDaemonExportInterval:
    def test_build_takes_the_interval_from_the_environment(
        self, db_path, config_path, monkeypatch
    ):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(EXPORT_INTERVAL_ENV_VAR, "120")

        daemon = build_daemon(db_path=db_path, config_path=config_path, port=0)

        try:
            assert daemon.export_interval == 120.0
        finally:
            daemon.shutdown()

    def test_explicit_interval_wins_over_the_environment(
        self, db_path, config_path, monkeypatch
    ):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(EXPORT_INTERVAL_ENV_VAR, "120")

        daemon = build_daemon(
            db_path=db_path, config_path=config_path, port=0, export_interval=42.0
        )

        try:
            assert daemon.export_interval == 42.0
        finally:
            daemon.shutdown()

    def test_build_rejects_a_non_numeric_interval(self, db_path, config_path, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")
        monkeypatch.setenv(EXPORT_INTERVAL_ENV_VAR, "каждую минуту")

        with pytest.raises(ValueError) as excinfo:
            build_daemon(db_path=db_path, config_path=config_path, port=0)

        assert EXPORT_INTERVAL_ENV_VAR in str(excinfo.value)


class TestExportTick:
    def test_disabled_section_is_a_no_op(self, db_path, config_path, registry, fake_exporter):
        daemon = make_daemon(db_path, config_path, registry)

        daemon._export_tick(now=1000.0)

        assert daemon._exporter is None, "выключенный экспорт не создаёт соединение"
        assert fake_exporter.instances == []
        assert messages(db_path) == []

    def test_enabled_section_runs_a_pass_and_logs_the_summary(
        self, db_path, config_path, registry, fake_exporter
    ):
        daemon = make_daemon(db_path, config_path, registry, config_data=config_with())

        daemon._export_tick(now=1000.0)

        assert len(fake_exporter.instances) == 1
        exporter = fake_exporter.instances[0]
        assert exporter.passes == [1000.0]
        assert exporter.settings.enabled is True
        assert exporter.settings.batch_size == 500

        rows = log_rows(db_path)
        assert [row["message"] for row in rows] == ["export tick"]
        assert rows[0]["category"] == "export"
        assert json.loads(rows[0]["fields"]) == {"rows": {"logs": 3, "clicks": 1}}

    def test_settings_come_from_the_config_on_every_tick(
        self, db_path, config_path, registry, fake_exporter
    ):
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            config_data=config_with(host="db.internal", port=6432, sslmode="require"),
        )

        daemon._export_tick(now=1000.0)

        settings = fake_exporter.instances[0].settings
        assert (settings.host, settings.port, settings.sslmode) == ("db.internal", 6432, "require")
        assert settings.user == "adclicker"
        assert settings.dbname == "adclicker_export"

    def test_changed_settings_recreate_the_connection(
        self, db_path, config_path, registry, fake_exporter
    ):
        daemon = make_daemon(
            db_path, config_path, registry, config_data=config_with(host="db.internal")
        )
        daemon._export_tick(now=1000.0)
        first = fake_exporter.instances[0]

        daemon.server.patch_config({"export": {"host": "db.other"}})
        daemon._export_tick(now=2000.0)

        assert first.closed == 1, "старое соединение обязано быть закрыто"
        assert len(fake_exporter.instances) == 2
        assert fake_exporter.instances[1].settings.host == "db.other"
        assert daemon._exporter is fake_exporter.instances[1]

    def test_unchanged_settings_keep_the_same_connection(
        self, db_path, config_path, registry, fake_exporter
    ):
        daemon = make_daemon(db_path, config_path, registry, config_data=config_with())

        daemon._export_tick(now=1000.0)
        daemon._export_tick(now=2000.0)

        assert len(fake_exporter.instances) == 1
        assert fake_exporter.instances[0].closed == 0

    def test_failed_pass_drops_the_connection_so_the_next_tick_can_reconnect(
        self, db_path, config_path, registry, fake_exporter
    ):
        """Сгоревшее соединение (рестарт PG) не должно прожить до рестарта демона."""
        FakeExporter.raise_on_pass = True
        daemon = make_daemon(db_path, config_path, registry, config_data=config_with())

        with pytest.raises(ExportError):
            daemon._export_tick(now=1000.0)

        assert daemon._exporter is None, "умершее соединение не должно пережить тик"
        assert fake_exporter.instances[0].closed == 1

        FakeExporter.raise_on_pass = False
        daemon._export_tick(now=2000.0)

        assert len(fake_exporter.instances) == 2, "следующий тик обязан открыть новое"
        assert daemon._exporter is fake_exporter.instances[1]


class TestExportJobLifecycle:
    def test_zero_interval_never_starts_the_job(self, db_path, config_path, registry):
        daemon = make_daemon(db_path, config_path, registry, export_interval=0.0)

        daemon.start()
        try:
            time.sleep(0.3)

            assert thread_alive(EXPORT_THREAD_NAME) is False
        finally:
            daemon.shutdown()

    def test_job_ticks_and_stops_on_shutdown(
        self, db_path, config_path, registry, fake_exporter
    ):
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            config_data=config_with(),
            export_interval=0.05,
        )

        daemon.start()
        assert thread_alive(EXPORT_THREAD_NAME) is True
        assert wait_until(lambda: len(fake_exporter.instances) == 1)
        assert wait_until(lambda: len(fake_exporter.instances[0].passes) >= 2), "job должен тикать"

        daemon.shutdown()

        assert thread_alive(EXPORT_THREAD_NAME) is False
        assert fake_exporter.instances[0].closed == 1, "shutdown закрывает соединение"
        assert daemon._exporter is None

    def test_failure_is_logged_and_the_job_keeps_ticking(
        self, db_path, config_path, registry, fake_exporter
    ):
        FakeExporter.raise_on_pass = True
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            config_data=config_with(),
            export_interval=0.05,
        )

        daemon.start()
        try:
            assert wait_until(lambda: "export tick failed" in messages(db_path)), (
                "сбой job'а обязан попасть в лог"
            )
            assert thread_alive(EXPORT_THREAD_NAME) is True, "нить обязана пережить сбой"
            assert wait_until(
                lambda: messages(db_path).count("export tick failed") >= 2
            ), "job должен продолжать после ошибки"
        finally:
            daemon.shutdown()

        assert thread_alive(EXPORT_THREAD_NAME) is False

    def test_daemon_does_not_start_the_exporter_while_disabled(
        self, db_path, config_path, registry, fake_exporter
    ):
        daemon = make_daemon(db_path, config_path, registry, export_interval=0.05)

        daemon.start()
        try:
            time.sleep(0.3)

            assert thread_alive(EXPORT_THREAD_NAME) is True, "расписание включено…"
            assert fake_exporter.instances == [], "…но секция export выключена"
        finally:
            daemon.shutdown()
