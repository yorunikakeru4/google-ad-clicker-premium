"""Авторизация прокси через Chrome DevTools Protocol.

Зачем: Chrome 142+ запрещает ``--load-extension`` для установки
прокси-расширения с кредами, поэтому логин/пароль отдаём через CDP.
Chrome ходит на настоящий прокси по ``--proxy-server=host:port`` без кредов,
прокси отвечает ``407``, Chrome шлёт ``Fetch.authRequired`` — отвечаем
``Fetch.continueWithAuth`` с ``ProvideCredentials``.

Модуль не знает про Selenium: на входе готовый DevTools-порт (или user_data_dir,
или ws-URL), поэтому всё тестируется без браузера. Креды никогда не попадают
в логи и в тексты исключений — маскирование то же, что в ``webdriver.py``:
первые/последние 3 символа, остальное ``***``, короткие значения — ``***``.

Помимо авторизации, менеджер ведёт метрику «запросы/час» (план.md, фаза 4):
на старте и на каждой новой сессии таргета уходит ``Network.enable``, пары
событий ``Network.*`` коррелирует :class:`engine.network_recorder.NetworkRecorder`
и готовые строки уходят в ``network_requests`` через общий логгер процесса —
``browser_id`` воркера, тот же writer, что и у логов, без нового соединения.
Запись без ответа не создаётся (статус до ответа неизвестен), а разобрать
событие не удалось — счётчики коррелятора плюс debug-лог без URL.

Три транспорта прокси (``resolve_proxy_transport``):
``cdp_auth`` (по умолчанию) | ``extension`` | ``direct``.

Подсчёт неудач: повторный ``Fetch.authRequired`` с тем же ``requestId``
означает, что прокси креды не принял (приём как в Playwright). Такой повтор
отвечаем ``CancelAuth`` и растим ``fail_count``; при ``fail_count >=
max_failures`` прокси считается мёртвым и один раз вызывается ``on_proxy_dead``.
Чужая схема (не Basic/Digest) и чужой источник (не Proxy) — тоже ``CancelAuth``,
но без вины прокси, счётчик не растёт.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from engine.cdp import CdpClient, resolve_browser_ws_url
from engine.log import get_logger
from engine.network_recorder import NetworkRecorder

__all__ = [
    "DEFAULT_PROXY_TRANSPORT",
    "PROXY_TRANSPORT_CDP_AUTH",
    "PROXY_TRANSPORT_DIRECT",
    "PROXY_TRANSPORT_EXTENSION",
    "PROXY_TRANSPORTS",
    "ProxyAuthManager",
    "ProxyCredentials",
    "create_proxy_auth",
    "mask_secret",
    "parse_proxy_credentials",
    "resolve_proxy_transport",
    "start_proxy_auth",
]

log = get_logger()

PROXY_TRANSPORT_CDP_AUTH = "cdp_auth"
PROXY_TRANSPORT_EXTENSION = "extension"
PROXY_TRANSPORT_DIRECT = "direct"
DEFAULT_PROXY_TRANSPORT = PROXY_TRANSPORT_CDP_AUTH
PROXY_TRANSPORTS = frozenset({PROXY_TRANSPORT_CDP_AUTH, PROXY_TRANSPORT_EXTENSION, PROXY_TRANSPORT_DIRECT})

# Схемы, для которых CDP умеет отдать логин/пароль.
_SUPPORTED_SCHEMES = frozenset({"Basic", "Digest"})


def mask_secret(value: str) -> str:
    """Маскирует секрет: длинные — крайние тройки, короткие — ``***``."""
    if not isinstance(value, str) or len(value) <= 6:
        return "***"
    return f"{value[:3]}***{value[-3:]}"


@dataclass(frozen=True)
class ProxyCredentials:
    """Логин/пароль прокси. ``repr`` замаскирован: секретов наружу нет."""

    username: str
    password: str

    def __repr__(self) -> str:
        return f"ProxyCredentials(username={mask_secret(self.username)!r}, password='***')"


def parse_proxy_credentials(proxy: str) -> tuple[str, str]:
    """Разбирает ``user:pass@host:port`` на (user, pass).

    Текст ошибки никогда не содержит входную строку — иначе пароль утёк
    бы в логи вызывающего кода вместе с исключением.
    """
    user_host: list[str] = []
    if isinstance(proxy, str) and proxy.count("@") == 1 and proxy.count(":") >= 2:
        user_host = proxy.split("@")
    if len(user_host) != 2 or ":" not in user_host[0]:
        raise ValueError("invalid proxy format, expected user:password@host:port")
    username, _, password = user_host[0].partition(":")
    host_port = user_host[1]
    if not username or not password or host_port.count(":") != 1:
        raise ValueError("invalid proxy format, expected user:password@host:port")
    host, _, port = host_port.partition(":")
    if not host or not port:
        raise ValueError("invalid proxy format, expected user:password@host:port")
    return username, password


def resolve_proxy_transport(value: str | None) -> str:
    """Нормализует ``webdriver.proxy_transport``; пусто — default ``cdp_auth``."""
    if not value:
        return DEFAULT_PROXY_TRANSPORT
    if value not in PROXY_TRANSPORTS:
        raise ValueError(f"unknown proxy_transport {value!r}, expected one of {sorted(PROXY_TRANSPORTS)}")
    return value


def _attached_session_id(message: Any) -> str | None:
    """``sessionId`` из Target.attachedToTarget/detachedFromTarget; None, если бито."""
    if not isinstance(message, dict):
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    session_id = params.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return None
    return session_id


class ProxyAuthManager:
    """Авторизация прокси по CDP плюс запись метрики сетевых запросов.

    ``start()`` обязана слать ``Target.setAutoAttach`` (иначе не ловятся
    вызовы из новых вкладок — а сценарий открывает вкладку на каждый клик)
    и ``Fetch.enable`` строго без ``patterns``: с ``patterns`` CDP начнёт
    перехватывать все запросы и требовать ``Fetch.continueRequest`` на каждый,
    вкладки зависнут. Там же отдельной командой включается ``Network.enable``:
    это и есть включение метрики «запросы/час».

    ``Target.attachedToTarget`` обрабатывается так же — новая сессия таргета
    получает и ``Fetch.enable`` (без patterns, как требует план.md §2.1), и
    ``Network.enable``: домены включаются per-session, события других
    сессий на неё не приходят. Закрытие сессии снимает её незавершённые
    запросы из окна ожидания коррелятора.
    """

    def __init__(
        self,
        client: CdpClient,
        username: str,
        password: str,
        max_failures: int = 3,
        on_proxy_dead: Callable[[], None] | None = None,
        owns_client: bool = False,
    ) -> None:
        if max_failures < 1:
            raise ValueError("max_failures must be >= 1")
        self._client = client
        self._username = username
        self._password = password
        self._max_failures = max_failures
        self._on_proxy_dead = on_proxy_dead
        self._owns_client = owns_client
        self._answered: set[str] = set()
        self._fail_count = 0
        self._dead = False
        self._dead_notified = False
        self._started = False
        self._unsubscribes: list[Callable[[], None]] = []
        self._recorder = NetworkRecorder()

    @property
    def fail_count(self) -> int:
        return self._fail_count

    @property
    def dead(self) -> bool:
        return self._dead

    @property
    def recorder(self) -> NetworkRecorder:
        """Коррелятор Network-событий; счётчики — для диагностики и тестов."""
        return self._recorder

    def start(self) -> ProxyAuthManager:
        """Подписывается на события, включает auto-attach, Fetch и Network."""
        if self._started:
            return self
        self._unsubscribes = [
            self._client.on("Fetch.authRequired", self.handle_event),
            self._client.on("Target.attachedToTarget", self.handle_attached),
            self._client.on("Target.detachedFromTarget", self.handle_detached),
            self._client.on("Network.requestWillBeSent", self.handle_network_event),
            self._client.on("Network.responseReceived", self.handle_network_event),
            self._client.on("Network.loadingFailed", self.handle_network_event),
        ]
        self._client.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )
        # Без patterns: иначе CDP перехватит весь трафик и вкладки встанут.
        self._client.send("Fetch.enable", {"handleAuthRequests": True})
        # Отдельной командой рядом с Fetch — включение метрики запросов/час.
        self._client.send("Network.enable")
        self._started = True
        log.info("proxy", "proxy CDP auth enabled", fields={"user": mask_secret(self._username)})
        log.debug("browser", "network metrics enabled")
        return self

    def stop(self) -> None:
        """Отписывается; свой клиент (из create_proxy_auth) ещё и закрывает."""
        for unsubscribe in self._unsubscribes:
            unsubscribe()
        self._unsubscribes = []
        self._started = False
        if self._owns_client:
            self._client.stop()

    def handle_event(self, message: dict[str, Any]) -> None:
        """Точка входа для событий CDP; посторонние методы игнорируются."""
        if not isinstance(message, dict) or message.get("method") != "Fetch.authRequired":
            return
        params = message.get("params")
        if not isinstance(params, dict):
            return
        self._answer_auth_challenge(params)

    def handle_attached(self, message: dict[str, Any]) -> None:
        """Новая сессия таргета: та же пара Fetch + Network, что и на старте."""
        session_id = _attached_session_id(message)
        if session_id is None:
            log.debug("proxy", "attachedToTarget without sessionId, ignoring")
            return
        # Fetch — тот же контракт §2.1 (handleAuthRequests, без patterns),
        # Network.enable — события этой сессии для метрики запросов/час.
        self._client.send(
            "Fetch.enable", {"handleAuthRequests": True}, session_id=session_id
        )
        self._client.send("Network.enable", session_id=session_id)

    def handle_detached(self, message: dict[str, Any]) -> None:
        """Сессия закрыта: её незавершённые запросы больше не дождутся ответа."""
        session_id = _attached_session_id(message)
        if session_id is None:
            return
        dropped = self._recorder.drop_session(session_id)
        if dropped:
            log.debug(
                "browser",
                "network requests dropped with closed session",
                fields={"dropped": dropped},
            )

    def handle_network_event(self, message: dict[str, Any]) -> None:
        """Коррелирует событие Network.* и пишет готовую запись в store."""
        recorder = self._recorder
        ignored_before = recorder.ignored
        record = recorder.handle(message)
        if record is not None:
            # browser_id подставляет биндинг общего логгера воркера; зеркала
            # нет — полный URL с query в файловый лог не уходит.
            log.record_network_request(
                method=record.method,
                url=record.url,
                resource_type=record.resource_type,
                status=record.status,
                ts=record.ts,
            )
            return
        if recorder.ignored > ignored_before:
            # Событие должно было дать запись, но разобрать его не удалось.
            # URL сознательно не логируется: это счётчик, а не отчёт.
            event = message.get("method") if isinstance(message, dict) else None
            log.debug(
                "browser",
                "network event dropped",
                fields={
                    "event": event if isinstance(event, str) else None,
                    "malformed": recorder.malformed,
                    "unmatched": recorder.unmatched_responses,
                },
            )

    def _answer_auth_challenge(self, params: dict[str, Any]) -> None:
        request_id = params.get("requestId")
        challenge = params.get("authChallenge")
        if not isinstance(request_id, str) or not request_id:
            log.debug("proxy", "Fetch.authRequired without requestId, ignoring")
            return
        if not isinstance(challenge, dict):
            challenge = {}
        source = challenge.get("source")
        scheme = challenge.get("scheme")
        if source != "Proxy" or scheme not in _SUPPORTED_SCHEMES:
            log.debug(
                "proxy",
                "non-proxy auth challenge, cancelling",
                fields={"source": source, "scheme": scheme},
            )
            self._cancel(request_id)
            return
        if request_id in self._answered:
            log.info("proxy", "proxy auth rejected for request, cancelling")
            self._cancel(request_id)
            self._register_failure()
            return
        self._answered.add(request_id)
        self._client.send(
            "Fetch.continueWithAuth",
            {
                "requestId": request_id,
                "authChallengeResponse": {
                    "response": "ProvideCredentials",
                    "username": self._username,
                    "password": self._password,
                },
            },
        )
        log.debug("proxy", "answered proxy auth challenge")

    def _cancel(self, request_id: str) -> None:
        self._client.send(
            "Fetch.continueWithAuth",
            {"requestId": request_id, "authChallengeResponse": {"response": "CancelAuth"}},
        )

    def _register_failure(self) -> None:
        self._fail_count += 1
        if self._fail_count >= self._max_failures and not self._dead:
            self._dead = True
        if self._dead and not self._dead_notified:
            self._dead_notified = True
            log.warning(
                "proxy",
                "proxy marked dead after auth failures",
                fields={"fail_count": self._fail_count},
            )
            if self._on_proxy_dead is not None:
                self._on_proxy_dead()

    def __enter__(self) -> ProxyAuthManager:
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()


def create_proxy_auth(
    username: str,
    password: str,
    ws_url: str | None = None,
    devtools_port: int | None = None,
    user_data_dir: str | Path | None = None,
    debugger_address: str | None = None,
    max_failures: int = 3,
    on_proxy_dead: Callable[[], None] | None = None,
    client_factory: Callable[..., CdpClient] = CdpClient,
    start_timeout: float = 10.0,
) -> ProxyAuthManager:
    """Строит клиент по готовому DevTools-порту и запускает авторизацию.

    Приоритет источника ws-URL: явный ``ws_url`` > ``user_data_dir`` >
    ``debugger_address`` > ``devtools_port``. Возвращённый менеджер владеет
    клиентом: ``stop()`` (или выход из ``with``) гасит и подписку, и сокет —
    вызывать это надо в том же ``finally``, что закрывает браузер.
    """
    if ws_url is None:
        if user_data_dir is not None:
            ws_url = resolve_browser_ws_url(user_data_dir=user_data_dir)
        elif debugger_address is not None:
            ws_url = resolve_browser_ws_url(debugger_address=debugger_address)
        elif devtools_port is not None:
            ws_url = resolve_browser_ws_url(port=devtools_port)
        else:
            raise ValueError("need ws_url, user_data_dir, debugger_address or devtools_port")
    client = client_factory(ws_url)
    client.start(timeout=start_timeout)
    manager = ProxyAuthManager(
        client,
        username,
        password,
        max_failures=max_failures,
        on_proxy_dead=on_proxy_dead,
        owns_client=True,
    )
    try:
        manager.start()
    except Exception:
        client.stop()
        raise
    return manager


start_proxy_auth = create_proxy_auth
