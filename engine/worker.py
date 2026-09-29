"""Воркер: один процесс на браузер, крутящий сценарии в цикле.

Единственная точка входа, через которую демон запускает браузер::

    python -m engine.worker --browser-id br-1 --db adclicker.db

Модуль сознательно **не импортирует legacy-код на верхнем уровне**. Selenium,
``config_reader`` и ``search_controller`` подключаются лениво внутри
``legacy_source()``: в противном случае юнит-тесты цикла тянули бы за собой
чтение настоящего ``config.json``, запись в ``logs/`` и заглушку seleniumbase,
а тесты переставали бы быть быстрыми и изолированными.

Что здесь живёт, а что нет:

* **живёт** — цикл, окно запуска, пауза, очередь запросов, устойчивость к
  сбоям конфига и сценария и heartbeat в БД (фоновым потоком: воркер стучит
  сам, супервизор только наблюдает и по отсутствию роста помечает его
  зависшим);
* **не живёт** — надзор за процессом. Падение самого воркера разбирает
  супервизор (backoff, circuit breaker), а не этот модуль.

Поэтому ``run()`` возвращает код возврата и никогда не бросает исключение:
любая неожиданность либо залогирована и пережита циклом, либо означает, что
запуститься вообще нельзя (нет ``browser_id``).
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, time as clock_time
from typing import Any, Callable, Mapping, Protocol, Sequence

from engine.control_plane.state import StateStore
from engine.db import migrations
from engine.log import StructuredLogger
from engine.scheduler import (
    OUTSIDE_INTERVAL_WAIT_SECONDS,
    IntervalError,
    inside_running_interval,
    next_round_item,
    next_worker_item,
    stagger_seconds,
    worker_index_from_browser_id,
)
from engine.store import StoreWriter

# Коды возврата main(). Разные, чтобы супервизор и тесты могли отличить
# «не смог стартовать» от «поработал и остановился по сигналу».
EXIT_OK = 0
EXIT_CONFIG_ERROR = 2

# Как часто воркер, ждущий паузы, перечитывает флаг. Полсекунды: заметить
# resume быстро, но не превращать ожидание в опрос в SQLite сотню раз в секунду.
PAUSE_POLL_SECONDS = 0.5

# Как часто воркер сам пишет heartbeat в БД (StoreWriter.heartbeat, upsert).
# Своя константа, а НЕ настройка супервизора: тот тянет пул прокси и решает
# за демон, а воркеру важно только подтверждать, что его цикл работает.
# Контракт с противоположной стороны: супервизор объявляет воркера зависшим
# через stale_after = 3 x 5 с, поэтому интервал обязан быть заметно меньше
# порога — иначе живой воркер погиб бы от ложного срабатывания.
# Пишет отдельный поток, а не цикл: сценарий может минутами висеть в
# браузере, и heartbeat обязан идти всё это время — иначе длинный, но
# здоровый прогон выглядел бы зависанием.
WORKER_HEARTBEAT_INTERVAL_SECONDS = 5.0

# Сколько ждать завершения heartbeat-потока при остановке. Нужен только на
# случай, если запись уже идёт (sqlite busy_timeout — 5 с): обычный стоп
# мгновенный, потому что поток спит в Event.wait и просыпается по флагу.
HEARTBEAT_STOP_GRACE_SECONDS = 10.0

# Переменные окружения, которые супервизор передаёт воркеру.
BROWSER_ID_ENV = "ADCLICKER_BROWSER_ID"
DB_ENV = "ADCLICKER_DB"
# Размер пула нужен, чтобы очередь запросов делилась по фактическому числу
# воркеров, а не по behavior.browser_count: API позволяет запустить иное
# число, и без этого два браузера брали бы один и тот же запрос.
POOL_SIZE_ENV = "ADCLICKER_POOL_SIZE"
# Прокси, назначенный воркеру супервизором. Формат — как в proxies.txt:
# user:pass@host:port. Пусто/не задано = legacy-поведение (список из
# config.paths.proxy_file). Контракт фиксированный: читают его и воркер,
# и ad_clicker.resolve_proxy, передаёт супервизор.
PROXY_ENV = "ADCLICKER_PROXY"

# Сигналы, по которым воркер завершается. Оба превращаются в установку флага:
# тяжёлая работа внутри обработчика сигнала недопустима.
SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)

__all__ = [
    "BROWSER_ID_ENV",
    "DB_ENV",
    "EXIT_CONFIG_ERROR",
    "EXIT_OK",
    "PAUSE_POLL_SECONDS",
    "POOL_SIZE_ENV",
    "PROXY_ENV",
    "HeartbeatSender",
    "ScenarioRequest",
    "ScheduleSettings",
    "SourceError",
    "WORKER_HEARTBEAT_INTERVAL_SECONDS",
    "WorkSource",
    "WorkerRunner",
    "legacy_source",
    "main",
    "pool_size_from_environ",
    "proxy_from_environ",
]


class SourceError(RuntimeError):
    """Источник работы не смог отдать данные (битый конфиг, пропавший файл).

    Отдельный тип вместо ``SystemExit``, которым legacy сигнализирует об этих
    же случаях: ``SystemExit`` внутри цикла убил бы процесс и увёл бы воркер
    в бесконечные рестарты супервизора вместо паузы с понятной записью в лог.
    """


@dataclass(frozen=True)
class ScheduleSettings:
    """Планировочные настройки одного воркера.

    Отдельный датасет, а не ссылка на ``config_reader``: цикл читает конфиг
    заново на каждой итерации, чтобы правки из UI применялись без рестарта
    демона, и не должен держать живой объект, который перезапишется под ним.
    """

    interval_start: str = ""
    interval_end: str = ""
    loop_wait_time: float = 60.0
    wait_factor: float = 1.0
    browser_count: int = 1
    multiprocess_style: int = 1
    send_to_android: bool = False


@dataclass(frozen=True)
class ScenarioRequest:
    """Один прогон сценария: что, чем и в каком порядке делать."""

    browser_id: str
    worker_index: int
    pool_size: int
    round_index: int
    query: str
    proxy: str | None
    device_id: str | None


class WorkSource(Protocol):
    """Контракт между циклом воркера и legacy-кодом.

    Выделен в Protocol, чтобы тесты подставляли фейк без наследования и без
    импорта Selenium — вся логика цикла проверяется на чистом Python.
    """

    def reload_settings(self) -> ScheduleSettings: ...

    def queries(self) -> list[str]: ...

    def proxies(self) -> list[str]: ...

    def devices(self) -> list[str]: ...

    def run_scenario(self, request: ScenarioRequest) -> bool: ...


# Причина, по которой воркер ждёт вместо работы. Кортеж, а не строка: уровень
# и текст сообщения едут вместе, а сравнение кортежей даёт «логировать только
# при смене причины» бесплатно.
WaitReason = tuple[str, str]


# --- heartbeat воркера ------------------------------------------------------


class HeartbeatSender:
    """Фоновая запись heartbeat от имени воркера.

    Воркер — единственный, кто может подтвердить, что его цикл работает:
    только живой процесс способен писать ``heartbeat_at`` в БД. Супервизор
    heartbeat не пишет — он наблюдает за ростом значения и по отсутствию
    роста помечает воркера зависшим, поэтому поток обязан переживать любую
    долгую работу цикла. Отсюда daemon-поток, а не точка в цикле: сценарий
    в браузере может идти дольше порога stale (15 с), и запись из цикла
    замолчала бы ровно на время самого тяжёлого прогона.

    Остановка — явный ``stop()`` из ``main()`` ПЕРЕД ``writer.close()``:
    ``Event`` будит сон немедленно, а ``join()`` гарантирует, что записи в
    уже закрытый writer не будет. Ошибки записи не роняют поток: контракт
    ``StoreWriter`` — не бросать, счётчики ``dropped``/``last_error`` хранят
    причину, а нарушение контракта уходит в ``logs`` тем же writer'ом —
    молчаливая смерть потока выглядела бы как зависание воркера.
    """

    def __init__(
        self,
        writer: StoreWriter,
        browser_id: str,
        interval: float | None = None,
    ):
        if interval is not None and interval <= 0:
            raise ValueError(f"interval должен быть положительным, получено {interval!r}")
        self._writer = writer
        self._browser_id = browser_id
        self._interval = (
            WORKER_HEARTBEAT_INTERVAL_SECONDS if interval is None else interval
        )
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Запускает поток. Повторный вызов — no-op, а не второй поток."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="worker-heartbeat", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Гасит поток и дожидается его. Вызывается до ``writer.close()``."""
        self._halt.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=HEARTBEAT_STOP_GRACE_SECONDS)

    def _run(self) -> None:
        # Первый heartbeat сразу, без ожидания интервала: строка воркера
        # становится свежей в тот же момент, когда процесс начал работать.
        while True:
            self._beat()
            if self._halt.wait(self._interval):
                return

    def _beat(self) -> None:
        try:
            self._writer.heartbeat(self._browser_id)
        except Exception as exc:  # noqa: BLE001 - контракт writer'а: не бросает
            self._writer.log(
                "ERROR",
                "scheduler",
                "worker heartbeat write failed",
                browser_id=self._browser_id,
                fields={"error": str(exc), "error_type": type(exc).__name__},
            )


class WorkerRunner:
    """Цикл воркера.

    Потокобезопасности внутри нет и не нужно: цикл ведёт один поток, а
    heartbeat-поток (:class:`HeartbeatSender`) пишет только через
    потокобезопасный ``StoreWriter`` и сюда не заглядывает. Остановка
    приходит из обработчика сигнала, который только ставит ``stop_event``.
    """

    def __init__(
        self,
        *,
        browser_id: str,
        store: StateStore,
        source: WorkSource,
        logger: StructuredLogger | None = None,
        stop_event: threading.Event | None = None,
        now: Callable[[], clock_time] | None = None,
        pause_poll_seconds: float = PAUSE_POLL_SECONDS,
        outside_wait_seconds: float = OUTSIDE_INTERVAL_WAIT_SECONDS,
        pool_size: int | None = None,
    ):
        self._browser_id = browser_id
        self._store = store
        self._source = source
        self._logger = logger
        self._stop = stop_event if stop_event is not None else threading.Event()
        self._now = now if now is not None else datetime.now().time
        self._pause_poll_seconds = pause_poll_seconds
        self._outside_wait_seconds = outside_wait_seconds
        self._pool_size = pool_size
        # Последняя причина ожидания. None — воркер работал; по ней видно,
        # сколько раз и из-за чего простой повторялся.
        self._wait_reason: WaitReason | None = None

    # --- жизненный цикл ---------------------------------------------------

    def run(self) -> int:
        """Крутит сценарии, пока не попросят остановиться. Всегда EXIT_OK.

        Единственный ненулевой код — ``EXIT_CONFIG_ERROR``, и он не
        возвращается отсюда: его отдаёт ``main()`` до создания раннера.
        """
        worker_index = worker_index_from_browser_id(self._browser_id)
        self._stagger(worker_index)
        self._log("INFO", "scheduler", "worker started", {"worker_index": worker_index})

        round_index = 0
        while not self._stop.is_set():
            # Пауза проверяется ДО чтения конфига: пока воркер ждёт resume,
            # ему нечего планировать, а опрос config.json каждые 0.5 с на
            # каждом воркере — это лишние чтения файла и ложные ERROR в
            # момент, когда UI переписывает конфиг.
            if self._store.is_pause_requested():
                if self._wait_for(
                    ("INFO", "worker is paused, waiting for resume"),
                    self._pause_poll_seconds,
                ):
                    break
                continue

            settings = self._reload_settings()
            if settings is None:
                continue

            try:
                inside = inside_running_interval(
                    self._now(), settings.interval_start, settings.interval_end
                )
            except IntervalError as exc:
                if self._wait_for(("ERROR", str(exc)), self._outside_wait_seconds):
                    break
                continue

            if not inside:
                if self._wait_for(
                    ("INFO", "outside running interval, waiting"),
                    self._outside_wait_seconds,
                ):
                    break
                continue

            try:
                request = self._build_request(settings, worker_index, round_index)
            except (SourceError, ValueError) as exc:
                if self._wait_for(
                    ("ERROR", f"cannot build scenario request: {exc}"),
                    self._outside_wait_seconds,
                ):
                    break
                continue

            self._resume(worker_index, round_index)
            self._run_scenario(request, round_index)
            round_index += 1

            if self._wait(max(0.0, float(settings.loop_wait_time))):
                break

        self._log("INFO", "scheduler", "worker stopped")
        return EXIT_OK

    # --- шаги цикла -------------------------------------------------------

    def _stagger(self, worker_index: int) -> None:
        """Развожу старты воркеров, чтобы не рвать один chromedriver.

        Проверка флага до сна, а не после: уже остановленный воркер не должен
        спать лишние секунды перед выходом.
        """
        if self._stop.is_set():
            return
        try:
            settings = self._source.reload_settings()
            delay = stagger_seconds(worker_index, settings.wait_factor)
        except Exception:  # noqa: BLE001 - конфиг разберёт основной цикл
            delay = 0.0
        if delay > 0:
            self._wait(delay)

    def _reload_settings(self) -> ScheduleSettings | None:
        """Читает конфиг заново. None — не удалось, воркер ждёт и повторит."""
        try:
            return self._source.reload_settings()
        except Exception as exc:  # noqa: BLE001 - источник legacy, ошибки любые
            if isinstance(exc, SourceError):
                detail = str(exc)
            else:
                detail = f"{type(exc).__name__}: {exc}"
            self._wait_for(
                ("ERROR", f"config reload failed: {detail}"), self._outside_wait_seconds
            )
            # Остановка во время ожидания обрабатывается вызывающим циклом:
            # он проверяет stop_event на верхней итерации.
            return None

    def _build_request(
        self, settings: ScheduleSettings, worker_index: int, round_index: int
    ) -> ScenarioRequest:
        pool_size = self._pool_size
        if pool_size is None or pool_size < 1:
            # Значение из окружения не пришло (запуск вручную из терминала):
            # берём число из конфига, как это делал legacy run_ad_clicker.
            pool_size = max(1, int(settings.browser_count))

        queries = self._source.queries()
        if not queries:
            raise SourceError("список запросов пуст — нечего искать")

        if int(settings.multiprocess_style) == 2:
            query = next_round_item(queries, round_index)
        else:
            query = next_worker_item(queries, worker_index, pool_size, round_index)

        proxies = self._source.proxies()
        proxy = (
            next_worker_item(proxies, worker_index, pool_size, round_index)
            if proxies
            else None
        )

        return ScenarioRequest(
            browser_id=self._browser_id,
            worker_index=worker_index,
            pool_size=pool_size,
            round_index=round_index,
            query=query,
            proxy=proxy,
            device_id=self._device_for(settings, worker_index),
        )

    def _device_for(self, settings: ScheduleSettings, worker_index: int) -> str | None:
        if not settings.send_to_android:
            return None
        devices = self._source.devices()
        if not devices:
            return None
        return devices[(worker_index - 1) % len(devices)]

    def _run_scenario(self, request: ScenarioRequest, round_index: int) -> None:
        # Исключение и «звершено с ошибкой» — два разных способа сказать одно
        # и то же: legacy run_scenario глотает исключения сам (иначе упал бы
        # весь прогон, как было в ad_clicker.main), поэтому возвращаемый флаг
        # здесь единственный носитель результата. Без него в БД уезжала бы
        # «успех» на каждом упавшем прогоне.
        try:
            completed = self._source.run_scenario(request)
        except (Exception, SystemExit) as exc:
            # SystemExit ловим отдельно: legacy бросает его при отсутствии
            # файла запросов, и он не должен убивать процесс воркера.
            completed = False
            detail = str(exc) or type(exc).__name__
            fields = {
                "round_index": round_index,
                "query": request.query,
                "error": detail,
                "error_type": type(exc).__name__,
            }
        else:
            fields = {
                "round_index": round_index,
                "query": request.query,
                "error": None if completed else "сценарий завершился с ошибкой, см. legacy-лог",
            }

        if completed:
            self._log("INFO", "browser", "scenario finished", fields)
        else:
            self._log("ERROR", "browser", "scenario failed", fields)

    # --- ожидание ---------------------------------------------------------

    def _wait_for(self, reason: WaitReason, seconds: float) -> bool:
        """Ждёт, повторяя лог только при смене причины. True — остановились.

        Дедупликация обязательна: воркер вне окна ждёт по минуте, и без неё
        он писал бы в ``logs`` по строке каждый час на протяжении суток.
        """
        if reason != self._wait_reason:
            level, message = reason
            self._log(level, "scheduler", message)
            self._wait_reason = reason
        return self._stop.wait(seconds)

    def _wait(self, seconds: float) -> bool:
        return self._stop.wait(seconds)

    def _resume(self, worker_index: int, round_index: int) -> None:
        if self._wait_reason is None:
            return
        self._log(
            "INFO",
            "scheduler",
            "worker resumed",
            {"previous_wait": self._wait_reason[1], "worker_index": worker_index},
        )
        self._wait_reason = None

    def _log(
        self,
        level: str,
        category: str,
        message: str,
        fields: dict[str, object] | None = None,
    ) -> None:
        if self._logger is None:
            return
        self._logger.log(
            level,
            category,
            message,
            browser_id=self._browser_id,
            fields=fields,
        )


# --- legacy-мост -----------------------------------------------------------


class _LegacySource:
    """``WorkSource`` поверх legacy-модулей в корне репозитория.

    Все импорты ленивые и обёрнуты в ``SourceError``: ``config_reader``,
    ``utils`` и ``proxy`` при ошибке бросают ``SystemExit``, а ``SystemExit``
    внутри цикла выглядел бы как чистое завершение процесса и спрятал бы
    причину от супервизора и от лога.
    """

    def reload_settings(self) -> ScheduleSettings:
        try:
            from config_reader import config

            config.read_parameters()
        except SystemExit as exc:
            raise SourceError(_describe_system_exit(exc, "config.json")) from exc
        except OSError as exc:
            raise SourceError(f"config.json не читается: {exc}") from exc

        behavior = config.behavior
        if behavior is None:
            # read_parameters() уже прошёл, но вернул None: секция behavior
            # не доехала. Без проверки ниже было бы молчаливое падение по
            # None-атрибуту, которое не назвало бы причину.
            raise SourceError("config.json: секция behavior не прочитана")
        return ScheduleSettings(
            interval_start=behavior.running_interval_start or "",
            interval_end=behavior.running_interval_end or "",
            loop_wait_time=float(behavior.loop_wait_time or 0),
            wait_factor=float(behavior.wait_factor or 1.0),
            browser_count=int(behavior.browser_count or 1),
            multiprocess_style=int(behavior.multiprocess_style or 1),
            send_to_android=bool(behavior.send_to_android),
        )

    def queries(self) -> list[str]:
        path = _configured_str("paths.query_file", _raw_field(_legacy_paths(), "query_file"))
        # Пустой путь проверяется ДО чтения: ``Path("")`` — это ``Path(".")``,
        # и ``open(".")`` дал бы ``IsADirectoryError``, который мимо
        # ``SystemExit`` вылетел бы из цикла и увёл воркер в рестарты вместо
        # паузы с понятной записью в лог. Пустой ``query_file`` при этом
        # разрешён самим ``config_reader`` — это не ошибка конфига, а
        # неприменимый режим, о котором воркер обязан сказать словами.
        if not path.strip():
            raise SourceError("paths.query_file не задан — список неоткуда взять")
        return _read_items("paths.query_file", path, _legacy_queries)

    def proxies(self) -> list[str]:
        # Env-контракт супервизора главнее legacy-источников: прокси,
        # закреплённый за воркером, не должен зависеть от файла со списком
        # или от webdriver.proxy. Пустая переменная — прежний путь ниже.
        assigned = proxy_from_environ()
        if assigned is not None:
            return [assigned]
        # Наследие legacy: список шёл из файла, одиночный прокси — из
        # webdriver.proxy. run_ad_clicker падал, если не было файла; воркер
        # в этом случае работает с одним и тем же прокси напрямую, а при
        # полном отсутствии — без прокси (whitelist-IP).
        file_path = _configured_str("paths.proxy_file", _raw_field(_legacy_paths(), "proxy_file"))
        if file_path.strip():
            return _read_items("paths.proxy_file", file_path, _legacy_proxies)
        single = _configured_str("webdriver.proxy", _raw_field(_legacy_webdriver(), "proxy"))
        if single.strip():
            return [single]
        return []

    def devices(self) -> list[str]:
        try:
            from adb import adb_controller

            adb_controller.get_connected_devices()
            return list(adb_controller.devices)
        except Exception as exc:  # noqa: BLE001 - adb может отсутствовать
            raise SourceError(f"adb недоступен: {exc}") from exc

    def run_scenario(self, request: ScenarioRequest) -> bool:
        import ad_clicker

        return ad_clicker.run_scenario(
            query=request.query,
            proxy=request.proxy,
            browser_id=request.browser_id,
            device_id=request.device_id,
        )


def _raw_field(section: object, name: str) -> object:
    """Достаёт поле секции конфига, не падая на незаполненной секции."""
    if section is None:
        return None
    return getattr(section, name, None)


def _configured_str(field: str, raw: object) -> str:
    """Строка из конфига; ``None`` и пустое — пустая строка, остальное — ошибка.

    Не-строка (``123``, ``true``, список) обязана стать SourceError здесь, а
    не ``TypeError`` внутри ``Path(...)`` в legacy: ``TypeError`` нет ни в
    ``_read_items``, ни в перехвате цикла, и воркер ушёл бы в рестарты вместо
    паузы с причиной в логе. JSON-``null`` и ``""`` — это «не задано», а не
    ошибка: оба разрешены самим ``config_reader``.
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise SourceError(f"{field} должен быть строкой, получено {type(raw).__name__}")
    return raw


def _read_items(field: str, path: str, reader: Callable[[], list[str]]) -> list[str]:
    """Читает список и превращает все legacy-отказы в ``SourceError``.

    И ``SystemExit``, которым legacy сигналит об ошибке, и ``OSError`` от
    ``open()`` обязаны стать SourceError: оба вышли бы из ``run()`` и убили
    бы процесс, а супервизор прочитал бы это как падение и пошёл бы в
    backoff вместо паузы с причиной в логе. ``TypeError`` — страховка на
    случай, если значение пути неожиданно окажется не строкой уже внутри
    legacy-кода.
    """
    try:
        return reader()
    except SystemExit as exc:
        raise SourceError(_describe_system_exit(exc, field)) from exc
    except OSError as exc:
        raise SourceError(f"{field}={path!r} не читается: {exc}") from exc
    except TypeError as exc:
        raise SourceError(f"{field}={path!r} непригодно как путь: {exc}") from exc


def _legacy_paths():
    try:
        from config_reader import config

        return config.paths
    except (SystemExit, OSError) as exc:
        raise SourceError(f"config.json не читается: {exc}") from exc


def _legacy_webdriver():
    try:
        from config_reader import config

        return config.webdriver
    except (SystemExit, OSError) as exc:
        raise SourceError(f"config.json не читается: {exc}") from exc


def _legacy_queries() -> list[str]:
    from utils import get_queries

    return get_queries()


def _legacy_proxies() -> list[str]:
    from proxy import get_proxies

    return get_proxies()


def _describe_system_exit(exc: SystemExit, field: str) -> str:
    """Делает ``SystemExit`` читаемым и называет виноватое поле конфига.

    Название поля обязательно: legacy-текст вида «Couldn't find queries file»
    не говорит, какая настройка привела к отказу, а в лог это попадает так,
    как есть и показывается в UI без доработки.
    """
    detail = str(exc).strip()
    if detail:
        return f"{field}: {detail}"
    return f"{field}: legacy завершился без пояснения (код {exc.code})"


def legacy_source() -> WorkSource:
    """Фабрика legacy-источника. Отдельная функция, а не вызов в ``main``:

    тесты подменяют именно её, поэтому ``main()`` проверяется целиком, не
    затрагивая Selenium.
    """
    return _LegacySource()


# --- точка входа -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adclicker-worker",
        description="Воркер Google Ad Clicker: один процесс на браузер",
    )
    parser.add_argument(
        "--browser-id",
        default=os.environ.get(BROWSER_ID_ENV, ""),
        help=f"идентификатор воркера (или переменная {BROWSER_ID_ENV})",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get(DB_ENV, "adclicker.db"),
        help=f"путь к БД (или переменная {DB_ENV})",
    )
    return parser


def pool_size_from_environ(environ: dict[str, str] | None = None) -> int | None:
    """Размер пула из окружения супервизора. None — значение не пришло.

    Мусор и нули трактуются как отсутствие значения, а не как ошибка: воркер
    запускают вручную для отладки, и падение из-за незнакомой переменной
    только мешает.
    """
    source = os.environ if environ is None else environ
    raw = source.get(POOL_SIZE_ENV, "").strip()
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if value >= 1 else None


def proxy_from_environ(environ: Mapping[str, str] | None = None) -> str | None:
    """Прокси, назначенное супервизором через ``ADCLICKER_PROXY``. None — не задано.

    Пустое значение и значение из пробелов — «не назначено», а не ошибка:
    переменная есть в окружении воркера и тогда, когда супервизор прокси не
    выдавал, и падать на ней нельзя. Пробелы по краям срезаются — значение
    проходит через env без кавычек. ``environ`` — параметр ради тестов, как у
    :func:`pool_size_from_environ`.
    """
    source = os.environ if environ is None else environ
    value = source.get(PROXY_ENV, "").strip()
    return value or None


def _install_signal_handlers(stop_event: threading.Event) -> dict[int, object]:
    """Ставит обработчики, которые только поднимают флаг остановки.

    Соединение с БД, запись лога и остановка Chrome внутри обработчика
    недопустимы: между сигналами может прийти второй, а обработчик выполняется
    в главном потоке. Прежние обработчики возвращаются после работы ``main``,
    чтобы тест и встроенный запуск не остались с подменённым SIGTERM.
    """
    previous: dict[int, Any] = {}

    def _on_signal(signal_number: int, frame: object) -> None:
        stop_event.set()

    for signal_number in SHUTDOWN_SIGNALS:
        previous[signal_number] = signal.getsignal(signal_number)
        signal.signal(signal_number, _on_signal)
    return previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    if threading.current_thread() is not threading.main_thread():
        return
    for signal_number, handler in previous.items():
        signal.signal(signal_number, handler)


def main(
    argv: Sequence[str] | None = None,
    *,
    source_factory: Callable[[], WorkSource] = legacy_source,
    stop_event: threading.Event | None = None,
) -> int:
    """Точка входа воркера. Возвращает код, не вызывая ``sys.exit``."""
    args = build_parser().parse_args(argv)

    browser_id = (args.browser_id or "").strip()
    if not browser_id:
        print(
            f"не задан --browser-id или переменная {BROWSER_ID_ENV}: "
            "воркер не знает, как себя подписать в логах",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR

    db_path = args.db
    try:
        migrations.migrate(db_path)
    except Exception as exc:  # noqa: BLE001 - SchemaTooNewError, OSError, битый файл
        # Exception, а не (OSError, ValueError): migrate дополнительно бросает
        # SchemaTooNewError (RuntimeError), и тот же код возврата обязан
        # получить и он — иначе контракт «2 = не смог стартовать» нарушается.
        print(f"не удалось открыть БД {db_path}: {exc}", file=sys.stderr)
        _report_startup_failure(db_path, browser_id, exc)
        return EXIT_CONFIG_ERROR

    store = StateStore(db_path)
    writer = StoreWriter(db_path)
    logger = StructuredLogger(writer, browser_id)
    stop = stop_event if stop_event is not None else threading.Event()
    heartbeat = HeartbeatSender(writer, browser_id)
    previous_handlers: dict[int, Any] = {}

    try:
        previous_handlers = _install_signal_handlers(stop)
        # Heartbeat стартует после инициализации store/writer и до цикла:
        # строка воркера должна стать живой сразу после старта процесса, а
        # не после первого сценария.
        heartbeat.start()
        runner = WorkerRunner(
            browser_id=browser_id,
            store=store,
            source=source_factory(),
            logger=logger,
            stop_event=stop,
            pool_size=pool_size_from_environ(),
        )
        return runner.run()
    finally:
        # Порядок важен: сначала снимаем свои обработчики, потом гасим writer —
        # иначе SIGTERM, пришедший между ними, доставил бы флаг уже никому.
        _restore_signal_handlers(previous_handlers)
        # Heartbeat-поток останавливается и дожидается ДО close(): иначе он
        # мог бы дописать запись уже в закрытый writer. Поздняя запись была бы
        # отброшена счётчиком потерь, но гонку с close() лучше не устраивать.
        heartbeat.stop()
        writer.close()


def _report_startup_failure(db_path: str, browser_id: str, error: Exception) -> None:
    """Доводит причину неудачного старта до БД.

    stderr воркера супервизор гасит (``DEVNULL``), поэтому без этой записи
    в UI осталось бы только «exit code 2» без причины. Запись best-effort:
    если БД не открывается, чинить её уже некому, и в этом случае причина
    остаётся хотя бы в stderr.
    """
    try:
        StateStore(db_path).log(
            "ERROR",
            "browser",
            "worker failed to start",
            {"error": str(error), "error_type": type(error).__name__},
            browser_id=browser_id,
        )
    except Exception as nested:  # noqa: BLE001 - БД сама и есть источник сбоя
        print(f"причина сбоя не записана в БД: {nested}", file=sys.stderr)


if __name__ == "__main__":
    # sys.exit, а не голый main(): супервизор принимает решения по коду
    # возврата, и потерянный здесь код сделал бы падение и остановку
    # одинаковыми.
    sys.exit(main())
