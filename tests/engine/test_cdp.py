"""Тесты engine.cdp: разбор DevTools-порта, построение ws-URL, тонкий WS-клиент.

Chrome не запускается: вместо него — файлы в tmp_path, настоящий HTTP-сервер
на 127.0.0.1 для /json/version и минимальный RFC6455-сервер для проверки
клиента по живому сокету. Поддельный WebSocket (FakeWs) — только для
юнит-проверок логики переподключения и мультиплексирования.
"""

from __future__ import annotations

import base64
import hashlib
import json
import queue
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from engine.cdp import (
    CdpClient,
    CdpError,
    browser_ws_url_from_active_port,
    read_devtools_active_port,
    resolve_browser_ws_url,
    ws_url_from_json_version,
)


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeWs:
    """Поддельный сокет для юнит-тестов: пишет в память, читает из очереди."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.incoming: queue.Queue[str] = queue.Queue()
        self.closed = False
        self._lock = threading.Lock()

    def send(self, data: str) -> None:
        with self._lock:
            self.sent.append(data)

    def recv(self, timeout: float | None = None) -> str:
        try:
            return self.incoming.get(timeout=timeout or 0.2)
        except queue.Empty:
            raise TimeoutError("no message") from None

    def close(self) -> None:
        self.closed = True

    def sent_json(self) -> list[dict]:
        with self._lock:
            return [json.loads(s) for s in self.sent]


def _make_client(ws: FakeWs, **kwargs) -> CdpClient:
    return CdpClient("ws://127.0.0.1:1/devtools/browser/fake", ws_factory=lambda url, timeout: ws, **kwargs)


# --- DevToolsActivePort ---


def test_read_devtools_active_port_ok(tmp_path: Path) -> None:
    (tmp_path / "DevToolsActivePort").write_text("19233\n/devtools/browser/uuid-1\n", encoding="utf-8")
    assert read_devtools_active_port(tmp_path) == (19233, "/devtools/browser/uuid-1")


def test_read_devtools_active_port_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_devtools_active_port(tmp_path / "no-such-dir")


def test_read_devtools_active_port_bad_port(tmp_path: Path) -> None:
    (tmp_path / "DevToolsActivePort").write_text("notaport\n/devtools/browser/uuid-1\n", encoding="utf-8")
    with pytest.raises(CdpError, match="DevToolsActivePort"):
        read_devtools_active_port(tmp_path)


def test_read_devtools_active_port_truncated(tmp_path: Path) -> None:
    (tmp_path / "DevToolsActivePort").write_text("19233\n", encoding="utf-8")
    with pytest.raises(CdpError, match="DevToolsActivePort"):
        read_devtools_active_port(tmp_path)


def test_read_devtools_active_port_port_out_of_range(tmp_path: Path) -> None:
    (tmp_path / "DevToolsActivePort").write_text("99999\n/devtools/browser/uuid-1\n", encoding="utf-8")
    with pytest.raises(CdpError, match="DevToolsActivePort"):
        read_devtools_active_port(tmp_path)


def test_browser_ws_url_from_active_port() -> None:
    assert (
        browser_ws_url_from_active_port(19233, "/devtools/browser/uuid-1")
        == "ws://127.0.0.1:19233/devtools/browser/uuid-1"
    )


# --- /json/version по настоящему сокету ---


class _VersionHandler(BaseHTTPRequestHandler):
    payload: dict = {}

    def do_GET(self) -> None:  # noqa: N802
        assert self.path == "/json/version"
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def version_server():
    server = HTTPServer(("127.0.0.1", 0), _VersionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def test_ws_url_from_json_version(version_server: HTTPServer) -> None:
    port = version_server.server_address[1]
    _VersionHandler.payload = {
        "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/uuid-9",
        "Browser": "Chrome/142.0",
    }
    assert ws_url_from_json_version(port) == f"ws://127.0.0.1:{port}/devtools/browser/uuid-9"


def test_ws_url_from_json_version_missing_field(version_server: HTTPServer) -> None:
    _VersionHandler.payload = {"Browser": "Chrome/142.0"}
    with pytest.raises(CdpError, match="webSocketDebuggerUrl"):
        ws_url_from_json_version(version_server.server_address[1])


def test_ws_url_from_json_version_connection_refused() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(CdpError, match="/json/version"):
        ws_url_from_json_version(free_port)


def test_resolve_prefers_active_port_file(tmp_path: Path, version_server: HTTPServer) -> None:
    (tmp_path / "DevToolsActivePort").write_text("19233\n/devtools/browser/from-file\n", encoding="utf-8")
    _VersionHandler.payload = {
        "webSocketDebuggerUrl": f"ws://127.0.0.1:{version_server.server_address[1]}/devtools/browser/x"
    }
    url = resolve_browser_ws_url(
        user_data_dir=tmp_path, debugger_address=f"127.0.0.1:{version_server.server_address[1]}"
    )
    assert url == "ws://127.0.0.1:19233/devtools/browser/from-file"


def test_resolve_via_debugger_address(version_server: HTTPServer) -> None:
    port = version_server.server_address[1]
    _VersionHandler.payload = {"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/uuid-7"}
    assert (
        resolve_browser_ws_url(debugger_address=f"127.0.0.1:{port}")
        == f"ws://127.0.0.1:{port}/devtools/browser/uuid-7"
    )


def test_resolve_without_sources_raises() -> None:
    with pytest.raises(CdpError, match="DevTools"):
        resolve_browser_ws_url()


# --- CdpClient на FakeWs ---


def test_client_echoes_response_by_id() -> None:
    ws = FakeWs()
    client = _make_client(ws)
    client.start(timeout=2.0)
    try:
        received: list[dict] = []

        def answer() -> None:
            assert _wait_until(lambda: len(ws.sent_json()) == 1), ws.sent_json()
            request = ws.sent_json()[0]
            ws.incoming.put(json.dumps({"id": request["id"], "result": {"ok": True}}))

        worker = threading.Thread(target=answer, daemon=True)
        worker.start()
        assert client.send_command("Browser.getVersion", timeout=2.0) == {"ok": True}
        worker.join(timeout=2.0)
        assert received == []
    finally:
        client.stop()


def test_client_dispatches_events_by_session_id() -> None:
    ws = FakeWs()
    client = _make_client(ws)
    client.start(timeout=2.0)
    try:
        got: list[dict] = []
        client.on("Fetch.authRequired", got.append)
        ws.incoming.put(
            json.dumps({"method": "Fetch.authRequired", "params": {"requestId": "1"}, "sessionId": "AAA"})
        )
        ws.incoming.put(
            json.dumps({"method": "Fetch.authRequired", "params": {"requestId": "2"}, "sessionId": "BBB"})
        )
        assert _wait_until(lambda: len(got) == 2), got
        assert [m["sessionId"] for m in got] == ["AAA", "BBB"]
        assert [m["params"]["requestId"] for m in got] == ["1", "2"]
    finally:
        client.stop()


def test_client_reconnects_with_backoff() -> None:
    attempts: list[int] = []
    live = FakeWs()

    def flaky(url: str, timeout: float) -> FakeWs:
        attempts.append(1)
        if len(attempts) <= 2:
            raise ConnectionError("boom")
        return live

    client = CdpClient("ws://127.0.0.1:1/x", ws_factory=flaky, initial_backoff=0.01, max_backoff=0.02)
    client.start(timeout=5.0)
    try:
        assert client.connected
        assert len(attempts) == 3
    finally:
        client.stop()


def test_client_send_without_connection_raises() -> None:
    client = CdpClient("ws://127.0.0.1:1/x", ws_factory=lambda url, timeout: FakeWs())
    with pytest.raises(CdpError, match="not connected"):
        client.send("Target.setAutoAttach", {"autoAttach": True})


# --- CdpClient по настоящему сокету: минимальный RFC6455-сервер ---


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _FakeCdpServer(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._listen = socket.socket()
        self._listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen.bind(("127.0.0.1", 0))
        self._listen.listen(1)
        self.port = self._listen.getsockname()[1]
        self.received: list[dict] = []
        self.ready = threading.Event()
        self.done = threading.Event()

    def _read_exact(self, conn: socket.socket, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = conn.recv(count - len(data))
            if not chunk:
                raise ConnectionError("closed")
            data += chunk
        return data

    def _read_frame(self, conn: socket.socket) -> str | None:
        head = self._read_exact(conn, 2)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", self._read_exact(conn, 2))
        key = self._read_exact(conn, 4) if masked else b""
        payload = self._read_exact(conn, length) if length else b""
        if masked:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        if opcode == 0x8:
            return None
        if opcode == 0x9:  # ping -> pong
            conn.sendall(b"\x8a\x00")
            return self._read_frame(conn)
        assert opcode == 0x1
        return payload.decode("utf-8")

    def _send_text(self, conn: socket.socket, text: str) -> None:
        payload = text.encode()
        if len(payload) < 126:
            conn.sendall(b"\x81" + bytes([len(payload)]) + payload)
        else:
            conn.sendall(b"\x81" + bytes([126]) + struct.pack(">H", len(payload)) + payload)

    def run(self) -> None:
        self.ready.set()
        conn, _ = self._listen.accept()
        with conn, self._listen:
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            key = [
                line.split(b": ", 1)[1].decode()
                for line in request.split(b"\r\n")
                if line.lower().startswith(b"sec-websocket-key")
            ][0]
            accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode()
            )
            while True:
                text = self._read_frame(conn)
                if text is None:
                    break
                message = json.loads(text)
                self.received.append(message)
                self._send_text(conn, json.dumps({"id": message["id"], "result": {}}))
            self.done.set()


def test_client_roundtrip_over_real_socket() -> None:
    server = _FakeCdpServer()
    server.start()
    assert server.ready.wait(timeout=2.0)
    client = CdpClient(f"ws://127.0.0.1:{server.port}/devtools/browser/live")
    client.start(timeout=5.0)
    try:
        assert client.send_command("Target.setAutoAttach", {"autoAttach": True}, timeout=5.0) == {}
        assert _wait_until(lambda: len(server.received) == 1)
        assert server.received[0]["method"] == "Target.setAutoAttach"
        assert server.received[0]["params"] == {"autoAttach": True}
    finally:
        client.stop()


# --- добивающие тесты после мутационного прогона ---


def test_read_devtools_active_port_boundary_ports_ok(tmp_path: Path) -> None:
    (tmp_path / "DevToolsActivePort").write_text("1\n/devtools/browser/min\n", encoding="utf-8")
    assert read_devtools_active_port(tmp_path) == (1, "/devtools/browser/min")
    (tmp_path / "DevToolsActivePort").write_text("65535\n/devtools/browser/max\n", encoding="utf-8")
    assert read_devtools_active_port(tmp_path) == (65535, "/devtools/browser/max")


def test_read_devtools_active_port_zero_port_rejected(tmp_path: Path) -> None:
    (tmp_path / "DevToolsActivePort").write_text("0\n/devtools/browser/zero\n", encoding="utf-8")
    with pytest.raises(CdpError, match="bad port"):
        read_devtools_active_port(tmp_path)


def test_read_devtools_active_port_unreadable(tmp_path: Path) -> None:
    (tmp_path / "DevToolsActivePort").mkdir()
    with pytest.raises(CdpError, match="unreadable"):
        read_devtools_active_port(tmp_path)


def test_ws_url_from_json_version_non_string_url(version_server: HTTPServer) -> None:
    _VersionHandler.payload = {"webSocketDebuggerUrl": 12345}
    with pytest.raises(CdpError, match="webSocketDebuggerUrl"):
        ws_url_from_json_version(version_server.server_address[1])


def test_ws_url_from_json_version_unreachable_mentions_cause() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(CdpError, match="URLError"):
        ws_url_from_json_version(free_port)


def test_resolve_without_sources_message() -> None:
    with pytest.raises(CdpError, match="^no DevTools source"):
        resolve_browser_ws_url()


def test_resolve_bad_debugger_address() -> None:
    with pytest.raises(CdpError, match="gave no DevTools endpoint"):
        resolve_browser_ws_url(debugger_address="127.0.0.1:notaport")


def test_resolve_uses_custom_host_for_json_version() -> None:
    from http.server import HTTPServer

    server = HTTPServer(("127.0.0.2", 0), _VersionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        _VersionHandler.payload = {
            "webSocketDebuggerUrl": f"ws://127.0.0.2:{port}/devtools/browser/alt"
        }
        assert (
            resolve_browser_ws_url(debugger_address=f"127.0.0.2:{port}", host="127.0.0.2")
            == f"ws://127.0.0.2:{port}/devtools/browser/alt"
        )
        assert (
            resolve_browser_ws_url(port=port, host="127.0.0.2")
            == f"ws://127.0.0.2:{port}/devtools/browser/alt"
        )
    finally:
        server.shutdown()


def test_resolve_respects_custom_host(tmp_path: Path) -> None:
    (tmp_path / "DevToolsActivePort").write_text("19233\n/devtools/browser/h\n", encoding="utf-8")
    assert (
        resolve_browser_ws_url(user_data_dir=tmp_path, host="10.9.9.9")
        == "ws://10.9.9.9:19233/devtools/browser/h"
    )


def test_send_includes_session_id() -> None:
    ws = FakeWs()
    client = _make_client(ws)
    client.start(timeout=2.0)
    try:
        client.send("Fetch.enable", {"handleAuthRequests": True}, session_id="S1")
        assert ws.sent_json() == [
            {
                "id": 1,
                "method": "Fetch.enable",
                "params": {"handleAuthRequests": True},
                "sessionId": "S1",
            }
        ]
    finally:
        client.stop()


def test_send_without_session_id_omits_field() -> None:
    ws = FakeWs()
    client = _make_client(ws)
    client.start(timeout=2.0)
    try:
        command_id = client.send("Target.setAutoAttach", {"autoAttach": True})
        assert command_id == 1
        assert ws.sent_json() == [
            {"id": 1, "method": "Target.setAutoAttach", "params": {"autoAttach": True}}
        ]
    finally:
        client.stop()


class _BreakingSendWs(FakeWs):
    def send(self, data: str) -> None:
        raise RuntimeError("pipe broken")


def test_send_failure_mentions_method_and_cause() -> None:
    client = CdpClient("ws://127.0.0.1:1/x", ws_factory=lambda url, timeout: _BreakingSendWs())
    client.start(timeout=2.0)
    try:
        with pytest.raises(CdpError, match=r"CDP send Target\.X failed: RuntimeError"):
            client.send("Target.X")
    finally:
        client.stop()


def test_send_command_forwards_session_id() -> None:
    ws = FakeWs()
    client = _make_client(ws)
    client.start(timeout=2.0)
    try:
        result_box: list[object] = []
        worker = threading.Thread(
            target=lambda: result_box.append(client.send_command("Target.ping", session_id="S7", timeout=2.0)),
            daemon=True,
        )
        worker.start()
        assert _wait_until(lambda: len(ws.sent_json()) == 1), ws.sent_json()
        request = ws.sent_json()[0]
        ws.incoming.put(json.dumps({"id": request["id"], "result": {}}))
        worker.join(timeout=2.0)
        assert result_box == [{}]
        assert request.get("sessionId") == "S7"
    finally:
        client.stop()


def _reply_with_error_when_sent(ws: FakeWs) -> None:
    """Ответить на ошибку CDP после того, как команда реально ушла в сокет.

    Ответ нельзя класть в очередь заранее: receive-поток мгновенно
    диспатчит его, а подписка на ответ появляется только после отправки.
    """

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        sent = ws.sent_json()
        if sent:
            ws.incoming.put(
                json.dumps({"id": sent[0]["id"], "error": {"code": -32000, "message": "nope"}})
            )
            return
        time.sleep(0.005)


def test_send_command_error_response_raises() -> None:
    ws = FakeWs()
    client = _make_client(ws)
    client.start(timeout=2.0)
    try:
        responder = threading.Thread(target=_reply_with_error_when_sent, args=(ws,), daemon=True)
        responder.start()
        with pytest.raises(CdpError, match="CDP err-cmd error"):
            client.send_command("err-cmd", timeout=2.0)
        responder.join(timeout=3.0)
    finally:
        client.stop()


def test_send_command_timeout() -> None:
    ws = FakeWs()
    client = _make_client(ws)
    client.start(timeout=2.0)
    try:
        with pytest.raises(CdpError, match="timed out"):
            client.send_command("Target.hang", timeout=0.2)
    finally:
        client.stop()


def test_dispatch_event_with_id_goes_to_handler() -> None:
    ws = FakeWs()
    client = _make_client(ws)
    client.start(timeout=2.0)
    try:
        got: list[dict] = []
        client.on("Fetch.authRequired", got.append)
        ws.incoming.put(
            json.dumps({"id": 999, "method": "Fetch.authRequired", "params": {"requestId": "1"}})
        )
        assert _wait_until(lambda: len(got) == 1), got
        assert got[0]["params"] == {"requestId": "1"}
    finally:
        client.stop()


def test_unknown_method_event_ignored_and_client_survives() -> None:
    ws = FakeWs()
    client = _make_client(ws)
    client.start(timeout=2.0)
    try:
        got: list[dict] = []
        client.on("Fetch.authRequired", got.append)
        ws.incoming.put(json.dumps({"method": "Nope.unknown", "params": {}}))
        ws.incoming.put(json.dumps({"method": "Fetch.authRequired", "params": {"requestId": "1"}}))
        assert _wait_until(lambda: len(got) == 1), got
    finally:
        client.stop()


def test_unsubscribe_stops_delivery() -> None:
    ws = FakeWs()
    client = _make_client(ws)
    client.start(timeout=2.0)
    try:
        got: list[dict] = []
        unsubscribe = client.on("Fetch.authRequired", got.append)
        unsubscribe()
        ws.incoming.put(json.dumps({"method": "Fetch.authRequired", "params": {"requestId": "1"}}))
        time.sleep(0.3)
        assert got == []
    finally:
        client.stop()


def test_start_timeout_raises() -> None:
    def never(url: str, timeout: float) -> FakeWs:
        raise ConnectionError("down")

    client = CdpClient("ws://127.0.0.1:1/x", ws_factory=never, initial_backoff=0.01, max_backoff=0.02)
    try:
        with pytest.raises(CdpError, match="timed out"):
            client.start(timeout=0.2)
    finally:
        client.stop()


def test_no_reconnect_churn_while_healthy() -> None:
    calls: list[str] = []
    ws = FakeWs()

    def counting(url: str, timeout: float) -> FakeWs:
        calls.append(url)
        return ws

    client = CdpClient("ws://127.0.0.1:1/x", ws_factory=counting)
    client.start(timeout=2.0)
    try:
        time.sleep(0.6)
        assert calls == ["ws://127.0.0.1:1/x"]
    finally:
        client.stop()


def test_backoff_grows_on_connect_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as time_module

    sleeps: list[float] = []
    monkeypatch.setattr(time_module, "sleep", sleeps.append)

    def never(url: str, timeout: float) -> FakeWs:
        raise ConnectionError("down")

    client = CdpClient(
        "ws://127.0.0.1:1/x", ws_factory=never, initial_backoff=0.05, max_backoff=10.0
    )
    try:
        with pytest.raises(CdpError, match="timed out"):
            client.start(timeout=0.5)
    finally:
        client.stop()
    assert sleeps[:3] == [0.05, 0.1, 0.2]


class _BreakingRecvWs(FakeWs):
    def __init__(self, calls: list[str]) -> None:
        super().__init__()
        self._calls = calls

    def recv(self, timeout: float | None = None) -> str:
        self._calls.append("recv")
        raise ConnectionError("lost")


class _FlakyRecvWs(FakeWs):
    """Сокет, который начинает рвать соединение по команде."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = False

    def recv(self, timeout: float | None = None) -> str:
        if self.fail:
            raise ConnectionError("lost")
        return super().recv(timeout)


def test_backoff_grows_on_recv_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as time_module

    ws = _FlakyRecvWs()
    factory_calls: list[str] = []

    def factory(url: str, timeout: float) -> FakeWs:
        # Первое подключение успешно (backoff сбрасывается), дальше — обрывы:
        # рост виден на стыке recv-ветви и factory-ветви.
        factory_calls.append(url)
        if len(factory_calls) == 1:
            return ws
        raise ConnectionError("down")

    client = CdpClient(
        "ws://127.0.0.1:1/x", ws_factory=factory, initial_backoff=0.05, max_backoff=10.0
    )
    client.start(timeout=2.0)
    try:
        assert client.connected
        sleeps: list[float] = []
        monkeypatch.setattr(time_module, "sleep", sleeps.append)
        ws.fail = True
        deadline = time.monotonic() + 2.0
        while len(sleeps) < 3 and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert sleeps[:3] == [0.05, 0.1, 0.2]
    finally:
        client.stop()


def test_client_recovers_after_recv_failure() -> None:
    factory_calls: list[str] = []

    def factory(url: str, timeout: float) -> _BreakingRecvWs:
        factory_calls.append(url)
        return _BreakingRecvWs(factory_calls)

    client = CdpClient(
        "ws://127.0.0.1:1/x", ws_factory=factory, initial_backoff=0.01, max_backoff=0.02
    )
    client.start(timeout=2.0)
    try:
        assert _wait_until(lambda: len(factory_calls) >= 2, timeout=3.0), factory_calls
    finally:
        client.stop()


def test_poll_timeout_does_not_kill_loop() -> None:
    ws = FakeWs()
    client = _make_client(ws)
    client.start(timeout=2.0)
    try:
        got: list[dict] = []
        client.on("Fetch.authRequired", got.append)
        time.sleep(0.7)
        ws.incoming.put(json.dumps({"method": "Fetch.authRequired", "params": {"requestId": "late"}}))
        assert _wait_until(lambda: len(got) == 1), got
    finally:
        client.stop()


def test_stop_without_start_is_safe() -> None:
    CdpClient("ws://127.0.0.1:1/x", ws_factory=lambda url, timeout: FakeWs()).stop()
