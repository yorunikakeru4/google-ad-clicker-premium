"""Тесты демона: сборка, сигналы, отказ стартовать без токена.

Демон — точка, где сходятся БД, конфиг, супервизор и HTTP. Проверяется
собранный целиком: поднимается, отвечает, по SIGTERM корректно гасит воркеров
и пишет финальную запись в БД.
"""

import json
import os
import signal
import threading
import urllib.error
import urllib.request

import pytest

from engine.control_plane import daemon as daemon_module
from engine.control_plane.api import TOKEN_ENV_VAR, TOKEN_HEADER
from engine.control_plane.config import Config
from engine.control_plane.daemon import Daemon, build_daemon, main
from engine.control_plane.state import StateStore
from engine.control_plane.supervisor import Supervisor, SupervisorSettings
from tests.engine.control_plane.test_supervisor import FakeClock, FakeProcessRegistry

TOKEN = "test-token-abc123"


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
def store(db_path):
    return StateStore(db_path)


def make_daemon(tmp_path, db_path, config_path, registry, clock=None, **overrides):
    """Демон с подменёнными часами и фабрикой процессов.

    Настоящие часы здесь не нужны: демон проверяется на переходе состояний, а
    не на таймингах, и сон в тестах только замедляет прогон.
    """
    store = StateStore(db_path)
    config = Config.from_dict(overrides.pop("config_data", None) or {"behavior": {"browser_count": 2}})
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
            clock=clock or FakeClock(),
            browser_ids=lambda n: [f"br-{i + 1}" for i in range(n)],
        ),
        "config": config,
    }
    kwargs.update(overrides)
    return Daemon(**kwargs)


def get_json(daemon, path, token=TOKEN):
    url = f"http://127.0.0.1:{daemon.port}{path}"
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header(TOKEN_HEADER, token)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def post_json(daemon, path, body=None, token=TOKEN):
    url = f"http://127.0.0.1:{daemon.port}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(url, data=data, method="POST")
    if token:
        req.add_header(TOKEN_HEADER, token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class TestBuildDaemon:
    """Сборка демона из аргументов командной строки и окружения."""

    def test_build_creates_daemon_on_free_port(self, db_path, config_path, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "env-token")

        daemon = build_daemon(db_path=db_path, config_path=config_path, port=0)

        try:
            assert daemon.port == 0 or daemon.port > 0
            assert daemon.token == "env-token"
        finally:
            daemon.shutdown()

    def test_build_uses_config_file_when_present(self, db_path, config_path, monkeypatch):
        config_path.write_text(
            json.dumps({"behavior": {"browser_count": 5}}), encoding="utf-8"
        )
        monkeypatch.setenv(TOKEN_ENV_VAR, "env-token")

        daemon = build_daemon(db_path=db_path, config_path=config_path, port=0)

        try:
            assert daemon.config.get("behavior.browser_count") == 5
        finally:
            daemon.shutdown()

    def test_build_fails_without_token(self, db_path, config_path, monkeypatch):
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)

        with pytest.raises(daemon_module.MissingTokenError):
            build_daemon(db_path=db_path, config_path=config_path, port=0)

    def test_build_rejects_external_bind(self, db_path, config_path, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "env-token")

        with pytest.raises(ValueError):
            build_daemon(db_path=db_path, config_path=config_path, port=0, host="0.0.0.0")

    def test_build_runs_migrations_on_startup(self, tmp_path, config_path, monkeypatch):
        """Свежая установка не должна требовать отдельного шага миграции."""
        from engine.db import migrations

        monkeypatch.setenv(TOKEN_ENV_VAR, "env-token")
        fresh_db = tmp_path / "nested" / "new.db"

        daemon = build_daemon(db_path=fresh_db, config_path=config_path, port=0)

        try:
            assert fresh_db.exists()
            assert migrations.migrate(fresh_db) == migrations.SCHEMA_VERSION
        finally:
            daemon.shutdown()


class TestDaemonLifecycle:
    """Старт, работа, остановка демона целиком."""

    def test_start_and_stop_daemon(self, tmp_path, db_path, config_path, registry):
        daemon = make_daemon(tmp_path, db_path, config_path, registry)

        daemon.start()
        assert daemon.port > 0
        daemon.shutdown()

    def test_health_endpoint_works_after_start(self, tmp_path, db_path, config_path, registry):
        daemon = make_daemon(tmp_path, db_path, config_path, registry)
        daemon.start()

        try:
            status, body = get_json(daemon, "/health")
            assert status == 200
            assert body["status"] == "ok"
        finally:
            daemon.shutdown()

    def test_shutdown_terminates_workers(self, tmp_path, db_path, config_path, registry):
        daemon = make_daemon(tmp_path, db_path, config_path, registry)
        daemon.start()
        post_json(daemon, "/control/start", {"workers": 2})

        daemon.shutdown()

        assert sorted(registry.terminated) == ["br-1", "br-2"]

    def test_shutdown_writes_final_state_to_db(self, tmp_path, db_path, config_path, registry):
        daemon = make_daemon(tmp_path, db_path, config_path, registry)
        daemon.start()
        post_json(daemon, "/control/start", {"workers": 1})
        post_json(daemon, "/control/pause")

        daemon.shutdown()

        store = StateStore(db_path)
        assert store.get_run_state() == "stopped"
        assert store.is_pause_requested() is False
        assert store.get_worker("br-1")["status"] == "stopped"
        assert store.get_worker("br-1")["pid"] is None

    def test_shutdown_closes_open_runs(self, tmp_path, db_path, config_path, registry):
        daemon = make_daemon(tmp_path, db_path, config_path, registry)
        daemon.start()
        post_json(daemon, "/control/start", {"workers": 2})

        daemon.shutdown()

        store = StateStore(db_path)
        with store._connect() as conn:
            rows = conn.execute("SELECT status, ended_at FROM runs").fetchall()
        assert len(rows) == 2
        assert all(row["ended_at"] is not None for row in rows)

    def test_shutdown_is_idempotent(self, tmp_path, db_path, config_path, registry):
        daemon = make_daemon(tmp_path, db_path, config_path, registry)
        daemon.start()
        post_json(daemon, "/control/start", {"workers": 1})
        daemon.shutdown()

        daemon.shutdown()  # не должно бросать

    def test_shutdown_stops_background_supervisor(self, tmp_path, db_path, config_path, registry):
        daemon = make_daemon(tmp_path, db_path, config_path, registry)
        daemon.start()
        post_json(daemon, "/control/start", {"workers": 1})

        daemon.shutdown()

        assert not any(
            thread.name == "supervisor" and thread.is_alive()
            for thread in threading.enumerate()
        )

    def test_daemon_logs_startup_and_shutdown(self, tmp_path, db_path, config_path, registry):
        daemon = make_daemon(tmp_path, db_path, config_path, registry)
        daemon.start()
        daemon.shutdown()

        store = StateStore(db_path)
        with store._connect() as conn:
            messages = [row[0] for row in conn.execute("SELECT message FROM logs").fetchall()]
        assert any("daemon started" in m for m in messages)
        assert any("daemon stopped" in m for m in messages)

    def test_token_is_not_written_to_log(self, tmp_path, db_path, config_path, registry):
        daemon = make_daemon(tmp_path, db_path, config_path, registry)
        daemon.start()
        post_json(daemon, "/control/start", {"workers": 1})
        daemon.shutdown()

        store = StateStore(db_path)
        with store._connect() as conn:
            dump = "\n".join(
                str(row[0]) for row in conn.execute("SELECT fields FROM logs").fetchall()
            )
        assert TOKEN not in dump

    def test_requests_after_shutdown_fail(self, tmp_path, db_path, config_path, registry):
        daemon = make_daemon(tmp_path, db_path, config_path, registry)
        daemon.start()
        port = daemon.port
        daemon.shutdown()

        url = f"http://127.0.0.1:{port}/health"
        req = urllib.request.Request(url, method="GET")
        req.add_header(TOKEN_HEADER, TOKEN)
        with pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(req, timeout=5)


class TestSignalHandling:
    """SIGTERM и SIGINT гасят демон так же, как shutdown()."""

    def test_sigterm_triggers_graceful_shutdown(self, tmp_path, db_path, config_path, registry):
        daemon = make_daemon(tmp_path, db_path, config_path, registry)
        daemon.install_signal_handlers()
        daemon.start()
        post_json(daemon, "/control/start", {"workers": 2})

        os.kill(os.getpid(), signal.SIGTERM)
        daemon.wait_for_shutdown(timeout=10)

        assert sorted(registry.terminated) == ["br-1", "br-2"]

    def test_sigint_triggers_graceful_shutdown(self, tmp_path, db_path, config_path, registry):
        daemon = make_daemon(tmp_path, db_path, config_path, registry)
        daemon.install_signal_handlers()
        daemon.start()
        post_json(daemon, "/control/start", {"workers": 1})

        os.kill(os.getpid(), signal.SIGINT)
        daemon.wait_for_shutdown(timeout=10)

        assert registry.terminated == ["br-1"]

    def test_previous_sigterm_handler_is_restored_on_exit(self, tmp_path, db_path, config_path):
        daemon = make_daemon(tmp_path, db_path, config_path, None)
        previous = signal.getsignal(signal.SIGTERM)
        daemon.install_signal_handlers()
        assert signal.getsignal(signal.SIGTERM) is not previous

        daemon.shutdown()
        assert signal.getsignal(signal.SIGTERM) is previous

    def test_shutdown_started_by_signal_still_restores_handlers(
        self, tmp_path, db_path, config_path, registry
    ):
        """Остановку делает отдельный поток, а снять перехват обязан главный.

        signal.signal() из не главного потока бросает ValueError, поэтому
        возвращать обработчики должен тот, кто ждёт остановки, — то есть
        main() в главном потоке.
        """
        daemon = make_daemon(tmp_path, db_path, config_path, registry)
        previous = signal.getsignal(signal.SIGTERM)
        daemon.install_signal_handlers()
        daemon.start()
        post_json(daemon, "/control/start", {"workers": 1})

        os.kill(os.getpid(), signal.SIGTERM)
        daemon.wait_for_shutdown(timeout=10)

        assert signal.getsignal(signal.SIGTERM) is previous

    def test_shutdown_from_foreign_thread_defers_handler_restore(
        self, tmp_path, db_path, config_path, registry
    ):
        """Поток, не главный, не имеет права трогать перехват сигналов."""
        daemon = make_daemon(tmp_path, db_path, config_path, registry)
        previous = signal.getsignal(signal.SIGTERM)
        daemon.install_signal_handlers()
        installed = signal.getsignal(signal.SIGTERM)

        foreign = threading.Thread(target=daemon.shutdown)
        foreign.start()
        foreign.join(timeout=10)
        assert not foreign.is_alive()

        assert signal.getsignal(signal.SIGTERM) is installed
        assert installed is not previous

        daemon.wait_for_shutdown(timeout=0.1)
        assert signal.getsignal(signal.SIGTERM) is previous

    def test_wait_for_shutdown_reports_whether_stop_happened(
        self, tmp_path, db_path, config_path, registry
    ):
        """Возвращаемое значение различает «гасится» и «завис».

        Реальный сигнал здесь не посылается: обработчик здесь не установлен, и
        SIGTERM ушёл бы дефолтным disposition — то есть убил бы сам раннер.
        Путь обработчика проверен выше настоящими os.kill, а здесь нужна только
        семантика флага и ожидания.
        """
        daemon = make_daemon(tmp_path, db_path, config_path, registry)
        daemon.start()
        post_json(daemon, "/control/start", {"workers": 1})

        assert daemon.wait_for_shutdown(timeout=0.05) is False
        assert registry.terminated == [], "таймаут ожидания не должен ничего гасить"

        daemon._on_signal(signal.SIGTERM, None)

        assert daemon.wait_for_shutdown(timeout=10) is True
        assert registry.terminated == ["br-1"], "после флага остановки пул должен быть погашен"


class TestMain:
    """main() разбирает аргументы и корректно сообщает об ошибках."""

    def test_missing_token_exits_with_error(self, db_path, config_path, monkeypatch, capsys):
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)

        exit_code = main(["--db", str(db_path), "--config", str(config_path)])

        assert exit_code == 2
        captured = capsys.readouterr()
        assert TOKEN_ENV_VAR in captured.err

    def test_invalid_bind_address_exits_with_error(self, db_path, config_path, monkeypatch, capsys):
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")

        exit_code = main(
            [
                "--db",
                str(db_path),
                "--config",
                str(config_path),
                "--host",
                "0.0.0.0",
            ]
        )

        assert exit_code == 2
        assert "127.0.0.1" in capsys.readouterr().err

    def test_broken_config_exits_with_error(self, db_path, config_path, monkeypatch, capsys):
        config_path.write_text('{"behavior": {"browser_count": 999}}', encoding="utf-8")
        monkeypatch.setenv(TOKEN_ENV_VAR, "tok")

        exit_code = main(["--db", str(db_path), "--config", str(config_path)])

        assert exit_code == 2
        assert "browser_count" in capsys.readouterr().err
