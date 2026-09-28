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
