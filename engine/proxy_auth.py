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


class ProxyAuthManager:
    """Отвечает на ``Fetch.authRequired`` кредами прокси через готовый CdpClient.

    ``start()`` обязана слать ``Target.setAutoAttach`` (иначе не ловятся
    вызовы из новых вкладок — а сценарий открывает вкладку на каждый клик)
    и ``Fetch.enable`` строго без ``patterns``: с ``patterns`` CDP начнёт
    перехватывать все запросы и требовать ``Fetch.continueRequest`` на каждый,
    вкладки зависнут.
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
        self._unsubscribe: Callable[[], None] | None = None

    @property
    def fail_count(self) -> int:
        return self._fail_count

    @property
    def dead(self) -> bool:
        return self._dead

    def start(self) -> ProxyAuthManager:
        """Подписывается на authRequired, включает auto-attach и Fetch-домен."""
        if self._started:
            return self
        self._unsubscribe = self._client.on("Fetch.authRequired", self.handle_event)
        self._client.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )
        # Без patterns: иначе CDP перехватит весь трафик и вкладки встанут.
        self._client.send("Fetch.enable", {"handleAuthRequests": True})
        self._started = True
        log.info("proxy", "proxy CDP auth enabled", fields={"user": mask_secret(self._username)})
        return self

    def stop(self) -> None:
        """Отписывается; свой клиент (из create_proxy_auth) ещё и закрывает."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
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
