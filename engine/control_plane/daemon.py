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
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Sequence

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

# Сигналы, по которым демон завершается. SIGINT — Ctrl+C при запуске из
# терминала, SIGTERM — systemd/stop-скрипт. Оба обязаны приводить к
# одинаковой последовательности: SIGTERM -> grace -> SIGKILL по воркерам.
SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)

# Коды возврата main(). Разные, чтобы вызывающая сторона (тест, сервис,
# скрипт установки) могла отличить "нет токена" от "сломан конфиг".
EXIT_OK = 0
EXIT_CONFIG_ERROR = 2


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
    ):
        self.db_path = Path(db_path)
        self.config_path = Path(config_path)
        self.token = token
        self.port = port
        self.store = store or StateStore(self.db_path)
        self.config = config or Config.load(self.config_path)
        self.supervisor = supervisor or Supervisor(
            store=self.store,
            settings=supervisor_settings_from_config(self.config),
        )
        self.server = ControlPlaneServer(
            supervisor=self.supervisor,
            config=self.config,
            token=self.token,
            config_path=self.config_path,
            host=host,
            port=port,
        )
        self._stop_event = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._supervisor_thread: threading.Thread | None = None
        self._shutdown_thread: threading.Thread | None = None
        self._previous_handlers: dict[int, Any] = {}
        self._started = False

    # --- жизненный цикл ---------------------------------------------------

    def start(self) -> None:
        """Поднимает сервер и фоновый цикл супервизора."""
        if self._started:
            return
        self._stop_event.clear()
        self.server.start()
        self.port = self.server.port
        self._supervisor_thread = self.supervisor.start_background()
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

            thread = self._supervisor_thread
            if thread is not None:
                stop_event = getattr(thread, "stop_event", None)
                if stop_event is not None:
                    stop_event.set()
                # Ждём поток: иначе он может дописать heartbeat в БД уже после
                # того, как демон объявил себя остановленным.
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
) -> Daemon:
    """Собирает демона для запуска как самостоятельного процесса.

    Применяет схему к БД: демон запускается на голой машине, где отдельного
    шага "применить миграции" нет, и без этого первый же запрос упал бы с
    "no such table: workers".
    """
    from engine.db import migrations

    resolved_token = token if token is not None else token_from_environ()
    migrations.migrate(db_path)

    return Daemon(
        db_path=db_path,
        config_path=config_path,
        token=resolved_token,
        port=port,
        host=host,
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
