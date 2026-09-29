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
from pathlib import Path
from typing import Any, Sequence

from engine.captcha_threshold import CaptchaThresholdPolicy
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
    ):
        self.db_path = Path(db_path)
        self.config_path = Path(config_path)
        self.token = token
        self.port = port
        self.store = store or StateStore(self.db_path)
        self.config = config or Config.load(self.config_path)
        self.proxy_check_interval = proxy_check_interval
        # Период проверки порога CAPTCHA — своя настройка запуска, а не
        # соседний интервал: выключить один job'ом можно, не выключая другой.
        self.captcha_check_interval = captcha_check_interval
        # Три job'а ротации логов — свои интервалы по той же причине:
        # выключить защиту от роста не должно значить отключить закрытие дня.
        self.day_close_interval = day_close_interval
        self.retention_interval = retention_interval
        self.db_size_interval = db_size_interval
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
            settings=supervisor_settings_from_config(self.config),
            # Тот же пул, что у /control/proxies и расписания: иначе
            # назначения супервизора и то, что видит UI, разъезжались бы.
            proxy_pool=self.proxy_pool,
            profile_pool=self.profile_pool,
        )
        self.server = ControlPlaneServer(
            supervisor=self.supervisor,
            config=self.config,
            token=self.token,
            config_path=self.config_path,
            host=host,
            port=port,
            proxy_pool=self.proxy_pool,
            profile_pool=self.profile_pool,
            proxy_checker=self.proxy_checker,
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
        self._shutdown_thread: threading.Thread | None = None
        self._previous_handlers: dict[int, Any] = {}
        self._started = False

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
            # таймеру — иначе shutdown ждал бы закрытия дня.
            for attribute, thread in (
                ("_day_close_thread", self._day_close_thread),
                ("_retention_thread", self._retention_thread),
                ("_db_size_thread", self._db_size_thread),
            ):
                if thread is not None:
                    thread.join(timeout=SHUTDOWN_GRACE_SECONDS)
                setattr(self, attribute, None)

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

        Конфиг уже прошёл валидацию при загрузке (диапазон 0..100, enum
        действий), поэтому здесь только приведение типа: нечисловое значение
        должно упасть здесь, в читаемом месте, а не внутри сравнения доли.
        """
        return (
            float(self.config.get("behavior.captcha_threshold_percent")),
            str(self.config.get("behavior.captcha_threshold_action")),
        )

    # --- ротация логов: закрытие дня, retention, защита от роста ----------

    def _current_config(self) -> Config:
        """Конфиг с последним применённым патчем из UI.

        POST /control/config заменяет объект у HTTP-сервера (и у его
        bound-обработчика), но не у демона — а план требует, чтобы настройка
        хранения логов применялась без рестарта. Поэтому чтение идёт от
        сервера: при сборке демон передал ему тот же объект, что и держит
        сам, так что инъекция конфига в тестах работает как раньше.
        """
        return getattr(self.server, "config", self.config)

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

        Сбой тика (нет места, БД занята) логируется и не роняет ни нить, ни
        демон — тот же паттерн, что у проверки прокси.
        """
        while True:
            wait = (
                self.day_close_interval
                if self.day_close_interval is not None
                else seconds_until_day_close(time.time())
            )
            if stop_event.wait(wait):
                return
            try:
                self._day_close_tick()
            except Exception as exc:  # noqa: BLE001 - расписание обязано пережить сбой
                self.store.log(
                    "ERROR", "scheduler", "day close failed", {"error": type(exc).__name__}
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
        поле config.json, а ``0`` выключает защиту прямо во время работы.
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
) -> Daemon:
    """Собирает демона для запуска как самостоятельного процесса.

    Применяет схему к БД: демон запускается на голой машине, где отдельного
    шага "применить миграции" нет, и без этого первый же запрос упал бы с
    "no such table: workers".

    ``proxy_check_interval=None`` и ``captcha_check_interval=None`` — прочитать
    соответствующий интервал из окружения; явное значение важнее окружения
    (так тесты и встраиваемый запуск задают своё, не меняя environ). Нечисловое
    значение окружения — ``ValueError``: молчаливый дефолт при опечатке включил
    бы таймер, который никто не заказывал. То же для трёх интервалов ротации
    логов (``day_close_interval``, ``retention_interval``,
    ``db_size_interval``); у закрытия дня ``None`` из окружения означает
    расписание в 23:59, а не выключенный job.
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
    migrations.migrate(db_path)

    return Daemon(
        db_path=db_path,
        config_path=config_path,
        token=resolved_token,
        port=port,
        host=host,
        proxy_check_interval=resolved_interval,
        captcha_check_interval=resolved_captcha_interval,
        day_close_interval=resolved_day_close,
        retention_interval=resolved_retention,
        db_size_interval=resolved_db_size,
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
