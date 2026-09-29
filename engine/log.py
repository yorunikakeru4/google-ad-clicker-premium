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
store, ни сломанное зеркало — наружу не выходит: счётчик ``dropped`` +
``last_error`` + fallback. То же правило действует на
:meth:`StructuredLogger.record_network_request`: запись метрики в
``network_requests`` из CDP-потока не может уронить воркер.

Dual-write
----------
Каждая запись дублируется в legacy stdlib-логгер из ``logger.py``: до фазы 9
файловый ротационный лог и консоль работают ровно как раньше, а таблица
``logs`` получает то же самое событие структурированно. Зеркало — необязательный
параметр конструктора (``mirror=``): его подставляет :func:`get_logger`, из
которого создан весь продакшн-код. Уровень зеркала совпадает с уровнем
записи, ``fields`` приклеиваются к сообщению в читаемом виде
(:func:`render_mirror_message`) и продублированы в ``extra`` под ключами
``category``/``fields``. ``exc_info`` уходит только в зеркало: в таблицу он не
попадает, там для него есть JSON-поля. Упало зеркало — наружу не выходит
(``last_error`` с пометкой «зеркало»), а запись в store при этом уже сделана.

Сообщение против полей
----------------------
f-строка раскладывается так: статическая часть становится ``message``,
изменяемые значения (url, задержки, коды ошибок, счётчики) уходят в
``fields`` и в message не дублируются. Вызов, где текста целиком нет
(сообщением шло одно исключение), получает статический контекст в
``message``, а исходная ошибка — в ``fields["error"]``. Исключение из
``quit()`` браузера оставлено сообщением: именно его ищут в файловом логе,
а тип уходит в отдельное поле.

Маппинг категорий (зафиксирован, новые вызовы выбирают из него)
---------------------------------------------------------------

===========================  ==================================================
Категория                    Что туда идёт
===========================  ==================================================
``proxy``                    прокси и сетные траблы: 407 и авторизация по CDP,
                             IP/гео-запросы по адресу прокси, boost-запросы
``browser``                  драйвер и браузер: создание и закрытие, профили,
                             user-agent, окна, cookie/cache, adb-устройства,
                             жесты на странице
``captcha``                  CAPTCHA: обнаружение, 2captcha, ответы сервиса
``click``                    клики и выбор: поисковый запрос, сбор и фильтрация
                             ссылок, клики, отчёты по кликам, Telegram-уведомления
                             о найденных объявлениях
``cleanup``                  очистка профилей и файлов
``scheduler``                цикл и расписание: паузы, интервалы, запуск
                             воркеров, этапы жизненного цикла сценария,
                             lifecycle Telegram-бота и точки входа без
                             собственного предмета
===========================  ==================================================

Хук выбирает категорию по своему предмету, а не по файлу: ``captcha_seen``
→ ``captcha``, ``*_ad_click``/``after_clicks`` → ``click``,
``*_browser_close`` → ``browser``, остальные этапы сценария → ``scheduler``.

Accessor
--------
:func:`get_logger` лениво открывает :class:`~engine.store.StoreWriter` на пути
из ``ADCLICKER_DB`` (дефолт ``adclicker.db`` в cwd), применяет миграции и
кэширует инстанс по пути: повторный вызов возвращает тот же объект. Ошибка
создания не роняет вызывающего — вместо writer'а подставляется заглушка,
которая бросает при записи, и логгер гасит это в ``dropped``/``last_error``.
"""

from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path
from typing import Any, Callable

from engine.db import migrations
from engine.store import StoreWriter

CATEGORIES = frozenset({"proxy", "browser", "captcha", "click", "cleanup", "scheduler"})
LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})

# Путь к БД: переменная окружения и имя файла по умолчанию.
DB_ENV_VAR = "ADCLICKER_DB"
DEFAULT_DB_NAME = "adclicker.db"

_UNREPRESENTABLE = "<unrepresentable>"

# Зеркало: (level, message, *, exc_info, extra) -> None.
Mirror = Callable[..., None]


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


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return _safe_repr(value)


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


def render_mirror_message(message: Any, fields: Any) -> str:
    """Сообщение с приклеенными полями — вид записи в legacy-файловом логе.

    Поля идут через тот же санитайзер, что и в таблицу, поэтому зеркало не
    может упасть на ядовитом значении и показывает ровно то, что ушло в БД.
    """

    text = _safe_str(message)
    if fields is None:
        return text
    try:
        safe = _safe_value(fields)
        if isinstance(safe, dict):
            rendered = " ".join(f"{key}={value!r}" for key, value in safe.items())
        else:
            rendered = repr(safe)
    except Exception as exc:
        rendered = f"{_UNREPRESENTABLE} ({type(exc).__name__})"
    if not rendered:
        return text
    return f"{text} {rendered}" if text else rendered


def legacy_mirror(
    level: str,
    message: str,
    *,
    exc_info: Any = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Дублирует одну запись в legacy stdlib-логгер из ``logger.py``.

    Импорт стоит внутри функции: ``logger.py`` при импорте создаёт каталог
    ``logs/`` в cwd, а это побочный эффект, который нужен только зеркалу.
    Ошибки отсюда не гасятся здесь намеренно — их ловит
    :meth:`StructuredLogger.log` и кладёт в ``last_error``, поэтому в модуле
    не появляется ни одного пустого ``except``.
    """

    from logger import logger as legacy_logger

    getattr(legacy_logger, level.lower())(message, exc_info=exc_info, extra=extra)


class _UnavailableStore:
    """Подмена StoreWriter, когда БД открыть не удалось.

    ``log()`` и ``flush()`` всегда бросают с текстом причины:
    :class:`StructuredLogger` превращает это в счётчик ``dropped`` и
    ``last_error``, поэтому вызывающий код продолжает работать, а потеря
    остаётся видимой.
    """

    def __init__(self, error: str) -> None:
        self._error = error

    def log(self, **kwargs: Any) -> None:
        raise RuntimeError(self._error)

    def record_network_request(self, **kwargs: Any) -> None:
        raise RuntimeError(self._error)

    def mark_degraded(self, **kwargs: Any) -> None:
        raise RuntimeError(self._error)

    def record_diagnostic(self, **kwargs: Any) -> None:
        raise RuntimeError(self._error)

    def record_captcha_event(self, **kwargs: Any) -> None:
        raise RuntimeError(self._error)

    def flush(self) -> None:
        raise RuntimeError(self._error)


class StructuredLogger:
    """Логгер, привязанный к воркеру. Потокобезопасен: блокировка покрывает
    счётчики и биндинг ``browser_id``, сама запись сериализуется внутри
    StoreWriter."""

    def __init__(
        self,
        store: Any,
        browser_id: str | None = None,
        mirror: Mirror | None = None,
    ):
        self._store = store
        self._browser_id = browser_id
        self._mirror = mirror
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

    @property
    def browser_id(self) -> str | None:
        """browser_id, подставленный в записи по умолчанию."""
        with self._lock:
            return self._browser_id

    def bind(self, browser_id: str | None) -> None:
        """Задать browser_id по умолчанию для последующих записей.

        Явный ``browser_id=`` в вызове методов логгирования перебивает
        биндинг. Зачем: ``ad_clicker --id 3`` узнаёт свой id после импорта
        модулей, а модули берут общий логгер из :func:`get_logger`.
        """

        with self._lock:
            self._browser_id = browser_id

    def flush(self) -> None:
        """Сбросить буфер в БД. Ошибки гасятся так же, как при записи."""

        try:
            self._store.flush()
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)

    def record_network_request(
        self,
        method: str,
        url: str,
        resource_type: str | None = None,
        status: int | None = None,
        browser_id: str | None = None,
        ts: float | None = None,
    ) -> None:
        """Записать строку ``network_requests`` в store процесса.

        Метрика, а не логовое событие: ``browser_id`` берётся из биндинга
        логгера (``browser_id=`` в вызове перебивает), в legacy-зеркало не
        дублируется — полный URL с query в файловый лог не попадает. Ошибки
        гасятся так же, как у :meth:`log`: счётчик ``dropped`` +
        ``last_error``, исключение наружу не выходит.
        """

        with self._lock:
            target = self._browser_id if browser_id is None else browser_id
        try:
            self._store.record_network_request(
                method=method,
                url=url,
                resource_type=resource_type,
                status=status,
                browser_id=target,
                ts=ts,
            )
        except Exception as exc:
            with self._lock:
                self._dropped += 1
                self._last_error = str(exc)

    def mark_degraded(self, reason: str, browser_id: str | None = None) -> None:
        """Пометить воркер деградировавшим в ``workers``.

        Сигнал живёт в таблице ``workers``, а не в ``logs``: супервизор и UI
        читают статус воркера, а не перебирают сообщения. ``browser_id``
        берётся из биндинга (``browser_id=`` в вызове перебивает), причина
        ``reason`` не должна содержать креды прокси — маскирование остаётся
        за вызывающим кодом. Без ``browser_id`` (CLI-прогон без ``--id``)
        строку некуда писать, поэтому вызов пропускается: WARNING о
        деградации вызывающий код делает сам. Ошибки гасятся так же, как у
        :meth:`record_network_request` — счётчик ``dropped`` + ``last_error``,
        исключение наружу не выходит.
        """
        with self._lock:
            target = self._browser_id if browser_id is None else browser_id
        if target is None:
            return
        try:
            self._store.mark_degraded(browser_id=target, reason=reason)
        except Exception as exc:
            with self._lock:
                self._dropped += 1
                self._last_error = str(exc)

    def record_diagnostic(self, browser_id: str | None = None, **fields: Any) -> None:
        """Записать снимок диагностики в ``diagnostics``.

        Тот же контракт, что у :meth:`mark_degraded`: ``browser_id`` берётся
        из биндинга (явный аргумент перебивает), запись немедленная и не
        зеркалируется в legacy-лог — снимок живёт в своей таблице, а не в
        ``logs``. Ошибки хранения гасятся счётчиком ``dropped`` и текстом в
        ``last_error``, исключение наружу не выходит.
        """
        with self._lock:
            target = self._browser_id if browser_id is None else browser_id
        if target is None:
            return
        try:
            self._store.record_diagnostic(browser_id=target, **fields)
        except Exception as exc:
            with self._lock:
                self._dropped += 1
                self._last_error = str(exc)

    def record_captcha_event(self, browser_id: str | None = None, **fields: Any) -> None:
        """Записать событие CAPTCHA в ``captcha_events``.

        Путь воркера тот же, что у :meth:`record_diagnostic`: общий логгер
        процесса, немедленная запись, ошибки в ``dropped``/``last_error``,
        без зеркалирования в legacy-лог (строка живёт в своей таблице).

        Отличие: ``browser_id`` опционален и NULL-строка не отбрасывается —
        событие CAPTCHA ценно само по себе (страница, sitekey, исход), а
        потеря из-за CLI-прогона без ``--id`` хуже пустой привязки.
        Явный аргумент, как обычно, перебивает биндинг.
        """
        with self._lock:
            target = self._browser_id if browser_id is None else browser_id
        try:
            self._store.record_captcha_event(browser_id=target, **fields)
        except Exception as exc:
            with self._lock:
                self._dropped += 1
                self._last_error = str(exc)

    def log(
        self,
        level: str,
        category: str,
        message: str,
        browser_id: str | None = None,
        fields: dict[str, Any] | None = None,
        exc_info: Any = None,
    ) -> None:
        if level not in LEVELS:
            raise ValueError(f"неизвестный уровень логгирования: {level!r}")
        if category not in CATEGORIES:
            raise ValueError(f"неизвестная категория лога: {category!r}")
        payload = encode_fields(fields)
        with self._lock:
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

        mirror = self._mirror
        if mirror is not None:
            try:
                mirror(
                    level,
                    render_mirror_message(message, fields),
                    exc_info=exc_info,
                    extra={"category": category, "fields": fields},
                )
            except Exception as exc:
                with self._lock:
                    self._last_error = f"зеркало в legacy-лог: {exc}"

    def debug(
        self,
        category: str,
        message: str,
        browser_id: str | None = None,
        fields: dict[str, Any] | None = None,
        exc_info: Any = None,
    ) -> None:
        self.log("DEBUG", category, message, browser_id=browser_id, fields=fields, exc_info=exc_info)

    def info(
        self,
        category: str,
        message: str,
        browser_id: str | None = None,
        fields: dict[str, Any] | None = None,
        exc_info: Any = None,
    ) -> None:
        self.log("INFO", category, message, browser_id=browser_id, fields=fields, exc_info=exc_info)

    def warning(
        self,
        category: str,
        message: str,
        browser_id: str | None = None,
        fields: dict[str, Any] | None = None,
        exc_info: Any = None,
    ) -> None:
        self.log(
            "WARNING", category, message, browser_id=browser_id, fields=fields, exc_info=exc_info
        )

    def error(
        self,
        category: str,
        message: str,
        browser_id: str | None = None,
        fields: dict[str, Any] | None = None,
        exc_info: Any = None,
    ) -> None:
        self.log("ERROR", category, message, browser_id=browser_id, fields=fields, exc_info=exc_info)


# --- accessor -------------------------------------------------------------

_registry_lock = threading.Lock()
_stores: dict[str, Any] = {}
_loggers: dict[str, StructuredLogger] = {}


def resolve_db_path() -> str:
    """Абсолютный путь БД логов: ``ADCLICKER_DB``, иначе ``adclicker.db`` в cwd.

    Путь резолвится на каждый вызов, чтобы ключ кэша совпадал с реально
    открытым файлом даже при относительном значении env и смене каталога.
    """

    raw = os.environ.get(DB_ENV_VAR) or DEFAULT_DB_NAME
    return str(Path(raw).expanduser().resolve())


def _open_store(path: str) -> Any:
    try:
        migrations.migrate(path)
        return StoreWriter(path)
    except Exception as exc:
        return _UnavailableStore(f"хранилище логов {path} недоступно: {exc}")


def get_logger(browser_id: str | None = None) -> StructuredLogger:
    """Общий логгер процесса, пишущий и в ``logs``, и в legacy-лог.

    Первый вызов на данном пути открывает StoreWriter и применяет миграции;
    последующие возвращают тот же инстанс (потокобезопасно). ``browser_id``
    перебиндовывает общий логгер — см. :meth:`StructuredLogger.bind`.
    Неудачное создание кэшируется вместе с инстансом: иначе объект был бы
    нестабильным, а причину всё равно несёт ``last_error`` первой записи.
    """

    path = resolve_db_path()
    with _registry_lock:
        store = _stores.get(path)
        if store is None:
            store = _open_store(path)
            _stores[path] = store
        logger = _loggers.get(path)
        if logger is None:
            logger = StructuredLogger(store, mirror=legacy_mirror)
            _loggers[path] = logger
        if browser_id is not None:
            logger.bind(browser_id)
        return logger
