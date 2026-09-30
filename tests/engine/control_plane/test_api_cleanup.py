"""Тесты HTTP-хендлеров очистки (план §5, фаза 10).

Контракт параллельной UI-ветки:

* ``POST /control/cleanup/run`` — синхронный прогон, тело ``{}`` или
  ``{"dry_run": true}``, ответ ``{"report": {...счётчики...}}``;
* ``GET /control/cleanup/status`` — ``{"last": {ts, report} | null,
  "next_run": epoch | null}``, состояние из kv.

Сервер в тестах получает сервис с корнями в ``tmp_path``: боевой сервис ходит
по системному tempdir, и тест не имеет права туда заглядывать. Ошибки — те же
коды, что у остальных хендлеров: без токена 401, не тот метод 405, кривое тело
400.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import pytest

from engine.cleanup import (
    CLEANUP_LAST_REPORT_KEY,
    CLEANUP_LAST_TS_KEY,
    CLEANUP_NEXT_RUN_KEY,
    CleanupService,
    default_targets,
)
from engine.control_plane import api
from engine.control_plane.api import TOKEN_HEADER, ControlPlaneServer
from engine.control_plane.config import Config
from engine.control_plane.state import StateStore
from engine.control_plane.supervisor import Supervisor, SupervisorSettings
from tests.engine.control_plane.test_supervisor import FakeClock, FakeProcessRegistry

TOKEN = "cleanup-api-token"

OLD_SECONDS = 7200


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
def roots(tmp_path):
    temp_root = tmp_path / "temp"
    (temp_root / "uc_profiles").mkdir(parents=True)
    (temp_root / "sb_profiles").mkdir(parents=True)
    screenshots_root = tmp_path / "shots"
    screenshots_root.mkdir()
    return temp_root, screenshots_root


@pytest.fixture
def cleanup_service(store, roots):
    temp_root, screenshots_root = roots
    return CleanupService(
        store, targets=default_targets(temp_root, screenshots_root)
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
def clock():
    return FakeClock()


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
def config():
    return Config.from_dict(
        {"behavior": {"browser_count": 2, "cleanup_time": "04:00", "cleanup_interval_days": 1}}
    )


@pytest.fixture
def server(supervisor, config, tmp_path, cleanup_service):
    """Живой сервер на свободном порту loopback, гасится после теста.

    ``cleanup_next_run`` не передаётся: без демона расписания нет, и ручной
    запуск не трогает kv-цель (это отдельно проверено в тесте с провайдером).
    """
    instance = ControlPlaneServer(
        supervisor=supervisor,
        config=config,
        token=TOKEN,
        config_path=tmp_path / "config.json",
        host="127.0.0.1",
        port=0,
        cleanup_service=cleanup_service,
    )
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def request(server, path, method="GET", token=TOKEN, body=None):
    """HTTP-запрос к поднятому серверу. Возвращает (status, json)."""
    url = f"http://{server.host}:{server.port}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        req.add_header(TOKEN_HEADER, token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {"_raw": payload}
        return exc.code, parsed


def make_orphan(roots, name="profile_1234"):
    """Осиротевший профиль в tmp, старше грейса."""
    temp_root, _ = roots
    path = temp_root / "uc_profiles" / name
    path.mkdir()
    (path / "Cookies").write_bytes(b"C" * 100)
    stamp = time.time() - OLD_SECONDS
    os.utime(path, (stamp, stamp))
    return path


class TestCleanupRun:
    """POST /control/cleanup/run: синхронный прогон и отчёт по контракту."""

    def test_run_removes_orphans_and_returns_the_contract_report(
        self, server, roots, cleanup_service
    ):
        orphan = make_orphan(roots)

        status, payload = request(server, "/control/cleanup/run", method="POST", body={})

        assert status == 200
        assert set(payload) == {"report"}
        report = payload["report"]
        assert set(report) == {
            "removed",
            "removed_bytes",
            "skipped_active",
            "errors",
            "duration_ms",
            "dry_run",
        }
        assert report["removed"] == 1
        assert report["removed_bytes"] > 0
        assert report["skipped_active"] == 0
        assert report["errors"] == 0
        assert isinstance(report["duration_ms"], int)
        assert report["dry_run"] is False
        assert not orphan.exists()

    def test_run_without_a_body_is_a_real_run(self, server, roots):
        orphan = make_orphan(roots)

        status, payload = request(server, "/control/cleanup/run", method="POST")

        assert status == 200
        assert payload["report"]["removed"] == 1
        assert not orphan.exists()

    def test_dry_run_counts_candidates_but_removes_nothing(self, server, roots):
        orphan = make_orphan(roots)

        status, payload = request(
            server, "/control/cleanup/run", method="POST", body={"dry_run": True}
        )

        assert status == 200
        report = payload["report"]
        assert report["dry_run"] is True
        assert report["removed"] == 1, "dry_run перечисляет кандидатов"
        assert orphan.exists(), "dry_run не имеет права удалять"

    def test_dry_run_does_not_move_the_last_run_marker(self, server, roots, store):
        make_orphan(roots)

        request(server, "/control/cleanup/run", method="POST", body={"dry_run": True})

        assert store.get_flag(CLEANUP_LAST_TS_KEY) is None
        assert store.get_flag(CLEANUP_LAST_REPORT_KEY) is None

    def test_run_writes_the_report_for_status(self, server, roots, store):
        make_orphan(roots)

        _, payload = request(server, "/control/cleanup/run", method="POST", body={})
        _, status_payload = request(server, "/control/cleanup/status")

        assert status_payload["last"]["report"] == payload["report"]
        assert status_payload["last"]["ts"] == pytest.approx(
            float(store.get_flag(CLEANUP_LAST_TS_KEY))
        )

    def test_run_recalculates_next_run_when_the_daemon_provides_it(
        self, supervisor, config, tmp_path, cleanup_service
    ):
        """После ручного запуска цель в kv пересчитывается, а не протухает."""
        instance = ControlPlaneServer(
            supervisor=supervisor,
            config=config,
            token=TOKEN,
            config_path=tmp_path / "config.json",
            host="127.0.0.1",
            port=0,
            cleanup_service=cleanup_service,
            cleanup_next_run=lambda: 1711954800.0,
        )
        instance.start()
        try:
            status, payload = request(instance, "/control/cleanup/run", method="POST", body={})
            raw = cleanup_service.store.get_flag(CLEANUP_NEXT_RUN_KEY)
        finally:
            instance.stop()

        assert status == 200, payload
        assert raw == "1711954800.0"

    @pytest.mark.parametrize("dry_run", ["yes", 1, None, {}])
    def test_non_boolean_dry_run_is_rejected(self, server, dry_run):
        status, payload = request(
            server, "/control/cleanup/run", method="POST", body={"dry_run": dry_run}
        )

        assert status == 400
        assert payload["error"]["code"] == "invalid_request"

    @pytest.mark.parametrize("body", [[1, 2], "текст", 5])
    def test_non_object_body_is_rejected(self, server, body):
        status, payload = request(
            server, "/control/cleanup/run", method="POST", body=body
        )

        assert status == 400
        assert payload["error"]["code"] == "invalid_request"

    def test_run_requires_the_token(self, server, roots):
        make_orphan(roots)

        status, payload = request(
            server, "/control/cleanup/run", method="POST", token=None, body={}
        )

        assert status == 401
        assert payload["error"]["code"] == "unauthorized"

    def test_get_on_the_run_path_is_method_not_allowed(self, server):
        status, payload = request(server, "/control/cleanup/run")

        assert status == 405
        assert payload["error"]["code"] == "method_not_allowed"


class TestCleanupStatus:
    """GET /control/cleanup/status: последний отчёт и ближайший запуск."""

    def test_status_before_any_run(self, server, store):
        """До первой записи демона next_run считается по расписанию конфига."""
        status, payload = request(server, "/control/cleanup/status")

        assert status == 200
        assert set(payload) == {"last", "next_run"}
        assert payload["last"] is None
        assert isinstance(payload["next_run"], (int, float))
        assert payload["next_run"] > time.time()

    def test_status_returns_the_stored_next_run(self, server, store):
        store.set_flag(CLEANUP_NEXT_RUN_KEY, "1711954800.5")

        _, payload = request(server, "/control/cleanup/status")

        assert payload["next_run"] == 1711954800.5

    def test_status_empty_next_run_means_null(self, server, store):
        store.set_flag(CLEANUP_NEXT_RUN_KEY, "")

        _, payload = request(server, "/control/cleanup/status")

        assert payload["next_run"] is None

    def test_status_reports_the_last_run(self, server, store):
        store.set_flag(CLEANUP_LAST_TS_KEY, "1711954800.0")
        store.set_flag(
            CLEANUP_LAST_REPORT_KEY,
            json.dumps({"removed": 2, "removed_bytes": 10, "errors": 0}),
        )

        _, payload = request(server, "/control/cleanup/status")

        assert payload["last"] == {
            "ts": 1711954800.0,
            "report": {"removed": 2, "removed_bytes": 10, "errors": 0},
        }

    def test_status_requires_the_token(self, server):
        status, payload = request(server, "/control/cleanup/status", token=None)

        assert status == 401
        assert payload["error"]["code"] == "unauthorized"

    def test_post_on_the_status_path_is_method_not_allowed(self, server):
        status, payload = request(server, "/control/cleanup/status", method="POST", body={})

        assert status == 405
        assert payload["error"]["code"] == "method_not_allowed"

    def test_unknown_cleanup_path_is_not_found(self, server):
        status, payload = request(server, "/control/cleanup")

        assert status == 404
        assert payload["error"]["code"] == "not_found"


def test_cleanup_routes_are_registered():
    """Маршруты обязаны быть в таблице — иначе хендлеры недостижимы."""
    assert ("POST", "/control/cleanup/run") in api._ROUTES
    assert ("GET", "/control/cleanup/status") in api._ROUTES
