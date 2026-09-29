"""Структурированный логгер поверх StoreWriter.

Категории и уровни — закрытый словарь: неизвестно откуда взявшаяся категория
уже не отфильтруется в UI и не соберётся в агрегаты, поэтому молча принимать
её нельзя. Неизвестные категория/уровень — ошибка программирования и
проверяются сразу исключением: так она всплывает в разработке и в тестах,
а не в проде в виде мусора в таблице.

Это сочетается с железным правилом «логгер никогда не бросает исключение»
так: правило покрывает путь логгирования во время работы (сериализация
полей, запись в store), а не misuse API. После того как аргументы прошли
проверку словаря, ничто внутри — ни ядовитые значения в fields, ни упавший
store — наружу не выходит: счётчик ``dropped`` + ``last_error`` + fallback.
"""

from __future__ import annotations

import json
import math
import threading
from typing import Any

from engine.store import StoreWriter

CATEGORIES = frozenset({"proxy", "browser", "captcha", "click", "cleanup", "scheduler"})
LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})

_UNREPRESENTABLE = "<unrepresentable>"


def _safe_key(key: Any) -> str:
    if isinstance(key, str):
        return key
    try:
        return str(key)
    except Exception:
        return _UNREPRESENTABLE


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:
        return f"{_UNREPRESENTABLE} {type(value).__name__}"


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # NaN/Inf — не валидный JSON для читателя на другой стороне (UI),
        # поэтому только конечные числа идут как есть.
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, dict):
        return {_safe_key(key): _safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return _safe_repr(value)


def encode_fields(fields: Any) -> str | None:
    """fields в JSON-строку для колонки logs.fields. Тотальная функция."""
    if fields is None:
        return None
    safe = _safe_value(fields)
    try:
        return json.dumps(safe, ensure_ascii=False, sort_keys=True)
    except Exception:
        # Санитайзер выше обязан выдавать сериализуемое; эта ветка —
        # страховка, а не основной путь: фиксируем факт урезания прямо
        # в payload, чтобы потеря была видна, а не молчаливой.
        return json.dumps({"_fields_error": _safe_repr(fields)}, ensure_ascii=False)


class StructuredLogger:
    """Логгер, привязанный к воркеру. Потокобезопасен: блокировка покрывает
    только счётчики, сама запись сериализуется внутри StoreWriter."""

    def __init__(self, store: StoreWriter, browser_id: str | None = None):
        self._store = store
        self._browser_id = browser_id
        self._lock = threading.Lock()
        self._dropped = 0
        self._last_error: str | None = None

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def log(
        self,
        level: str,
        category: str,
        message: str,
        browser_id: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        if level not in LEVELS:
            raise ValueError(f"неизвестный уровень логгирования: {level!r}")
        if category not in CATEGORIES:
            raise ValueError(f"неизвестная категория лога: {category!r}")
        payload = encode_fields(fields)
        target = self._browser_id if browser_id is None else browser_id
        try:
            self._store.log(
                level=level,
                category=category,
                message=message,
                browser_id=target,
                fields=payload,
            )
        except Exception as exc:
            with self._lock:
                self._dropped += 1
                self._last_error = str(exc)

    def debug(
        self,
        category: str,
        message: str,
        browser_id: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        self.log("DEBUG", category, message, browser_id=browser_id, fields=fields)

    def info(
        self,
        category: str,
        message: str,
        browser_id: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        self.log("INFO", category, message, browser_id=browser_id, fields=fields)

    def warning(
        self,
        category: str,
        message: str,
        browser_id: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        self.log("WARNING", category, message, browser_id=browser_id, fields=fields)

    def error(
        self,
        category: str,
        message: str,
        browser_id: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        self.log("ERROR", category, message, browser_id=browser_id, fields=fields)
