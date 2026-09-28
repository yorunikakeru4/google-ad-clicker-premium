"""Тесты engine.proxy_auth: CDP-авторизация прокси без --load-extension.

Chrome не запускается: все события CDP подаются через FakeWs из test_cdp,
либо напрямую в ProxyAuthManager.handle_event. Каждый тест задаёт конкретный
вход и конкретное ожидаемое тело исходящего CDP-сообщения.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from engine.cdp import CdpClient
from engine.proxy_auth import (
    DEFAULT_PROXY_TRANSPORT,
    PROXY_TRANSPORT_CDP_AUTH,
    PROXY_TRANSPORT_DIRECT,
    PROXY_TRANSPORT_EXTENSION,
    PROXY_TRANSPORTS,
    ProxyAuthManager,
    ProxyCredentials,
    create_proxy_auth,
    mask_secret,
    parse_proxy_credentials,
    resolve_proxy_transport,
)
from tests.engine.test_cdp import FakeWs, _make_client, _wait_until

USERNAME = "spyuser9"
PASSWORD = "Sup3rS3cret!"


def _auth_event(request_id: str, source: str = "Proxy", scheme: str = "Basic") -> dict:
    return {
        "method": "Fetch.authRequired",
        "params": {
            "requestId": request_id,
            "authChallenge": {"source": source, "scheme": scheme, "origin": "https://10.0.0.1:8080"},
        },
    }


def _started_manager(ws: FakeWs, **kwargs) -> ProxyAuthManager:
    client = _make_client(ws)
    client.start(timeout=2.0)
    manager = ProxyAuthManager(client, USERNAME, PASSWORD, **kwargs)
    manager.start()
    assert _wait_until(lambda: len(ws.sent_json()) == 2), ws.sent_json()
    ws.sent.clear()
    return manager


def _sent_methods(ws: FakeWs, method: str) -> list[dict]:
    return [m for m in ws.sent_json() if m.get("method") == method]


# --- маскирование и креды ---


def test_mask_secret_long_value() -> None:
    assert mask_secret("mylogin7") == "myl***in7"


def test_mask_secret_short_value() -> None:
    assert mask_secret("ab") == "***"
    assert mask_secret("") == "***"


def test_credentials_repr_hides_password() -> None:
    creds = ProxyCredentials(username=USERNAME, password=PASSWORD)
    assert PASSWORD not in repr(creds)
    assert USERNAME not in repr(creds)
    assert "myl***in7" in repr(ProxyCredentials(username="mylogin7", password="x"))


def test_parse_proxy_credentials_ok() -> None:
    assert parse_proxy_credentials(f"{USERNAME}:{PASSWORD}@10.0.0.1:8080") == (USERNAME, PASSWORD)


def test_parse_proxy_credentials_error_hides_password() -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_proxy_credentials(f"{USERNAME}:{PASSWORD}@10.0.0.1")
    assert PASSWORD not in str(exc_info.value)
    assert USERNAME not in str(exc_info.value)


def test_resolve_proxy_transport() -> None:
    assert resolve_proxy_transport(None) == DEFAULT_PROXY_TRANSPORT
    assert resolve_proxy_transport("") == DEFAULT_PROXY_TRANSPORT
    assert resolve_proxy_transport("extension") == PROXY_TRANSPORT_EXTENSION
    assert resolve_proxy_transport("direct") == PROXY_TRANSPORT_DIRECT
    assert DEFAULT_PROXY_TRANSPORT == PROXY_TRANSPORT_CDP_AUTH
    assert PROXY_TRANSPORTS == frozenset({"cdp_auth", "extension", "direct"})
    with pytest.raises(ValueError, match="proxy_transport"):
        resolve_proxy_transport("socks")


# --- старт: auto-attach + Fetch.enable без patterns ---


def test_start_sends_auto_attach_and_enable_without_patterns() -> None:
    ws = FakeWs()
    client = _make_client(ws)
    client.start(timeout=2.0)
    manager = ProxyAuthManager(client, USERNAME, PASSWORD)
    try:
        manager.start()
        assert _wait_until(lambda: len(ws.sent_json()) == 2), ws.sent_json()
        bodies = ws.sent_json()
        assert [b["method"] for b in bodies] == ["Target.setAutoAttach", "Fetch.enable"]
        assert [b["id"] for b in bodies] == [1, 2]
        assert bodies[0]["params"] == {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True,
        }
        assert bodies[1]["params"] == {"handleAuthRequests": True}
        assert "patterns" not in bodies[1]["params"]
        assert "sessionId" not in bodies[0]
    finally:
        manager.stop()


def test_manager_registers_auth_handler() -> None:
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        ws.incoming.put(json.dumps(_auth_event("REQ-1")))
        assert _wait_until(lambda: len(_sent_methods(ws, "Fetch.continueWithAuth")) == 1)
    finally:
        manager.stop()


# --- Fetch.authRequired ---


def test_auth_required_answers_with_credentials() -> None:
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        ws.incoming.put(json.dumps(_auth_event("REQ-1")))
        assert _wait_until(lambda: len(_sent_methods(ws, "Fetch.continueWithAuth")) == 1)
        answer = _sent_methods(ws, "Fetch.continueWithAuth")[0]
        assert answer["params"] == {
            "requestId": "REQ-1",
            "authChallengeResponse": {
                "response": "ProvideCredentials",
                "username": USERNAME,
                "password": PASSWORD,
            },
        }
        assert manager.fail_count == 0
        assert not manager.dead
    finally:
        manager.stop()


def test_auth_required_retry_cancels_and_counts_failure() -> None:
    ws = FakeWs()
    manager = _started_manager(ws, max_failures=5)
    try:
        ws.incoming.put(json.dumps(_auth_event("REQ-1")))
        assert _wait_until(lambda: len(_sent_methods(ws, "Fetch.continueWithAuth")) == 1)
        ws.incoming.put(json.dumps(_auth_event("REQ-1")))
        assert _wait_until(lambda: len(_sent_methods(ws, "Fetch.continueWithAuth")) == 2)
        retry = _sent_methods(ws, "Fetch.continueWithAuth")[1]
        assert retry["params"] == {
            "requestId": "REQ-1",
            "authChallengeResponse": {"response": "CancelAuth"},
        }
        assert manager.fail_count == 1
        assert not manager.dead
    finally:
        manager.stop()


def test_auth_required_unknown_scheme_cancels_without_counting() -> None:
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        ws.incoming.put(json.dumps(_auth_event("REQ-9", scheme="Negotiate")))
        assert _wait_until(lambda: len(_sent_methods(ws, "Fetch.continueWithAuth")) == 1)
        answer = _sent_methods(ws, "Fetch.continueWithAuth")[0]
        assert answer["params"] == {
            "requestId": "REQ-9",
            "authChallengeResponse": {"response": "CancelAuth"},
        }
        assert manager.fail_count == 0
    finally:
        manager.stop()


def test_auth_required_server_source_cancels_without_counting() -> None:
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        ws.incoming.put(json.dumps(_auth_event("REQ-5", source="Server", scheme="Basic")))
        assert _wait_until(lambda: len(_sent_methods(ws, "Fetch.continueWithAuth")) == 1)
        answer = _sent_methods(ws, "Fetch.continueWithAuth")[0]
        assert answer["params"]["authChallengeResponse"] == {"response": "CancelAuth"}
        assert manager.fail_count == 0
    finally:
        manager.stop()


def test_repeated_failures_mark_proxy_dead_once() -> None:
    ws = FakeWs()
    deaths: list[bool] = []
    manager = _started_manager(ws, max_failures=2, on_proxy_dead=lambda: deaths.append(True))
    try:
        for request_id in ("REQ-A", "REQ-B", "REQ-C"):
            ws.incoming.put(json.dumps(_auth_event(request_id)))
            ws.incoming.put(json.dumps(_auth_event(request_id)))
        assert _wait_until(lambda: manager.fail_count == 3, timeout=3.0)
        assert manager.dead
        assert deaths == [True]
    finally:
        manager.stop()


def test_unrelated_events_are_ignored() -> None:
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        ws.incoming.put(json.dumps({"method": "Target.attachedToTarget", "params": {}}))
        time.sleep(0.2)
        assert _sent_methods(ws, "Fetch.continueWithAuth") == []
        assert manager.fail_count == 0
    finally:
        manager.stop()


# --- утечки кредов ---


def test_credentials_never_reach_logs(caplog: pytest.LogCaptureFixture) -> None:
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        with caplog.at_level(logging.DEBUG, logger="engine.proxy_auth"):
            ws.incoming.put(json.dumps(_auth_event("REQ-1")))
            ws.incoming.put(json.dumps(_auth_event("REQ-1")))
            assert _wait_until(lambda: manager.fail_count == 1)
            manager.stop()
        assert PASSWORD not in caplog.text
        assert USERNAME not in caplog.text
    finally:
        manager.stop()


# --- фоновая жизнь ---


def test_create_proxy_auth_resolves_port_over_real_http() -> None:
    from http.server import HTTPServer

    from tests.engine.test_cdp import _VersionHandler

    ws = FakeWs()
    seen_urls: list[str] = []

    def fake_client(url: str, **kwargs) -> CdpClient:
        seen_urls.append(url)
        return _make_client(ws)

    server = HTTPServer(("127.0.0.1", 0), _VersionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        _VersionHandler.payload = {"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/z"}
        manager = create_proxy_auth(
            devtools_port=port, username=USERNAME, password=PASSWORD, client_factory=fake_client
        )
        try:
            assert seen_urls == [f"ws://127.0.0.1:{port}/devtools/browser/z"]
            assert _wait_until(lambda: len(ws.sent_json()) == 2)
        finally:
            manager.stop()
    finally:
        server.shutdown()


def test_stop_unsubscribes_and_closes_owned_client() -> None:
    ws = FakeWs()
    client = _make_client(ws)
    client.start(timeout=2.0)
    manager = ProxyAuthManager(client, USERNAME, PASSWORD, owns_client=True)
    manager.start()
    assert _wait_until(lambda: len(ws.sent_json()) == 2)
    manager.stop()
    assert ws.closed
    assert not client.connected


# --- добивающие тесты после мутационного прогона ---


def test_mask_secret_boundary_lengths() -> None:
    assert mask_secret("123456") == "***"
    assert mask_secret("1234567") == "123***567"


@pytest.mark.parametrize(
    "bad_proxy",
    [
        "nocolon@host:port",
        "user:pass:extra",
        "u:p@h:1:2",
        ":p@h:1",
        "u:@h:1",
        "u:p@:1",
        "u:p@h:",
        "user:pass@host",
    ],
)
def test_parse_proxy_credentials_rejects_malformed(bad_proxy: str) -> None:
    with pytest.raises(ValueError, match="^invalid proxy format"):
        parse_proxy_credentials(bad_proxy)


def test_parse_proxy_credentials_rejects_non_string() -> None:
    from typing import Any

    bad: Any = None
    with pytest.raises(ValueError, match="^invalid proxy format"):
        parse_proxy_credentials(bad)


def test_parse_proxy_credentials_allows_colon_in_password() -> None:
    assert parse_proxy_credentials("u:p:ss@h:1") == ("u", "p:ss")


def test_manager_rejects_bad_max_failures() -> None:
    ws = FakeWs()
    client = _make_client(ws)
    with pytest.raises(ValueError, match="^max_failures"):
        ProxyAuthManager(client, USERNAME, PASSWORD, max_failures=0)
    ProxyAuthManager(client, USERNAME, PASSWORD, max_failures=1)


def test_manager_dead_initially_false() -> None:
    ws = FakeWs()
    manager = ProxyAuthManager(_make_client(ws), USERNAME, PASSWORD)
    assert manager.dead is False
    assert manager.fail_count == 0


def test_manager_default_max_failures_is_three() -> None:
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        answered = 0
        for request_id, expected_fails, expected_dead in (("D-A", 1, False), ("D-B", 2, False), ("D-C", 3, True)):
            ws.incoming.put(json.dumps(_auth_event(request_id)))
            ws.incoming.put(json.dumps(_auth_event(request_id)))
            answered += 2
            assert _wait_until(
                lambda: len(_sent_methods(ws, "Fetch.continueWithAuth")) == answered
            )
            assert _wait_until(lambda: manager.fail_count == expected_fails)
            assert manager.dead is expected_dead
    finally:
        manager.stop()


def test_start_twice_sends_commands_once() -> None:
    ws = FakeWs()
    client = _make_client(ws)
    client.start(timeout=2.0)
    manager = ProxyAuthManager(client, USERNAME, PASSWORD)
    try:
        manager.start()
        manager.start()
        time.sleep(0.2)
        assert len(ws.sent_json()) == 2
    finally:
        manager.stop()


def test_stop_then_start_resubscribes() -> None:
    ws = FakeWs()
    client = _make_client(ws)
    client.start(timeout=2.0)
    manager = ProxyAuthManager(client, USERNAME, PASSWORD)
    try:
        manager.start()
        manager.stop()
        manager.start()
        assert _wait_until(lambda: len(ws.sent_json()) == 4), ws.sent_json()
        ws.incoming.put(json.dumps(_auth_event("REQ-R")))
        assert _wait_until(lambda: len(_sent_methods(ws, "Fetch.continueWithAuth")) == 1)
    finally:
        manager.stop()


def test_stop_ignores_events_afterwards() -> None:
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        manager.stop()
        ws.sent.clear()
        ws.incoming.put(json.dumps(_auth_event("REQ-1")))
        time.sleep(0.3)
        assert _sent_methods(ws, "Fetch.continueWithAuth") == []
    finally:
        manager.stop()


def test_stop_without_start_is_safe() -> None:
    ProxyAuthManager(_make_client(FakeWs()), USERNAME, PASSWORD).stop()


@pytest.mark.parametrize("garbage", [None, "nope", 123, [], {"method": "Nope.other"}])
def test_handle_event_rejects_garbage(garbage: object) -> None:
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        manager.handle_event(garbage)  # type: ignore[arg-type]
        time.sleep(0.2)
        assert _sent_methods(ws, "Fetch.continueWithAuth") == []
    finally:
        manager.stop()


def test_auth_required_without_request_id_ignored() -> None:
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        ws.incoming.put(json.dumps({"method": "Fetch.authRequired", "params": {"requestId": 123}}))
        ws.incoming.put(json.dumps({"method": "Fetch.authRequired", "params": {}}))
        time.sleep(0.3)
        assert _sent_methods(ws, "Fetch.continueWithAuth") == []
        assert manager.fail_count == 0
    finally:
        manager.stop()


def test_auth_required_without_challenge_cancels() -> None:
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        ws.incoming.put(
            json.dumps({"method": "Fetch.authRequired", "params": {"requestId": "R1"}})
        )
        assert _wait_until(lambda: len(_sent_methods(ws, "Fetch.continueWithAuth")) == 1)
        answer = _sent_methods(ws, "Fetch.continueWithAuth")[0]
        assert answer["params"] == {
            "requestId": "R1",
            "authChallengeResponse": {"response": "CancelAuth"},
        }
    finally:
        manager.stop()


def test_create_proxy_auth_without_source_raises() -> None:
    with pytest.raises(ValueError, match="^need ws_url"):
        create_proxy_auth(username=USERNAME, password=PASSWORD)


def test_create_proxy_auth_full_wiring(tmp_path) -> None:
    (tmp_path / "DevToolsActivePort").write_text("19311\n/devtools/browser/wired\n", encoding="utf-8")
    ws = FakeWs()
    seen_urls: list[str] = []
    deaths: list[bool] = []

    def fake_client(url: str, **kwargs) -> CdpClient:
        seen_urls.append(url)
        return _make_client(ws)

    manager = create_proxy_auth(
        user_data_dir=tmp_path,
        username=USERNAME,
        password=PASSWORD,
        on_proxy_dead=lambda: deaths.append(True),
        client_factory=fake_client,
    )
    try:
        assert seen_urls == ["ws://127.0.0.1:19311/devtools/browser/wired"]
        assert _wait_until(lambda: len(ws.sent_json()) == 2)
        ws.sent.clear()

        answered = 0
        for request_id, expected_fails, expected_dead in (("W-A", 1, False), ("W-B", 2, False), ("W-C", 3, True)):
            ws.incoming.put(json.dumps(_auth_event(request_id)))
            ws.incoming.put(json.dumps(_auth_event(request_id)))
            answered += 2
            assert _wait_until(
                lambda: len(_sent_methods(ws, "Fetch.continueWithAuth")) == answered
            ), ws.sent_json()
            assert _wait_until(lambda: manager.fail_count == expected_fails)
            assert manager.dead is expected_dead

        provide = _sent_methods(ws, "Fetch.continueWithAuth")[0]
        assert provide["params"]["authChallengeResponse"] == {
            "response": "ProvideCredentials",
            "username": USERNAME,
            "password": PASSWORD,
        }
        assert deaths == [True]
    finally:
        manager.stop()
    assert ws.closed


def test_create_proxy_auth_via_debugger_address() -> None:
    from http.server import HTTPServer

    from tests.engine.test_cdp import _VersionHandler

    ws = FakeWs()
    seen_urls: list[str] = []

    def fake_client(url: str, **kwargs) -> CdpClient:
        seen_urls.append(url)
        return _make_client(ws)

    server = HTTPServer(("127.0.0.1", 0), _VersionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        _VersionHandler.payload = {"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/d"}
        manager = create_proxy_auth(
            debugger_address=f"127.0.0.1:{port}",
            username=USERNAME,
            password=PASSWORD,
            client_factory=fake_client,
        )
        try:
            assert seen_urls == [f"ws://127.0.0.1:{port}/devtools/browser/d"]
        finally:
            manager.stop()
    finally:
        server.shutdown()


def test_create_proxy_auth_forwards_max_failures() -> None:
    ws = FakeWs()

    def fake_client(url: str, **kwargs) -> CdpClient:
        return _make_client(ws)

    manager = create_proxy_auth(
        ws_url="ws://127.0.0.1:1/x", username=USERNAME, password=PASSWORD, max_failures=1,
        client_factory=fake_client,
    )
    try:
        assert _wait_until(lambda: len(ws.sent_json()) == 2)
        ws.incoming.put(json.dumps(_auth_event("F-1")))
        ws.incoming.put(json.dumps(_auth_event("F-1")))
        assert _wait_until(lambda: manager.dead is True, timeout=3.0)
        assert manager.fail_count == 1
    finally:
        manager.stop()


def test_manager_default_does_not_own_client() -> None:
    ws = FakeWs()
    client = _make_client(ws)
    client.start(timeout=2.0)
    manager = ProxyAuthManager(client, USERNAME, PASSWORD)
    try:
        manager.start()
        assert _wait_until(lambda: len(ws.sent_json()) == 2)
        manager.stop()
        assert not ws.closed
        assert client.connected
    finally:
        client.stop()
