"""Тонкий WebSocket-клиент Chrome DevTools Protocol.

Знает только транспорт: подключение к DevTools-endpoint, отправка команд,
приём событий с мультиплексированием по ``sessionId`` и переподключение
с backoff в фоновом потоке. Никакой логики авторизации прокси здесь нет —
она живёт в :mod:`engine.proxy_auth`.

Поиск DevTools-порта (порядок важен):
1. файл ``<user_data_dir>/DevToolsActivePort`` (строка 1 — порт, строка 2 — путь);
2. ``debugger_address`` вида ``host:port`` через ``/json/version``;
3. явный ``port`` через ``/json/version``.

Клиент никогда не пишет тела сообщений в лог: исходящий
``Fetch.continueWithAuth`` везёт пароль, поэтому в лог уходит только имя
метода. Сторона Selenium не блокируется: приём идёт в отдельном потоке.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

import websocket

from engine.log import get_logger

__all__ = [
    "CdpClient",
    "CdpError",
    "browser_ws_url_from_active_port",
    "read_devtools_active_port",
    "resolve_browser_ws_url",
    "ws_url_from_json_version",
]

log = get_logger()

_WS_TIMEOUT = 0.5
_MAX_PORT = 65535

# Фабрика сокета: (url, timeout) -> объект с send(str)/recv(timeout)/close().
WsFactory = Callable[[str, float], Any]


class CdpError(Exception):
    """Не удалось найти DevTools-порт, подключиться или отправить команду."""


def read_devtools_active_port(user_data_dir: str | Path) -> tuple[int, str]:
    """Читает ``DevToolsActivePort``: возвращает (порт, путь браузера).

    Raises:
        FileNotFoundError: файла нет — Chrome ещё не поднял DevTools.
        CdpError: файл битый (нет строк, мусор в порте, порт вне диапазона).
    """
    path = Path(user_data_dir) / "DevToolsActivePort"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise CdpError(f"DevToolsActivePort unreadable at {path}: {exc}") from exc
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[1].startswith("/"):
        raise CdpError(f"DevToolsActivePort malformed at {path}")
    try:
        port = int(lines[0])
    except ValueError:
        raise CdpError(f"DevToolsActivePort has bad port at {path}") from None
    if not 1 <= port <= _MAX_PORT:
        raise CdpError(f"DevToolsActivePort has bad port at {path}")
    return port, lines[1]


def browser_ws_url_from_active_port(port: int, browser_path: str, host: str = "127.0.0.1") -> str:
    """Собирает ``ws://`` URL браузера из данных DevToolsActivePort."""
    return f"ws://{host}:{port}{browser_path}"


def ws_url_from_json_version(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> str:
    """Забирает ``webSocketDebuggerUrl`` из ``/json/version`` по живому сокету.

    Raises:
        CdpError: endpoint недоступен, не JSON или нет поля
            ``webSocketDebuggerUrl``.
    """
    url = f"http://{host}:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except CdpError:
        raise
    except Exception as exc:
        raise CdpError(f"/json/version unreachable at {url}: {type(exc).__name__}") from exc
    if not isinstance(payload, dict) or not payload.get("webSocketDebuggerUrl"):
        raise CdpError(f"/json/version has no webSocketDebuggerUrl at {url}")
    ws_url = payload["webSocketDebuggerUrl"]
    if not isinstance(ws_url, str) or not ws_url.startswith("ws://"):
        raise CdpError(f"/json/version has no webSocketDebuggerUrl at {url}")
    return ws_url


def resolve_browser_ws_url(
    user_data_dir: str | Path | None = None,
    debugger_address: str | None = None,
    port: int | None = None,
    host: str = "127.0.0.1",
) -> str:
    """Находит ws-URL браузера: сначала файл, потом debugger_address, потом порт."""
    if user_data_dir is not None:
        try:
            found_port, browser_path = read_devtools_active_port(user_data_dir)
        except FileNotFoundError:
            pass
        else:
            return browser_ws_url_from_active_port(found_port, browser_path, host)
    if debugger_address:
        endpoint_port = debugger_address.rsplit(":", 1)[-1]
        try:
            return ws_url_from_json_version(int(endpoint_port), host)
        except (ValueError, CdpError) as exc:
            raise CdpError(f"debugger_address {debugger_address!r} gave no DevTools endpoint") from exc
    if port is not None:
        return ws_url_from_json_version(port, host)
    raise CdpError("no DevTools source: need user_data_dir, debugger_address or port")


def _default_ws_factory(url: str, timeout: float) -> Any:
    return websocket.create_connection(url, timeout=timeout)


class CdpClient:
    """CDP-клиент поверх WebSocket с фоновым приёмом и переподключением.

    Отправка потокобезопасна, приём идёт в отдельном daemon-потоке и
    раздаётся подписчикам :meth:`on` по имени метода; ``sessionId`` из
    сообщения сохраняется как есть, поэтому события из разных таргетов
    (вкладок) мультиплексируются одним обработчиком.

    ``start()`` блокируется до первого подключения (или ``timeout``):
    вызывающий код после него может сразу слать команды. Дальнейшие
    обрывы переживаются внутри: backoff от ``initial_backoff`` с удвоением
    до ``max_backoff``, счётчик сбрасывается после успешного подключения.
    """

    def __init__(
        self,
        url: str,
        ws_factory: WsFactory | None = None,
        reconnect: bool = True,
        initial_backoff: float = 0.2,
        max_backoff: float = 5.0,
        timeout: float = 5.0,
    ) -> None:
        self._url = url
        self._ws_factory = ws_factory or _default_ws_factory
        self._reconnect = reconnect
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._send_lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._handlers: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
        self._handlers_lock = threading.Lock()
        self._ws: Any | None = None
        self._ws_lock = threading.Lock()
        self._running = False
        self._connected = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return self._url

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self, timeout: float = 10.0) -> CdpClient:
        """Запускает фоновый поток и ждёт первого подключения.

        Raises:
            CdpError: подключиться за ``timeout`` не удалось.
        """
        if self._running:
            return self
        self._running = True
        self._thread = threading.Thread(target=self._run, name="cdp-recv", daemon=True)
        self._thread.start()
        if not self._connected.wait(timeout=timeout):
            self.stop()
            raise CdpError(f"CDP connect timed out after {timeout}s")
        return self

    def stop(self) -> None:
        """Гасит фоновый поток и закрывает сокет. Идемпотентно."""
        self._running = False
        self._drop_connection()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def on(self, method: str, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """Подписывается на CDP-событие; возвращает функцию отписки."""
        with self._handlers_lock:
            self._handlers.setdefault(method, []).append(callback)

        def unsubscribe() -> None:
            with self._handlers_lock:
                callbacks = self._handlers.get(method, [])
                if callback in callbacks:
                    callbacks.remove(callback)

        return unsubscribe

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> int:
        """Отправляет команду без ожидания ответа; возвращает её id."""
        message: dict[str, Any] = {"id": next(self._ids), "method": method}
        if params is not None:
            message["params"] = params
        if session_id is not None:
            message["sessionId"] = session_id
        with self._send_lock:
            ws = self._ws
            if ws is None:
                raise CdpError(f"CDP not connected, cannot send {method}")
            # Лог до отправки и снаружи окна «отправлено → подписан на ответ»:
            # запись в store и зеркало занимают время, а send_command ставит
            # обработчик только после возврата отсюда. Всё, что стоит между
            # ws.send() и этой подпиской, — это окно, в которое быстрый ответ
            # CDP приходит раньше регистрации и теряется.
            log.debug("browser", "CDP ->", fields={"method": method})
            try:
                ws.send(json.dumps(message))
            except Exception as exc:
                self._drop_connection()
                raise CdpError(f"CDP send {method} failed: {type(exc).__name__}") from exc
        return message["id"]

    def send_command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float = 5.0,
    ) -> Any:
        """Отправляет команду и ждёт ответа с тем же id; возвращает ``result``.

        Raises:
            CdpError: нет подключения, ответ с ``error`` или истёк ``timeout``.
        """
        event = threading.Event()
        box: dict[str, Any] = {}
        command_id = self.send(method, params, session_id)
        with self._pending_lock:
            self._pending[command_id] = (event, box)
        try:
            if not event.wait(timeout=timeout):
                raise CdpError(f"CDP {method} timed out after {timeout}s")
        finally:
            with self._pending_lock:
                self._pending.pop(command_id, None)
        if "error" in box:
            raise CdpError(f"CDP {method} error: {box['error']}")
        return box.get("result")

    def _drop_connection(self) -> None:
        with self._ws_lock:
            ws, self._ws = self._ws, None
        self._connected.clear()
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _run(self) -> None:
        backoff = self._initial_backoff
        while self._running:
            ws = self._ws
            if ws is None:
                try:
                    ws = self._ws_factory(self._url, self._timeout)
                except Exception:
                    log.debug(
                        "browser", "CDP connect failed", fields={"retry_in_s": round(backoff, 2)}
                    )
                    time.sleep(backoff)
                    backoff = min(backoff * 2, self._max_backoff)
                    if not self._reconnect:
                        self._running = False
                        return
                    continue
                with self._ws_lock:
                    self._ws = ws
                backoff = self._initial_backoff
                self._connected.set()
                # websocket-client не принимает timeout в recv(): таймаут
                # ставится на сокет, а recv() вызывается без аргументов.
                # Поддельные сокеты в тестах без settimeout получают timeout
                # аргументом recv(timeout=...).
                use_recv_timeout_arg = not hasattr(ws, "settimeout")
                if not use_recv_timeout_arg:
                    try:
                        ws.settimeout(_WS_TIMEOUT)
                    except Exception:
                        use_recv_timeout_arg = True
            try:
                raw = ws.recv(timeout=_WS_TIMEOUT) if use_recv_timeout_arg else ws.recv()
            except (TimeoutError, websocket.WebSocketTimeoutException):
                continue
            except Exception:
                log.debug("browser", "CDP connection lost, reconnecting")
                self._drop_connection()
                if not self._reconnect:
                    self._running = False
                    return
                time.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                continue
            self._dispatch(raw)

    def _dispatch(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            log.debug("browser", "CDP ignoring non-JSON frame")
            return
        if not isinstance(message, dict):
            return
        if "method" not in message and "id" in message:
            with self._pending_lock:
                pending = self._pending.get(message["id"])
            if pending is not None:
                event, box = pending
                box.update(message)
                event.set()
            return
        method = message.get("method")
        if not isinstance(method, str):
            return
        with self._handlers_lock:
            callbacks = list(self._handlers.get(method, ()))
        for callback in callbacks:
            try:
                callback(message)
            except Exception:
                log.debug("browser", "CDP handler failed", fields={"method": method})

    def __enter__(self) -> CdpClient:
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()
