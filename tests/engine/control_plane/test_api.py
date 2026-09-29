"""Тесты HTTP API control plane.

Сервер поднимается на 127.0.0.1 со свободным портом (порт 0) и гасится в
фикстуре. Это настоящий сокет, но не сеть наружу: демон слушает только
loopback, и тест это отдельно проверяет.

Проверяется в первую очередь то, что ломает эксплуатацию: демон не должен
отвечать без токена, не должен слушать вне loopback и не должен отдавать HTML
с трейсбеком вместо JSON.
"""

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from engine.control_plane import config as config_module
from engine.control_plane import api
from engine.control_plane.api import (
    TOKEN_ENV_VAR,
    TOKEN_HEADER,
    ControlPlaneServer,
    MissingTokenError,
    token_from_environ,
)
from engine.control_plane.config import Config
from engine.control_plane.state import StateStore, WorkerStatus
from engine.control_plane.supervisor import Supervisor, SupervisorSettings
from tests.engine.control_plane.test_supervisor import FakeClock, FakeProcessRegistry

TOKEN = "s3cret-token-value"
BASE_URL_TOKEN = "http://{host}:{port}"


@pytest.fixture
def db_path(tmp_path):
    from engine.db import migrations

    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


@pytest.fixture
def store(db_path):
    return StateStore(db_path)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def registry():
    return FakeProcessRegistry()


@pytest.fixture
def settings():
    return SupervisorSettings(
        heartbeat_interval=5.0,
        shutdown_grace_seconds=0.2,
        restart_backoff_base=0.05,
        restart_backoff_max=0.1,
        max_restarts=2,
        restart_count_reset_after=3600.0,
        max_workers=8,
    )


@pytest.fixture
def supervisor(store, registry, clock, settings):
    return Supervisor(
        store=store,
        settings=settings,
        spawn=registry,
        clock=clock,
        browser_ids=lambda n: [f"br-{i + 1}" for i in range(n)],
    )


@pytest.fixture
def config():
    return Config.from_dict(config_module.default_config())


@pytest.fixture
def server(supervisor, config, tmp_path):
    """Живой сервер на свободном порту loopback, гасится после теста."""
    instance = ControlPlaneServer(
        supervisor=supervisor,
        config=config,
        token=TOKEN,
        config_path=tmp_path / "config.json",
        host="127.0.0.1",
        port=0,
    )
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def request(server, path, method="GET", token=TOKEN, body=None, headers=None):
    """HTTP-запрос к поднятому серверу. Возвращает (status, json, headers)."""
    url = f"http://{server.host}:{server.port}{path}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        req.add_header(TOKEN_HEADER, token)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = response.read().decode("utf-8")
            return response.status, json.loads(payload), dict(response.headers)
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {"_raw": payload}
        return exc.code, parsed, dict(exc.headers)


class TestBinding:
    """Сервер слушает только loopback. Это проверка безопасности, а не стиля."""

    def test_binds_to_loopback_only(self, server):
        assert server.host == "127.0.0.1"

    def test_socket_is_not_reachable_on_external_interface(self, server):
        """Порт слушает 127.0.0.1, а не 0.0.0.0.

        Проверяется connect'ом на адрес, поднятый на том же порту: если бы
        демон слушал 0.0.0.0, соединение прошло бы и здесь.
        """
        external = _external_address()
        if external is None:
            pytest.skip("нет внешнего адреса для проверки")

        with pytest.raises(OSError):
            with socket.create_connection((external, server.port), timeout=2):
                pass

    def test_rejects_non_loopback_bind_address(self, supervisor, config, tmp_path):
        """Конструктор не даст поднять демон на наружном интерфейсе."""
        with pytest.raises(ValueError) as excinfo:
            ControlPlaneServer(
                supervisor=supervisor,
                config=config,
                token=TOKEN,
                config_path=tmp_path / "config.json",
                host="0.0.0.0",
                port=0,
            )

        assert "127.0.0.1" in str(excinfo.value)

    def test_rejects_routable_bind_address(self, supervisor, config, tmp_path):
        with pytest.raises(ValueError):
            ControlPlaneServer(
                supervisor=supervisor,
                config=config,
                token=TOKEN,
                config_path=tmp_path / "config.json",
                host="192.168.1.10",
                port=0,
            )

    def test_server_reports_assigned_port(self, server):
        assert server.port > 0, "порт 0 должен быть заменён на реально выданный"

    def test_default_host_is_loopback(self, supervisor, config, tmp_path):
        instance = ControlPlaneServer(
            supervisor=supervisor, config=config, token=TOKEN, config_path=tmp_path / "config.json"
        )

        assert instance.host == "127.0.0.1"


def _external_address() -> str | None:
    """Внешний адрес этой машины, если он есть (для проверки bind)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            address = sock.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") else address


class TestTokenSource:
    """Токен берётся из окружения; без него демон не стартует вовсе."""

    def test_reads_token_from_environment(self, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "from-env")

        assert token_from_environ() == "from-env"

    def test_missing_variable_raises(self, monkeypatch):
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)

        with pytest.raises(MissingTokenError) as excinfo:
            token_from_environ()

        assert TOKEN_ENV_VAR in str(excinfo.value)

    def test_empty_variable_raises(self, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "")

        with pytest.raises(MissingTokenError):
            token_from_environ()

    def test_whitespace_only_variable_raises(self, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "   ")

        with pytest.raises(MissingTokenError):
            token_from_environ()

    def test_token_env_var_name_is_documented(self):
        assert TOKEN_ENV_VAR == "ADCLICKER_CONTROL_TOKEN"

    def test_server_refuses_to_start_without_token(self, supervisor, config, tmp_path):
        """Конструктор падает, а не поднимает защищённый лишь "всем желающим" сервер."""
        with pytest.raises(MissingTokenError):
            ControlPlaneServer(
                supervisor=supervisor, config=config, token="", config_path=tmp_path / "config.json"
            )


class TestAuthorization:
    """Без верного токена — 401 ни на одном endpoint, включая /health."""

    def test_health_requires_token(self, server):
        status, body, _ = request(server, "/health", token=None)

        assert status == 401
        assert body["error"]["code"] == "unauthorized"

    def test_state_requires_token(self, server):
        status, _, _ = request(server, "/state", token=None)

        assert status == 401

    def test_start_requires_token(self, server):
        status, _, _ = request(server, "/control/start", method="POST", token=None)

        assert status == 401

    def test_config_requires_token(self, server):
        status, _, _ = request(server, "/control/config", token=None)

        assert status == 401

    def test_wrong_token_is_rejected(self, server):
        status, body, _ = request(server, "/health", token="wrong-token")

        assert status == 401
        assert body["error"]["code"] == "unauthorized"

    def test_token_prefix_is_not_enough(self, server):
        status, _, _ = request(server, "/health", token=TOKEN[:-1])

        assert status == 401

    def test_token_with_extra_suffix_is_rejected(self, server):
        status, _, _ = request(server, "/health", token=TOKEN + "x")

        assert status == 401

    def test_empty_token_header_is_rejected(self, server):
        status, _, _ = request(server, "/health", token="")

        assert status == 401

    def test_valid_token_passes(self, server):
        status, body, _ = request(server, "/health")

        assert status == 200
        assert body["status"] == "ok"

    def test_authorization_header_form_is_accepted(self, server):
        url = f"http://{server.host}:{server.port}/health"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {TOKEN}")

        with urllib.request.urlopen(req, timeout=10) as response:
            assert response.status == 200

    def test_authorization_header_with_wrong_token_is_rejected(self, server):
        url = f"http://{server.host}:{server.port}/health"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", "Bearer nope")

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(req, timeout=10)

        assert excinfo.value.code == 401

    def test_auth_error_message_does_not_leak_expected_token(self, server):
        status, body, _ = request(server, "/health", token="wrong")

        assert TOKEN not in json.dumps(body)

    def test_auth_is_checked_before_routing(self, server):
        """Несуществующий путь без токена — 401, а не 404.

        Иначе по коду ответа можно было бы перечислить маршруты демона.
        """
        status, _, _ = request(server, "/nope", token=None)

        assert status == 401


class TestHealthEndpoint:
    def test_health_is_ok_when_idle(self, server):
        status, body, _ = request(server, "/health")

        assert status == 200
        assert body["status"] == "ok"
        assert body["state"] == "stopped"
        assert body["version"] == api.PROTOCOL_VERSION

    def test_health_reports_worker_counts(self, server):
        request(server, "/control/start", method="POST", body={"workers": 2})

        status, body, _ = request(server, "/health")

        assert status == 200
        assert body["workers_total"] == 2
        assert body["workers_alive"] == 2

    def test_health_rejects_post(self, server):
        status, body, _ = request(server, "/health", method="POST")

        assert status == 405
        assert body["error"]["code"] == "method_not_allowed"


class TestStateEndpoint:
    def test_state_reports_stopped_at_start(self, server):
        status, body, _ = request(server, "/state")

        assert status == 200
        assert body["state"] == "stopped"
        assert body["paused"] is False
        assert body["workers"] == []
        assert body["worker_count"] == 0

    def test_state_lists_workers_after_start(self, server):
        request(server, "/control/start", method="POST", body={"workers": 3})

        status, body, _ = request(server, "/state")

        assert status == 200
        assert body["state"] == "running"
        assert body["worker_count"] == 3
        assert [w["browser_id"] for w in body["workers"]] == ["br-1", "br-2", "br-3"]

    def test_state_worker_has_pid_and_status(self, server):
        request(server, "/control/start", method="POST", body={"workers": 1})

        _, body, _ = request(server, "/state")

        worker = body["workers"][0]
        assert isinstance(worker["pid"], int)
        assert worker["status"] in {"starting", "running"}
        assert worker["restart_count"] == 0

    def test_state_reflects_pause(self, server):
        request(server, "/control/start", method="POST", body={"workers": 2})
        request(server, "/control/pause", method="POST")

        status, body, _ = request(server, "/state")

        assert status == 200
        assert body["state"] == "paused"
        assert body["paused"] is True
        assert body["worker_count"] == 2, "пауза не убивает воркеров: их строки остаются"

    def test_pause_keeps_worker_processes_running(self, server, registry):
        """Пауза = "доработай сценарий", а не SIGTERM.

        Проверяется по живым процессам: состояние в БД может совпадать, а вот
        убитые процессы означали бы потерю результатов текущего сценария.
        """
        request(server, "/control/start", method="POST", body={"workers": 2})
        request(server, "/control/pause", method="POST")

        assert len(registry.alive()) == 2
        assert registry.terminated == []

    def test_state_reports_started_but_unticked_worker_as_starting(self, server, registry):
        """До первого тика воркер честно starting, а не running.

        Различать эти состояния важно: running должен означать "процесс пережил
        запуск", а старт процесса ещё этого не гарантирует.
        """
        request(server, "/control/start", method="POST", body={"workers": 1})

        _, body, _ = request(server, "/state")

        assert body["workers"][0]["status"] == "starting"
        assert body["alive_count"] == 0

    def test_state_reports_pid_cleared_after_stop(self, server):
        request(server, "/control/start", method="POST", body={"workers": 2})
        request(server, "/control/stop", method="POST")

        _, body, _ = request(server, "/state")

        assert all(worker["pid"] is None for worker in body["workers"])
        assert body["alive_count"] == 0

    def test_state_has_updated_at(self, server):
        _, body, _ = request(server, "/state")

        assert body["updated_at"] > 0

    def test_state_reports_degraded_worker(self, server, store):
        """Сигнал воркера обязан дойти до /state — иначе UI его не видит.

        ``mark_degraded`` пишет строку из процесса воркера, а HTTP-ответ
        собирает супервизорский снимок: здесь проверяется именно этот путь,
        включая сохранение PID (деградация не убивает процесс).
        """
        request(server, "/control/start", method="POST", body={"workers": 1})
        store.set_status("br-1", WorkerStatus.DEGRADED, error="cdp connection lost")

        status, body, _ = request(server, "/state")

        assert status == 200
        worker = body["workers"][0]
        assert worker["status"] == "degraded"
        assert worker["last_error"] == "cdp connection lost"
        assert isinstance(worker["pid"], int)

    def test_state_rejects_post(self, server):
        status, _, _ = request(server, "/state", method="POST")

        assert status == 405

    def test_state_does_not_expose_token(self, server):
        _, body, _ = request(server, "/state")

        assert TOKEN not in json.dumps(body)


class TestControlStart:
    def test_start_spawns_workers(self, server, registry):
        status, body, _ = request(server, "/control/start", method="POST", body={"workers": 2})

        assert status == 200
        assert len(registry.created) == 2
        assert body["state"] == "running"
        assert body["workers"] == ["br-1", "br-2"]

    def test_start_without_body_uses_browser_count_from_config(self, server, registry, config):
        """Без тела запроса берётся behavior.browser_count — клиенту не нужно
        знать дефолт демона."""
        status, body, _ = request(server, "/control/start", method="POST")

        assert status == 200
        assert len(registry.created) == config.get("behavior.browser_count") == 2
        assert body["workers"] == ["br-1", "br-2"]

    def test_start_without_body_follows_updated_config(self, server, registry):
        request(server, "/control/config", method="POST", body={"behavior": {"browser_count": 3}})

        status, body, _ = request(server, "/control/start", method="POST")

        assert status == 200
        assert len(registry.created) == 3
        assert body["workers"] == ["br-1", "br-2", "br-3"]


    def test_start_twice_returns_conflict(self, server, registry):
        request(server, "/control/start", method="POST", body={"workers": 2})

        status, body, _ = request(server, "/control/start", method="POST", body={"workers": 2})

        assert status == 409
        assert body["error"]["code"] == "already_running"
        assert len(registry.created) == 2, "повторный start не должен создавать процессы"

    def test_start_with_zero_workers_is_rejected(self, server, registry):
        status, body, _ = request(server, "/control/start", method="POST", body={"workers": 0})

        assert status == 400
        assert body["error"]["code"] == "invalid_worker_count"
        assert registry.created == []

    def test_start_with_too_many_workers_is_rejected(self, server, registry):
        status, body, _ = request(server, "/control/start", method="POST", body={"workers": 99})

        assert status == 400
        assert body["error"]["code"] == "invalid_worker_count"
        assert registry.created == []

    def test_start_with_non_integer_workers_is_rejected(self, server, registry):
        status, body, _ = request(server, "/control/start", method="POST", body={"workers": "two"})

        assert status == 400
        assert body["error"]["code"] == "invalid_request"
        assert registry.created == []


class TestControlPauseResume:
    def test_pause_sets_paused_state(self, server, registry):
        request(server, "/control/start", method="POST", body={"workers": 2})

        status, body, _ = request(server, "/control/pause", method="POST")

        assert status == 200
        assert body["state"] == "paused"
        assert registry.terminated == [], "пауза не должна убивать воркеров"

    def test_pause_without_start_is_conflict(self, server):
        status, body, _ = request(server, "/control/pause", method="POST")

        assert status == 409
        assert body["error"]["code"] == "not_running"

    def test_resume_clears_pause(self, server):
        request(server, "/control/start", method="POST", body={"workers": 1})
        request(server, "/control/pause", method="POST")

        status, body, _ = request(server, "/control/resume", method="POST")

        assert status == 200
        assert body["state"] == "running"
        assert body["paused"] is False

    def test_resume_without_pause_is_conflict(self, server):
        request(server, "/control/start", method="POST", body={"workers": 1})

        status, body, _ = request(server, "/control/resume", method="POST")

        assert status == 409
        assert body["error"]["code"] == "not_paused"

    def test_pause_is_idempotent(self, server):
        request(server, "/control/start", method="POST", body={"workers": 1})

        status, _, _ = request(server, "/control/pause", method="POST")
        second, _, _ = request(server, "/control/pause", method="POST")

        assert status == 200
        assert second == 200

    def test_pause_rejects_get(self, server):
        status, _, _ = request(server, "/control/pause")

        assert status == 405


class TestControlStop:
    def test_stop_terminates_workers(self, server, registry):
        request(server, "/control/start", method="POST", body={"workers": 3})

        status, body, _ = request(server, "/control/stop", method="POST")

        assert status == 200
        assert sorted(registry.terminated) == ["br-1", "br-2", "br-3"]
        assert body["state"] == "stopped"

    def test_stop_without_start_is_idempotent(self, server):
        status, body, _ = request(server, "/control/stop", method="POST")

        assert status == 200
        assert body["state"] == "stopped"

    def test_stop_allows_subsequent_start(self, server, registry):
        request(server, "/control/start", method="POST", body={"workers": 1})
        request(server, "/control/stop", method="POST")

        status, _, _ = request(server, "/control/start", method="POST", body={"workers": 1})

        assert status == 200
        assert len(registry.created) == 2

    def test_stop_kills_stubborn_workers(self, server, registry):
        registry.ignores_sigterm = True
        request(server, "/control/start", method="POST", body={"workers": 2})

        status, _, _ = request(server, "/control/stop", method="POST")

        assert status == 200
        assert sorted(registry.killed) == ["br-1", "br-2"]


class TestControlRestart:
    def test_restart_replaces_workers(self, server, registry):
        request(server, "/control/start", method="POST", body={"workers": 2})

        status, body, _ = request(server, "/control/restart", method="POST", body={"workers": 3})

        assert status == 200
        assert len(registry.created) == 5
        assert len(registry.alive()) == 3
        assert body["state"] == "running"

    def test_restart_without_start_is_conflict(self, server):
        status, body, _ = request(server, "/control/restart", method="POST")

        assert status == 409
        assert body["error"]["code"] == "not_running"

    def test_restart_keeps_configured_count_when_body_omitted(self, server, registry, config):
        request(server, "/control/start", method="POST", body={"workers": 1})

        status, _, _ = request(server, "/control/restart", method="POST")

        assert status == 200
        assert len(registry.alive()) == config.get("behavior.browser_count")


class TestConfigEndpoint:
    def test_get_returns_config(self, server):
        status, body, _ = request(server, "/control/config")

        assert status == 200
        assert set(body["config"]) == {"paths", "webdriver", "behavior"}

    def test_get_masks_secret(self, server, supervisor, config, tmp_path):
        config = config_module.Config.from_dict(
            config_module.default_config()
        ).patch({"behavior": {"2captcha_apikey": "REAL-KEY"}})
        instance = ControlPlaneServer(
            supervisor=supervisor,
            config=config,
            token=TOKEN,
            config_path=tmp_path / "config.json",
        )
        instance.start()
        try:
            _, body, _ = request(instance, "/control/config")
        finally:
            instance.stop()

        assert body["config"]["behavior"]["2captcha_apikey"] == config_module.SECRET_MASK
        assert "REAL-KEY" not in json.dumps(body)

    def test_post_updates_config(self, server):
        status, body, _ = request(
            server, "/control/config", method="POST", body={"behavior": {"browser_count": 5}}
        )

        assert status == 200
        assert body["config"]["behavior"]["browser_count"] == 5

    def test_post_persists_to_disk(self, server, tmp_path):
        request(server, "/control/config", method="POST", body={"behavior": {"click_order": 9}})

        saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert saved["behavior"]["click_order"] == 9

    def test_post_with_masked_secret_keeps_real_value(self, supervisor, tmp_path, registry, clock):
        """Патч, в котором секрет пришёл маской, не затирает настоящий ключ.

        Тело — то, что UI собрал из GET-ответа: маска возвращается как есть.
        До фикса ``_deep_merge`` клал бы её значением, а ``save()`` закрепил
        потерю в config.json.
        """
        real = config_module.Config.from_dict(config_module.default_config()).patch(
            {"behavior": {"2captcha_apikey": "REAL-KEY"}}
        )
        instance = ControlPlaneServer(
            supervisor=supervisor, config=real, token=TOKEN, config_path=tmp_path / "config.json"
        )
        instance.start()
        try:
            status, body, _ = request(
                instance,
                "/control/config",
                method="POST",
                body={
                    "behavior": {
                        "2captcha_apikey": config_module.SECRET_MASK,
                        "click_order": 3,
                    }
                },
            )
        finally:
            instance.stop()

        assert status == 200
        assert body["config"]["behavior"]["2captcha_apikey"] == config_module.SECRET_MASK
        assert body["config"]["behavior"]["click_order"] == 3
        # Живой конфиг демона — на bound-классе обработчика, не на сервере.
        live = instance.handler_class.config
        assert live.get("behavior.2captcha_apikey") == "REAL-KEY"
        saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert saved["behavior"]["2captcha_apikey"] == "REAL-KEY"

    def test_get_then_post_round_trip_keeps_secret(self, supervisor, tmp_path, registry, clock):
        """GET → POST того же конфига: секрет цел и в памяти, и на диске."""
        real = config_module.Config.from_dict(config_module.default_config()).patch(
            {
                "behavior": {"2captcha_apikey": "REAL-KEY"},
                "webdriver": {"proxy": "http://user:pw@1.2.3.4:8080"},
            }
        )
        instance = ControlPlaneServer(
            supervisor=supervisor, config=real, token=TOKEN, config_path=tmp_path / "config.json"
        )
        instance.start()
        try:
            _, got, _ = request(instance, "/control/config")
            assert got["config"]["behavior"]["2captcha_apikey"] == config_module.SECRET_MASK
            assert got["config"]["webdriver"]["proxy"] == config_module.SECRET_MASK
            assert "REAL-KEY" not in json.dumps(got)

            status, posted, _ = request(
                instance, "/control/config", method="POST", body=got["config"]
            )
        finally:
            instance.stop()

        assert status == 200
        assert posted["config"]["behavior"]["2captcha_apikey"] == config_module.SECRET_MASK
        # Живой конфиг демона — на bound-классе обработчика, не на сервере.
        live = instance.handler_class.config
        assert live.get("behavior.2captcha_apikey") == "REAL-KEY"
        assert live.get("webdriver.proxy") == "http://user:pw@1.2.3.4:8080"
        text = (tmp_path / "config.json").read_text(encoding="utf-8")
        assert config_module.SECRET_MASK not in text
        assert "REAL-KEY" in text
        assert "user:pw" in text
        reloaded = config_module.Config.load(tmp_path / "config.json")
        assert reloaded.get("behavior.2captcha_apikey") == "REAL-KEY"
        assert reloaded.get("webdriver.proxy") == "http://user:pw@1.2.3.4:8080"

    def test_post_invalid_value_returns_400_with_problems(self, server, tmp_path):
        status, body, _ = request(
            server, "/control/config", method="POST", body={"behavior": {"browser_count": 99}}
        )

        assert status == 400
        assert body["error"]["code"] == "invalid_config"
        assert body["error"]["problems"][0]["field"] == "behavior.browser_count"

    def test_post_invalid_value_does_not_persist(self, server, tmp_path):
        request(server, "/control/config", method="POST", body={"behavior": {"browser_count": 99}})

        assert not (tmp_path / "config.json").exists(), "невалидный конфиг не должен попасть на диск"

    def test_post_unknown_key_returns_400(self, server):
        status, body, _ = request(
            server, "/control/config", method="POST", body={"behavior": {"bogus": 1}}
        )

        assert status == 400
        assert body["error"]["code"] == "invalid_config"

    def test_post_non_object_body_returns_400(self, server):
        """Список вместо объекта — ошибка формы запроса, а не конфигурации:
        ни одно поле конфига не проверялось и нарушено не было."""
        status, body, _ = request(server, "/control/config", method="POST", body=[1, 2])

        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    def test_get_after_post_reflects_change(self, server):
        request(server, "/control/config", method="POST", body={"behavior": {"click_order": 11}})

        _, body, _ = request(server, "/control/config")

        assert body["config"]["behavior"]["click_order"] == 11


class TestErrorResponses:
    """Ошибки — JSON с кодом. HTML-трейсбек пользователю не годится."""

    def test_unknown_path_returns_json_404(self, server):
        status, body, _ = request(server, "/nope")

        assert status == 404
        assert body["error"]["code"] == "not_found"

    def test_wrong_method_returns_json_405(self, server):
        status, body, _ = request(server, "/control/start", method="DELETE")

        assert status == 405
        assert body["error"]["code"] == "method_not_allowed"

    def test_405_includes_allow_header(self, server):
        _, _, headers = request(server, "/control/start", method="DELETE")

        assert headers.get("Allow") == "POST"

    def test_malformed_json_returns_400(self, server):
        url = f"http://{server.host}:{server.port}/control/config"
        req = urllib.request.Request(url, data=b"{not json", method="POST")
        req.add_header(TOKEN_HEADER, TOKEN)
        req.add_header("Content-Type", "application/json")

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(req, timeout=10)

        assert excinfo.value.code == 400
        payload = json.loads(excinfo.value.read().decode("utf-8"))
        assert payload["error"]["code"] == "invalid_json"

    def test_error_body_never_contains_traceback(self, server):
        """Трейсбек наружу утекает пути и, возможно, секреты из окружения."""
        status, body, _ = request(server, "/nope")

        assert status == 404
        assert "Traceback" not in json.dumps(body)
        assert "File \"" not in json.dumps(body)

    def test_error_body_shape_is_consistent(self, server):
        _, body, _ = request(server, "/nope")

        assert set(body) == {"error"}
        assert {"code", "message"} <= set(body["error"])

    def test_oversized_body_is_rejected(self, server):
        url = f"http://{server.host}:{server.port}/control/config"
        payload = json.dumps({"behavior": {"excludes": "x" * 200_000}}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header(TOKEN_HEADER, TOKEN)
        req.add_header("Content-Type", "application/json")

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(req, timeout=10)

        assert excinfo.value.code == 413

    def test_internal_error_is_json_500(self, server, monkeypatch):
        """Необработанное исключение тоже не должно утечь трейсбеком."""
        def boom(self_handler):
            raise RuntimeError("database is on fire")

        monkeypatch.setattr(server.handler_class, "_handle_state", boom)

        status, body, _ = request(server, "/state")

        assert status == 500
        assert body["error"]["code"] == "internal_error"
        assert "database is on fire" not in json.dumps(body)
        assert "Traceback" not in json.dumps(body)

    def test_internal_error_is_recorded_in_database(self, server, monkeypatch, store):
        """Пользователю трейсбек не показываем, но для разбора он остаётся."""
        def boom(self_handler):
            raise RuntimeError("selenium exploded")

        monkeypatch.setattr(server.handler_class, "_handle_state", boom)
        request(server, "/state")

        with store._connect() as conn:
            rows = conn.execute(
                "SELECT message, fields FROM logs WHERE category = 'api'"
            ).fetchall()
        assert any("unhandled" in row["message"] for row in rows)
        assert any("selenium exploded" in (row["fields"] or "") for row in rows)



class TestConcurrentRequests:
    """HTTP-сервер обслуживает несколько запросов одновременно."""

    def test_parallel_state_and_control_requests(self, server):
        results = []
        errors = []

        def call(path, method="GET", body=None):
            try:
                results.append(request(server, path, method=method, body=body))
            except Exception as exc:  # noqa: BLE001 - тест обязан увидеть причину
                errors.append(exc)

        threads = [
            threading.Thread(target=call, args=("/state",)),
            threading.Thread(target=call, args=("/health",)),
            threading.Thread(target=call, args=("/control/start", "POST", {"workers": 3})),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert errors == []
        assert all(status in (200, 409) for status, _, _ in results)

    def test_three_workers_start_and_stop_over_http(self, server, registry, store):
        """Полный сценарий через настоящий сокет, 3 воркера."""
        status, _, _ = request(server, "/control/start", method="POST", body={"workers": 3})
        assert status == 200

        _, body, _ = request(server, "/state")
        assert body["worker_count"] == 3
        assert len(registry.alive()) == 3

        status, _, _ = request(server, "/control/stop", method="POST")
        assert status == 200
        assert len(registry.alive()) == 0
        assert {w["status"] for w in store.list_workers()} == {"stopped"}
