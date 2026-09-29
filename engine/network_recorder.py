"""Корреляция CDP-событий Network.* в записи ``network_requests``.

Отдельный модуль — потому что это чистая логика: на входе словарь события,
на выходе готовая запись или ``None`` плюс счётчики. Ни сокетов, ни БД, ни
логгера здесь нет, поэтому коррелятор тестируется без браузера. Обвязка —
подписка на события, ``Network.enable``, запись через логгер процесса —
живёт в :mod:`engine.proxy_auth`.

Пара событий
------------
Браузер шлёт ``Network.requestWillBeSent`` (method, url, type) и позже
``Network.responseReceived`` (status) с одним и тем же ``requestId``. Запись
создаётся только на ответе: тогда известен ``status``, а ``ts`` берётся из
момента запроса — окно «запросы/час» должно отражать, когда запрос случился.

Почему ключ пары — ``(sessionId, requestId)``: ``requestId`` уникален внутри
сессии таргета, а сценарий открывает вкладку на каждый клик — две вкладки
могут выдать одинаковый id и перепутать URL'ы без привязки к сессии.

Что делает с «неправильным»:

- битое событие (не словарь, нет ``params``, ``requestId`` не строка, нет
  ``request``/``response``) → счётчик ``malformed``, никакого исключения;
- ответ на чужой/неизвестный ``requestId`` → ``unmatched_responses``;
- запрос без ответа остаётся в окне ожидания; окно ограничено
  ``max_pending`` (вытеснение старых → ``evicted``), а закрытие таргета
  снимает пары своей сессии через :meth:`drop_session`;
- ``Network.loadingFailed`` снимает ожидание без записи →
  ``failed_requests``: статуса не будет вовсе, а выдумывать его нельзя;
- событие чужого метода игнорируется молча — коррелятор его не обслуживает.

Открытый вопрос: запрос, ответа на который ещё нет (или не будет), строкой
не становится — ``status`` до ответа неизвестен, а писать NULL «впрок»
значило бы считать запрос дважды, когда ответ придёт.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

__all__ = ["NetworkRecord", "NetworkRecorder"]

# События, которые обслуживает коррелятор. Всё остальное — не его зона.
_CORRELATED_METHODS = frozenset(
    {"Network.requestWillBeSent", "Network.responseReceived", "Network.loadingFailed"}
)

_DEFAULT_MAX_PENDING = 4096


@dataclass(frozen=True)
class NetworkRecord:
    """Готовая строка ``network_requests``."""

    ts: float
    method: str
    url: str
    resource_type: str | None
    status: int | None


@dataclass(frozen=True)
class _PendingRequest:
    """Запрос, ждущий ответа, — ровно то, что нужно для записи."""

    ts: float
    method: str
    url: str
    resource_type: str | None


def _as_status(value: Any) -> int | None:
    """HTTP-статус из параметров события; нечисловое значение — None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


class NetworkRecorder:
    """Коррелятор ``requestWillBeSent``/``responseReceived`` по requestId.

    Не потокобезопасен намеренно: события приходят из одного receive-потока
    :class:`engine.cdp.CdpClient`. ``clock`` подменяется в тестах — время
    берётся один раз, на момент запроса.
    """

    def __init__(
        self,
        max_pending: int = _DEFAULT_MAX_PENDING,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if isinstance(max_pending, bool) or not isinstance(max_pending, int) or max_pending < 1:
            raise ValueError(f"max_pending должен быть целым >= 1, получено {max_pending!r}")
        self._clock = clock
        self._max_pending = max_pending
        self._pending: OrderedDict[tuple[Any, str], _PendingRequest] = OrderedDict()
        self._malformed = 0
        self._unmatched_responses = 0
        self._evicted = 0
        self._failed_requests = 0
        self._emitted = 0

    # --- счётчики (диагностика, их же смотрит обвязка) ---------------------

    @property
    def pending(self) -> int:
        """Сколько запросов сейчас ждут ответа."""
        return len(self._pending)

    @property
    def malformed(self) -> int:
        """События, которые не удалось разобрать."""
        return self._malformed

    @property
    def unmatched_responses(self) -> int:
        """Ответы (и только ответы) без запроса в окне ожидания."""
        return self._unmatched_responses

    @property
    def evicted(self) -> int:
        """Пары, вытесненные из окна: лимит памяти или закрытие сессии."""
        return self._evicted

    @property
    def failed_requests(self) -> int:
        """Запросы, снятые ``Network.loadingFailed`` без записи."""
        return self._failed_requests

    @property
    def emitted(self) -> int:
        """Сколько записей отдано наружу (включая промежуточные редиректы)."""
        return self._emitted

    @property
    def ignored(self) -> int:
        """События, которые должны были дать запись, но не разобрались."""
        return self._malformed + self._unmatched_responses

    # --- вход ------------------------------------------------------------

    def handle(self, message: Any) -> NetworkRecord | None:
        """Принять событие CDP, вернуть готовую запись или None.

        Никогда не бросает: всё, что не разобрано, уходит в счётчики.
        """
        if not isinstance(message, dict):
            self._malformed += 1
            return None
        method = message.get("method")
        if not isinstance(method, str):
            self._malformed += 1
            return None
        if method not in _CORRELATED_METHODS:
            return None
        params = message.get("params")
        if not isinstance(params, dict):
            self._malformed += 1
            return None
        request_id = params.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            self._malformed += 1
            return None
        key = (message.get("sessionId"), request_id)

        if method == "Network.requestWillBeSent":
            return self._on_request(key, params)
        if method == "Network.responseReceived":
            return self._on_response(key, params)
        return self._on_loading_failed(key)

    def drop_session(self, session_id: Any) -> int:
        """Снять все ожидающие пары сессии (таргет закрыт); возвращает число."""
        stale = [key for key in self._pending if key[0] == session_id]
        for key in stale:
            del self._pending[key]
        self._evicted += len(stale)
        return len(stale)

    # --- ветки событий ----------------------------------------------------

    def _on_request(
        self, key: tuple[Any, str], params: dict[str, Any]
    ) -> NetworkRecord | None:
        request = params.get("request")
        if not isinstance(request, dict):
            self._malformed += 1
            return None
        url = request.get("url")
        http_method = request.get("method")
        if not isinstance(url, str) or not isinstance(http_method, str):
            self._malformed += 1
            return None
        resource_type = params.get("type")
        if not isinstance(resource_type, str):
            resource_type = None

        # Повторный requestWillBeSent по тому же ключу — редирект: прошлый
        # хоп закрывается его redirectResponse, новая попытка уходит в окно.
        record = None
        previous = self._pending.pop(key, None)
        if previous is not None:
            redirect = params.get("redirectResponse")
            if isinstance(redirect, dict):
                record = NetworkRecord(
                    ts=previous.ts,
                    method=previous.method,
                    url=previous.url,
                    resource_type=previous.resource_type,
                    status=_as_status(redirect.get("status")),
                )
                self._emitted += 1
        self._pending[key] = _PendingRequest(
            ts=self._clock(),
            method=http_method,
            url=url,
            resource_type=resource_type,
        )
        self._evict_overflow()
        return record

    def _on_response(
        self, key: tuple[Any, str], params: dict[str, Any]
    ) -> NetworkRecord | None:
        response = params.get("response")
        if not isinstance(response, dict):
            self._malformed += 1
            return None
        pending = self._pending.pop(key, None)
        if pending is None:
            self._unmatched_responses += 1
            return None
        resource_type = pending.resource_type
        if resource_type is None:
            event_type = params.get("type")
            if isinstance(event_type, str):
                resource_type = event_type
        self._emitted += 1
        return NetworkRecord(
            ts=pending.ts,
            method=pending.method,
            url=pending.url,
            resource_type=resource_type,
            status=_as_status(response.get("status")),
        )

    def _on_loading_failed(self, key: tuple[Any, str]) -> None:
        if self._pending.pop(key, None) is not None:
            self._failed_requests += 1
        return None

    def _evict_overflow(self) -> None:
        while len(self._pending) > self._max_pending:
            self._pending.popitem(last=False)
            self._evicted += 1
