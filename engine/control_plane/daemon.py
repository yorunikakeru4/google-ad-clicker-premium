"""Демон: собирает БД, конфиг, супервизор и HTTP в один процесс.

Это единственный модуль, который знает про всё сразу, и поэтому он же
разбирает аргументы командной строки и ставит обработчики сигналов. Держим
его тонким: вся логика живёт в соседних модулях, здесь только связывание.

**Порядок остановки важен.** Сначала HTTP перестаёт принимать запросы, потом
гасится супервизор, и только потом закрывается запись в БД. Обратный порядок
приводит к гонке: HTTP-запрос /control/start успевает записать воркера в БД
после того, как супервизор уже всё остановил, и в базе остаётся строка
"starting" без процесса.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from engine.captcha_threshold import CaptchaThresholdPolicy
from engine.cleanup import (
    CleanupService,
    is_cleanup_due,
    last_run_ts,
    seconds_until_cleanup,
)
from engine.control_plane.api import (
    LOOPBACK_HOST,
    ControlPlaneServer,
    MissingTokenError,
    token_from_environ,
)
from engine.control_plane.config import Config, ConfigError
from engine.control_plane.state import StateStore
from engine.control_plane.supervisor import (
    DEFAULT_MAX_RESTARTS,
    HEARTBEAT_INTERVAL_SECONDS,
    RESTART_BACKOFF_BASE_SECONDS,
    RESTART_BACKOFF_MAX_SECONDS,
    SHUTDOWN_GRACE_SECONDS,
    Supervisor,
    SupervisorSettings,
)
from engine.log_rotation import (
    default_export_dir,
    enforce_db_size_limit,
    export_day,
    local_day,
    run_retention,
    seconds_until_day_close,
)
from engine.proxy_health import ProxyHealthChecker
from engine.profile_pool import ProfilePool
from engine.proxy_pool import ProxyError, ProxyPool

# Сигналы, по которым демон завершается. SIGINT — Ctrl+C при запуске из
# терминала, SIGTERM — systemd/stop-скрипт. Оба обязаны приводить к
# одинаковой последовательности: SIGTERM -> grace -> SIGKILL по воркерам.
SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)

# Коды возврата main(). Разные, чтобы вызывающая сторона (тест, сервис,
# скрипт установки) могла отличить "нет токена" от "сломан конфиг".
EXIT_OK = 0
EXIT_CONFIG_ERROR = 2

# Интервал периодической проверки прокси в секундах. Дефолт — раз в час;
# 0 (и любое отрицательное) расписание выключает.
DEFAULT_PROXY_CHECK_INTERVAL_SECONDS = 3600.0

# Интервал задаётся окружением, а не config.json: конфиг — файл, который UI
# перезаписывает целиком и схему которого читает config_reader, а вопрос
# «как часто фоновой задаче ходить по сети» относится к запуску демона
# (systemd/launchd), не к настройкам кликера.
PROXY_CHECK_INTERVAL_ENV_VAR = "ADCLICKER_PROXY_CHECK_INTERVAL"

# Имя нити расписания: тесты ищут его при остановке, как "supervisor".
PROXY_CHECK_THREAD_NAME = "proxy-check"

# Интервал периодической проверки доли CAPTCHA в секундах. Дефолт — раз в
# минуту: порог считается по скользящему часу, и более частые опросы ничего
# не решают, а раз в секунду держали бы лишнее соединение с БД. 0 (и любое
# отрицательное) расписание выключает — политика молчит, доля в дашборде при
# этом считается независимо.
DEFAULT_CAPTCHA_CHECK_INTERVAL_SECONDS = 60.0

# Интервал задаётся окружением, а не config.json — та же причина, что и у
# PROXY_CHECK_INTERVAL_ENV_VAR: частота фоновой задачи относится к запуску
# демона (systemd/launchd), а не к настройкам кликера. Порог и действие при
# этом остаются полями config.json и читаются на каждом тике.
CAPTCHA_CHECK_INTERVAL_ENV_VAR = "ADCLICKER_CAPTCHA_CHECK_INTERVAL"

# Имя нити расписания порога: отдельное от proxy-check, чтобы остановка и
# диагностика не путали два независимых job'а.
CAPTCHA_CHECK_THREAD_NAME = "captcha-check"

# Очистка профилей (план §5, фаза 10). Имя нити — как у остальных job'ов,
# чтобы shutdown и тесты находили её тем же способом.
CLEANUP_THREAD_NAME = "cleanup"

# Стартовая задержка первого прогона очистки. Сироты после аварийного Kill
# должны убираться сразу при старте демона, а не копиться до ближайшего
# cleanup_time, — но не раньше, чем воркеры успеют подняться и браузеры
# открыть свои каталоги: зачистка в момент спавна конкурировала бы с ним.
# Поверх грейса по mtime в самом сервисе это даёт две независимые защиты.
DEFAULT_CLEANUP_STARTUP_GRACE_SECONDS = 30.0

# Пауза после ошибки расписания очистки (нечисловое время в конфиге, сбой
# чтения kv): нить должна жаловаться и продолжать, а не умирать или крутить
# горячий цикл.
CLEANUP_SCHEDULE_RETRY_SECONDS = 60.0


def captcha_check_interval_from_environ(environ: dict[str, str] | None = None) -> float:
    """Интервал периодической проверки порога CAPTCHA в секундах.

    Пустая/не заданная переменная — дефолт модуля, ``0`` и отрицательные —
    расписание выключено, нечисловое значение — ``ValueError`` с именем
    переменной: демон не стартует и говорит, что именно не так, а не молча
    включает интервал, которого никто не заказывал.
    """
    source = os.environ if environ is None else environ
    raw = source.get(CAPTCHA_CHECK_INTERVAL_ENV_VAR, "")
    if not raw.strip():
        return DEFAULT_CAPTCHA_CHECK_INTERVAL_SECONDS
    try:
        interval = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{CAPTCHA_CHECK_INTERVAL_ENV_VAR} должна быть числом секунд, "
            f"получено {raw.strip()!r}"
        ) from exc
    return interval if interval > 0 else 0.0


def proxy_check_interval_from_environ(environ: dict[str, str] | None = None) -> float:
    """Интервал периодической проверки прокси в секундах.

    Пустая/не заданная переменная — дефолт модуля, ``0`` и отрицательные —
    расписание выключено, нечисловое значение — ``ValueError`` с именем
    переменной: демон не стартует и говорит, что именно не так, вместо
    тихого часового интервала вместо секунд.
    """
    source = os.environ if environ is None else environ
    raw = source.get(PROXY_CHECK_INTERVAL_ENV_VAR, "")
    if not raw.strip():
        return DEFAULT_PROXY_CHECK_INTERVAL_SECONDS
    try:
        interval = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{PROXY_CHECK_INTERVAL_ENV_VAR} должна быть числом секунд, "
            f"получено {raw.strip()!r}"
        ) from exc
    return interval if interval > 0 else 0.0


# --- ротация логов (план §5, фаза 9) ---------------------------------------
#
# Три нити на stop_event, тот же паттерн, что у proxy-check и captcha-check.
# Все три интервала — настройки запуска демона (окружение), а не config.json:
# как часто ходить фоновой задаче, решает systemd/launchd, а сами настройки
# хранения (log_retention_days, log_file_level, db_size_limit_mb) — поля
# config.json и читаются из конфига на каждом тике.

# Имена нитей: тесты ищут их при остановке, как "supervisor"/"proxy-check".
DAY_CLOSE_THREAD_NAME = "day-close"
RETENTION_THREAD_NAME = "retention"
DB_SIZE_THREAD_NAME = "db-size"

# Закрытие дня. Пусто/не задано — расписание: экспорт в 23:59 локального
# времени (значение по умолчанию и нормальный режим). Больше нуля — период
# в секундах вместо расписания (ускоренный режим для тестов и стенда).
# Ноль и отрицательные — job выключен. Нечисловое — ValueError: демон не
# стартует и говорит, что именно не так, а не молча закрывает день не тогда.
DAY_CLOSE_INTERVAL_ENV_VAR = "ADCLICKER_DAY_CLOSE_INTERVAL"

# Задержка второй допроводки после полуночи, в секундах: 00:00:05 — время на
# то, чтобы записи последней минуты суток успели доехать из буферов воркеров
# в БД до повторного экспорта.
MIDNIGHT_REEXPORT_DELAY_SECONDS = 5

# Чистка старых дней: после каждого закрытия дня и, независимо от него, раз
# в сутки; 0 (и любое отрицательное) расписание выключает.
RETENTION_INTERVAL_ENV_VAR = "ADCLICKER_RETENTION_INTERVAL"
DEFAULT_RETENTION_INTERVAL_SECONDS = 86400.0

# Защита от роста БД: по умолчанию раз в час; 0 (и любое отрицательное)
# выключает. Сам лимит — db_size_limit_mb из config.json, 0 там тоже выключает.
DB_SIZE_INTERVAL_ENV_VAR = "ADCLICKER_DB_SIZE_INTERVAL"
DEFAULT_DB_SIZE_INTERVAL_SECONDS = 3600.0


def _seconds_from_environ(env_var: str, default: float, environ: dict[str, str] | None) -> float:
    """Секунды из окружения: пусто — дефолт, 0/минус — выключено, мусор — ValueError.

    Повторяет договорённость proxy/captcha-расписаний: ноль означает «job
    выключен», а не «каждый тик», иначе опечатка в env превратилась бы в
    горячий цикл.
    """
    source = os.environ if environ is None else environ
    raw = source.get(env_var, "")
    if not raw.strip():
        return default
    try:
        interval = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{env_var} должна быть числом секунд, получено {raw.strip()!r}"
        ) from exc
    return interval if interval > 0 else 0.0


def retention_interval_from_environ(environ: dict[str, str] | None = None) -> float:
    """Интервал чистки старых дней логов в секундах (см. ``_seconds_from_environ``)."""
    return _seconds_from_environ(
        RETENTION_INTERVAL_ENV_VAR, DEFAULT_RETENTION_INTERVAL_SECONDS, environ
    )


def db_size_interval_from_environ(environ: dict[str, str] | None = None) -> float:
    """Интервал защиты от роста БД в секундах (см. ``_seconds_from_environ``)."""
    return _seconds_from_environ(
        DB_SIZE_INTERVAL_ENV_VAR, DEFAULT_DB_SIZE_INTERVAL_SECONDS, environ
    )


def day_close_interval_from_environ(environ: dict[str, str] | None = None) -> float | None:
    """Расписание закрытия дня.

    Возвращает ``None`` — закрывать день по расписанию, в 23:59 локального
    времени (это дефолт, а не «выключено»); положительное число — период в
    секундах вместо расписания (тесты/стенд); ``0`` — job выключен, как у
    остальных расписаний демона. Нечисловое значение — ``ValueError`` с именем
    переменной: опечатка не должна молча превращаться в выключенное закрытие
    дня, из-за которого экспорт и retention не сработали бы вообще.
    """
    source = os.environ if environ is None else environ
    raw = source.get(DAY_CLOSE_INTERVAL_ENV_VAR, "")
    if not raw.strip():
        return None
    try:
        interval = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{DAY_CLOSE_INTERVAL_ENV_VAR} должна быть числом секунд, "
            f"получено {raw.strip()!r}"
        ) from exc
    return interval if interval > 0 else 0.0


# Очистка профилей (план §5, фаза 10). Пусто/не задано — расписание по
# ``behavior.cleanup_time`` с интервалом ``behavior.cleanup_interval_days``
# (это штатный режим); больше нуля — период в секундах вместо расписания
# (тесты/стенд, при этом интервал в днях не применяется); ноль и отрицательные
# — job выключен. Нечисловое — ValueError по той же причине, что и у
# закрытия дня: опечатка не должна молча выключить зачистку сирот.
CLEANUP_INTERVAL_ENV_VAR = "ADCLICKER_CLEANUP_INTERVAL"


def cleanup_interval_from_environ(environ: dict[str, str] | None = None) -> float | None:
    """Расписание очистки профилей (см. контракт у ``CLEANUP_INTERVAL_ENV_VAR``)."""
    source = os.environ if environ is None else environ
    raw = source.get(CLEANUP_INTERVAL_ENV_VAR, "")
    if not raw.strip():
        return None
    try:
        interval = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{CLEANUP_INTERVAL_ENV_VAR} должна быть числом секунд, "
            f"получено {raw.strip()!r}"
        ) from exc
    return interval if interval > 0 else 0.0


def seconds_until_midnight_reexport(now: float) -> float:
    """Секунд до ближайшей допроводки вчерашнего дня — цель 00:00:05.

    Как и у ``seconds_until_day_close``: цель всегда строго в будущем, иначе
    job, проснувшийся ровно в 00:00:05 (или позже, например из-за долгого
    основного тика), ждал бы допроводки ещё сутки.
    """
    local = time.localtime(now)
    midnight = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))
    target = midnight + MIDNIGHT_REEXPORT_DELAY_SECONDS
    if target <= now:
        tomorrow = date(local.tm_year, local.tm_mon, local.tm_mday) + timedelta(days=1)
        target = (
            time.mktime((tomorrow.year, tomorrow.month, tomorrow.day, 0, 0, 0, 0, 0, -1))
            + MIDNIGHT_REEXPORT_DELAY_SECONDS
        )
    return target - now


def previous_local_day(now: float) -> str:
    """Локальная дата суток, предшествующих дате ``now``, в ``YYYY-MM-DD``.

    День вычитается из строки даты, а не из unix-секунд: сутки с перехода на
    летнее время бывают не 86400 секунд, и вычитание секунд дало бы то ли
    «позавчера», то ли сегодняшний день. Ровно та же логика нужна допроводке
    в 00:00:0X — «вчера» там это день, закрытый в 23:59.
    """
    today = datetime.strptime(local_day(now), "%Y-%m-%d")
    return (today - timedelta(days=1)).strftime("%Y-%m-%d")


class Daemon:
    """Демон целиком: HTTP-сервер плюс супервизор в отдельном потоке."""

    def __init__(
        self,
        db_path: str | Path,
        config_path: str | Path,
        token: str,
        port: int = 8787,
        host: str = LOOPBACK_HOST,
        store: StateStore | None = None,
        supervisor: Supervisor | None = None,
        config: Config | None = None,
        proxy_check_interval: float = DEFAULT_PROXY_CHECK_INTERVAL_SECONDS,
        proxy_checker: Any = None,
        captcha_check_interval: float = DEFAULT_CAPTCHA_CHECK_INTERVAL_SECONDS,
        captcha_policy: Any = None,
        # Ротация логов: None — закрытие дня по расписанию (23:59), 0 —
        # выключено, >0 — период в секундах. Retention и защита от роста —
        # как у proxy-check: дефолтный интервал, 0 выключает.
        day_close_interval: float | None = None,
        retention_interval: float = DEFAULT_RETENTION_INTERVAL_SECONDS,
        db_size_interval: float = DEFAULT_DB_SIZE_INTERVAL_SECONDS,
        # Очистка профилей (план §5, фаза 10): ``0`` — выключено, ``None`` —
        # расписание по ``behavior.cleanup_time``, больше нуля — период в
        # секундах. Дефолт намеренно «выключено», а не «расписание»: это
        # единственный job, который ходит по системному tempdir вне репозитория,
        # а тесты собирают ``Daemon`` напрямую. Боевой путь (``build_daemon``)
        # включает расписание, когда окружение не задано.
        cleanup_interval: float | None = 0.0,
        cleanup_startup_grace: float = DEFAULT_CLEANUP_STARTUP_GRACE_SECONDS,
        cleanup_service: Any = None,
    ):
        self.db_path = Path(db_path)
        self.config_path = Path(config_path)
        self.token = token
        self.port = port
        self.store = store or StateStore(self.db_path)
        # Начальный конфиг — только для сборки супервизора и сервера: сама
        # «живая» копия отныне одна и принадлежит ControlPlaneServer (см.
        # property config ниже). Держать у демона собственный экземпляр
        # значило бы читать значение до рестарта — ровно тот дефект.
        initial_config = config or Config.load(self.config_path)
        self.proxy_check_interval = proxy_check_interval
        # Период проверки порога CAPTCHA — своя настройка запуска, а не
        # соседний интервал: выключить один job'ом можно, не выключая другой.
        self.captcha_check_interval = captcha_check_interval
        # Три job'а ротации логов — свои интервалы по той же причине:
        # выключить защиту от роста не должно значить отключить закрытие дня.
        self.day_close_interval = day_close_interval
        self.retention_interval = retention_interval
        self.db_size_interval = db_size_interval
        # Очистка профилей: сервис один на демон, потому что плановая нить и
        # HTTP-хендлеры должны ходить по одним корням, по одному замку и к
        # одной метке последнего прогона в kv — иначе ручной запуск и тик
        # проходили бы по tempdir одновременно, а status показывал бы то,
        # что записал из двух последний.
        self.cleanup_interval = cleanup_interval
        self.cleanup_startup_grace = cleanup_startup_grace
        self.cleanup_service = (
            cleanup_service if cleanup_service is not None else CleanupService(store=self.store)
        )
        # Пул и проверяющий — свои у демона, а не у HTTP-сервера: та же пара
        # обслуживает и /control/proxies, и расписание, иначе ручная проверка
        # и фоновая не знали бы друг о друге и шли бы параллельно.
        self.proxy_pool = ProxyPool(self.store.db_path)
        # Профили: один экземпляр на всех потребителей — HTTP-список, спавн
        # супервизора и реапер, — иначе выдача и то, что видит UI, разъезжались бы.
        self.profile_pool = ProfilePool(self.store.db_path)
        self.proxy_checker = (
            proxy_checker
            if proxy_checker is not None
            else ProxyHealthChecker(self.proxy_pool, on_error=self._log_check_failure)
        )
        self.supervisor = supervisor or Supervisor(
            store=self.store,
            settings=supervisor_settings_from_config(initial_config),
            # Тот же пул, что у /control/proxies и расписания: иначе
            # назначения супервизора и то, что видит UI, разъезжались бы.
            proxy_pool=self.proxy_pool,
            profile_pool=self.profile_pool,
        )
        self.server = ControlPlaneServer(
            supervisor=self.supervisor,
            config=initial_config,
            token=self.token,
            config_path=self.config_path,
            host=host,
            port=port,
            proxy_pool=self.proxy_pool,
            profile_pool=self.profile_pool,
            proxy_checker=self.proxy_checker,
            cleanup_service=self.cleanup_service,
            # Пересчёт ближайшего запуска после ручного ``/control/cleanup/run``:
            # статус в kv обязан учитывать, что ручной прогон сдвинул интервал.
            cleanup_next_run=self._cleanup_next_run_at,
        )
        # Политика порога — своя у демона, как и проверяющий прокси: она
        # работает в своём расписании, а действия (pause/rotate) делает через
        # тот же экземпляр супервизора, что и HTTP-кнопки.
        self.captcha_policy = (
            captcha_policy
            if captcha_policy is not None
            else CaptchaThresholdPolicy(store=self.store, supervisor=self.supervisor)
        )
        self._stop_event = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._supervisor_thread: threading.Thread | None = None
        self._proxy_check_thread: threading.Thread | None = None
        self._captcha_check_thread: threading.Thread | None = None
        self._day_close_thread: threading.Thread | None = None
        self._retention_thread: threading.Thread | None = None
        self._db_size_thread: threading.Thread | None = None
        self._cleanup_thread: threading.Thread | None = None
        self._shutdown_thread: threading.Thread | None = None
        self._previous_handlers: dict[int, Any] = {}
        self._started = False

    @property
    def config(self) -> Config:
        """Текущий конфиг демона — та же ссылка, которую меняет POST.

        Раньше у демона была собственная копия, созданная при сборке и не
        обновляемая ни при чём: job порога CAPTCHA читал её напрямую и видел
        старый ``captcha_threshold_*`` до рестарта. Теперь property уводит в
        ``ControlPlaneServer``, где конфиг меняется под замком в
        ``patch_config`` — тот же объект читают ``_current_config`` (его
        используют retention/db-size/day-close) и обработчики HTTP.
        """
        return self.server.config

    # --- жизненный цикл ---------------------------------------------------

    def start(self) -> None:
        """Поднимает сервер, цикл супервизора и фоновые расписания."""
        if self._started:
            return
        self._stop_event.clear()
        self.server.start()
        self.port = self.server.port
        self._supervisor_thread = self.supervisor.start_background()
        self._proxy_check_thread = self._start_proxy_check_loop()
        self._captcha_check_thread = self._start_captcha_check_loop()
        self._day_close_thread = self._start_day_close_loop()
        self._retention_thread = self._start_retention_loop()
        self._db_size_thread = self._start_db_size_loop()
        self._cleanup_thread = self._start_cleanup_loop()
        self._started = True
        # Токен в лог не пишется никогда: логи демона читаются из UI и
        # попадают в отчёты о поддержке.
        self.store.log(
            "INFO",
            "daemon",
            "daemon started",
            {"host": self.server.host, "port": self.port},
        )

    def shutdown(self) -> None:
        """Гасит демон: HTTP, супервизор, финальные записи в БД.

        Порядок задан в модульном докстринге и здесь: сервер, потом
        супервизор, потом отметка в базе. Повторный вызов безопасен — демон
        может получить и SIGTERM, и Ctrl+C подряд.

        Метод thread-safe: его зовут и из main(), и из потока, который поднял
        обработчик сигнала. Поэтому снятие перехвата сигналов вынесено из сюда
        в _restore_from_main_thread: signal.signal() разрешён только главному
        потоку, и вызов из потока остановки упал бы с ValueError.
        """
        with self._shutdown_lock:
            if not self._started:
                self._restore_from_main_thread()
                return
            self._started = False
            self._stop_event.set()

            self.server.stop()

            # Расписание спит на _stop_event, который уже взведён: join —
            # это ожидание пробуждения, а не истечения интервала.
            check_thread = self._proxy_check_thread
            if check_thread is not None:
                check_thread.join(timeout=SHUTDOWN_GRACE_SECONDS)
            self._proxy_check_thread = None

            # Проверка порога засыпает на своём интервале и просыпается по
            # тому же stop_event, поэтому join не ждёт истечения интервала —
            # как и у расписания прокси.
            captcha_thread = self._captcha_check_thread
            if captcha_thread is not None:
                captcha_thread.join(timeout=SHUTDOWN_GRACE_SECONDS)
            self._captcha_check_thread = None

            # Три job'а ротации логов спят до 23:59 / до суток / до часа,
            # поэтому остановка идёт по взведённому stop_event, а не по
            # таймеру — иначе shutdown ждал бы закрытия дня. По одному блоку
            # на нить, как у прокси и порога: видно, что именно ждём.
            day_close_thread = self._day_close_thread
            if day_close_thread is not None:
                day_close_thread.join(timeout=SHUTDOWN_GRACE_SECONDS)
            self._day_close_thread = None

            retention_thread = self._retention_thread
            if retention_thread is not None:
                retention_thread.join(timeout=SHUTDOWN_GRACE_SECONDS)
            self._retention_thread = None

            db_size_thread = self._db_size_thread
            if db_size_thread is not None:
                db_size_thread.join(timeout=SHUTDOWN_GRACE_SECONDS)
            self._db_size_thread = None

            # Очистка спит до стартового grace или до следующей цели
            # расписания, поэтому остановка идёт по взведённому stop_event —
            # как у остальных четырёх нитей выше.
            cleanup_thread = self._cleanup_thread
            if cleanup_thread is not None:
                cleanup_thread.join(timeout=SHUTDOWN_GRACE_SECONDS)
            self._cleanup_thread = None

            thread = self._supervisor_thread
            if thread is not None:
                stop_event = getattr(thread, "stop_event", None)
                if stop_event is not None:
                    stop_event.set()
                # Ждём поток: иначе он может дописать в БД статусы, логи и
                # рестарты уже после того, как демон объявил себя
                # остановленным. Heartbeat он не пишет — его пишут воркеры.
                thread.join(timeout=SHUTDOWN_GRACE_SECONDS)
            self._supervisor_thread = None

            self.supervisor.stop()
            self.store.log("INFO", "daemon", "daemon stopped", {"port": self.port})
            self._restore_from_main_thread()

    def __enter__(self) -> Daemon:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.shutdown()

    # --- сигналы ----------------------------------------------------------

    def install_signal_handlers(self) -> None:
        """Перехватывает SIGTERM/SIGINT и гасит демон корректно.

        Без этого демон, убитый SIGTERM, не успевает погасить воркеров: они
        остаются сиротами с открытыми Chrome, а записи в БД — в состоянии
        running. Прежние обработчики сохраняются и возвращаются при остановке,
        чтобы встроенный демон мог снять перехват.
        """
        for signal_number in SHUTDOWN_SIGNALS:
            self._previous_handlers[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, self._on_signal)

    def _on_signal(self, signal_number: int, frame: object) -> None:
        # Обработчик сигнала выполняется в главном потоке, и тяжёлая работа
        # (SIGTERM/SIGKILL воркерам, запись в БД) внутри него опасна: между
        # шагами может прийти следующий сигнал. Поэтому только ставим флаг,
        # а гасит демон отдельный поток.
        #
        # Ссылка на поток ставится ДО флага: ждущий в wait_for_shutdown()
        # просыпается по флагу и обязан тут же увидеть поток, иначе он
        # вернётся, не дождавшись конца остановки.
        thread = threading.Thread(
            target=self.shutdown, name="daemon-shutdown", daemon=True
        )
        self._shutdown_thread = thread
        self._stop_event.set()
        thread.start()

    def _restore_from_main_thread(self) -> None:
        """Снимает перехват сигналов, но только из главного потока.

        signal.signal() работает лишь в главном потоке главного интерпретатора,
        а останавливает демон поток, поднятый обработчиком сигнала. Отсюда
        разделение: остановку делает любой поток, а возвращать обработчики —
        тот, кто ждёт остановки (см. wait_for_shutdown).
        """
        if threading.current_thread() is not threading.main_thread():
            return
        self._restore_signal_handlers()

    def _restore_signal_handlers(self) -> None:
        for signal_number, handler in self._previous_handlers.items():
            signal.signal(signal_number, handler)
        self._previous_handlers.clear()

    def wait_for_shutdown(self, timeout: float | None = None) -> bool:
        """Ждёт флага остановки. Для main() и для тестов.

        timeout=None означает «ждать, пока придёт сигнал» — это режим main():
        демон не имеет права выйти сам через N секунд, потому что пользователь
        не запускал его как разовую задачу.

        Возвращает True, если остановка случилась, и False по таймауту:
        вызывающая сторона должна отличать «демон гасится» от «демон завис»,
        иначе истёкший таймаут выглядел бы как тихая остановка.

        Побочный эффект — единственное место, где гарантированно снимается
        перехват сигналов: поток остановки не имеет на это права.
        """
        stopped = self._stop_event.wait(timeout)
        shutdown_thread = self._shutdown_thread
        if shutdown_thread is not None:
            shutdown_thread.join(timeout)
            self._shutdown_thread = None
        if threading.current_thread() is threading.main_thread():
            self._restore_signal_handlers()
        return stopped


    # --- периодическая проверка прокси -----------------------------------

    def _start_proxy_check_loop(self) -> threading.Thread | None:
        """Поднимает нить расписания; None — расписание выключено (интервал <= 0)."""
        if self.proxy_check_interval <= 0:
            return None
        thread = threading.Thread(
            target=self._run_proxy_check_loop,
            kwargs={"stop_event": self._stop_event},
            name=PROXY_CHECK_THREAD_NAME,
            daemon=True,
        )
        thread.start()
        return thread

    def _run_proxy_check_loop(self, stop_event: threading.Event) -> None:
        """Ждёт интервал, запускает проверку, повторяет, пока демон жив.

        Первый запуск — через интервал после старта: немедленная проверка
        при каждом рестарте удвоила бы нагрузку на прокси в момент и так
        самого нагруженного события. Остановка идёт по тому же stop_event,
        что и у остальных фоновых работ демона, поэтому shutdown не ждёт
        истечения интервала.
        """
        while True:
            if stop_event.wait(self.proxy_check_interval):
                return
            try:
                self.proxy_checker.start()
            except ProxyError as exc:
                # CheckInProgressError во время ручной проверки — штатное
                # состояние, а не сбой: в лог уходит причина пропуска.
                self.store.log(
                    "WARNING",
                    "proxy",
                    "periodic proxy check skipped",
                    {"error": str(exc)},
                )
            except Exception as exc:  # noqa: BLE001 - расписание обязано пережить сбой
                # Имя типа, а не str(exc): этот путь не про ошибки пула и не
                # обязан быть свободен от постороннего содержимого.
                self.store.log(
                    "ERROR",
                    "proxy",
                    "periodic proxy check failed to start",
                    {"error": type(exc).__name__},
                )

    def _log_check_failure(self, exc: Exception) -> None:
        """Ошибки фоновой проверки — в таблицу logs, а не в stderr демона."""
        self.store.log("ERROR", "proxy", "proxy health check failed", {"error": str(exc)})

    # --- периодическая проверка порога CAPTCHA ---------------------------

    def _start_captcha_check_loop(self) -> threading.Thread | None:
        """Поднимает нить расписания; None — расписание выключено (интервал <= 0)."""
        if self.captcha_check_interval <= 0:
            return None
        thread = threading.Thread(
            target=self._run_captcha_check_loop,
            kwargs={"stop_event": self._stop_event},
            name=CAPTCHA_CHECK_THREAD_NAME,
            daemon=True,
        )
        thread.start()
        return thread

    def _run_captcha_check_loop(self, stop_event: threading.Event) -> None:
        """Ждёт интервал, сверяет долю CAPTCHA с порогом, повторяет.

        Первый запуск — через интервал после старта, как и у проверки
        прокси: немедленная проверка на каждом рестарте добавила бы шума ровно
        в тот момент, когда данных ещё нет. Остановка идёт по тому же
        stop_event, что и у остальных фоновых работ демона.

        Порог и действие читаются из конфига на каждом тике, а не запоминаются
        при сборке демона: это поля config.json, а не константы запуска.
        Edge-семантика (одно действие на переход) живёт внутри политики, здесь
        только расписание.

        Ни упавшая формула, ни упавшее действие не убивают нить: ошибка тика
        уходит в лог с категорией ``captcha`` и именем типа (текст исключения
        БД в лог не попадает — там может быть путь к файлу пользователя).
        """
        while True:
            if stop_event.wait(self.captcha_check_interval):
                return
            try:
                threshold_percent, action = self._captcha_threshold_settings()
                self.captcha_policy.check(threshold_percent, action)
            except Exception as exc:  # noqa: BLE001 - расписание обязано пережить сбой
                self.store.log(
                    "ERROR",
                    "captcha",
                    "periodic captcha threshold check failed",
                    {"error": type(exc).__name__},
                )

    def _captcha_threshold_settings(self) -> tuple[float, str]:
        """Порог и действие с текущего конфига демона: ``(процент, действие)``.

        Чтение идёт через property ``config``, то есть с последним
        применённым патчем из UI: порог и действие действуют на каждом
        следующем тике без рестарта. Конфиг уже прошёл валидацию при
        загрузке/патче (диапазон 0..100, enum действий), поэтому здесь
        только приведение типа: нечисловое значение должно упасть здесь, в
        читаемом месте, а не внутри сравнения доли.
        """
        return (
            float(self.config.get("behavior.captcha_threshold_percent")),
            str(self.config.get("behavior.captcha_threshold_action")),
        )

    # --- ротация логов: закрытие дня, retention, защита от роста ----------

    def _current_config(self) -> Config:
        """Конфиг с последним применённым патчем из UI.

        POST /control/config меняет конфиг под замком ``ControlPlaneServer``,
        и чтение оттуда — единственный путь для job'ов демона (retention,
        db-size, day-close): держать у демона собственную копию значило бы
        читать значение до рестарта. ``Daemon.config`` (property) ведёт в ту
        же ссылку, так что все читатели ходят к одному объекту.
        """
        return self.server.config

    def _day_close_tick(self) -> None:
        """Закрыть день: экспорт в ``logs/YYYY-MM-DD.log``, затем retention.

        Порядок из плана: сначала снимок дня уходит в файл, потом старое
        удаляется — иначе retention могла бы выкинуть ещё не выгруженный день.
        Настройки берутся с каждого тика (``_current_config``), а не
        запоминаются при сборке: это поля config.json.
        """
        day = local_day(time.time())
        config = self._current_config()
        export_dir = default_export_dir()
        exported = export_day(
            self.db_path, day, export_dir, str(config.get("behavior.log_file_level"))
        )
        result = run_retention(
            self.db_path, day, int(config.get("behavior.log_retention_days")), export_dir
        )
        self.store.log(
            "INFO",
            "scheduler",
            "day closed",
            {
                "day": day,
                "exported": exported is not None,
                "deleted_rows": result.deleted_rows,
                "deleted_files": len(result.deleted_files),
            },
        )

    def _midnight_reexport_tick(self) -> None:
        """Допроводка вчерашнего дня сразу после полуночи (00:00:0X).

        Основной проход закрыл день в 23:59:00 и физически не видел записей
        23:59:00–23:59:59 — они приходят в БД после снимка. Здесь выполняется
        тот же экспорт (тем же ``log_file_level``) за **вчерашний** day:
        ``export_day`` перезаписывает файл целиком, поэтому проход идемпотентен
        — повторный запуск не плодит дубли, а опоздавшие строки попадают в
        файл. Retention не выполняется: её проход уже был в основном тике, а
        вчерашний день ей ещё не стар.
        """
        day = previous_local_day(time.time())
        config = self._current_config()
        exported = export_day(
            self.db_path, day, default_export_dir(), str(config.get("behavior.log_file_level"))
        )
        self.store.log(
            "INFO",
            "scheduler",
            "yesterday export topped up",
            {"day": day, "exported": exported is not None},
        )

    def _retention_tick(self) -> None:
        """Удалить дни старше ``log_retention_days``: строки и файлы экспорта."""
        config = self._current_config()
        result = run_retention(
            self.db_path,
            local_day(time.time()),
            int(config.get("behavior.log_retention_days")),
            default_export_dir(),
        )
        if result.deleted_rows or result.deleted_files:
            self.store.log(
                "INFO",
                "cleanup",
                "expired logs purged",
                {
                    "cutoff": result.cutoff,
                    "rows": result.deleted_rows,
                    "files": len(result.deleted_files),
                },
            )

    def _db_size_tick(self) -> None:
        """Держать размер БД в ``db_size_limit_mb``; 0 — лимит выключен.

        Недостижимый лимит — WARNING, а не остановка: защита не имеет права
        блокировать запись логов, иначе полный диск превращался бы в потерю
        видимости именно в тот момент, когда она нужнее всего.
        """
        limit_mb = int(self._current_config().get("behavior.db_size_limit_mb"))
        if limit_mb <= 0:
            return
        result = enforce_db_size_limit(
            self.db_path, limit_mb, export_dir=default_export_dir()
        )
        fields = {
            "limit_mb": limit_mb,
            "size_bytes": result.size_bytes,
            "deleted_days": len(result.deleted_days),
        }
        if not result.fits:
            self.store.log(
                "WARNING",
                "cleanup",
                "db size limit is not reachable",
                {**fields, "error": result.error},
            )
        elif result.deleted_days:
            self.store.log(
                "INFO",
                "cleanup",
                "oldest log days removed to fit db size limit",
                fields,
            )

    # --- нити расписаний ---------------------------------------------------

    def _start_day_close_loop(self) -> threading.Thread | None:
        """Поднимает нить закрытия дня; None — job выключен (интервал <= 0)."""
        if self.day_close_interval is not None and self.day_close_interval <= 0:
            return None
        thread = threading.Thread(
            target=self._run_day_close_loop,
            kwargs={"stop_event": self._stop_event},
            name=DAY_CLOSE_THREAD_NAME,
            daemon=True,
        )
        thread.start()
        return thread

    def _run_day_close_loop(self, stop_event: threading.Event) -> None:
        """Спит до 23:59 (или до env-периода), закрывает день, повторяет.

        Первого запуска «сразу при старте» тут намеренно нет: закрытие дня —
        момент расписания, а экспорт в момент старта переписал бы уже
        закрытый файл и ничего не добавил. Ноль в интервале нить не поднимает
        (см. ``_start_day_close_loop``), поэтому busy-loop исключён.
        Остановка идёт по тому же stop_event, что и у остальных фоновых работ
        демона: shutdown не ждёт наступления 23:59.

        **Второй проход (полночь).** Основной экспорт в 23:59:00 не видит
        записи 23:59:00–23:59:59: они приходят в БД после снимка дня. Поэтому
        в расписании сразу после закрытия дня идёт допроводка в 00:00:0X за
        вчерашний day (``_midnight_reexport_tick``): файл перезаписывается
        целиком, проход идемпотентен и добавляет только опоздавшие строки.
        Retention во втором проходе не выполняется — её уже сделал основной
        тик. В env-периодном режиме (``day_close_interval > 0``) допроводка не
        нужна: экспорт и так крутится непрерывно и пропущенной минуты нет.

        Сбой тика (нет места, БД занята) логируется и не роняет ни нить, ни
        демон — тот же паттерн, что у проверки прокси.
        """
        while True:
            if self.day_close_interval is None:
                if stop_event.wait(seconds_until_day_close(time.time())):
                    return
                self._run_tick(self._day_close_tick, "day close failed")
                if stop_event.wait(seconds_until_midnight_reexport(time.time())):
                    return
                self._run_tick(self._midnight_reexport_tick, "midnight export failed")
                continue
            if stop_event.wait(self.day_close_interval):
                return
            self._run_tick(self._day_close_tick, "day close failed")

    def _run_tick(self, tick: Callable[[], None], error_message: str, category: str = "scheduler") -> None:
        """Один тик расписания: сбой уходит в лог и не убивает нить.

        Общая обёртка для обоих проходов закрытия дня, чтобы по тексту ошибки
        было видно, упал основной экспорт в 23:59 или полночная допроводка.
        ``category`` поднимается вызывающим: очистка профилей пишет свои
        сбои в ``cleanup``, а закрытие дня — в ``scheduler``, как и раньше.
        """
        try:
            tick()
        except Exception as exc:  # noqa: BLE001 - расписание обязано пережить сбой
            self.store.log(
                "ERROR", category, error_message, {"error": type(exc).__name__}
            )

    def _start_retention_loop(self) -> threading.Thread | None:
        """Поднимает нить чистки старых дней; None — расписание выключено."""
        if self.retention_interval <= 0:
            return None
        thread = threading.Thread(
            target=self._run_retention_loop,
            kwargs={"stop_event": self._stop_event},
            name=RETENTION_THREAD_NAME,
            daemon=True,
        )
        thread.start()
        return thread

    def _run_retention_loop(self, stop_event: threading.Event) -> None:
        """Ждёт интервал (сутки по умолчанию), чистит, повторяет.

        Первый запуск — через интервал после старта, как и у проверки прокси:
        retention и так идёт после каждого закрытия дня, а немедленная чистка
        на каждом рестарте удвоила бы нагрузку в и без того нагруженный момент.
        """
        while True:
            if stop_event.wait(self.retention_interval):
                return
            try:
                self._retention_tick()
            except Exception as exc:  # noqa: BLE001 - расписание обязано пережить сбой
                self.store.log(
                    "ERROR", "cleanup", "retention failed", {"error": type(exc).__name__}
                )

    def _start_db_size_loop(self) -> threading.Thread | None:
        """Поднимает нить защиты от роста БД; None — расписание выключено."""
        if self.db_size_interval <= 0:
            return None
        thread = threading.Thread(
            target=self._run_db_size_loop,
            kwargs={"stop_event": self._stop_event},
            name=DB_SIZE_THREAD_NAME,
            daemon=True,
        )
        thread.start()
        return thread

    def _run_db_size_loop(self, stop_event: threading.Event) -> None:
        """Ждёт интервал (час по умолчанию), сверяет размер БД, повторяет.

        Сам лимит читается из конфига на каждом тике: ``db_size_limit_mb`` —
        поле config.json, и ``0`` выключает защиту прямо во время работы.
        """
        while True:
            if stop_event.wait(self.db_size_interval):
                return
            try:
                self._db_size_tick()
            except Exception as exc:  # noqa: BLE001 - расписание обязано пережить сбой
                self.store.log(
                    "ERROR", "cleanup", "db size check failed", {"error": type(exc).__name__}
                )

    # --- очистка профилей (план §5, фаза 10) -------------------------------

    def _cleanup_tick(self) -> None:
        """Один прогон очистки: проверка интервала и работа сервиса.

        Интервал ``cleanup_interval_days`` проверяется только в штатном режиме
        (``cleanup_interval is None``): в env-периодном режиме период в
        секундах заменяет и расписание, и интервал — иначе ускоренные тесты и
        стенд не увидели бы повторных прогонов. Ручной запуск через HTTP
        интервал не проверяет вовсе: кнопка — явное намерение оператора, а
        сдвинутая метка последнего прогона и так отодвинет следующий
        плановый запуск.

        Настройки берутся с каждого тика (``_current_config``): и время, и
        период — поля config.json, меняемые из UI без рестарта.
        """
        config = self._current_config()
        if self.cleanup_interval is None:
            interval_days = int(config.get("behavior.cleanup_interval_days"))
            if not is_cleanup_due(time.time(), last_run_ts(self.store), interval_days):
                self.store.log(
                    "INFO", "cleanup", "cleanup run skipped", {"reason": "interval"}
                )
                return
        self.cleanup_service.run(config)

    def _cleanup_next_run_at(self, now: float) -> float | None:
        """Эпоха следующего разрешённого запуска; None — job выключен.

        В расписательном режиме цель считается через ``seconds_until_cleanup``
        с учётом интервала от последнего прогона — ровно той формулой, по
        которой нить засыпает, чтобы status и сама нить не разъезжались. В
        env-периодном режиме цель — ``now + период``: приближение, но и нить
        пересчитывает её после каждого тика.
        """
        if self.cleanup_interval is not None:
            if self.cleanup_interval <= 0:
                return None
            return now + self.cleanup_interval
        config = self._current_config()
        return now + seconds_until_cleanup(
            now,
            str(config.get("behavior.cleanup_time")),
            int(config.get("behavior.cleanup_interval_days")),
            last_run_ts(self.store),
        )

    def _start_cleanup_loop(self) -> threading.Thread | None:
        """Поднимает нить очистки; None — job выключен (интервал <= 0)."""
        if self.cleanup_interval is not None and self.cleanup_interval <= 0:
            # Выключенный job не имеет права показывать расписание в
            # /control/cleanup/status: пустое значение в kv — это «null».
            self.cleanup_service.set_next_run(None)
            return None
        thread = threading.Thread(
            target=self._run_cleanup_loop,
            kwargs={"stop_event": self._stop_event},
            name=CLEANUP_THREAD_NAME,
            daemon=True,
        )
        thread.start()
        return thread

    def _run_cleanup_loop(self, stop_event: threading.Event) -> None:
        """Стартовая зачистка через grace, затем расписание, пока жив демон.

        **Первый прогон.** Осиротевшие после аварийного Kill профили не должны
        копиться до ближайшего ``cleanup_time``: через ``cleanup_startup_grace``
        нить делает полный прогон и только потом засыпает до цели. Прогон
        записывает метку в kv, поэтому он ограничен и общим
        ``cleanup_interval_days``: перезапуск демона сразу после чистки не
        даёт второго прогона. ``next_run`` пишется дважды и честно: сначала
        «прогон через grace», потом — реальная цель расписания.

        Сбой тика уходит в лог и не роняет ни нить, ни демон — тот же
        паттерн, что у проверки прокси. Сбой расписания (невалидное время в
        конфиге, отказ чтения kv) логируется и ждёт
        ``CLEANUP_SCHEDULE_RETRY_SECONDS``: нить обязана пережить ошибку, но
        не крутить горячий цикл.
        """
        try:
            self.cleanup_service.set_next_run(time.time() + self.cleanup_startup_grace)
        except Exception as exc:  # noqa: BLE001 - запись в kv не должна убивать нить
            self.store.log(
                "ERROR", "cleanup", "cleanup schedule failed", {"error": type(exc).__name__}
            )
        if stop_event.wait(self.cleanup_startup_grace):
            return
        self._run_tick(self._cleanup_tick, "cleanup failed", category="cleanup")

        while True:
            try:
                target = self._cleanup_next_run_at(time.time())
            except Exception as exc:  # noqa: BLE001 - расписание обязано пережить сбой
                self.store.log(
                    "ERROR", "cleanup", "cleanup schedule failed", {"error": type(exc).__name__}
                )
                if stop_event.wait(CLEANUP_SCHEDULE_RETRY_SECONDS):
                    return
                continue
            if target is None:
                # Расписание выключили после старта нити (единственный путь —
                # перезапуск демона): ждём и пересчитываем, не умирая молча.
                if stop_event.wait(CLEANUP_SCHEDULE_RETRY_SECONDS):
                    return
                continue
            try:
                self.cleanup_service.set_next_run(target)
            except Exception as exc:  # noqa: BLE001 - см. выше
                self.store.log(
                    "ERROR", "cleanup", "cleanup schedule failed", {"error": type(exc).__name__}
                )
            if stop_event.wait(max(target - time.time(), 0.001)):
                return
            self._run_tick(self._cleanup_tick, "cleanup failed", category="cleanup")


def supervisor_settings_from_config(config: Config) -> SupervisorSettings:
    """Собирает настройки супервизора из конфига.

    Пока берутся только те параметры, которые конфиг действительно содержит;
    таймауты остановки и backoff остаются дефолтами модуля supervisor, потому
    что в config.json для них полей нет, а выдумывать новые секции в чужом
    формате — значит рассинхронизировать UI и демон.
    """
    return SupervisorSettings(
        heartbeat_interval=HEARTBEAT_INTERVAL_SECONDS,
        shutdown_grace_seconds=SHUTDOWN_GRACE_SECONDS,
        restart_backoff_base=RESTART_BACKOFF_BASE_SECONDS,
        restart_backoff_max=RESTART_BACKOFF_MAX_SECONDS,
        max_restarts=DEFAULT_MAX_RESTARTS,
    )


def build_daemon(
    db_path: str | Path,
    config_path: str | Path,
    token: str | None = None,
    port: int = 8787,
    host: str = LOOPBACK_HOST,
    proxy_check_interval: float | None = None,
    captcha_check_interval: float | None = None,
    day_close_interval: float | None = None,
    retention_interval: float | None = None,
    db_size_interval: float | None = None,
    cleanup_interval: float | None = None,
) -> Daemon:
    """Собирает демона для запуска как самостоятельного процесса.

    Применяет схему к БД: демон запускается на голой машине, где отдельного
    шага "применить миграции" нет, и без этого первый же запрос упал бы с
    "no such table: workers".

    ``proxy_check_interval=None`` и ``captcha_check_interval=None`` — прочитать
    соответствующий интервал из окружения; явное значение важнее окружения
    (так тесты и встраиваемый запуск задают своё, не меняя environ). Нечисловое
    значение окружения — ``ValueError``: молчаливый дефолт при опечатке включил
    бы таймер, который никто не заказывал. То же для четырёх интервалов:
    трёх job'ов ротации логов (``day_close_interval``, ``retention_interval``,
    ``db_size_interval``) и очистки профилей (``cleanup_interval``); у закрытия
    дня и у очистки ``None`` из окружения означает расписание (23:59 и
    ``behavior.cleanup_time`` соответственно), а не выключенный job.

    Конфиг читается один раз здесь и передаётся демону: из него же
    применяется уровень файлового лога (``behavior.log_file_level``) — до
    первого обращения к legacy-зеркалу.
    """
    from engine.db import migrations

    resolved_token = token if token is not None else token_from_environ()
    resolved_interval = (
        proxy_check_interval_from_environ()
        if proxy_check_interval is None
        else proxy_check_interval
    )
    resolved_captcha_interval = (
        captcha_check_interval_from_environ()
        if captcha_check_interval is None
        else captcha_check_interval
    )
    resolved_day_close = (
        day_close_interval_from_environ() if day_close_interval is None else day_close_interval
    )
    resolved_retention = (
        retention_interval_from_environ() if retention_interval is None else retention_interval
    )
    resolved_db_size = (
        db_size_interval_from_environ() if db_size_interval is None else db_size_interval
    )
    resolved_cleanup = (
        cleanup_interval_from_environ() if cleanup_interval is None else cleanup_interval
    )
    migrations.migrate(db_path)
    config = Config.load(config_path)
    # Уровень файлового лога — поле config.json, и читается конфиг именно
    # здесь, при старте демона: legacy-зеркало пишет в adclicker.log уже с
    # нужным уровнем. Импорт ленивый — logger.py создаёт каталог logs/ при
    # импорте, и модуль демона не должен делать этого ни при разборе
    # аргументов, ни при импорте из тестов.
    from logger import apply_file_level

    apply_file_level(str(config.get("behavior.log_file_level")))

    return Daemon(
        db_path=db_path,
        config_path=config_path,
        token=resolved_token,
        port=port,
        host=host,
        config=config,
        proxy_check_interval=resolved_interval,
        captcha_check_interval=resolved_captcha_interval,
        day_close_interval=resolved_day_close,
        retention_interval=resolved_retention,
        db_size_interval=resolved_db_size,
        cleanup_interval=resolved_cleanup,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adclicker-daemon",
        description="Демон Google Ad Clicker: супервизор воркеров и HTTP-управление",
    )
    parser.add_argument("--db", default="adclicker.db", help="путь к БД (по умолчанию adclicker.db)")
    parser.add_argument("--config", default="config.json", help="путь к config.json")
    parser.add_argument(
        "--port",
        type=int,
        default=8787,
        help="порт HTTP на loopback; 0 — выбрать свободный",
    )
    parser.add_argument(
        "--host",
        default=LOOPBACK_HOST,
        help=f"адрес биндинга; допустим только loopback ({LOOPBACK_HOST})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа демона. Возвращает код возврата, ничего не печатая в stdout.

    main() не вызывает sys.exit: так его можно вызвать из теста и проверить
    код возврата, не порождая SystemExit.
    """
    args = build_parser().parse_args(argv)

    try:
        daemon = build_daemon(
            db_path=args.db,
            config_path=args.config,
            port=args.port,
            host=args.host,
        )
    except ConfigError as exc:
        # Раньше ветки: ConfigError — подкласс ValueError, и следующая же
        # ветка перехватывала его первой, так что сообщение «ошибка конфигурации»
        # с перечнем полей пользователь никогда не видел.
        print(f"ошибка конфигурации: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except MissingTokenError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except ValueError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except OSError as exc:
        print(f"ошибка БД: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    daemon.install_signal_handlers()
    daemon.start()
    try:
        # timeout=None: демон живёт, пока не придёт SIGTERM/SIGINT, а не
        # фиксированное число секунд. Ctrl+C сюда не доходит — его перехватил
        # install_signal_handlers.
        daemon.wait_for_shutdown()
    except KeyboardInterrupt:
        # Перехват мог быть не установлен (например, main() звали из теста).
        daemon.shutdown()
    else:
        daemon.shutdown()
    return EXIT_OK


if __name__ == "__main__":
    # sys.exit, а не голый main(): код возврата должен дойти до вызывающей
    # стороны (launchd, sidecar Tauri, прогон из терминала), иначе «нет
    # токена» и «нормально отработал» неразличимы.
    sys.exit(main())
