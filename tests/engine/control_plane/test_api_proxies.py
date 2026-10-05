"""Тесты HTTP-эндпоинтов прокси: контракт, маскирование, 409/404/400.

Сервер настоящий (loopback, свободный порт), как в ``test_api.py``. Здесь
проверяется то, что нельзя сломать без поломки UI:

- шесть маршрутов ``/control/proxies*`` с форматами ответов из контракта;
- креды в сыром тексте ответа: ни логина, ни пароля — только ``SECRET_MASK``;
- путь импорта берётся из конфига, а не из запроса;
- ``409 proxy_in_use`` и ``409 check_in_progress`` — состояния, а не ошибки.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from engine.control_plane import config as config_module
from engine.control_plane.api import TOKEN_HEADER, ControlPlaneServer
from engine.control_plane.config import Config
from engine.control_plane.state import StateStore
from engine.control_plane.supervisor import Supervisor, SupervisorSettings
from engine.proxy_health import ProxyHealthChecker
from engine.proxy_pool import ProxyPool
from tests.engine.control_plane.test_supervisor import FakeClock, FakeProcessRegistry
from tests.engine.test_proxy_health import LOOPBACK_TARGET, LoopbackProxy, closed_port

TOKEN = "proxy-pool-test-token"


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
def config():
    return Config.from_dict(config_module.default_config())


@pytest.fixture
def pool(db_path):
    return ProxyPool(db_path)


@pytest.fixture
def checker(pool):
    return ProxyHealthChecker(pool, target=LOOPBACK_TARGET, timeout=1.0)


@pytest.fixture
def make_server(supervisor, tmp_path, pool, checker):
    """Фабрика серверов: нужна, когда конфиг меняется под конкретный тест."""
    servers = []

    def factory(config_instance=None, proxy_pool=None, proxy_checker=None):
        instance = ControlPlaneServer(
            supervisor=supervisor,
            config=config_instance or Config.from_dict(config_module.default_config()),
            token=TOKEN,
            config_path=tmp_path / "config.json",
            host="127.0.0.1",
            port=0,
            proxy_pool=proxy_pool or pool,
            proxy_checker=proxy_checker or checker,
        )
        instance.start()
        servers.append(instance)
        return instance

    yield factory
    for instance in servers:
        instance.stop()


@pytest.fixture
def server(make_server):
    return make_server()


def call(server, path, method="GET", token=TOKEN, body=None):
    """HTTP-запрос. Возвращает (status, json, raw_text) — сырой текст нужен
    для проверки, что креды не попали в ответ даже вне полей JSON."""
    url = f"http://{server.host}:{server.port}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        request.add_header(TOKEN_HEADER, token)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"_raw": raw}
        return exc.code, payload, raw


def add_remote(pool, host, port, username="alice", password="s3cr3t"):
    result = pool.add_lines([f"{username}:{password}@{host}:{port}"])
    assert result["added"] == 1, result
    return pool.list_proxies()[0]


def assign_worker(db_path, proxy_id, browser_id, status="running", pid=99):
    StateStore(db_path).register_worker(browser_id, pid)
    from engine.db import migrations

    conn = migrations.connect(db_path)
    try:
        conn.execute(
            "UPDATE workers SET proxy_id = ?, status = ? WHERE browser_id = ?",
            (proxy_id, status, browser_id),
        )
        conn.commit()
    finally:
        conn.close()


class TestProxyListContract:
    def test_requires_token(self, server):
        status, body, _ = call(server, "/control/proxies", token=None)

        assert status == 401
        assert body["error"]["code"] == "unauthorized"

    def test_returns_every_contract_field(self, server, pool, db_path):
        added = add_remote(pool, "127.0.0.1", closed_port())
        assign_worker(db_path, added["id"], "br-3")

        status, body, _ = call(server, "/control/proxies")

        assert status == 200
        assert set(body) == {"proxies"}
        assert set(body["proxies"][0]) == {
            "id",
            "label",
            "scheme",
            "host",
            "port",
            "username",
            "password",
            "country",
            "latency_ms",
            "is_alive",
            "fail_count",
            "last_checked_at",
            "last_error",
            "assigned_browser_id",
            "usage_count",
        }

    def test_reports_assignment_and_usage_count(self, server, pool, db_path):
        added = add_remote(pool, "127.0.0.1", closed_port())
        assign_worker(db_path, added["id"], "br-3")
        pool.record_usage(added["id"], browser_id="br-3", result="ok", latency_ms=5)

        _, body, _ = call(server, "/control/proxies")

        assert body["proxies"][0]["assigned_browser_id"] == "br-3"
        assert body["proxies"][0]["usage_count"] == 1

    def test_raw_response_contains_no_credentials(self, server, pool):
        add_remote(pool, "127.0.0.1", closed_port(), username="alice", password="s3cr3t")

        _, _, raw = call(server, "/control/proxies")

        assert "s3cr3t" not in raw
        assert "alice" not in raw
        assert config_module.SECRET_MASK in raw

    def test_empty_credentials_stay_empty_in_response(self, server, pool):
        pool.add_lines(["127.0.0.1:8080"])

        _, body, _ = call(server, "/control/proxies")

        assert not body["proxies"][0]["username"]
        assert not body["proxies"][0]["password"]

    def test_empty_list_is_valid(self, server):
        status, body, _ = call(server, "/control/proxies")

        assert status == 200
        assert body == {"proxies": []}

    def test_put_is_not_allowed(self, server):
        """Маршрут списка знает только GET и POST — остальное 405 с Allow."""
        status, body, _ = call(server, "/control/proxies", method="PUT", body={})

        assert status == 405
        assert body["error"]["code"] == "method_not_allowed"


class TestProxyAdd:
    def test_adds_lines(self, server, db_path):
        status, body, _ = call(
            server,
            "/control/proxies",
            method="POST",
            body={"lines": ["alice:s3cr3t@10.0.0.1:8080", "10.0.0.2:9090"]},
        )

        assert status == 200
        assert body == {"added": 2, "skipped": 0, "problems": []}
        assert len(pool_rows(db_path)) == 2

    def test_skips_bad_lines_and_duplicates_with_problems(self, server, pool):
        add_remote(pool, "10.0.0.1", 8080)

        status, body, _ = call(
            server,
            "/control/proxies",
            method="POST",
            body={
                "lines": [
                    "alice:s3cr3t@10.0.0.1:8080",
                    "не прокси",
                    "bob:s3cr3t@10.0.0.3:8080",
                ]
            },
        )

        assert status == 200
        assert body["added"] == 1
        assert body["skipped"] == 2
        assert [problem["line_index"] for problem in body["problems"]] == [0, 1]
        assert all(problem["message"] for problem in body["problems"])

    def test_response_never_echoes_credentials(self, server):
        _, body, raw = call(
            server,
            "/control/proxies",
            method="POST",
            body={"lines": ["alice:SUPER-SECRET@"]},
        )

        assert body["problems"]
        assert "SUPER-SECRET" not in raw
        assert "alice" not in raw

    def test_missing_lines_field_is_invalid_request(self, server):
        status, body, _ = call(server, "/control/proxies", method="POST", body={})

        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    def test_lines_must_be_a_list(self, server):
        status, body, _ = call(
            server, "/control/proxies", method="POST", body={"lines": "10.0.0.1:8080"}
        )

        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    def test_lines_must_contain_strings(self, server):
        status, body, _ = call(
            server, "/control/proxies", method="POST", body={"lines": [None, 5]}
        )

        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    def test_malformed_json_is_invalid_json(self, server):
        url = f"http://{server.host}:{server.port}/control/proxies"
        request = urllib.request.Request(url, data=b"{nope", method="POST")
        request.add_header(TOKEN_HEADER, TOKEN)
        request.add_header("Content-Type", "application/json")

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=10)

        assert excinfo.value.code == 400
        assert json.loads(excinfo.value.read().decode())["error"]["code"] == "invalid_json"

    def test_requires_token(self, server):
        status, body, _ = call(
            server,
            "/control/proxies",
            method="POST",
            token=None,
            body={"lines": ["10.0.0.1:8080"]},
        )

        assert status == 401
        assert body["error"]["code"] == "unauthorized"


class TestProxyImport:
    @pytest.fixture
    def proxy_file(self, tmp_path):
        path = tmp_path / "proxies.txt"
        path.write_text(
            "alice:s3cr3t@10.0.0.1:8080\n10.0.0.2:9090\nкривая\n", encoding="utf-8"
        )
        return path

    @pytest.fixture
    def import_server(self, make_server, proxy_file):
        config = Config.from_dict(config_module.default_config()).patch(
            {"paths": {"proxy_file": str(proxy_file)}}
        )
        return make_server(config_instance=config)

    def test_imports_from_configured_file(self, import_server, db_path):
        status, body, _ = call(import_server, "/control/proxies/import", method="POST", body={})

        assert status == 200
        assert body["added"] == 2
        assert body["skipped"] == 1
        assert [problem["line_index"] for problem in body["problems"]] == [2]
        assert all(problem["message"] for problem in body["problems"])
        assert len(pool_rows(db_path)) == 2

    def test_request_path_is_ignored(self, import_server, proxy_file, tmp_path, db_path):
        """Path traversal закрыт: путь из тела запроса не читается вовсе."""
        other = tmp_path / "other.txt"
        other.write_text("bob:s3cr3t@10.9.9.9:9999\n", encoding="utf-8")

        status, body, _ = call(
            import_server, "/control/proxies/import", method="POST", body={"path": str(other)}
        )

        assert status == 200
        assert body["added"] == 2
        hosts = {row["host"] for row in pool_rows(db_path)}
        assert "10.9.9.9" not in hosts
        assert "10.0.0.1" in hosts

    def test_without_configured_file_is_400(self, server, tmp_path):
        status, body, _ = call(server, "/control/proxies/import", method="POST", body={})

        assert status == 400
        assert body["error"]["code"] == "proxy_import_failed"

    def test_missing_file_is_400(self, make_server, tmp_path):
        config = Config.from_dict(config_module.default_config()).patch(
            {"paths": {"proxy_file": str(tmp_path / "nope.txt")}}
        )
        instance = make_server(config_instance=config)

        status, body, _ = call(instance, "/control/proxies/import", method="POST", body={})

        assert status == 400
        assert body["error"]["code"] == "proxy_import_failed"

    def test_import_requires_token(self, import_server):
        status, _, _ = call(
            import_server, "/control/proxies/import", method="POST", token=None, body={}
        )

        assert status == 401


class TestProxyDelete:
    def test_deletes_proxy(self, server, pool, db_path):
        added = add_remote(pool, "10.0.0.1", 8080)

        status, body, _ = call(
            server, "/control/proxies/delete", method="POST", body={"id": added["id"]}
        )

        assert status == 200
        assert body == {"deleted": True}
        assert pool_rows(db_path) == []

    def test_in_use_returns_409(self, server, pool, db_path):
        added = add_remote(pool, "10.0.0.1", 8080)
        assign_worker(db_path, added["id"], "br-1", status="running")

        status, body, _ = call(
            server, "/control/proxies/delete", method="POST", body={"id": added["id"]}
        )

        assert status == 409
        assert body["error"]["code"] == "proxy_in_use"
        assert "s3cr3t" not in json.dumps(body)
        assert len(pool_rows(db_path)) == 1

    def test_stopped_worker_does_not_block_delete(self, server, pool, db_path):
        added = add_remote(pool, "10.0.0.1", 8080)
        assign_worker(db_path, added["id"], "br-1", status="stopped", pid=None)

        status, body, _ = call(
            server, "/control/proxies/delete", method="POST", body={"id": added["id"]}
        )

        assert status == 200
        assert body == {"deleted": True}

    def test_unknown_id_is_404(self, server):
        status, body, _ = call(
            server, "/control/proxies/delete", method="POST", body={"id": 424242}
        )

        assert status == 404
        assert body["error"]["code"] == "proxy_not_found"

    @pytest.mark.parametrize("value", ["5", None, True, 1.5, []])
    def test_non_integer_id_is_invalid_request(self, server, value):
        status, body, _ = call(
            server, "/control/proxies/delete", method="POST", body={"id": value}
        )

        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    def test_requires_token(self, server, pool):
        added = add_remote(pool, "10.0.0.1", 8080)

        status, _, _ = call(
            server,
            "/control/proxies/delete",
            method="POST",
            token=None,
            body={"id": added["id"]},
        )

        assert status == 401

    def test_get_on_delete_path_is_405(self, server):
        status, body, _ = call(server, "/control/proxies/delete")

        assert status == 405
        assert body["error"]["code"] == "method_not_allowed"


class TestProxyCheck:
    @pytest.fixture
    def servers(self):
        created = []

        def make(**kwargs):
            stub = LoopbackProxy(**kwargs)
            created.append(stub)
            return stub

        yield make
        for stub in created:
            stub.close()

    def test_starts_background_check(self, server, checker, pool, db_path, servers):
        stub = servers()
        add_remote(pool, "127.0.0.1", stub.port)

        status, body, _ = call(server, "/control/proxies/check", method="POST", body={})

        assert status == 200
        assert body == {"started": True}
        checker.join(timeout=10)
        assert checker.last_summary == {"checked": 1, "alive": 1, "dead": 0}
        _, listed, _ = call(server, "/control/proxies")
        assert listed["proxies"][0]["is_alive"] == 1
        assert listed["proxies"][0]["last_checked_at"]

    def test_second_start_returns_409(self, server, checker, pool, servers):
        stub = servers(silent=True)
        add_remote(pool, "127.0.0.1", stub.port)

        first, _, _ = call(server, "/control/proxies/check", method="POST", body={})
        second, body, _ = call(server, "/control/proxies/check", method="POST", body={})

        try:
            assert first == 200
            assert second == 409
            assert body["error"]["code"] == "check_in_progress"
        finally:
            checker.join(timeout=10)

    def test_check_requires_token(self, server):
        status, _, _ = call(server, "/control/proxies/check", method="POST", token=None, body={})

        assert status == 401

    def test_get_on_check_path_is_405(self, server):
        status, _, _ = call(server, "/control/proxies/check")

        assert status == 405


class TestProxyFilePath:
    """GET /control/proxies/file: путь для «открыть proxies.txt» из UI.

    UI не знает каталога демона, поэтому путь отдаётся уже абсолютным —
    ровно тем, от которого читает ``import_file``. Запрос не может указать
    другой файл: значения в теле нет вообще.
    """

    @staticmethod
    def _server_with_file(make_server, path: str):
        config = Config.from_dict(config_module.default_config()).patch(
            {"paths": {"proxy_file": path}}
        )
        return make_server(config_instance=config)

    def test_absolute_path_is_returned_untouched(self, make_server, tmp_path):
        target = tmp_path / "proxies.txt"
        target.write_text("10.0.0.1:8080\n", encoding="utf-8")
        instance = self._server_with_file(make_server, str(target))

        status, body, _ = call(instance, "/control/proxies/file")

        assert status == 200
        assert body == {"path": str(target), "exists": True}
        assert Path(body["path"]).is_absolute()

    def test_relative_path_resolves_from_process_cwd(self, make_server, tmp_path, monkeypatch):
        (tmp_path / "proxies.txt").write_text("10.0.0.1:8080\n", encoding="utf-8")
        instance = self._server_with_file(make_server, "proxies.txt")
        monkeypatch.chdir(tmp_path)

        status, body, _ = call(instance, "/control/proxies/file")

        assert status == 200
        assert Path(body["path"]).resolve() == (tmp_path / "proxies.txt").resolve()
        assert body["exists"] is True

    def test_missing_file_is_200_with_exists_false(self, make_server, tmp_path):
        instance = self._server_with_file(make_server, str(tmp_path / "nope.txt"))

        status, body, _ = call(instance, "/control/proxies/file")

        assert status == 200, "отсутствие файла — не ошибка запроса, а условие"
        assert body == {"path": str(tmp_path / "nope.txt"), "exists": False}

    def test_without_configured_file_is_400(self, server):
        status, body, _ = call(server, "/control/proxies/file")

        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    def test_file_requires_token(self, server):
        status, _, _ = call(server, "/control/proxies/file", token=None)

        assert status == 401

    def test_post_on_file_path_is_405(self, server):
        status, _, _ = call(server, "/control/proxies/file", method="POST", body={})

        assert status == 405

    def test_request_body_cannot_point_at_another_file(self, make_server, tmp_path):
        """Path traversal закрыт: путь в теле запроса не читается вовсе."""
        other = tmp_path / "other.txt"
        other.write_text("bob:s3cr3t@10.9.9.9:9999\n", encoding="utf-8")
        configured = tmp_path / "proxies.txt"
        configured.write_text("10.0.0.1:8080\n", encoding="utf-8")
        instance = self._server_with_file(make_server, str(configured))

        status, body, _ = call(
            instance, "/control/proxies/file", method="GET", body={"path": str(other)}
        )

        assert status == 200
        assert body["path"] == str(configured)


def pool_rows(db_path):
    from engine.db import migrations

    conn = migrations.connect(db_path)
    try:
        return [
            dict(row) for row in conn.execute("SELECT * FROM proxies ORDER BY id").fetchall()
        ]
    finally:
        conn.close()
