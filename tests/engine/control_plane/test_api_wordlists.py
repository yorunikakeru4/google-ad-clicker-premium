"""Тесты HTTP-эндпоинтов списков: запросы (Key Words) и домены.

Сервер настоящий (loopback, свободный порт), как в ``test_api.py``. Здесь
проверяется контракт, по которому строятся два новых экрана UI (план §5,
фаза 13):

- форматы ответов восьми маршрутов ``/control/queries*`` и ``/control/domains*``;
- список живёт в файле из конфига, а не в БД: add/delete действительно
  меняют файл, который читает движок;
- пустой ``paths.query_file`` + заданный ``behavior.query`` → источник
  переключается на файл, а одиночный запрос первым строкой переезжает в него;
- ``behavior.own_domain``/``behavior.excludes`` отдаются списком доменов для
  карточки настроек, но правятся только общим POST /control/config.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from engine.control_plane import config as config_module
from engine.control_plane.api import TOKEN_HEADER, ControlPlaneServer
from engine.control_plane.config import Config
from engine.control_plane.state import StateStore
from engine.control_plane.supervisor import Supervisor, SupervisorSettings
from tests.engine.control_plane.test_supervisor import FakeClock, FakeProcessRegistry

TOKEN = "wordlist-test-token"


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
def supervisor(store):
    return Supervisor(
        store=store,
        settings=SupervisorSettings(
            heartbeat_interval=5.0,
            shutdown_grace_seconds=0.2,
            restart_backoff_base=0.05,
            restart_backoff_max=0.1,
            max_restarts=2,
            restart_count_reset_after=3600.0,
            max_workers=8,
        ),
        spawn=FakeProcessRegistry(),
        clock=FakeClock(),
        browser_ids=lambda n: [f"br-{i + 1}" for i in range(n)],
    )


@pytest.fixture
def make_config(tmp_path, monkeypatch):
    """Конфиг для теста; относительные пути резолвятся в tmp_path.

    ``monkeypatch.chdir`` обязателен: относительный ``queries.txt`` из
    дефолтного конфига иначе привёл бы демон к файлу в корне репозитория.
    """

    def factory(query_file: str = "", query: str = "", domains_file: str = "domains.txt"):
        monkeypatch.chdir(tmp_path)
        raw = config_module.default_config()
        raw["paths"]["query_file"] = query_file
        raw["paths"]["filtered_domains"] = domains_file
        raw["behavior"]["query"] = query
        return Config.from_dict(raw)

    return factory


@pytest.fixture
def make_server(supervisor, tmp_path):
    servers = []

    def factory(config_instance=None):
        instance = ControlPlaneServer(
            supervisor=supervisor,
            config=config_instance
            if config_instance is not None
            else Config.from_dict(config_module.default_config()),
            token=TOKEN,
            config_path=tmp_path / "config.json",
            host="127.0.0.1",
            port=0,
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


def call(
    server, path: str, method: str = "GET", token: str | None = TOKEN, body=None
) -> tuple[int, Any]:
    """HTTP-запрос; тело — Any: JSON-ответ демона без схемы, как в test_api."""
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


# --- запросы: список и добавление ------------------------------------------------


class TestQueriesList:
    def test_requires_token(self, server):
        status, body = call(server, "/control/queries", token=None)

        assert status == 401
        assert body["error"]["code"] == "unauthorized"

    def test_default_config_is_single_query_source(self, server, make_server, make_config):
        server = make_server(make_config(query_file="", query="wireless keyboard"))

        status, body = call(server, "/control/queries")

        assert status == 200
        assert body == {
            "queries": [],
            "source": "single",
            "query_file": "",
            "query": "wireless keyboard",
        }

    def test_file_source_returns_normalized_lines(self, server, make_server, make_config, tmp_path):
        (tmp_path / "queries.txt").write_text(
            "'wireless keyboard'\nusb hub\n\n", encoding="utf-8"
        )
        server = make_server(make_config(query_file="queries.txt"))

        status, body = call(server, "/control/queries")

        assert status == 200
        assert body["source"] == "file"
        assert body["queries"] == ["wireless keyboard", "usb hub"]

    def test_missing_file_reads_as_empty_list(self, make_server, make_config):
        server = make_server(make_config(query_file="absent.txt"))

        status, body = call(server, "/control/queries")

        assert status == 200
        assert body["queries"] == []
        assert body["source"] == "file"


class TestQueriesAdd:
    def test_adds_to_existing_file(self, make_server, make_config, tmp_path):
        (tmp_path / "queries.txt").write_text("first\n", encoding="utf-8")
        server = make_server(make_config(query_file="queries.txt"))

        status, body = call(server, "/control/queries", method="POST", body={"lines": ["second"]})

        assert status == 200
        assert body["added"] == 1
        assert body["skipped"] == 0
        assert body["switched"] is False
        assert body["source"] == "file"
        assert (tmp_path / "queries.txt").read_text(encoding="utf-8") == "first\nsecond\n"

    def test_creates_file_when_query_file_is_empty(self, make_server, make_config, tmp_path):
        server = make_server(make_config(query_file=""))

        status, body = call(server, "/control/queries", method="POST", body={"lines": ["usb hub"]})

        assert status == 200
        assert body["switched"] is True
        assert body["query_file"] == "queries.txt"
        assert (tmp_path / "queries.txt").read_text(encoding="utf-8") == "usb hub\n"
        # конфиг переключён на файл и перезаписан на диске
        saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert saved["paths"]["query_file"] == "queries.txt"
        assert saved["behavior"]["query"] == ""

    def test_single_query_moves_into_the_file(
        self, make_server, make_config, tmp_path
    ):
        """Одиночный запрос не теряется: он первым строкой переезжает в файл."""
        server = make_server(make_config(query_file="", query="wireless keyboard"))

        status, body = call(
            server, "/control/queries", method="POST", body={"lines": ["usb hub"]}
        )

        assert status == 200
        assert body["switched"] is True
        assert (tmp_path / "queries.txt").read_text(encoding="utf-8") == (
            "wireless keyboard\nusb hub\n"
        )
        saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert saved["behavior"]["query"] == ""

    def test_duplicates_are_reported_not_written(self, make_server, make_config, tmp_path):
        (tmp_path / "queries.txt").write_text("usb hub\n", encoding="utf-8")
        server = make_server(make_config(query_file="queries.txt"))

        status, body = call(
            server, "/control/queries", method="POST", body={"lines": ["USB Hub"]}
        )

        assert status == 200
        assert body["added"] == 0
        assert body["skipped"] == 1
        assert body["problems"] == ["уже есть: USB Hub"]
        assert (tmp_path / "queries.txt").read_text(encoding="utf-8") == "usb hub\n"

    def test_rejects_body_without_lines(self, server):
        status, body = call(server, "/control/queries", method="POST", body={"query": "x"})

        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    def test_rejects_non_string_lines(self, server):
        status, body = call(server, "/control/queries", method="POST", body={"lines": [42]})

        assert status == 400
        assert body["error"]["code"] == "invalid_request"


# --- запросы: удаление и путь к файлу --------------------------------------------


class TestQueriesDelete:
    def test_deletes_by_value(self, make_server, make_config, tmp_path):
        (tmp_path / "queries.txt").write_text("first\nsecond\n", encoding="utf-8")
        server = make_server(make_config(query_file="queries.txt"))

        status, body = call(
            server, "/control/queries/delete", method="POST", body={"queries": ["second"]}
        )

        assert status == 200
        assert body == {"deleted": 1, "skipped": 0, "problems": []}
        assert (tmp_path / "queries.txt").read_text(encoding="utf-8") == "first\n"

    def test_delete_all(self, make_server, make_config, tmp_path):
        (tmp_path / "queries.txt").write_text("first\nsecond\n", encoding="utf-8")
        server = make_server(make_config(query_file="queries.txt"))

        status, body = call(
            server, "/control/queries/delete", method="POST", body={"all": True}
        )

        assert status == 200
        assert body["deleted"] == 2
        assert (tmp_path / "queries.txt").read_text(encoding="utf-8") == ""

    def test_empty_targets_are_rejected(self, make_server, make_config):
        server = make_server(make_config(query_file="queries.txt"))

        status, body = call(
            server, "/control/queries/delete", method="POST", body={"queries": []}
        )

        assert status == 400
        assert body["error"]["code"] == "invalid_request"

    def test_without_query_file_is_invalid_request(self, make_server, make_config):
        server = make_server(make_config(query_file=""))

        status, body = call(
            server, "/control/queries/delete", method="POST", body={"all": True}
        )

        assert status == 400
        assert "paths.query_file" in body["error"]["message"]

    def test_missing_value_is_reported_not_fatal(self, make_server, make_config, tmp_path):
        (tmp_path / "queries.txt").write_text("first\n", encoding="utf-8")
        server = make_server(make_config(query_file="queries.txt"))

        status, body = call(
            server,
            "/control/queries/delete",
            method="POST",
            body={"queries": ["ghost"]},
        )

        assert status == 200
        assert body["deleted"] == 0
        assert body["problems"] == ["не найдено: ghost"]


class TestQueriesFile:
    def test_returns_absolute_path_and_existence(self, make_server, make_config, tmp_path):
        (tmp_path / "queries.txt").write_text("q\n", encoding="utf-8")
        server = make_server(make_config(query_file="queries.txt"))

        status, body = call(server, "/control/queries/file")

        assert status == 200
        assert body["path"] == str(tmp_path / "queries.txt")
        assert body["exists"] is True

    def test_reports_absent_file(self, make_server, make_config):
        server = make_server(make_config(query_file="absent.txt"))

        status, body = call(server, "/control/queries/file")

        assert status == 200
        assert body["exists"] is False

    def test_without_query_file_is_invalid_request(self, make_server, make_config):
        server = make_server(make_config(query_file=""))

        status, body = call(server, "/control/queries/file")

        assert status == 400
        assert "paths.query_file" in body["error"]["message"]


# --- домены: список и настройки --------------------------------------------------


class TestDomainsList:
    def test_returns_domains_and_settings(self, make_server, make_config, tmp_path):
        (tmp_path / "domains.txt").write_text("www.edelind.de\n", encoding="utf-8")
        server = make_server(make_config(domains_file="domains.txt"))

        status, body = call(server, "/control/domains")

        assert status == 200
        assert body == {
            "domains": ["edelind.de"],
            "filtered_domains": "domains.txt",
            "own_domain": "",
            "excludes": "",
        }

    def test_settings_come_from_config(self, make_server, make_config):
        config = make_config()
        config = config.patch(
            {"behavior": {"own_domain": "edelind.de", "excludes": "goldkette"}}
        )
        server = make_server(config)

        status, body = call(server, "/control/domains")

        assert status == 200
        assert body["own_domain"] == "edelind.de"
        assert body["excludes"] == "goldkette"

    def test_settings_are_changed_only_via_config_endpoint(
        self, make_server, make_config
    ):
        """Список доменов не умеет писать в behavior: это делает POST /control/config."""
        server = make_server(make_config())

        status, _ = call(
            server, "/control/domains", method="POST", body={"lines": ["a.de"]}
        )

        assert status == 200
        status, body = call(server, "/control/config")
        assert status == 200
        assert body["config"]["behavior"]["own_domain"] == ""
        assert body["config"]["behavior"]["excludes"] == ""


class TestDomainsAdd:
    def test_normalizes_and_writes_domains(self, make_server, make_config, tmp_path):
        server = make_server(make_config(domains_file="domains.txt"))

        status, body = call(
            server,
            "/control/domains",
            method="POST",
            body={"lines": ["https://www.edelind.de/x", "booking.com"]},
        )

        assert status == 200
        assert body["added"] == 2
        assert body["switched"] is False
        assert (tmp_path / "domains.txt").read_text(encoding="utf-8") == (
            "edelind.de\nbooking.com\n"
        )

    def test_creates_file_when_path_is_empty(self, make_server, make_config, tmp_path):
        server = make_server(make_config(domains_file=""))

        status, body = call(
            server, "/control/domains", method="POST", body={"lines": ["edelind.de"]}
        )

        assert status == 200
        assert body["switched"] is True
        assert (tmp_path / "domains.txt").read_text(encoding="utf-8") == "edelind.de\n"
        saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert saved["paths"]["filtered_domains"] == "domains.txt"


class TestDomainsDelete:
    def test_deletes_by_value(self, make_server, make_config, tmp_path):
        (tmp_path / "domains.txt").write_text("edelind.de\nbooking.com\n", encoding="utf-8")
        server = make_server(make_config(domains_file="domains.txt"))

        status, body = call(
            server, "/control/domains/delete", method="POST", body={"domains": ["edelind.de"]}
        )

        assert status == 200
        assert body == {"deleted": 1, "skipped": 0, "problems": []}
        assert (tmp_path / "domains.txt").read_text(encoding="utf-8") == "booking.com\n"

    def test_without_path_is_invalid_request(self, make_server, make_config):
        server = make_server(make_config(domains_file=""))

        status, body = call(
            server, "/control/domains/delete", method="POST", body={"all": True}
        )

        assert status == 400
        assert "paths.filtered_domains" in body["error"]["message"]

    def test_missing_value_is_reported_not_fatal(self, make_server, make_config, tmp_path):
        (tmp_path / "domains.txt").write_text("edelind.de\n", encoding="utf-8")
        server = make_server(make_config(domains_file="domains.txt"))

        status, body = call(
            server, "/control/domains/delete", method="POST", body={"domains": ["ghost.de"]}
        )

        assert status == 200
        assert body["deleted"] == 0
        assert body["problems"] == ["не найдено: ghost.de"]


class TestDomainsFile:
    def test_returns_absolute_path_and_existence(self, make_server, make_config, tmp_path):
        (tmp_path / "domains.txt").write_text("edelind.de\n", encoding="utf-8")
        server = make_server(make_config(domains_file="domains.txt"))

        status, body = call(server, "/control/domains/file")

        assert status == 200
        assert body == {"path": str(tmp_path / "domains.txt"), "exists": True}

    def test_reports_absent_file(self, make_server, make_config):
        server = make_server(make_config(domains_file="absent.txt"))

        status, body = call(server, "/control/domains/file")

        assert status == 200
        assert body["exists"] is False
