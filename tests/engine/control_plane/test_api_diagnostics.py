"""HTTP-эндпоинт сбора диагностики: контракт ``POST /control/diagnostics/collect``.

Контракт (план.md, фаза 7): тело — ровно одно из ``{"browser_id": ...}`` или
``{"all": true}``, ответ — ``{"requested": n}``, где ``n`` число ЖИВЫХ
воркеров, которым выставлен kv-сигнал ``DIAGNOSTICS_REQUESTED_<id>``.
Сами снимки приходят в БД асинхронно (воркер собирает при живом браузере),
поэтому здесь проверяется только сигнал и валидация — не содержимое снимка.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from engine.control_plane import config as config_module
from engine.control_plane.api import TOKEN_HEADER, ControlPlaneServer
from engine.control_plane.config import Config
from engine.control_plane.state import StateStore
from engine.control_plane.supervisor import Supervisor, SupervisorSettings
from engine.diagnostics import DIAGNOSTICS_FLAG_PREFIX, diagnostics_flag_key
from tests.engine.control_plane.test_supervisor import FakeClock, FakeProcessRegistry

TOKEN = "diagnostics-collect-token"


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
def supervisor(store, registry, settings):
    return Supervisor(
        store=store,
        settings=settings,
        spawn=registry,
        clock=FakeClock(),
        browser_ids=lambda n: [f"br-{i + 1}" for i in range(n)],
    )


@pytest.fixture
def server(supervisor, tmp_path):
    instance = ControlPlaneServer(
        supervisor=supervisor,
        config=Config.from_dict(config_module.default_config()),
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


def call(server, path, method="POST", token=TOKEN, body=None):
    """HTTP-запрос. Возвращает (status, json)."""
    url = f"http://{server.host}:{server.port}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        request.add_header(TOKEN_HEADER, token)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"_raw": raw}
        return exc.code, payload


def start_workers(server, count):
    status, body = call(server, "/control/start", body={"workers": count})
    assert status == 200, body
    return body["workers"]


def signal_of(store, browser_id):
    return store.get_flag(diagnostics_flag_key(browser_id))


class TestValidation:
    """400 invalid_request на всё, что не соответствует контракту."""

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"browser_id": "br-1", "all": True},
            {"browser_id": 5},
            {"browser_id": None},
            {"browser_id": True},
            {"browser_id": []},
            {"browser_id": ""},
            {"browser_id": "   "},
            {"all": False},
            {"all": "true"},
            {"all": 1},
            {"all": None},
        ],
    )
    def test_bad_bodies_are_invalid_request(self, server, body):
        status, payload = call(server, "/control/diagnostics/collect", body=body)

        assert status == 400
        assert payload["error"]["code"] == "invalid_request"

    @pytest.mark.parametrize("body", [["browser_id"], "br-1", 42])
    def test_non_object_body_is_invalid_request(self, server, body):
        status, payload = call(server, "/control/diagnostics/collect", body=body)

        assert status == 400
        assert payload["error"]["code"] == "invalid_request"

    def test_malformed_json_is_invalid_json(self, server):
        url = f"http://{server.host}:{server.port}/control/diagnostics/collect"
        request = urllib.request.Request(url, data=b"{nope", method="POST")
        request.add_header(TOKEN_HEADER, TOKEN)
        request.add_header("Content-Type", "application/json")

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)

        assert excinfo.value.code == 400
        payload = json.loads(excinfo.value.read().decode())
        assert payload["error"]["code"] == "invalid_json"

    def test_requires_token(self, server):
        status, payload = call(
            server, "/control/diagnostics/collect", token=None, body={"all": True}
        )

        assert status == 401
        assert payload["error"]["code"] == "unauthorized"

    def test_get_on_the_path_is_405(self, server):
        status, payload = call(server, "/control/diagnostics/collect", method="GET")

        assert status == 405
        assert payload["error"]["code"] == "method_not_allowed"

    def test_validation_happens_before_any_flag_is_set(self, server, store):
        status, _ = call(server, "/control/diagnostics/collect", body={"all": True, "browser_id": ""})

        assert status == 400
        assert signal_of(store, "br-1") is None


class TestCollectSingleBrowser:
    def test_alive_worker_gets_the_flag(self, server, store):
        start_workers(server, 1)

        status, payload = call(
            server, "/control/diagnostics/collect", body={"browser_id": "br-1"}
        )

        assert status == 200
        assert payload == {"requested": 1}
        value = signal_of(store, "br-1")
        assert value is not None
        assert float(value) > 0, "значение — метка времени, по ней воркер видит свежесть"

    def test_unknown_worker_is_not_signalled(self, server, store):
        start_workers(server, 1)

        status, payload = call(
            server, "/control/diagnostics/collect", body={"browser_id": "br-404"}
        )

        assert status == 200
        assert payload == {"requested": 0}
        assert signal_of(store, "br-404") is None

    def test_stopped_worker_is_not_signalled(self, server, store, registry):
        start_workers(server, 1)
        registry.created[0].exit(1)
        # Супервизор на тике узнаёт о смерти и снимает воркера из реестра.
        assert supervisor_alive(server) == []

        status, payload = call(
            server, "/control/diagnostics/collect", body={"browser_id": "br-1"}
        )

        assert payload == {"requested": 0}
        assert signal_of(store, "br-1") is None

    def test_repeated_request_advances_the_timestamp(self, server, store):
        start_workers(server, 1)

        call(server, "/control/diagnostics/collect", body={"browser_id": "br-1"})
        first = signal_of(store, "br-1")
        call(server, "/control/diagnostics/collect", body={"browser_id": "br-1"})
        second = signal_of(store, "br-1")

        assert float(second) > float(first), (
            "каждый новый запрос обязан быть свежим для воркера"
        )

    def test_request_after_the_worker_cleared_the_flag(self, server, store):
        start_workers(server, 1)
        call(server, "/control/diagnostics/collect", body={"browser_id": "br-1"})
        # Воркер сработал: флаг снят, своё «последнее обработанное» — метка выше.
        store.set_flag(diagnostics_flag_key("br-1"), "")

        status, payload = call(
            server, "/control/diagnostics/collect", body={"browser_id": "br-1"}
        )

        assert payload == {"requested": 1}
        assert signal_of(store, "br-1"), "новый запрос после снятия снова виден воркеру"


class TestCollectAll:
    def test_every_alive_worker_is_signalled(self, server, store):
        workers = start_workers(server, 3)

        status, payload = call(server, "/control/diagnostics/collect", body={"all": True})

        assert status == 200
        assert payload == {"requested": 3}
        for browser_id in workers:
            assert signal_of(store, browser_id) is not None
        assert DIAGNOSTICS_FLAG_PREFIX in diagnostics_flag_key("br-1")

    def test_all_without_running_workers_is_zero(self, server, store):
        status, payload = call(server, "/control/diagnostics/collect", body={"all": True})

        assert status == 200
        assert payload == {"requested": 0}
        assert signal_of(store, "br-1") is None

    def test_dead_workers_drop_out_of_the_count(self, server, store, registry):
        workers = start_workers(server, 2)
        registry.created[1].exit(1)
        assert supervisor_alive(server) == [workers[0]]

        status, payload = call(server, "/control/diagnostics/collect", body={"all": True})

        assert payload == {"requested": 1}
        assert signal_of(store, workers[0]) is not None
        assert signal_of(store, workers[1]) is None


def supervisor_alive(server) -> list[str]:
    """Живые воркеры из реестра супервизора — тот же источник, что у ``all``."""
    return server.supervisor.alive_browser_ids()
