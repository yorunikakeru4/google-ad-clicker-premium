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
        assert answer["params"]["authChallengeResponse"] == {"response": "CancelAuth"}
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
