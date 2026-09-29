"""Супервизор воркеров: спавн, heartbeat, авторестарт, остановка.

Супервизор — единственное место, где демон решает, жив ли воркер. Модуль намеренно
не знает про Selenium, Chrome и HTTP: всё, с чем он работает, — это
``StateStore`` (запись в БД), колбэк ``spawn`` и часы. Это делает всю логику
жизненного цикла проверяемой без единого запущенного браузера.

**Протокол вместо подкласса.** ``spawn(browser_id, command, env)`` возвращает
объект с ``pid``, ``poll()``, ``terminate()``, ``kill()``, ``wait()`` — ровно то,
что умеет ``subprocess.Popen``. Тесты подставляют фейк, продакшн отдаёт
настоящие процессы, и разница между ними не влияет на решения супервизора.

**Почему задержка перед рестартом растёт.** Воркер падает по причине, которая
сейчас не пройдёт (прокси отвалился, диск полон, сайт лёг). Немедленный
рестарт превращает одно падение в плотный цикл из сотен падений в секунду —
процессы и логи съедают машину, а диагностировать причину уже нечем. Отсюда
экспонента с потолком.

**Circuit breaker.** Счётчик рестартов сбрасывается, только если воркер
продержался дольше ``restart_count_reset_after``. Иначе воркер, честно
работающий сутки с парой мелких сбоев, утром упёрся бы в потолок и больше не
поднялся. Обратная сторона: мгновенно падающий воркер исчерпывает потолок за
несколько секунд и замирает — это намеренно, потому что «поднимать зомби» и
выжигать CPU куда хуже, чем остановиться и показать в UI ``circuit_open``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from engine.control_plane.state import StateStore, WorkerStatus

# Heartbeat в БД раз в 5 секунд: чаще — лишние записи в SQLite, реже — UI
# начнёт считать живого воркера мёртвым.
HEARTBEAT_INTERVAL_SECONDS = 5.0

# SIGTERM, затем 10 секунд на доработку текущего сценария, затем SIGKILL.
SHUTDOWN_GRACE_SECONDS = 10.0

# Первая задержка перезапуска и её потолок.
RESTART_BACKOFF_BASE_SECONDS = 2.0
RESTART_BACKOFF_MAX_SECONDS = 60.0

# Потолок рестартов подряд, после которого воркер признаётся сломанным.
DEFAULT_MAX_RESTARTS = 5

# Сколько воркер должен продержаться, чтобы счётчик рестартов обнулился.
DEFAULT_RESTART_COUNT_RESET_AFTER = 300.0

# Воркер, который не прислал heartbeat дольше этого, считается зависшим.
# Три интервала heartbeat с запасом: одна потерянная запись — не зависание.
STALE_MULTIPLIER = 3.0
DEFAULT_STALE_AFTER_SECONDS = STALE_MULTIPLIER * HEARTBEAT_INTERVAL_SECONDS

# Потолок воркеров: страховка от «запустил 1000 браузеров» по кривому конфигу.
DEFAULT_MAX_WORKERS = 8


class SupervisorError(RuntimeError):
    """Базовая ошибка супервизора, отдаваемая в HTTP как JSON."""


class AlreadyRunningError(SupervisorError):
    """Start при уже работающих воркерах."""


class NotRunningError(SupervisorError):
    """Действие, требующее работающих воркеров, вызвано в остановленном состоянии."""


class NotPausedError(SupervisorError):
    """resume вызван без установленной паузы."""


class InvalidWorkerCountError(SupervisorError):
    """Запрошено недопустимое число воркеров."""


class WorkerSpawnError(SupervisorError):
    """Не удалось запустить процесс воркера."""


@dataclass(frozen=True)
class SupervisorSettings:
    """Параметры супервизора.

    Отдельный объект вместо аргументов: настройки приходят из конфига и из
    тестов, и их всегда нужно передавать одним куском, а не поимённо.
    """

    heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS
    shutdown_grace_seconds: float = SHUTDOWN_GRACE_SECONDS
    restart_backoff_base: float = RESTART_BACKOFF_BASE_SECONDS
    restart_backoff_max: float = RESTART_BACKOFF_MAX_SECONDS
    max_restarts: int = DEFAULT_MAX_RESTARTS
    restart_count_reset_after: float = DEFAULT_RESTART_COUNT_RESET_AFTER
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS
    max_workers: int = DEFAULT_MAX_WORKERS

    def __post_init__(self) -> None:
        if self.heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval должен быть положительным")
        if self.shutdown_grace_seconds < 0:
            raise ValueError("shutdown_grace_seconds не может быть отрицательным")
        if self.restart_backoff_base < 0:
            raise ValueError("restart_backoff_base не может быть отрицательным")
        if self.restart_backoff_max < self.restart_backoff_base:
            raise ValueError("restart_backoff_max меньше restart_backoff_base")
        if self.max_restarts < 0:
            raise ValueError("max_restarts не может быть отрицательным")


def backoff_delay(attempt: int, base: float, maximum: float) -> float:
    """Задержка перед рестартом при номере попытки ``attempt``.

    attempt <= 0 — ждать нечего (первый запуск, не рестарт) и возвращается 0.
    Рост идёт умножением, а не степенью: ``2 ** 10000`` — это бесконечность и
    OverflowError, а ограничение сверху должно давать потолок, а не исключение.
    """
    if attempt <= 0:
        return 0.0
    delay = base
    for _ in range(attempt - 1):
        if delay >= maximum:
            return maximum
        delay *= 2
    return min(delay, maximum)


class ProcessLike(Protocol):
    """Минимальный контракт дочернего процесса.

    Выделен в Protocol, а не в ABC, чтобы фейк в тестах не тянул base.py вниз.
    """

    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


SpawnFn = Callable[[str, list[str], dict[str, str]], ProcessLike]


class Clock:
    """Время и сон.

    Отдельный класс, а не прямые вызовы time.sleep: супервизор проверяется
    тестами, где ждать десять секунд grace нельзя, а моканный sleep обязан
    двигать тот же таймер, что и mtime, иначе тесты проверяли бы не то.
    """

    def time(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def default_browser_ids(count: int) -> list[str]:
    """Имена воркеров по умолчанию.

    ``br-1``... соответствует тому, что UI уже показывает в фильтрах по
    browser_id, и остаётся стабильным между рестартами одного воркера.
    """
    return [f"br-{index + 1}" for index in range(count)]


def default_command(browser_id: str) -> list[str]:
    """Команда запуска воркера по умолчанию.

    Модуль Python, а не бинарник в PATH: демон и воркер обязаны быть из одного
    окружения, иначе воркер не найдёт selenium, которого нет в его python.
    """
    return [os.environ.get("PYTHON", "python3"), "-m", "engine.worker", "--browser-id", browser_id]


def spawn_subprocess(browser_id: str, command: list[str], env: dict[str, str]) -> ProcessLike:
    """Настоящий запуск дочернего процесса.

    Новая сессия (start_new_session) нужна, чтобы SIGTERM супервизора не ушёл
    вглубь группы: процессы Chrome, запущенные воркером, пережили бы
    остановку демона и продолжали бы жрать память.
    """
    try:
        return subprocess.Popen(  # noqa: S603 - команда формируется нами, не пользователем
            command,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise WorkerSpawnError(f"не удалось запустить воркер {browser_id}: {exc}") from exc


@dataclass
class _Worker:
    """Живой воркер: процесс, время старта и состояние рестартов.

    Поля дублируют то, что лежит в БД, и это намеренно: решение «рестартить
    или открыть circuit breaker» принимается по счётчику из памяти, а в БД
    попадает тем же числом, чтобы UI видел ровно то, на чём решает демон.
    """

    browser_id: str
    process: ProcessLike
    started_at: float
    restart_count: int = 0
    last_heartbeat: float = 0.0
    # Момент, не раньше которого воркер снова поднимается. Сбросается при
    # каждом падении, поэтому backoff растёт от фактических попыток, а не
    # от номера итерации тика.
    next_start_at: float = 0.0
    run_id: int | None = None
    # Падение уже записано (счётчик и статус обновлены), ждём next_start_at.
    # Без этого флага каждый тик во время backoff засчитывал бы новое падение.
    crash_recorded: bool = False
    # circuit открыт: воркер больше не поднимается без явного рестарта демона.
    circuit_open: bool = False

    def is_alive(self) -> bool:
        return self.process.poll() is None



class Supervisor:
    """Надзор за пулом воркеров.

    Потокобезопасен: ``_lock`` защищает пул воркеров и не отпускается на время
    ввода-вывода в БД, поэтому два одновременных ``tick()`` (супервизор и
    HTTP-обработчик) не удвоят число процессов.
    """

    def __init__(
        self,
        store: StateStore,
        settings: SupervisorSettings | None = None,
        spawn: SpawnFn = spawn_subprocess,
        clock: Clock | None = None,
        browser_ids: Callable[[int], list[str]] = default_browser_ids,
        command_for: Callable[[str], list[str]] = default_command,
        env_for: Callable[[str], dict[str, str]] | None = None,
    ):
        self.store = store
        self.settings = settings or SupervisorSettings()
        self._spawn = spawn
        self._clock = clock or Clock()
        self._browser_ids = browser_ids
        self._command_for = command_for
        self._env_for = env_for or self._default_env
        self._workers: dict[str, _Worker] = {}
        self._pool_size = 0
        self._lock = threading.RLock()

    def _default_env(self, browser_id: str) -> dict[str, str]:
        env = dict(os.environ)
        # Воркер должен знать, кто он: по этому значению он пишет свои строки
        # в логи и клики, и UI раскладывает их по воркерам.
        env["ADCLICKER_BROWSER_ID"] = browser_id
        # Размер пула нужен для раздачи запросов, а признак многопроцессности —
        # чтобы webdriver.py не патчил chromedriver одновременно в N процессах.
        # Оба значения меняются только вместе с пулом, поэтому читаются здесь,
        # а не в БД: env достаётся воркеру атомарно со спавном.
        env["ADCLICKER_POOL_SIZE"] = str(max(0, self._pool_size))
        env["ADCLICKER_MULTI_BROWSERS"] = "1" if self._pool_size > 1 else "0"
        return env

    # --- жизненный цикл пула --------------------------------------------

    def start(self, count: int) -> list[str]:
        """Запускает ``count`` воркеров. Возвращает их browser_id.

        Повторный вызов при непустом пуле — ошибка, а не no-op: пользователь
        нажал «старт» второй раз, и молчаливый игнор скрыл бы от него, что
        запущено не 3, а 6 браузеров.
        """
        with self._lock:
            if self._workers:
                raise AlreadyRunningError(
                    f"воркеры уже запущены: {sorted(self._workers)}. "
                    "Сначала остановите их (POST /control/stop)"
                )
            self._validate_count(count)
            # Размер пула ставится ДО спавна: он уходит воркерам через env и
            # определяет, кому какой запрос достанется. Сброс — в stop() и в
            # откате ниже, иначе env следующего пула соврал бы.
            self._pool_size = count

            ids = self._browser_ids(count)
            self.store.set_run_state("running")
            try:
                for browser_id in ids:
                    self._start_worker(browser_id, restart_count=0)
            except SupervisorError:
                # Частично поднятый пул — худшее состояние: часть браузеров
                # работает, часть нет, и демон это не показывает. Откатываем.
                self._stop_all_locked(grace=self.settings.shutdown_grace_seconds)
                self._pool_size = 0
                self.store.set_run_state("stopped")
                raise
            return ids

    def _validate_count(self, count: int) -> None:
        if count < 1:
            raise InvalidWorkerCountError(
                f"число воркеров должно быть не меньше 1, получено {count}"
            )
        if count > self.settings.max_workers:
            raise InvalidWorkerCountError(
                f"число воркеров превышает потолок {self.settings.max_workers}, получено {count}"
            )

    def _start_worker(self, browser_id: str, restart_count: int) -> None:
        process = self._spawn_checked(browser_id)
        now = self._clock.wall()
        worker_id = self.store.register_worker(browser_id, process.pid, now=now)
        self._workers[browser_id] = _Worker(
            browser_id=browser_id,
            process=process,
            started_at=now,
            restart_count=restart_count,
            last_heartbeat=now,
            next_start_at=now,
            run_id=self.store.start_run(worker_id),
        )
        self.store.log(
            "INFO",
            "supervisor",
            "worker started",
            {"pid": process.pid, "restart_count": restart_count},
            browser_id=browser_id,
        )

    def _spawn_checked(self, browser_id: str) -> ProcessLike:
        """Запуск с приведением любой ошибки ОС к WorkerSpawnError.

        Подменяемая фабрика (в тестах и в будущем пути запуска) может бросить
        что угодно, а вызывающему нужен один тип ошибки, иначе обработчик
        HTTP отдаст 500 там, где положен 503 с понятным текстом.
        """
        try:
            return self._spawn(browser_id, self._command_for(browser_id), self._env_for(browser_id))
        except SupervisorError:
            raise
        except OSError as exc:
            raise WorkerSpawnError(f"не удалось запустить воркер {browser_id}: {exc}") from exc


    def stop(self) -> None:
        """Останавливает всех воркеров: SIGTERM, grace, SIGKILL, запись в БД.

        Повторный вызов безопасен: если пул уже пуст, просто закрываются записи
        о запусках — иначе двойной клик по «стоп» в UI оставил бы висящие
        записи runs без ended_at.
        """
        with self._lock:
            self._stop_all_locked(grace=self.settings.shutdown_grace_seconds)
            self._finish_open_runs("stopped")
            self.store.clear_pause()
            self.store.set_run_state("stopped")
            self._pool_size = 0
            self.store.log("INFO", "supervisor", "supervisor stopped", {"workers": 0})

    def _stop_all_locked(self, grace: float) -> None:
        """SIGTERM всем, общая выдержка, затем SIGKILL оставшимся.

        Сигналы рассылаются ПО ВСЕМ процессам до первого ожидания, а не по
        одному «сигнал -> ждать -> следующий»: при последовательном порядке
        остановка пула из N воркеров стоила бы N x grace, и Tauri-хост,
        дающий демону 10 секунд, SIGKILL'ил бы его посреди работы — вместе
        с ним погибал бы и остаток очереди, и воркеры (они в собственных
        сессиях) оставались бы сиротами с открытыми Chrome.

        Общий дедлайн, а не свой у каждого: ждать grace на каждого — ровно
        та арифметика, от которой мы уходим.
        """
        alive = [worker for worker in self._workers.values() if worker.is_alive()]
        for worker in alive:
            worker.process.terminate()

        deadline = self._clock.time() + grace
        for worker in alive:
            while worker.is_alive() and self._clock.time() < deadline:
                self._clock.sleep(0.1)

        # SIGKILL уходит всем, кто остался, до первого ожидания — та же
        # логика, что и с SIGTERM: иначе худший случай снова складывается
        # в N x grace и не влезает в STOP_GRACE Tauri-хоста.
        stragglers = [worker for worker in alive if worker.is_alive()]
        for worker in stragglers:
            # Не послушался SIGTERM: Chrome внутри мог зависнуть на диалоге.
            worker.process.kill()

        kill_deadline = self._clock.time() + grace
        for worker in stragglers:
            while worker.is_alive() and self._clock.time() < kill_deadline:
                self._clock.sleep(0.1)
            if worker.is_alive():
                self.store.log(
                    "WARN",
                    "supervisor",
                    "worker did not exit after SIGKILL",
                    {"pid": worker.process.pid},
                    browser_id=worker.browser_id,
                )

        for browser_id in list(self._workers):
            worker = self._workers.pop(browser_id)
            if not worker.circuit_open:
                self.store.set_status(browser_id, WorkerStatus.STOPPED)

    def restart(self, count: int) -> list[str]:
        """Полная перезагрузка пула: stop, затем start с новым числом воркеров."""
        with self._lock:
            if not self._workers:
                raise NotRunningError("нечего перезапускать: воркеры не запущены")
            self._validate_count(count)
            self._stop_all_locked(grace=self.settings.shutdown_grace_seconds)
            self._finish_open_runs("stopped")
            self._pool_size = count
            self.store.set_run_state("running")
            for browser_id in self._browser_ids(count):
                self._start_worker(browser_id, restart_count=0)
            return sorted(self._workers)

    def pause(self) -> None:
        """Просит воркеров доработать текущий сценарий и не начинать новый.

        Процессы не трогаются: убийство на середине сценария означало бы
        потерю результатов и полупустые записи runs.
        """
        with self._lock:
            if not self._workers:
                raise NotRunningError("нечего приостанавливать: воркеры не запущены")
            self.store.request_pause()
            self.store.set_run_state("paused")
            self.store.log("INFO", "supervisor", "pause requested", {"workers": len(self._workers)})

    def resume(self) -> None:
        """Снимает паузу: воркеры снова берут новые сценарии."""
        with self._lock:
            if not self.store.is_pause_requested():
                raise NotPausedError("пауза не установлена, снимать нечего")
            self.store.clear_pause()
            self.store.set_run_state("running" if self._workers else "stopped")
            self.store.log("INFO", "supervisor", "pause cleared", {"workers": len(self._workers)})

    def _finish_open_runs(self, status: str) -> None:
        """Закрывает записи runs, оставшиеся незакрытыми.

        Последняя запись в БД перед выходом демона: UI не должен остаться с
        навсегда «running»-запусками, по которым потом считает аптайм.
        """
        for worker_id in [w["id"] for w in self.store.list_workers()]:
            run_id = self.store.latest_run_id(worker_id)
            if run_id is None:
                continue
            with self.store._connect() as conn:
                row = conn.execute("SELECT ended_at FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is not None and row["ended_at"] is None:
                self.store.finish_run(run_id, status)

    # --- тик -------------------------------------------------------------

    def tick(self) -> None:
        """Один цикл надзора: heartbeat, сбор падений, рестарты по backoff.

        Синхронный и возвращающий управление: фоновый поток и HTTP-запрос
        вызывают одно и то же, и обе стороны получают одинаковое поведение.
        """
        with self._lock:
            self._heartbeat_due()
            self._mark_stale_workers()
            for browser_id, worker in list(self._workers.items()):
                self._reconcile(browser_id, worker)

    def _heartbeat_due(self) -> None:
        """Обновляет heartbeat тем воркерам, у кого истёк интервал."""
        now = self._clock.wall()
        for browser_id, worker in self._workers.items():
            if worker.circuit_open:
                continue
            if worker.last_heartbeat + self.settings.heartbeat_interval > now:
                continue
            self.store.heartbeat(browser_id, now=now)
            worker.last_heartbeat = now

    def _mark_stale_workers(self) -> None:
        """Помечает зависших воркеров, которые не прислали heartbeat вовремя.

        Процесс может быть жив, но бесполезен: браузер не отвечает, сокет
        заблокирован. По одному poll() такой воркер неотличим от здорового, и
        демон ждал бы его вечно. Решение принимает _reconcile: он увидит, что
        heartbeat устарел, и переведёт воркера в путь рестарта.
        """
        now = self._clock.wall()
        for browser_id, worker in self._workers.items():
            if worker.circuit_open or not worker.is_alive():
                continue
            if now - worker.last_heartbeat <= self.settings.stale_after_seconds:
                continue
            # Устаревший heartbeat — это и есть падение с точки зрения
            # демона: зависший процесс нельзя оставить в статусе running.
            self.store.log(
                "WARN",
                "supervisor",
                "worker heartbeat is stale, treating as dead",
                {"pid": worker.process.pid, "stale_after": self.settings.stale_after_seconds},
                browser_id=browser_id,
            )
            worker.process.terminate()
            worker.process.wait(timeout=self.settings.shutdown_grace_seconds)
            self.store.set_status(
                browser_id, WorkerStatus.BACKOFF, error="no heartbeat, worker considered stuck"
            )


    def _reconcile(self, browser_id: str, worker: _Worker) -> None:
        """Приводит одного воркера в соответствие с реальностью.

        Три состояния, и порядок проверок важен:

        1. circuit открыт — ничего не делаем, пользователь должен вмешаться;
        2. процесс жив — обновляем статус на running;
        3. процесс мёртв — либо ждём backoff, либо падение уже записано и
           пора поднимать, либо записываем падение впервые.
        """
        if worker.circuit_open:
            return

        if worker.is_alive():
            self._mark_running(browser_id, worker)
            return

        if worker.crash_recorded:
            self._maybe_respawn(browser_id, worker)
            return

        self._handle_crash(browser_id, worker)

    def _mark_running(self, browser_id: str, worker: _Worker) -> None:
        """Переводит воркера в running и обнуляет счётчик рестартов.

        Обнуление здесь, а не по отдельному таймеру, — потому что момент
        "воркер здоров" наступает ровно тогда, когда супервизор увидел живой
        процесс. Тикает чаще — перевода не происходит, статус уже running.
        """
        stored = self.store.get_worker(browser_id)
        if stored is not None and stored["status"] != WorkerStatus.RUNNING.value:
            self.store.set_status(browser_id, WorkerStatus.RUNNING)
        self._maybe_reset_restart_count(worker)

    def _maybe_respawn(self, browser_id: str, worker: _Worker) -> None:
        """Поднимает воркера, если срок backoff вышел."""
        if self._clock.wall() < worker.next_start_at:
            return
        self._restart_worker(browser_id, worker)

    def _maybe_reset_restart_count(self, worker: _Worker) -> None:
        """Обнуляет счётчик рестартов, если воркер проработал достаточно долго.

        Без этого потолок рестартов копился бы за всю жизнь воркера, и пара
        сбоев за сутки однажды остановила бы его навсегда. Порог — половина
        потолка: показывать пользователю, что демон считает воркера проблемным,
        полезно и до того, как circuit breaker его убьёт.
        """
        if worker.restart_count == 0:
            return
        if self._clock.wall() - worker.started_at < self.settings.restart_count_reset_after:
            return
        worker.restart_count = 0
        worker.crash_recorded = False
        self.store.reset_restart_count(worker.browser_id)

    def _handle_crash(self, browser_id: str, worker: _Worker) -> None:
        """Первое обнаружение падения: счётчик, статус, запись в лог.

        Рестарт здесь не происходит — только назначается время следующей
        попытки. Иначе падение, замеченное тиком, подняло бы воркер мгновенно,
        и весь backoff оказался бы мёртвым кодом.
        """
        returncode = worker.process.poll()
        worker.crash_recorded = True

        if worker.restart_count >= self.settings.max_restarts:
            self._open_circuit(browser_id, worker, returncode)
            return

        attempt = worker.restart_count + 1
        delay = backoff_delay(
            attempt,
            base=self.settings.restart_backoff_base,
            maximum=self.settings.restart_backoff_max,
        )
        worker.restart_count = attempt
        worker.next_start_at = self._clock.wall() + delay

        self.store.increment_restart_count(browser_id)
        self.store.set_status(
            browser_id,
            WorkerStatus.BACKOFF,
            error=f"exited with code {returncode}, restart in {delay:.1f}s",
        )
        self.store.log(
            "ERROR",
            "supervisor",
            "worker crash detected",
            {"exit_code": returncode, "restart_in": delay, "attempt": attempt},
            browser_id=browser_id,
        )
        self._finish_run(worker, "crashed", f"exit code {returncode}")

    def _open_circuit(self, browser_id: str, worker: _Worker, returncode: int | None) -> None:
        """Потолок рестартов исчерпан: воркер больше не поднимается.

        Раскрытая цепь означает «этот экземпляр кликера неисправен», а не
        «демон сломался»: остальные воркеры продолжают работать, а в UI
        строка воркера остаётся с last_error, чтобы пользователь понял причину.
        """
        worker.circuit_open = True
        reason = (
            f"restart limit reached ({self.settings.max_restarts}), "
            f"last exit code {returncode}"
        )
        # Переход в circuit_open идёт через set_status с pid=NULL, поэтому
        # убитый процесс не остаётся висеть в БД как живой.
        self.store.set_status(browser_id, WorkerStatus.CIRCUIT_OPEN, error=reason)
        self.store.log(
            "ERROR",
            "supervisor",
            "circuit breaker opened, worker will not be restarted",
            {"max_restarts": self.settings.max_restarts, "exit_code": returncode},
            browser_id=browser_id,
        )
        self._finish_run(worker, "crashed", reason)
        del self._workers[browser_id]

    def _restart_worker(self, browser_id: str, worker: _Worker) -> None:
        process = self._spawn_checked(browser_id)
        now = self._clock.wall()
        worker.process = process
        worker.started_at = now
        worker.last_heartbeat = now
        worker.next_start_at = now
        # Сбрасываем флаг, иначе следующее падение не было бы засчитано:
        # тик увидел бы crash_recorded=True и ушёл в ветку backoff без записи.
        worker.crash_recorded = False
        self.store.register_worker(browser_id, process.pid, now=now)
        self.store.set_status(browser_id, WorkerStatus.STARTING)
        self.store.log(
            "INFO",
            "supervisor",
            "worker restarted",
            {"pid": process.pid, "attempt": worker.restart_count},
            browser_id=browser_id,
        )


    def _finish_run(self, worker: _Worker, status: str, error: str | None) -> None:
        if worker.run_id is None:
            return
        self.store.finish_run(worker.run_id, status, error)
        worker.run_id = None

    # --- фоновый цикл ---------------------------------------------------

    def run_idle(self, stop_after: int | None = None, stop_event: threading.Event | None = None) -> None:
        """Цикл тиков, пока не попросят остановиться.

        Нужен главному потоку демона: без отдельного цикла демон заметил бы
        упавший воркер только когда кто-нибудь дёрнет /state, а в простое
        (пользователь закрыл UI) падение останется незамеченным.

        stop_event — внешняя остановка, stop_after — ограничение для тестов,
        чтобы не крутить бесконечный цикл.
        """
        ticks = 0
        while stop_after is None or ticks < stop_after:
            if stop_event is not None and stop_event.is_set():
                return
            self.tick()
            ticks += 1
            if stop_after is not None and ticks >= stop_after:
                return
            if stop_event is not None:
                if stop_event.wait(self.settings.heartbeat_interval):
                    return
            else:
                self._clock.sleep(self.settings.heartbeat_interval)

    def start_background(self) -> threading.Thread:
        """Запускает тики в отдельном потоке и возвращает его вместе с stop_event.

        HTTP-сервер живёт в своих потоках (ThreadingHTTPServer), и без
        отдельного потока супервизора демон не заметил бы упавший воркер, пока
        кто-нибудь не дёрнул /state.
        """
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self.run_idle,
            kwargs={"stop_event": stop_event},
            name="supervisor",
            daemon=True,
        )
        thread.stop_event = stop_event  # type: ignore[attr-defined]
        thread.start()
        return thread


    # --- наблюдение ------------------------------------------------------

    def alive_browser_ids(self) -> list[str]:
        with self._lock:
            return sorted(
                browser_id
                for browser_id, worker in self._workers.items()
                if worker.is_alive() and not worker.circuit_open
            )

    def process_count(self) -> int:
        with self._lock:
            return len(self._workers)

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._workers)

    def health(self) -> dict[str, Any]:
        """Сводка для /health: демон жив, пул и как он себя чувствует."""
        with self._lock:
            alive = self.alive_browser_ids()
            open_circuits = sorted(
                browser_id for browser_id, w in self._workers.items() if w.circuit_open
            )
            return {
                "supervisor_alive": True,
                "workers_total": len(self._workers),
                "workers_alive": len(alive),
                "workers_failed": len(open_circuits),
                "circuits_open": open_circuits,
                "paused": self.store.is_pause_requested(),
            }


def default_signal_handlers() -> dict[int, Any]:
    """Номера сигналов, по которым демон обязан завершиться.

    Вынесено отдельно, чтобы main() не тащил signal в себя и чтобы тесты могли
    проверить набор сигналов, не вызывая signal.signal.
    """
    return {signal.SIGTERM, signal.SIGINT}
