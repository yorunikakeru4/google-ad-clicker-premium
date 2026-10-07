"""Тесты HTTP-эндпоинтов профилей: контракт, 400/404/409, массовые операции.

Сервер настоящий (loopback, свободный порт), как в ``test_api_proxies.py``.
Здесь проверяется то, что нельзя сломать без поломки UI:

- семь маршрутов ``/control/profiles*`` с форматами ответов из контракта;
- тела запросов: валидация полей до записи, кривые значения — 400;
- удаление в трёх режимах (``{"id"}`` / ``{"ids"}`` / ``{"all"}``) и импорт
  строк либо файла из ``paths.user_agents``;
- ``409 profile_in_use`` — живой воркер не даёт удалить профиль;
- массовые операции (диапазон, сброс) возвращают счётчики, а не список.
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
from engine.profile_pool import ProfilePool
from engine.proxy_pool import ProxyPool
from tests.engine.control_plane.test_supervisor import FakeClock, FakeProcessRegistry

TOKEN = "profile-pool-test-token"


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
def profiles(db_path):
    return ProfilePool(db_path)


@pytest.fixture
def proxies(db_path):
    return ProxyPool(db_path)


@pytest.fixture
def make_server(supervisor, tmp_path, profiles, proxies):
    """Фабрика серверов: нужна, когда пул или конфиг меняются под тест."""
    servers = []

    def factory(config_instance=None, profile_pool=None, proxy_pool=None):
        instance = ControlPlaneServer(
            supervisor=supervisor,
            config=config_instance or Config.from_dict(config_module.default_config()),
            token=TOKEN,
            config_path=tmp_path / "config.json",
            host="127.0.0.1",
            port=0,
            profile_pool=profile_pool,
            proxy_pool=proxy_pool or ProxyPool(supervisor.store.db_path),
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
    """HTTP-запрос. Возвращает (status, json, raw_text)."""
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


def profile_rows(db_path):
    from engine.db import migrations

    conn = migrations.connect(db_path)
    try:
        return [
            dict(row) for row in conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()
        ]
    finally:
        conn.close()


def hold_profile(db_path, profile_id, browser_id, status="running", pid=99):
    """Воркер, живо держащий профиль: та же строка, что пишет супервизор."""
    store = StateStore(db_path)
    store.register_worker(browser_id, pid)
    store.assign_profile(browser_id, profile_id)
    from engine.db import migrations

    conn = migrations.connect(db_path)
    try:
        conn.execute(
            "UPDATE workers SET status = ? WHERE browser_id = ?", (status, browser_id)
        )
        conn.commit()
    finally:
        conn.close()


class TestProfileListContract:
    def test_requires_token(self, server):
        status, body, _ = call(server, "/control/profiles", token=None)

        assert status == 401
        assert body["error"]["code"] == "unauthorized"

    def test_returns_every_contract_field(self, server, profiles, db_path, proxies):
        proxies.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        proxy_id = proxies.list_proxies()[0]["id"]
        profiles.add_profiles(
            [
                {
                    "name": "alice",
                    "key_ref": "hooks:alice",
                    "proxy_id": proxy_id,
                    "user_agent": "UA/1.0",
                    "locale": "en-US",
                    "timezone": "UTC",
                    "fields": {"cookie_set": "alice.txt"},
                }
            ]
        )
        hold_profile(db_path, 1, "br-3")

        status, body, _ = call(server, "/control/profiles")

        assert status == 200
        assert set(body) == {"profiles"}
        assert set(body["profiles"][0]) == {
            "id",
            "name",
            "key_ref",
            "proxy_id",
            "user_agent",
            "locale",
            "timezone",
            "status",
            "last_used_at",
            "fields",
            "assigned_browser_id",
        }
        assert body["profiles"][0]["fields"] == {"cookie_set": "alice.txt"}
        assert body["profiles"][0]["assigned_browser_id"] == "br-3"

    def test_stopped_worker_does_not_show_as_assignment(self, server, profiles, db_path):
        profiles.add_profiles([{"name": "alice"}])
        hold_profile(db_path, 1, "br-3", status="stopped", pid=None)

        _, body, _ = call(server, "/control/profiles")

        assert body["profiles"][0]["assigned_browser_id"] is None

    def test_empty_list_is_valid(self, server):
        status, body, _ = call(server, "/control/profiles")

        assert status == 200
        assert body == {"profiles": []}

    def test_put_is_not_allowed(self, server):
        status, body, _ = call(server, "/control/profiles", method="PUT", body={})

        assert status == 405
        assert body["error"]["code"] == "method_not_allowed"


class TestProfileAdd:
    def test_adds_profiles(self, server, db_path):
        status, body, _ = call(
            server,
            "/control/profiles",
            method="POST",
            body={
                "profiles": [
                    {"name": "alice", "key_ref": "hooks:alice"},
                    {"name": "bob", "locale": "en-US"},
                ]
            },
        )

        assert status == 200
        assert body == {"added": 2, "skipped": 0, "problems": []}
        rows = profile_rows(db_path)
        assert [row["name"] for row in rows] == ["alice", "bob"]
        assert all(row["status"] == "free" for row in rows), (
            "новый профиль обязан начинаться со статуса free, даже в старой БД"
        )

    def test_skips_duplicates_and_bad_records_with_problems(self, server, profiles):
        profiles.add_profiles([{"name": "alice"}])

        status, body, _ = call(
            server,
            "/control/profiles",
            method="POST",
            body={
                "profiles": [
                    {"name": "alice"},
                    {"name": ""},
                    {"name": "bob", "fields": []},
                    {"name": "carol"},
                ]
            },
        )

        assert status == 200
        assert body["added"] == 1, "carol проходит, остальные три — нет"
        assert body["skipped"] == 3
        assert [problem["index"] for problem in body["problems"]] == [0, 1, 2]
        assert all(problem["message"] for problem in body["problems"])

    def test_missing_profiles_field_is_invalid_request(self, server):
        status, body, _ = call(server, "/control/profiles", method="POST", body={})

        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    def test_profiles_must_be_a_list(self, server):
        status, body, _ = call(
            server, "/control/profiles", method="POST", body={"profiles": "alice"}
        )

        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    def test_profiles_must_contain_objects(self, server):
        status, body, _ = call(
            server, "/control/profiles", method="POST", body={"profiles": ["alice", 5]}
        )

        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    def test_requires_token(self, server):
        status, _, _ = call(
            server,
            "/control/profiles",
            method="POST",
            token=None,
            body={"profiles": [{"name": "alice"}]},
        )

        assert status == 401

    def test_get_on_profiles_path_is_405(self, server):
        """GET обслуживается, POST обслуживается — остальное 405, а не 404."""
        status, body, _ = call(server, "/control/profiles", method="DELETE", body={})

        assert status == 405
        assert body["error"]["code"] == "method_not_allowed"


class TestProfileImport:
    def test_single_token_lines_generate_names(self, server, db_path):
        status, body, _ = call(
            server,
            "/control/profiles/import",
            method="POST",
            body={"lines": ["key-001", "key-002"]},
        )

        assert status == 200
        assert body == {"added": 2, "skipped": 0, "problems": []}
        rows = profile_rows(db_path)
        assert [(row["name"], row["key_ref"]) for row in rows] == [
            ("key-001", "key-001"),
            ("key-002", "key-002"),
        ]

    def test_explicit_names_and_duplicates(self, server, db_path):
        status, body, _ = call(
            server,
            "/control/profiles/import",
            method="POST",
            body={"lines": ["key-001 | alice", "key-001", "| nope", "key-002 | alice"]},
        )

        assert status == 200
        assert body["added"] == 1
        assert body["skipped"] == 3
        assert [problem["line_index"] for problem in body["problems"]] == [1, 2, 3]
        assert all(problem["message"] for problem in body["problems"])
        assert [row["name"] for row in profile_rows(db_path)] == ["alice"]

    def test_three_hundred_lines_in_one_request(self, server, db_path):
        lines = [f"key-{index:04d}" for index in range(300)]

        status, body, _ = call(
            server, "/control/profiles/import", method="POST", body={"lines": lines}
        )

        assert status == 200
        assert body == {"added": 300, "skipped": 0, "problems": []}
        assert len(profile_rows(db_path)) == 300

    def test_missing_lines_field_is_invalid_request(self, server):
        status, body, _ = call(server, "/control/profiles/import", method="POST", body={})

        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    def test_lines_must_contain_strings(self, server):
        status, body, _ = call(
            server, "/control/profiles/import", method="POST", body={"lines": [None, 5]}
        )

        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    def test_requires_token(self, server):
        status, _, _ = call(
            server,
            "/control/profiles/import",
            method="POST",
            token=None,
            body={"lines": ["key-001"]},
        )

        assert status == 401

    @pytest.mark.parametrize("body", [{}, {"file": False}, {"foo": 1}, ["lines"], "мусор"])
    def test_body_without_lines_or_file_true_is_invalid_request(self, server, body):
        """Ни lines, ни file: true — не импорт, а ошибка тела запроса."""
        status, payload, _ = call(server, "/control/profiles/import", method="POST", body=body)

        assert status == 400
        assert payload["error"]["code"] == "invalid_request"

    def test_ua_lines_are_imported_through_the_same_route(self, server, db_path):
        status, body, _ = call(
            server,
            "/control/profiles/import",
            method="POST",
            body={"lines": ["Mozilla/5.0 (Windows NT 10.0) Chrome/120.0", "key-001"]},
        )

        assert status == 200
        assert body == {"added": 2, "skipped": 0, "problems": []}
        rows = profile_rows(db_path)
        assert [row["name"] for row in rows] == ["UA-1", "key-001"]
        assert rows[0]["key_ref"] is None
        assert rows[0]["user_agent"] == "Mozilla/5.0 (Windows NT 10.0) Chrome/120.0"


class TestProfileImportFile:
    """``{"file": true}`` — путь только из конфига, как у прокси."""

    @pytest.fixture
    def user_agents_file(self, tmp_path):
        path = tmp_path / "user_agents.txt"
        path.write_text(
            "Mozilla/5.0 (Windows NT 10.0) Chrome/120.0\n"
            "\n"
            "key-001 | Имя\n"
            "| nope\n"
            "Mozilla/5.0 (Windows NT 10.0) Chrome/120.0\n",
            encoding="utf-8",
        )
        return path

    @pytest.fixture
    def import_server(self, make_server, user_agents_file):
        config = Config.from_dict(config_module.default_config()).patch(
            {"paths": {"user_agents": str(user_agents_file)}}
        )
        return make_server(config_instance=config)

    def test_imports_from_configured_file(self, import_server, db_path):
        status, body, _ = call(
            import_server, "/control/profiles/import", method="POST", body={"file": True}
        )

        assert status == 200
        assert body["added"] == 2
        assert body["skipped"] == 2
        assert [problem["line_index"] for problem in body["problems"]] == [3, 4]
        assert all(problem["message"] for problem in body["problems"])
        assert [row["name"] for row in profile_rows(db_path)] == ["UA-1", "Имя"]

    def test_request_path_is_ignored(self, import_server, tmp_path, db_path):
        """Path traversal закрыт: путь из тела запроса не читается вовсе."""
        other = tmp_path / "other.txt"
        other.write_text("Mozilla/5.0 (чужой) Chrome/99.0\n", encoding="utf-8")

        status, body, _ = call(
            import_server,
            "/control/profiles/import",
            method="POST",
            body={"file": True, "path": str(other)},
        )

        assert status == 200
        assert body["added"] == 2
        names = {row["name"] for row in profile_rows(db_path)}
        assert "UA-2" not in names, "UA-2 появился бы только из чужого файла"

    def test_without_configured_file_is_400(self, make_server):
        config = Config.from_dict(config_module.default_config()).patch(
            {"paths": {"user_agents": ""}}
        )
        instance = make_server(config_instance=config)

        status, body, _ = call(
            instance, "/control/profiles/import", method="POST", body={"file": True}
        )

        assert status == 400
        assert body["error"]["code"] == "profile_import_failed"

    def test_missing_file_is_400(self, make_server, tmp_path):
        config = Config.from_dict(config_module.default_config()).patch(
            {"paths": {"user_agents": str(tmp_path / "nope.txt")}}
        )
        instance = make_server(config_instance=config)

        status, body, _ = call(
            instance, "/control/profiles/import", method="POST", body={"file": True}
        )

        assert status == 400
        assert body["error"]["code"] == "profile_import_failed"

    def test_import_requires_token(self, import_server):
        status, _, _ = call(
            import_server, "/control/profiles/import", method="POST", token=None, body={"file": True}
        )

        assert status == 401


class TestProfileDelete:
    def test_deletes_a_profile(self, server, profiles, db_path):
        status, body, _ = call(
            server, "/control/profiles/delete", method="POST", body={"id": 1}
        )

        assert status == 404, "удалять нечего — строка не создавалась"

        profiles.add_profiles([{"name": "alice"}])
        status, body, _ = call(
            server, "/control/profiles/delete", method="POST", body={"id": 1}
        )

        assert status == 200
        assert body == {"deleted": 1}, "счётчик, а не булево: UI ждёт число"
        assert profile_rows(db_path) == []

    def test_in_use_returns_409(self, server, profiles, db_path):
        profiles.add_profiles([{"name": "alice"}])
        hold_profile(db_path, 1, "br-1", status="running")

        status, body, _ = call(
            server, "/control/profiles/delete", method="POST", body={"id": 1}
        )

        assert status == 409
        assert body["error"]["code"] == "profile_in_use"
        assert "br-1" in body["error"]["message"]
        assert len(profile_rows(db_path)) == 1

    def test_stopped_worker_does_not_block_delete(self, server, profiles, db_path):
        profiles.add_profiles([{"name": "alice"}])
        hold_profile(db_path, 1, "br-1", status="stopped", pid=None)

        status, body, _ = call(
            server, "/control/profiles/delete", method="POST", body={"id": 1}
        )

        assert status == 200
        assert body == {"deleted": 1}

    def test_unknown_id_is_404(self, server):
        status, body, _ = call(
            server, "/control/profiles/delete", method="POST", body={"id": 424242}
        )

        assert status == 404
        assert body["error"]["code"] == "profile_not_found"

    @pytest.mark.parametrize("value", ["5", None, True, 1.5, []])
    def test_non_integer_id_is_invalid_request(self, server, value):
        status, body, _ = call(
            server, "/control/profiles/delete", method="POST", body={"id": value}
        )

        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    def test_requires_token(self, server):
        status, _, _ = call(
            server, "/control/profiles/delete", method="POST", token=None, body={"id": 1}
        )

        assert status == 401

    def test_get_on_delete_path_is_405(self, server):
        status, body, _ = call(server, "/control/profiles/delete")

        assert status == 405
        assert body["error"]["code"] == "method_not_allowed"


class TestProfileDeleteMany:
    """Батч ``{"ids": [...]}`` — best-effort: занятые не роняют операцию.

    Контракт ответа тот же, что у прокси: ``deleted`` / ``skipped`` /
    ``problems`` — оператор видит и результат, и причину, почему профиль
    остался в списке.
    """

    def test_deletes_every_listed_profile(self, server, profiles, db_path):
        profiles.add_profiles([{"name": "p1"}, {"name": "p2"}, {"name": "p3"}])
        ids = [row["id"] for row in profiles.list_profiles()]

        status, body, _ = call(
            server, "/control/profiles/delete", method="POST", body={"ids": ids}
        )

        assert status == 200
        assert body == {"deleted": 3, "skipped": 0, "problems": []}
        assert profile_rows(db_path) == []

    def test_in_use_and_missing_are_skipped_but_others_go(self, server, profiles, db_path):
        profiles.add_profiles([{"name": "p1"}, {"name": "p2"}])
        busy_id, free_id = [row["id"] for row in profiles.list_profiles()]
        hold_profile(db_path, busy_id, "br-1", status="running")

        status, body, _ = call(
            server,
            "/control/profiles/delete",
            method="POST",
            body={"ids": [busy_id, free_id, 424242]},
        )

        assert status == 200, "занятая строка не должна ронять весь батч"
        assert body["deleted"] == 1
        assert body["skipped"] == 2
        assert f"id={busy_id}: назначен воркеру br-1" in body["problems"]
        assert "id=424242: профиль не найден" in body["problems"]
        assert [row["id"] for row in profile_rows(db_path)] == [busy_id]

    def test_duplicate_ids_delete_once_without_a_ghost_problem(self, server, profiles, db_path):
        profiles.add_profiles([{"name": "p1"}])

        status, body, _ = call(
            server, "/control/profiles/delete", method="POST", body={"ids": [1, 1]}
        )

        assert status == 200
        assert body == {"deleted": 1, "skipped": 0, "problems": []}
        assert profile_rows(db_path) == []

    @pytest.mark.parametrize("value", [[], "5", 5, None, [True], ["5"], {"id": 1}])
    def test_malformed_ids_is_invalid_request(self, server, value):
        status, body, _ = call(
            server, "/control/profiles/delete", method="POST", body={"ids": value}
        )

        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    def test_body_without_id_ids_or_all_is_invalid_request(self, server):
        status, body, _ = call(
            server, "/control/profiles/delete", method="POST", body={"foo": 1}
        )

        assert status == 400
        assert body["error"]["code"] == "invalid_request"


class TestProfileDeleteAll:
    """``{"all": true}`` — весь пул одним запросом, best-effort с отчётом."""

    @staticmethod
    def _add(profiles, count):
        profiles.add_profiles([{"name": f"p{index}"} for index in range(count)])
        return [row["id"] for row in profiles.list_profiles()]

    def test_all_deletes_every_profile(self, server, profiles, db_path):
        self._add(profiles, 3)

        status, body, _ = call(
            server, "/control/profiles/delete", method="POST", body={"all": True}
        )

        assert status == 200
        assert body == {"deleted": 3, "skipped": 0, "problems": []}
        assert profile_rows(db_path) == []

    def test_all_keeps_assigned_profile_and_reports_it(self, server, profiles, db_path):
        busy_id, free_id = self._add(profiles, 2)
        hold_profile(db_path, busy_id, "br-1", status="running")

        status, body, _ = call(
            server, "/control/profiles/delete", method="POST", body={"all": True}
        )

        assert status == 200, "назначенный профиль не должен ронять запрос"
        assert body["deleted"] == 1
        assert body["skipped"] == 1
        assert f"id={busy_id}: назначен воркеру br-1" in body["problems"]
        assert [row["id"] for row in profile_rows(db_path)] == [busy_id]

    def test_all_on_empty_pool_is_a_noop(self, server):
        status, body, _ = call(
            server, "/control/profiles/delete", method="POST", body={"all": True}
        )

        assert status == 200
        assert body == {"deleted": 0, "skipped": 0, "problems": []}

    @pytest.mark.parametrize("value", [False, "yes", 1, None])
    def test_all_must_be_true(self, server, value):
        """Непустое, но не True — не «удалить всё», а ошибка тела."""
        status, body, _ = call(
            server, "/control/profiles/delete", method="POST", body={"all": value}
        )

        assert status == 400
        assert body["error"]["code"] == "invalid_request"


class TestProfileAssign:
    def test_assigns_the_range_to_free_workers(self, server, profiles, db_path):
        profiles.add_profiles([{"name": "p1"}, {"name": "p2"}, {"name": "p3"}])
        for browser_id in ("br-1", "br-2"):
            StateStore(db_path).register_worker(browser_id, 100)

        status, body, _ = call(
            server,
            "/control/profiles/assign",
            method="POST",
            body={"start_id": 1, "end_id": 3},
        )

        assert status == 200
        assert body == {"assigned": 2, "available": 1}
        rows = profile_rows(db_path)
        assert [row["status"] for row in rows] == ["assigned", "assigned", "free"]
        with StateStore(db_path)._connect() as conn:
            holders = dict(
                (row["browser_id"], row["profile_id"])
                for row in conn.execute("SELECT browser_id, profile_id FROM workers")
            )
        assert holders == {"br-1": 1, "br-2": 2}, "порядок пар детерминирован"

    def test_range_leaves_profiles_outside_it_untouched(self, server, profiles, db_path):
        profiles.add_profiles([{"name": "p1"}, {"name": "p2"}, {"name": "p3"}])
        StateStore(db_path).register_worker("br-1", 100)

        _, body, _ = call(
            server,
            "/control/profiles/assign",
            method="POST",
            body={"start_id": 3, "end_id": 3},
        )

        assert body == {"assigned": 1, "available": 0}
        assert [row["status"] for row in profile_rows(db_path)] == [
            "free",
            "free",
            "assigned",
        ]

    def test_reversed_range_is_400(self, server, profiles):
        profiles.add_profiles([{"name": "p1"}, {"name": "p2"}])

        status, body, _ = call(
            server,
            "/control/profiles/assign",
            method="POST",
            body={"start_id": 2, "end_id": 1},
        )

        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    @pytest.mark.parametrize(
        "body",
        [
            {"start_id": "1", "end_id": 3},
            {"start_id": 0, "end_id": 3},
            {"start_id": None, "end_id": 3},
            {"start_id": 1},
            {"end_id": 3},
        ],
    )
    def test_invalid_bounds_are_400(self, server, body):
        status, payload, _ = call(
            server, "/control/profiles/assign", method="POST", body=body
        )

        assert status == 400
        assert payload["error"]["code"] == "invalid_request"

    def test_requires_token(self, server):
        status, _, _ = call(
            server,
            "/control/profiles/assign",
            method="POST",
            token=None,
            body={"start_id": 1, "end_id": 2},
        )

        assert status == 401


class TestProfileUnassign:
    def test_releases_every_profile_and_pointer(self, server, profiles, db_path):
        profiles.add_profiles([{"name": "p1"}, {"name": "p2"}, {"name": "p3"}])
        store = StateStore(db_path)
        store.register_worker("br-1", 100)
        store.register_worker("br-2", 101)
        profiles.assign_range(1, 3)
        profiles.set_status(3, "blocked")

        status, body, _ = call(server, "/control/profiles/unassign", method="POST", body={})

        assert status == 200
        assert body == {"released": 3}
        assert [row["status"] for row in profile_rows(db_path)] == ["free", "free", "free"]
        with store._connect() as conn:
            pointers = [
                row["profile_id"]
                for row in conn.execute("SELECT profile_id FROM workers")
            ]
        assert pointers == [None, None]

    def test_repeated_unassign_releases_nothing(self, server, profiles, db_path):
        profiles.add_profiles([{"name": "p1"}])
        StateStore(db_path).register_worker("br-1", 100)
        profiles.assign_range(1, 1)

        _, first, _ = call(server, "/control/profiles/unassign", method="POST", body={})
        _, second, _ = call(server, "/control/profiles/unassign", method="POST", body={})

        assert first == {"released": 1}
        assert second == {"released": 0}, "повторный сброс не освобождает ничего"

    def test_requires_token(self, server):
        status, _, _ = call(
            server, "/control/profiles/unassign", method="POST", token=None, body={}
        )

        assert status == 401

    def test_get_on_unassign_path_is_405(self, server):
        status, body, _ = call(server, "/control/profiles/unassign")

        assert status == 405
        assert body["error"]["code"] == "method_not_allowed"


class TestProfileStatus:
    def test_sets_an_operator_status(self, server, profiles, db_path):
        profiles.add_profiles([{"name": "alice"}])

        status, body, _ = call(
            server,
            "/control/profiles/status",
            method="POST",
            body={"id": 1, "status": "blocked"},
        )

        assert status == 200
        assert body == {"id": 1, "status": "blocked"}
        assert profile_rows(db_path)[0]["status"] == "blocked"

    @pytest.mark.parametrize("status_value", ["assigned", "active"])
    def test_system_statuses_are_400(self, server, profiles, db_path, status_value):
        profiles.add_profiles([{"name": "alice"}])

        status, body, _ = call(
            server,
            "/control/profiles/status",
            method="POST",
            body={"id": 1, "status": status_value},
        )

        assert status == 400
        assert body["error"]["code"] == "invalid_request"
        assert profile_rows(db_path)[0]["status"] == "free"

    @pytest.mark.parametrize("status_value", ["new", "", None, 5])
    def test_unknown_status_is_400(self, server, profiles, status_value):
        profiles.add_profiles([{"name": "alice"}])

        status, _, _ = call(
            server,
            "/control/profiles/status",
            method="POST",
            body={"id": 1, "status": status_value},
        )

        assert status == 400

    def test_unknown_id_is_404(self, server):
        status, body, _ = call(
            server,
            "/control/profiles/status",
            method="POST",
            body={"id": 424242, "status": "blocked"},
        )

        assert status == 404
        assert body["error"]["code"] == "profile_not_found"

    @pytest.mark.parametrize("value", ["1", None, True, 1.5])
    def test_non_integer_id_is_400(self, server, value):
        status, _, _ = call(
            server,
            "/control/profiles/status",
            method="POST",
            body={"id": value, "status": "blocked"},
        )

        assert status == 400

    def test_missing_status_field_is_400(self, server, profiles):
        profiles.add_profiles([{"name": "alice"}])

        status, _, _ = call(
            server, "/control/profiles/status", method="POST", body={"id": 1}
        )

        assert status == 400

    def test_requires_token(self, server):
        status, _, _ = call(
            server,
            "/control/profiles/status",
            method="POST",
            token=None,
            body={"id": 1, "status": "blocked"},
        )

        assert status == 401
