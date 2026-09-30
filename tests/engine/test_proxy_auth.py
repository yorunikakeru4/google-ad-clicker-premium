"""Тесты engine.proxy_auth: CDP-авторизация прокси без --load-extension.

Chrome не запускается: все события CDP подаются через FakeWs из test_cdp,
либо напрямую в ProxyAuthManager.handle_event. Каждый тест задаёт конкретный
вход и конкретное ожидаемое тело исходящего CDP-сообщения.

Помимо авторизации, менеджер включает метрику запросов: ``Network.enable``
на старте и на каждой новой сессии таргета, пары событий Network.* →
записи в ``network_requests`` через общий логгер процесса.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time

import pytest

from engine.cdp import CdpClient
from engine.log import get_logger
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

# Сколько команд уходит на старте: auto-attach + Fetch.enable + Network.enable.
# Browser-level на старте — только Target.setAutoAttach: реальный Chrome
# отвечает -32601 "'Network.enable' wasn't found" на browser-endpoint
# (поймано живым e2e-прогоном: 'cdp auth start failed: CdpError' на каждой
# сессии, юнит-фейк домены не валидировал). Fetch.enable и Network.enable
# уходят per-session в handle_attached.
START_COMMANDS = 1
# Команды, которые менеджер шлёт на каждую новую сессию таргета.
SESSION_COMMANDS = 2


def _auth_event(request_id: str, source: str = "Proxy", scheme: str = "Basic") -> dict:
    return {
        "method": "Fetch.authRequired",
        "params": {
            "requestId": request_id,
            "authChallenge": {"source": source, "scheme": scheme, "origin": "https://10.0.0.1:8080"},
        },
    }


def _network_request(
    request_id: str,
    url: str = "https://site.test/page",
    method: str = "GET",
    resource_type: str = "Document",
    session: str | None = None,
) -> dict:
    message: dict = {
        "method": "Network.requestWillBeSent",
        "params": {
            "requestId": request_id,
            "request": {"method": method, "url": url},
            "type": resource_type,
        },
    }
    if session is not None:
        message["sessionId"] = session
    return message


def _network_response(
    request_id: str, status: int = 200, session: str | None = None
) -> dict:
    message: dict = {
        "method": "Network.responseReceived",
        "params": {"requestId": request_id, "response": {"status": status}},
    }
    if session is not None:
        message["sessionId"] = session
    return message


def _network_rows(browser_id: str) -> list[sqlite3.Row]:
    """Строки network_requests в БД процесса (ADCLICKER_DB из conftest)."""

    with sqlite3.connect(os.environ["ADCLICKER_DB"]) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT ts, browser_id, method, url, resource_type, status "
            "FROM network_requests WHERE browser_id = ? ORDER BY id",
            (browser_id,),
        ).fetchall()


def _started_manager(ws: FakeWs, **kwargs) -> ProxyAuthManager:
    client = _make_client(ws)
    client.start(timeout=2.0)
    manager = ProxyAuthManager(client, USERNAME, PASSWORD, **kwargs)
    manager.start()
    assert _wait_until(lambda: len(ws.sent_json()) == START_COMMANDS), ws.sent_json()
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


# --- старт: auto-attach + Fetch.enable без patterns + Network.enable ---


def test_start_sends_only_auto_attach_on_browser_endpoint() -> None:
    """На старте — ровно Target.setAutoAttach.

    ``Fetch.enable``/``Network.enable`` на browser-endpoint не уходят:
    ``Network.enable`` там даёт -32601 (живой e2e), а нужные домены
    включаются per-session в ``handle_attached`` (контракт §2.1, п.4).
    """
    ws = FakeWs()
    client = _make_client(ws)
    client.start(timeout=2.0)
    manager = ProxyAuthManager(client, USERNAME, PASSWORD)
    try:
        manager.start()
        assert _wait_until(lambda: len(ws.sent_json()) == START_COMMANDS), ws.sent_json()
        bodies = ws.sent_json()
        assert [b["method"] for b in bodies] == ["Target.setAutoAttach"]
        assert bodies[0]["params"] == {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True,
        }
        assert "sessionId" not in bodies[0]
        # Ни Fetch, ни Network на browser-level: Chrome это отвергает.
        methods = {b["method"] for b in bodies}
        assert "Fetch.enable" not in methods
        assert "Network.enable" not in methods
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


def _paused_event(
    request_id: str, session_id: str | None = None, response_stage: bool = False
) -> dict:
    message: dict = {
        "method": "Fetch.requestPaused",
        "params": {"requestId": request_id, "request": {"url": "https://example.com/"}},
    }
    if response_stage:
        message["params"]["responseStatusCode"] = 200
    if session_id:
        message["sessionId"] = session_id
    return message


def test_paused_request_is_continued_without_auth_roundtrip() -> None:
    """Chrome 153 ставит ``requestPaused`` перед CONNECT даже без ``patterns``.

    Без ``continueRequest`` соединение в сеть не уходит, прокси не отдаёт
    407, ``authRequired`` не приходит — навигация виснет до таймаута.
    Поймано живым e2e-прогоном: страницы через авторизованный прокси
    не открывались ни разу, 0 событий авторизации.
    """
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        ws.incoming.put(json.dumps(_paused_event("P-1", session_id="S-9")))
        assert _wait_until(lambda: len(_sent_methods(ws, "Fetch.continueRequest")) == 1)
        sent = _sent_methods(ws, "Fetch.continueRequest")[0]
        assert sent["params"] == {"requestId": "P-1"}
        assert sent["sessionId"] == "S-9"
        # Пауза — не неудачная авторизация: счётчик не растёт.
        assert manager.fail_count == 0
    finally:
        manager.stop()


def test_response_stage_pause_uses_continue_response() -> None:
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        ws.incoming.put(json.dumps(_paused_event("P-2", response_stage=True)))
        assert _wait_until(lambda: len(_sent_methods(ws, "Fetch.continueResponse")) == 1)
        assert _sent_methods(ws, "Fetch.continueRequest") == []
    finally:
        manager.stop()


def test_lowercase_scheme_from_real_chrome_is_accepted() -> None:
    """Chrome шлёт scheme строчными (``basic``), а словарь — «Basic».

    Регистр несовпадение уводило вызов в CancelAuth: страница получала
    HTTP 407 и не открывалась — поймано живым e2e-прогоном через
    авторизованный прокси (unit-тесты использовали «Basic» с заглавной).
    """
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        event = _auth_event("REQ-lower", scheme="basic")
        ws.incoming.put(json.dumps(event))
        assert _wait_until(lambda: len(_sent_methods(ws, "Fetch.continueWithAuth")) == 1)
        sent = _sent_methods(ws, "Fetch.continueWithAuth")[0]
        assert sent["params"]["authChallengeResponse"]["response"] == "ProvideCredentials"
        assert manager.fail_count == 0
    finally:
        manager.stop()


def test_auth_answer_is_delivered_in_the_sessions_session() -> None:
    """Ответ на authRequired обязан уйти в ту же sessionId.

    Событие привязано к сессии (flatten), а ответ без sessionId Chrome
    отвергает — запрос падает с net::ERR_ABORTED, страница не открывается
    (поймано живым e2e-прогоном через авторизованный прокси).
    """
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        event = _auth_event("REQ-S")
        event["sessionId"] = "S-77"
        ws.incoming.put(json.dumps(event))
        assert _wait_until(lambda: len(_sent_methods(ws, "Fetch.continueWithAuth")) == 1)
        sent = _sent_methods(ws, "Fetch.continueWithAuth")[0]
        assert sent["sessionId"] == "S-77"
        # повтор того же requestId (CancelAuth) тоже уходит в сессию
        ws.incoming.put(json.dumps(event))
        assert _wait_until(lambda: len(_sent_methods(ws, "Fetch.continueWithAuth")) == 2)
        cancelled = _sent_methods(ws, "Fetch.continueWithAuth")[1]
        assert cancelled["params"]["authChallengeResponse"]["response"] == "CancelAuth"
        assert cancelled["sessionId"] == "S-77"
    finally:
        manager.stop()


def test_paused_and_auth_are_answered_independently() -> None:
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        ws.incoming.put(json.dumps(_paused_event("P-3")))
        ws.incoming.put(json.dumps(_auth_event("REQ-9")))
        assert _wait_until(lambda: len(_sent_methods(ws, "Fetch.continueRequest")) == 1)
        assert _wait_until(lambda: len(_sent_methods(ws, "Fetch.continueWithAuth")) == 1)
        assert manager.fail_count == 0
    finally:
        manager.stop()


# --- Network.enable на каждой новой сессии ---


def test_attached_session_gets_fetch_and_network_enable() -> None:
    """Новая вкладка = новая сессия: Fetch (без patterns) и Network уходят вместе."""
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        ws.incoming.put(
            json.dumps(
                {
                    "method": "Target.attachedToTarget",
                    "params": {"sessionId": "S-7", "targetInfo": {"type": "page"}},
                }
            )
        )
        assert _wait_until(lambda: len(ws.sent_json()) == 2), ws.sent_json()

        fetch, network = ws.sent_json()
        assert fetch["method"] == "Fetch.enable"
        assert fetch["params"] == {"handleAuthRequests": True}
        assert "patterns" not in fetch["params"]
        assert fetch["sessionId"] == "S-7"
        assert network["method"] == "Network.enable"
        assert network.get("params", {}) == {}
        assert network["sessionId"] == "S-7"
    finally:
        manager.stop()


def test_attached_session_without_id_is_ignored() -> None:
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        ws.incoming.put(json.dumps({"method": "Target.attachedToTarget", "params": {}}))
        ws.incoming.put(json.dumps({"method": "Target.attachedToTarget", "params": None}))
        time.sleep(0.2)

        assert ws.sent_json() == []
        assert manager.fail_count == 0
    finally:
        manager.stop()


def test_detached_session_drops_pending_requests() -> None:
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        ws.incoming.put(json.dumps(_network_request("N-1", session="S-1")))
        assert _wait_until(lambda: manager.recorder.pending == 1)

        ws.incoming.put(
            json.dumps({"method": "Target.detachedFromTarget", "params": {"sessionId": "S-1"}})
        )
        assert _wait_until(lambda: manager.recorder.pending == 0)
        assert manager.recorder.evicted == 1

        # Запоздавший ответ не создаёт запись и не роняет обработчик.
        ws.incoming.put(json.dumps(_network_response("N-1", session="S-1")))
        assert _wait_until(lambda: manager.recorder.unmatched_responses == 1)
        assert manager.recorder.emitted == 0
    finally:
        manager.stop()


# --- метрика: пары событий уходят в network_requests ---


def test_network_pair_is_written_to_network_requests() -> None:
    ws = FakeWs()
    worker_log = get_logger(browser_id="br-net-1")
    client = _make_client(ws)
    client.start(timeout=2.0)
    manager = ProxyAuthManager(client, USERNAME, PASSWORD)
    try:
        manager.start()
        assert _wait_until(lambda: len(ws.sent_json()) == START_COMMANDS), ws.sent_json()

        ws.incoming.put(json.dumps(_network_request("N-1")))
        ws.incoming.put(json.dumps(_network_response("N-1", status=204)))

        def row_ready() -> bool:
            worker_log.flush()
            return len(_network_rows("br-net-1")) == 1

        assert _wait_until(row_ready, timeout=3.0), _network_rows("br-net-1")
        row = _network_rows("br-net-1")[0]
        assert (
            row["browser_id"],
            row["method"],
            row["url"],
            row["resource_type"],
            row["status"],
        ) == ("br-net-1", "GET", "https://site.test/page", "Document", 204)
        assert row["ts"] > 0
        assert worker_log.dropped == 0
    finally:
        manager.stop()
        client.stop()
        worker_log.bind(None)


def test_garbage_network_events_do_not_break_worker() -> None:
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        for junk in (
            {"method": "Network.requestWillBeSent"},
            {"method": "Network.requestWillBeSent", "params": {"requestId": 3}},
            {
                "method": "Network.responseReceived",
                "params": {"requestId": "ghost", "response": {"status": 200}},
            },
        ):
            ws.incoming.put(json.dumps(junk))
        assert _wait_until(
            lambda: manager.recorder.malformed == 2
            and manager.recorder.unmatched_responses == 1
        ), (manager.recorder.malformed, manager.recorder.unmatched_responses)

        # Обработка мусора не сломала ни корректную пару, ни авторизацию.
        ws.incoming.put(json.dumps(_network_request("N-1")))
        ws.incoming.put(json.dumps(_network_response("N-1")))
        assert _wait_until(lambda: manager.recorder.emitted == 1)

        ws.incoming.put(json.dumps(_auth_event("REQ-1")))
        assert _wait_until(lambda: len(_sent_methods(ws, "Fetch.continueWithAuth")) == 1)
    finally:
        manager.stop()


def test_dropped_network_event_is_logged_without_url(caplog: pytest.LogCaptureFixture) -> None:
    ws = FakeWs()
    manager = _started_manager(ws)
    try:
        with caplog.at_level(logging.DEBUG, logger="logger"):
            ws.incoming.put(
                json.dumps(
                    {
                        "method": "Network.requestWillBeSent",
                        "params": {
                            "requestId": 5,
                            "request": {
                                "method": "GET",
                                "url": "https://site.test/p?token=SECRET9",
                            },
                        },
                    }
                )
            )
            assert _wait_until(lambda: manager.recorder.malformed == 1)
        mirrored = [record for record in caplog.records if record.name == "logger"]
        dropped = [
            record
            for record in mirrored
            if isinstance(getattr(record, "fields", None), dict)
            and record.fields.get("malformed")
        ]
        assert dropped, "отброшенное событие должно попасть в debug-лог со счётчиком"
        assert "SECRET9" not in caplog.text
    finally:
        manager.stop()


def test_full_url_with_query_stays_out_of_logs(caplog: pytest.LogCaptureFixture) -> None:
    """URL целиком живёт в таблице; в логи он не попадает — это метрика."""
    ws = FakeWs()
    worker_log = get_logger(browser_id="br-net-2")
    manager = _started_manager(ws)
    try:
        with caplog.at_level(logging.DEBUG, logger="logger"):
            ws.incoming.put(
                json.dumps(_network_request("N-9", url="https://site.test/p?token=SECRET123"))
            )
            ws.incoming.put(json.dumps(_network_response("N-9")))

            def row_ready() -> bool:
                worker_log.flush()
                return len(_network_rows("br-net-2")) == 1

            assert _wait_until(row_ready, timeout=3.0)
        assert [r["url"] for r in _network_rows("br-net-2")] == [
            "https://site.test/p?token=SECRET123"
        ]
        assert "SECRET123" not in caplog.text
    finally:
        manager.stop()
        worker_log.bind(None)


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
        # Записи уходят зеркалом в legacy-логгер из logger.py, поэтому
        # уровень поднимается именно для него.
        with caplog.at_level(logging.DEBUG, logger="logger"):
            ws.incoming.put(json.dumps(_auth_event("REQ-1")))
            ws.incoming.put(json.dumps(_auth_event("REQ-1")))
            assert _wait_until(lambda: manager.fail_count == 1)
            manager.stop()
        mirrored = [record for record in caplog.records if record.name == "logger"]
        assert mirrored, "авторизация должна что-то логировать — иначе проверка пустая"
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
            assert _wait_until(lambda: len(ws.sent_json()) == START_COMMANDS)
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
    assert _wait_until(lambda: len(ws.sent_json()) == START_COMMANDS)
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
        assert len(ws.sent_json()) == START_COMMANDS
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
        assert _wait_until(lambda: len(ws.sent_json()) == 2 * START_COMMANDS), ws.sent_json()
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
        assert _wait_until(lambda: len(ws.sent_json()) == START_COMMANDS)
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


def test_create_proxy_auth_falls_back_when_port_file_is_missing(tmp_path) -> None:
    """UC поднимает Chrome с фиксированным ``--remote-debugging-port``:

    файла ``DevToolsActivePort`` в профиле нет вовсе, и без перехода на
    ``debugger_address`` авторизация не поднимается ни на одной сессии —
    поймано живым e2e-прогоном (``cdp auth start failed: CdpError`` на
    каждом воркере, прокси сгорали в ротации).
    """
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
        _VersionHandler.payload = {
            "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/f"
        }
        manager = create_proxy_auth(
            username=USERNAME,
            password=PASSWORD,
            user_data_dir=tmp_path,  # каталог без DevToolsActivePort
            debugger_address=f"127.0.0.1:{port}",
            client_factory=fake_client,
        )
        try:
            assert seen_urls == [f"ws://127.0.0.1:{port}/devtools/browser/f"]
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
        assert _wait_until(lambda: len(ws.sent_json()) == START_COMMANDS)
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
        assert _wait_until(lambda: len(ws.sent_json()) == START_COMMANDS)
        manager.stop()
        assert not ws.closed
        assert client.connected
    finally:
        client.stop()
