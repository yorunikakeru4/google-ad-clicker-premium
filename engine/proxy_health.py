"""Массовая проверка доступности прокси: HTTP ``CONNECT``, N потоков, таймаут.

Метод: соединение с прокси и запрос ``CONNECT scheme://host:port`` к цели.
Успех — статус ``200`` **от самого прокси** (туннель установлен), ``latency_ms``
— время от соединения до этого ответа. Заголовок ``Proxy-Authorization: Basic``
приставляется, только если у прокси есть креды: 407 от прокси означает
«креды не прошли», и это отдельная причина смерти, а не сетевая ошибка.

Три решения, которые стоит знать:

1. **Проверка никогда не бросает.** Любая ошибка — результат со строкой
   причины: таймаут, отказ соединения, 407, чужой ответ. Нити проверки
   падать не должны, иначе остальные прокси остались бы без результата.
2. **Текст ошибки без кредов.** Он уходит в ``proxies.last_error``, а оттуда
   в ``GET /control/proxies``, поэтому собирается из статуса и типа
   исключения, а не из значений ``username``/``password`` и не из строки
   запроса.
3. **Результат пишется по мере проверки.** Каждый ответ сразу фиксируется в
   БД (:meth:`ProxyPool.record_check_result`), поэтому ``GET`` показывает
   прогресс, а падение процесса посреди проверки не стирает уже
   измеренное. Политика ``fail_count`` и сброса описана там же.

Политика параллелизма: ``CHECK_THREADS`` потоков на весь пул, по одному
прокси на поток, общий пул потоков закрывается по завершении — проверка
мёртвого прокси занимает не больше ``CHECK_TIMEOUT_SECONDS``, а не сумму
таймаутов по всем прокси. Цель по умолчанию — ``DEFAULT_CHECK_TARGET``;
тесты подменяют её на loopback, и за пределы 127.0.0.1 тесты не ходят.
"""

from __future__ import annotations

import base64
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from engine.proxy_pool import ProxyError, ProxyPool

# Потоков на пул. 84 прокси при таймауте 5 с — около минуты худшего случая
# вместо семи последовательных; больше не нужно: идёт речь о локальных
# сокетах и коротких транзакциях в SQLite.
CHECK_THREADS = 8

# Таймаут одной проверки: соединение и ответ прокси делят его пополам по
# смыслу (сокет использует его и на connect, и на recv).
CHECK_TIMEOUT_SECONDS = 5.0

# Куда CONNECT-имся, чтобы понять, пропускает ли прокси трафик.
# Формат — scheme://host:port; в тестах подменяется на loopback.
DEFAULT_CHECK_TARGET = "https://www.google.com:443"

# Предел чтения статусной строки: прокси, приславший мегабайты мусора,
# не должен съедать память демона.
_MAX_STATUS_BYTES = 8192
_READ_CHUNK = 1024


class CheckInProgressError(ProxyError):
    """Проверка уже выполняется — повторный запуск невозможен (409)."""


@dataclass(frozen=True)
class CheckResult:
    """Результат одной проверки. ``error`` — без кредов, по построению."""

    proxy_id: int
    alive: bool
    latency_ms: int | None
    error: str | None


def connect_address(target: str) -> str:
    """``host:port`` из ``scheme://host:port`` — строка запроса CONNECT.

    Порт по умолчанию берётся из схемы (https → 443, http → 80): цель вида
    ``https://host`` обязана означать тот же порт, что и в браузере.
    """
    parts = urlsplit(target)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("цель проверки должна быть вида scheme://host:port")
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return f"{parts.hostname}:{port}"


def _authorization_header(username: Any, password: Any) -> str | None:
    if not username and not password:
        return None
    raw = f"{username or ''}:{password or ''}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _build_request(address: str, authorization: str | None) -> bytes:
    lines = [f"CONNECT {address} HTTP/1.1", f"Host: {address}"]
    if authorization is not None:
        lines.append(f"Proxy-Authorization: {authorization}")
    lines.append("")
    lines.append("")
    return "\r\n".join(lines).encode("utf-8")


def _recv_status_line(sock: socket.socket) -> bytes:
    buffer = b""
    while b"\r\n" not in buffer:
        chunk = sock.recv(_READ_CHUNK)
        if not chunk:
            break
        buffer += chunk
        if len(buffer) > _MAX_STATUS_BYTES:
            break
    return buffer.split(b"\r\n", 1)[0]


def _parse_status(status_line: bytes) -> int | None:
    """Код ответа; None — ответ не по HTTP (прокси должен начинаться с HTTP/)."""
    if not status_line:
        return None
    parts = status_line.split(b" ", 2)
    if len(parts) < 2 or not parts[0].upper().startswith(b"HTTP/"):
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def check_proxy(
    proxy: Mapping[str, Any],
    *,
    target: str = DEFAULT_CHECK_TARGET,
    timeout: float = CHECK_TIMEOUT_SECONDS,
) -> CheckResult:
    """Проверяет один прокси. Исходов ровно два: ``alive`` либо ``error``.

    ``proxy`` — строка ``proxies`` с полями ``id``, ``host``, ``port``,
    ``username``, ``password``. Неверная цель (``ValueError`` из
    :func:`connect_address`) — ошибка конфигурации, а не состояние прокси,
    поэтому она всплывает наружу, а не превращается в «мёртвый прокси».
    """
    proxy_id = proxy.get("id")
    address = connect_address(target)
    authorization = _authorization_header(proxy.get("username"), proxy.get("password"))
    started = time.monotonic()

    try:
        sock = socket.create_connection((str(proxy["host"]), int(proxy["port"])), timeout=timeout)
    except TimeoutError:
        return CheckResult(proxy_id, False, None, f"таймаут подключения к прокси ({timeout} с)")
    except ConnectionRefusedError:
        return CheckResult(proxy_id, False, None, "соединение отклонено прокси")
    except socket.gaierror:
        return CheckResult(proxy_id, False, None, "не удалось разрешить адрес прокси")
    except OSError as exc:
        return CheckResult(proxy_id, False, None, f"ошибка соединения: {type(exc).__name__}")

    try:
        with sock:
            sock.settimeout(timeout)
            sock.sendall(_build_request(address, authorization))
            status_line = _recv_status_line(sock)
    except TimeoutError:
        return CheckResult(proxy_id, False, None, f"таймаут ответа прокси ({timeout} с)")
    except ConnectionRefusedError:
        return CheckResult(proxy_id, False, None, "соединение отклонено прокси")
    except OSError as exc:
        return CheckResult(proxy_id, False, None, f"ошибка соединения: {type(exc).__name__}")

    status = _parse_status(status_line)
    if status is None:
        return CheckResult(proxy_id, False, None, "прокси ответил не по HTTP")

    latency_ms = int(round((time.monotonic() - started) * 1000))
    if status == 200:
        return CheckResult(proxy_id, True, latency_ms, None)
    if status == 407:
        return CheckResult(proxy_id, False, None, "прокси ответил 407: требуется авторизация")
    return CheckResult(proxy_id, False, None, f"прокси ответил {status}")


def run_check(
    pool: ProxyPool,
    *,
    target: str = DEFAULT_CHECK_TARGET,
    timeout: float = CHECK_TIMEOUT_SECONDS,
    threads: int = CHECK_THREADS,
) -> dict[str, int]:
    """Проверяет весь пул и возвращает счётчики ``checked/alive/dead``.

    Пул потоков живёт внутри вызова: наружу не утекают ни нить, ни
    незавершённая проверка. Результаты пишутся по мере поступления —
    прокси, проверенный первым, виден в ``GET`` сразу, не дожидаясь остальных.
    """
    proxies = pool.list_for_check()
    if not proxies:
        return {"checked": 0, "alive": 0, "dead": 0}

    workers = max(1, min(threads, len(proxies)))
    alive = 0
    dead = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="proxy-health") as executor:
        futures = [
            executor.submit(check_proxy, proxy, target=target, timeout=timeout)
            for proxy in proxies
        ]
        for future in as_completed(futures):
            result = future.result()
            pool.record_check_result(
                result.proxy_id,
                alive=result.alive,
                latency_ms=result.latency_ms,
                error=result.error,
            )
            if result.alive:
                alive += 1
            else:
                dead += 1
    return {"checked": alive + dead, "alive": alive, "dead": dead}


class ProxyHealthChecker:
    """Единственная активная проверка на процесс плюс фоновый запуск.

    Флаг, а не очередь: второй запуск во время проверки — 409
    ``check_in_progress`` из HTTP и ``CheckInProgressError`` здесь. Очередь
    из двух проверок означала бы, что пользователь нажал кнопку и получил
    ответ «начато», а на экране увидел результат прогона, который он не
    запускал.
    """

    def __init__(
        self,
        pool: ProxyPool,
        *,
        target: str = DEFAULT_CHECK_TARGET,
        timeout: float = CHECK_TIMEOUT_SECONDS,
        threads: int = CHECK_THREADS,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        connect_address(target)
        self.target = target
        self.timeout = timeout
        self.threads = threads
        self._pool = pool
        self._on_error = on_error
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self.last_summary: dict[str, int] | None = None
        self.last_error: str | None = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def start(self) -> None:
        """Запускает проверку в фоне и сразу возвращает управление."""
        with self._lock:
            if self._running:
                raise CheckInProgressError("проверка прокси уже выполняется")
            self._running = True
            thread = threading.Thread(
                target=self._run_background, name="proxy-health", daemon=True
            )
            self._thread = thread
            thread.start()

    def run(self) -> dict[str, int]:
        """Блокирующая проверка — для тестов и синхронных вызовов."""
        with self._lock:
            if self._running:
                raise CheckInProgressError("проверка прокси уже выполняется")
            self._running = True
        try:
            summary = self._execute()
        except Exception as exc:
            self._record_failure(exc)
            raise
        else:
            self.last_summary = summary
            self.last_error = None
            return summary
        finally:
            with self._lock:
                self._running = False

    def join(self, timeout: float | None = None) -> None:
        """Дожидается фоновой проверки (для тестов и остановки демона)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run_background(self) -> None:
        try:
            self.last_summary = self._execute()
        except Exception as exc:  # noqa: BLE001 - падение нити не должно теряться молча
            self._record_failure(exc)
        else:
            self.last_error = None
        finally:
            with self._lock:
                self._running = False

    def _execute(self) -> dict[str, int]:
        return run_check(
            self._pool, target=self.target, timeout=self.timeout, threads=self.threads
        )

    def _record_failure(self, exc: Exception) -> None:
        self.last_error = str(exc) or type(exc).__name__
        if self._on_error is not None:
            self._on_error(exc)
