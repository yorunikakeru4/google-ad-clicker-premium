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

**Heartbeat.** В БД ``heartbeat_at`` пишет сам воркер — фоновым потоком через
``StoreWriter.heartbeat`` (upsert, см. ``engine/store.py``). Супервизор здесь
только наблюдает: раз в тик читает свежие отметки одним запросом и двигает
in-memory ``last_heartbeat`` строго по факту роста значения из БД. Это и есть
условие, при котором «живой, но зависший» процесс находится полным ``tick()``:
если бы писал супервизор, он обновлял бы отметку сам и обнулял бы возраст
перед проверкой stale — детект зависания был бы мёртв.

**Назначение прокси.** Супервизор — единственный, кто выдаёт
``ADCLICKER_PROXY``: пул читается на каждом спавне, выбор делается по живым
(``is_alive=1``) и не занятым другим живым воркером строкам, внутри
категории берётся наименее используемая (``usage_count``, затем ``id``), чтобы
нагрузка не садилась на один прокси. Свободных нет — прокси делится с WARNING
(старт блокировать нельзя); живых нет или пул пуст — спавн без
``ADCLICKER_PROXY``, как и до этой фичи. Наследие переменной из окружения
демона снимается: иначе воркеры мимо пула получили бы один и тот же прокси.
Факт выдачи уходит в ``workers.proxy_id`` (StateStore), строку ``proxy_usage``
и лог с категорией ``proxy`` — без кредов, только ``proxy_id``.

**Ротация.** Воркер, пометивший себя ``degraded`` (прокси/CDP перестал
работать), не остаётся в ``running``: ``tick()`` видит сигнал и подменяет
прокси на резервный — свободный живой, не текущий. Резерва нет — воркер
остаётся ``degraded`` с WARNING и пробует снова через backoff, процесс при
этом не убивается: без резерва убийство ничего не чинит, а теряло бы
работающий сценарий. Ротация — не падение воркера, поэтому ``restart_count``
и circuit breaker она не трогает (иначе несколько смен прокси убили бы
здорового воркера потолком рестартов); наблюдаемость дают ``proxy_usage``,
логи и статус в ``/state``. Темп задаётся тем же ``backoff_delay``, что и у
рестартов: повторная деградация сразу после подмены не превращается в
плотный цикл.

**Назначение профиля.** Каждый спавн (первый, респавн после падения, ротация
прокси) начинается с ``ProfilePool.take_for_worker``: выданный профиль уходит
воркеру в ``ADCLICKER_PROFILE_ID`` (десятичный id), а его собственный
``proxy_id`` становится ``ADCLICKER_PROXY`` — приоритетнее выбора из пула,
потому что аккаунт не должен уезжать в чужую геолокацию. Профиль переживает
и падение, и ротацию: полный релиз (``ProfilePool.release`` +
``release_assignment``) делается только на stop/kill и при раскрытии circuit
breaker. Страховка от потери назначения — реапер в ``tick()``: профиль, чей
воркер умер, удалён из реестра или не стучал heartbeat'ом, возвращается в
пул, но ``workers.profile_id`` остаётся — это память, благодаря которой
респавн получает тот же профиль.

**Лексика ``proxy_usage.result``** (пишется только здесь, значений больше нет):

* ``assigned`` — выдача при спавне/респавне;
* ``shared`` — выдача при дележе: свободных живых прокси не осталось;
* ``rotated`` — выдача резервного прокси при ротации;
* ``exhausted`` — запись по прокси, который воркер пометил деградировавшим.
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
from engine.profile_pool import ProfilePool
from engine.proxy_pool import ProxyError, ProxyPool

# Кадр наблюдения: так часто супервизор читает heartbeat'ы воркеров из БД
# (и так же спит между тиками). Саму запись в БД делает воркер со своей
# стороны своей константой (engine.worker.WORKER_HEARTBEAT_INTERVAL_SECONDS)
# — она дублируется, а не импортируется, чтобы воркер не тянул настройки
# демона. Часто — лишние чтения SQLite, реже — детект зависания опоздает.
HEARTBEAT_INTERVAL_SECONDS = 5.0

# Env-контракт воркера (engine.worker.PROXY_ENV): значение вида
# user:pass@host:port. Дублируется, а не импортируется из engine.worker:
# тот тянет selenium, а супервизор обязан оставаться бесплатным от браузера.
PROXY_ENV = "ADCLICKER_PROXY"

# Env-контракт профиля: десятичный id строки profiles, без ведущих нулей и
# без форматирования — ровно то, что печатает INTEGER PRIMARY KEY. Читает
# воркер/legacy (следующая волна), поэтому значение не кодируется ни во что
# ещё. Дублируется, а не импортируется, по той же причине, что и PROXY_ENV.
PROFILE_ENV = "ADCLICKER_PROFILE_ID"

# Лексика proxy_usage.result, которую пишет супервизор (см. докстринг модуля).
PROXY_USAGE_ASSIGNED = "assigned"
PROXY_USAGE_SHARED = "shared"
PROXY_USAGE_ROTATED = "rotated"
PROXY_USAGE_EXHAUSTED = "exhausted"

# Как каждый результат выглядит в логе. Уровень и формулировка живут здесь,
# а не в местах вызова: «дележ» и «исчерпание» — WARNING (нужно решение
# оператора), «выдача» — INFO, и расхождение между двумя ротациями
# невозможно по построению.
_PROXY_USAGE_LOGS: dict[str, tuple[str, str]] = {
    PROXY_USAGE_ASSIGNED: ("INFO", "proxy assigned"),
    PROXY_USAGE_SHARED: ("WARNING", "proxy shared: no free proxy left"),
    PROXY_USAGE_ROTATED: ("INFO", "proxy rotated"),
    PROXY_USAGE_EXHAUSTED: ("WARNING", "proxy exhausted"),
}

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
# Три интервала с запасом: воркер пишет раз в 5 с, супервизор читает раз в
# 5 с — 15 с переживают две потерянные записи подряд и грейс после спавна
# (первое наблюдение за новым воркером приходит не раньше интервала).
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

    # Кадр фонового цикла: run_idle спит между тиками, поэтому столько же
    # раз в секунду супервизор и читает heartbeat'ы воркеров. Саму запись в
    # БД делает воркер, а порог stale считается от того же интервала
    # (STALE_MULTIPLIER).
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


def _least_used(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Наименее используемые строки вперёд, при равенстве — по возрастанию id.

    ``usage_count`` берётся потому, что уже посчитан в ``list_proxies()``
    (join с ``proxy_usage``): отдельный запрос ради сортировки стоил бы
    второго прохода по таблице. ``id`` замыкает порядок, иначе два прокси с
    одинаковым счётчиком менялись бы местами между чтениями.
    """
    return sorted(rows, key=lambda row: (row["usage_count"], row["id"]))


def proxy_env_value(proxy: dict[str, Any]) -> str:
    """Значение ``ADCLICKER_PROXY`` для воркера: ``[user:pass@]host:port``.

    Схема (``socks5://`` и friends) не передаётся: env-контракт воркера
    (``engine.worker.proxy_from_environ``) зафиксирован на
    ``user:pass@host:port`` и раньше никогда не получал схемы. Строка
    уходит только в окружение процесса — в логи и в ``proxy_usage`` попадает
    исключительно ``proxy_id`` (см. ``_record_proxy_use``).
    """
    host_port = f"{proxy['host']}:{proxy['port']}"
    username = proxy.get("username")
    password = proxy.get("password")
    if username and password:
        return f"{username}:{password}@{host_port}"
    return host_port


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
    # Наблюдаемая отметка heartbeat, а не написанная супервизором: начальное
    # значение ставится при register/restart (это и есть грейс на первое
    # наблюдение), дальше растёт только по факту роста heartbeat_at в БД —
    # его пишет сам воркер. Замершее значение = кандидат на stale.
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
    # Ротация прокси: число попыток подмены и момент следующей. Живут в
    # памяти, как и restart_count, — решение принимает демон, а в БД
    # результат уходит статусом и строками proxy_usage.
    rotation_count: int = 0
    next_rotation_at: float = 0.0
    # «exhausted» для текущей деградации уже записан: без флага каждый тик
    # плодил бы новую строку, пока резерв не найден.
    degraded_recorded: bool = False

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
        proxy_pool: ProxyPool | None = None,
        profile_pool: ProfilePool | None = None,
    ):
        self.store = store
        self.settings = settings or SupervisorSettings()
        self._spawn = spawn
        self._clock = clock or Clock()
        self._browser_ids = browser_ids
        self._command_for = command_for
        self._env_for = env_for or self._default_env
        # Пул прокси — своя таблица в той же БД, поэтому пустой по умолчанию
        # экземпляр равнозначен «пула нет»: строк нет → спавн без прокси, как
        # до этой фичи. Демон передаёт свой (общий с /control/proxies).
        self.proxy_pool = (
            proxy_pool if proxy_pool is not None else ProxyPool(store.db_path)
        )
        # Профили — та же схема, что и у прокси: экземпляр по умолчанию
        # строится на той же БД, а демон передаёт свой (общий с
        # /control/profiles), чтобы выдача и HTTP не расходились.
        self.profile_pool = (
            profile_pool if profile_pool is not None else ProfilePool(store.db_path)
        )
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
        # Профиль берётся ДО спавна: он и его прокси уходят в env процесса,
        # а выбор прокси профилю проигрывает (см. _pick_spawn_proxy).
        profile = self._take_profile(browser_id)
        try:
            picked = self._pick_spawn_proxy(browser_id, profile)
            proxy = None if picked is None else picked[0]
            process = self._spawn_checked(browser_id, proxy, profile)
        except BaseException:
            # Спавн не состоялся: профиль нельзя оставить назначенным на
            # воркера, которого не будет, — иначе пул потеряет строку навсегда.
            if profile is not None:
                self.profile_pool.release(profile["id"])
            raise
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
        # Назначение — строго после register_worker: assign_proxy и
        # assign_profile делают UPDATE существующей строки, а для нового
        # воркера её ещё нет.
        self._bind_proxy(browser_id, picked)
        self._bind_profile(browser_id, profile)
        self.store.log(
            "INFO",
            "supervisor",
            "worker started",
            {"pid": process.pid, "restart_count": restart_count},
            browser_id=browser_id,
        )

    # --- профили: выдача, env, релиз --------------------------------------

    def _take_profile(self, browser_id: str) -> dict[str, Any] | None:
        """Берёт профиль под спавн. ``None`` — брать нечего.

        Если у воркера осталась ссылка на профиль, который выдать нельзя
        (заблокирован оператором или отобран живым соседом), ссылка гасится:
        воркер без профиля не должен выглядеть в БД как владеющий им.
        """
        profile = self.profile_pool.take_for_worker(browser_id)
        if profile is None:
            self.profile_pool.release_for_worker(browser_id)
        return profile

    def _bind_profile(self, browser_id: str, profile: dict[str, Any] | None) -> None:
        """Крепит выданный профиль к строке воркера и пишет лог.

        Вызывается после ``register_worker`` — иначе UPDATE не найдёт строки.
        В лог уходят только id: ``key_ref`` (ссылка на ключ) и креды прокси
        в ``logs`` не пишутся.
        """
        if profile is None:
            return
        self.store.assign_profile(browser_id, profile["id"])
        self.store.log(
            "INFO",
            "browser",
            "profile assigned",
            {"profile_id": profile["id"], "proxy_id": profile.get("proxy_id")},
            browser_id=browser_id,
        )

    def _release_profile(self, browser_id: str) -> None:
        """Полный релиз профиля воркера: статус в пул, ссылка — в NULL.

        Вызывается там, где процесс кончается насовсем (stop, kill,
        circuit-open). Ротация сюда не ходит.
        """
        stored = self.store.get_worker(browser_id)
        profile_id = None if stored is None else stored["profile_id"]
        if profile_id is None:
            return
        if self.profile_pool.release_for_worker(browser_id):
            self.store.log(
                "INFO",
                "browser",
                "profile released",
                {"profile_id": profile_id},
                browser_id=browser_id,
            )

    def _pick_spawn_proxy(
        self, browser_id: str, profile: dict[str, Any] | None
    ) -> tuple[dict[str, Any], str] | None:
        """Прокси спавна: профильный приоритетнее пула.

        У аккаунта свой прокси — геолокация, часовой пояс и язык привязаны к
        нему, и подмена на «свободный живой» из пула означала бы выход
        аккаунта из своей страны. Пул используется, только когда у профиля
        нет прокси либо его строка исчезла/помечена мёртвой.
        """
        from_profile = self._profile_proxy(browser_id, profile)
        if from_profile is not None:
            return from_profile
        return self._pick_proxy(browser_id, allow_shared=True)

    def _profile_proxy(
        self, browser_id: str, profile: dict[str, Any] | None
    ) -> tuple[dict[str, Any], str] | None:
        if profile is None or not profile.get("proxy_id"):
            return None
        proxy_id = profile["proxy_id"]
        proxy = self.proxy_pool.get(proxy_id)
        # get() с настоящими кредами (список их маскирует), и между чтениями
        # строку могли удалить. Жизнеспособность проверяется той же
        # колонкой, что и в _pick_proxy: мёртвый прокси не выдаётся никому.
        unavailable = proxy is None or proxy["is_alive"] != 1
        if unavailable:
            self.store.log(
                "WARNING",
                "proxy",
                "profile proxy unavailable, worker falls back to the pool",
                {"profile_id": profile["id"], "proxy_id": proxy_id},
                browser_id=browser_id,
            )
            return None
        return proxy, PROXY_USAGE_ASSIGNED

    # --- прокси: выбор, выдача, ротация -----------------------------------

    def _pick_proxy(
        self,
        browser_id: str,
        *,
        exclude: int | None = None,
        allow_shared: bool = False,
    ) -> tuple[dict[str, Any], str] | None:
        """Строка прокси с настоящими кредами и результат использования.

        ``None`` — выдавать нечего (пул пуст или живых строк нет): спавн
        продолжается без ``ADCLICKER_PROXY``, как и до этой фичи.

        Порядок отбора:

        1. живые (``is_alive=1``) и не исключённые — ротация не берёт
           текущий прокси, даже когда строка выглядит свободной;
        2. среди них сначала своё назначение (воркер, упавший и поднятый
           заново, продолжает работать через тот же прокси), потом свободные
           — не держимые ни одним живым воркером;
        3. при ``allow_shared`` — занятые другим живым воркером: дележ
           важнее отказа в старте, но помечается результатом ``shared``;
        4. иначе ``None``.

        Внутри каждой категории берётся наименее используемая строка
        (``usage_count``, затем ``id``): нагрузка распределяется по пулу, а
        порядок остаётся предсказуемым для тестов и разбора инцидентов.
        """
        rows = self.proxy_pool.list_proxies()
        candidates = [row for row in rows if row["is_alive"] == 1 and row["id"] != exclude]
        own = [row for row in candidates if row["assigned_browser_id"] == browser_id]
        free = [row for row in candidates if row["assigned_browser_id"] is None]
        shared: list[dict[str, Any]] = []
        if allow_shared:
            shared = [
                row
                for row in candidates
                if row["assigned_browser_id"] not in (None, browser_id)
            ]
        ordered: list[tuple[dict[str, Any], str]] = [
            (row, PROXY_USAGE_ASSIGNED) for row in _least_used(own)
        ]
        ordered += [(row, PROXY_USAGE_ASSIGNED) for row in _least_used(free)]
        ordered += [(row, PROXY_USAGE_SHARED) for row in _least_used(shared)]
        for row, result in ordered:
            proxy = self.proxy_pool.get(row["id"])
            if proxy is not None:
                # get() отдельно от list_proxies(): env нужны настоящие креды,
                # а список их маскирует. Между чтениями строку могли удалить —
                # тогда пропускаем следующую, а не выдаём полустроку.
                return proxy, result
        return None

    def _bind_proxy(self, browser_id: str, picked: tuple[dict[str, Any], str] | None) -> None:
        """Крепит выбор к строке воркера: ``workers.proxy_id`` + использование.

        Вызывается после ``register_worker`` — иначе UPDATE не найдёт строки.
        """
        if picked is None:
            # Только прокси: спавн без прокси — не событие смерти процесса,
            # и профиль, выданный перед спавном, обязан уцелеть.
            self.store.release_proxy(browser_id)
            if self.proxy_pool.list_proxies():
                # Пул непустой, но живых строк нет: воркер работает без
                # прокси не по своему выбору, и это должно быть видно.
                self.store.log(
                    "WARNING",
                    "proxy",
                    "no alive proxy available, worker runs without one",
                    {},
                    browser_id=browser_id,
                )
            return
        proxy, result = picked
        self.store.assign_proxy(browser_id, proxy["id"])
        self._record_proxy_use(proxy["id"], browser_id, result)

    def _record_proxy_use(
        self,
        proxy_id: int,
        browser_id: str,
        result: str,
        fields: dict[str, Any] | None = None,
    ) -> None:
        """Строка ``proxy_usage`` и лог с категорией ``proxy`` — всегда вместе.

        Счётчик в UI и событие для разбора инцидента читают разные места, и
        расхождение между ними выглядело бы как потеря записи. Креды не
        попадают ни туда, ни туда: уходит только ``proxy_id``.
        """
        level, message = _PROXY_USAGE_LOGS[result]
        try:
            self.proxy_pool.record_usage(proxy_id, browser_id=browser_id, result=result)
        except ProxyError as exc:
            # Прокси удалили между выбором и записью. Воркер уже запущен,
            # поэтому роняем только строку использования, а не спавн;
            # текст ProxyError построен без значений кредов (см. proxy_pool).
            self.store.log(
                "WARNING",
                "proxy",
                "proxy usage was not recorded",
                {"proxy_id": proxy_id, "error": str(exc)},
                browser_id=browser_id,
            )
        self.store.log(
            level,
            "proxy",
            message,
            {"proxy_id": proxy_id, "result": result, **(fields or {})},
            browser_id=browser_id,
        )

    def _handle_degraded(
        self, browser_id: str, worker: _Worker, stored: dict[str, Any]
    ) -> None:
        """Ротация прокси для воркера, пометившего себя ``degraded``.

        Сначала фиксируется исчерпание текущего прокси (ровно раз на
        деградацию — иначе каждый тик плодил бы строки), потом ищется
        резерв. Резерва нет — воркер не трогается: он остаётся degraded с
        WARNING и пробует снова через backoff, потому что убийство процесса
        без замены ничего не чинило бы, а теряло бы работающий сценарий.
        """
        now = self._clock.wall()
        if now < worker.next_rotation_at:
            return

        current_id = stored.get("proxy_id")
        if not worker.degraded_recorded:
            worker.degraded_recorded = True
            # Причина воркера — та же строка, что уже лежит в workers.last_error
            # и видна в UI: в лог она попадает, чтобы событие было читаемо
            # само по себе. Кредов в ней нет — их туда не кладёт mark_degraded.
            reason = stored.get("last_error")
            reason_fields: dict[str, Any] | None = {"reason": reason} if reason else None
            if current_id is None:
                self.store.log(
                    "WARNING",
                    "proxy",
                    "worker degraded without an assigned proxy",
                    reason_fields,
                    browser_id=browser_id,
                )
            else:
                self._record_proxy_use(
                    current_id,
                    browser_id,
                    PROXY_USAGE_EXHAUSTED,
                    reason_fields,
                )

        reserve = self._pick_proxy(browser_id, exclude=current_id, allow_shared=False)
        if reserve is None:
            self._defer_rotation(
                browser_id, worker, now, reason="no free alive proxy"
            )
            return
        self._rotate_to(browser_id, worker, current_id, reserve, now)

    def _defer_rotation(
        self, browser_id: str, worker: _Worker, now: float, *, reason: str
    ) -> None:
        """Откладывает ротацию: растёт интервал и пишется одна причина.

        Задержка считается тем же ``backoff_delay``, что и у рестартов, —
        это и есть защита от плотного цикла: подмена, за которой сразу идёт
        новая деградация, ждёт 2, 4, 8... секунд, а не крутится на каждом
        тике. Счётчик попыток сбрасывается только после здорового участка
        работы (см. ``_maybe_reset_restart_count``).
        """
        worker.rotation_count += 1
        delay = backoff_delay(
            worker.rotation_count,
            base=self.settings.restart_backoff_base,
            maximum=self.settings.restart_backoff_max,
        )
        worker.next_rotation_at = now + delay
        self.store.log(
            "WARNING",
            "proxy",
            "proxy rotation postponed, worker stays degraded",
            {"reason": reason, "retry_in": delay, "attempt": worker.rotation_count},
            browser_id=browser_id,
        )

    def _rotate_to(
        self,
        browser_id: str,
        worker: _Worker,
        old_proxy_id: int | None,
        reserve: tuple[dict[str, Any], str],
        now: float,
    ) -> None:
        """Гасит старый процесс и поднимает нового с резервным прокси.

        Процесс гасится до спавна: два живых процесса с одним browser_id
        означали бы два браузера, читающих один профиль и один набор логов.
        """
        proxy, _ = reserve
        if not self._terminate_one(worker):
            # Старый процесс пережил и SIGTERM, и SIGKILL. Второй запускать
            # нельзя, остаёмся degraded до следующей попытки.
            self._defer_rotation(
                browser_id, worker, now, reason="old process did not exit"
            )
            return

        self._finish_run(worker, "stopped", "proxy rotated")
        # Профиль переживает ротацию: новый процесс получает тот же профиль
        # и тот же env. release_assignment здесь НЕ вызывается — именно он в
        # фазе 5 сбрасывал профиль вместе с прокси, и воркер продолжал работу
        # уже без аккаунта (план.md, фаза 6 «Привязка профиля»).
        profile = self._take_profile(browser_id)
        try:
            process = self._spawn_checked(browser_id, proxy, profile)
        except BaseException:
            # Старый процесс уже погаш, новый не поднялся: без отката профиль
            # остался бы назначенным на воркера, у которого нет ни процесса,
            # ни следующего тика (исключение убивает цикл run_idle).
            if profile is not None:
                self.profile_pool.release(profile["id"])
            raise
        worker.process = process
        worker.started_at = now
        worker.last_heartbeat = now
        worker.next_start_at = now
        worker.crash_recorded = False
        worker.degraded_recorded = False
        worker.rotation_count += 1
        worker.next_rotation_at = now + backoff_delay(
            worker.rotation_count,
            base=self.settings.restart_backoff_base,
            maximum=self.settings.restart_backoff_max,
        )
        # register_worker ставит status=starting и не трогает proxy_id и
        # profile_id, поэтому назначения пишутся явно — тем же кодом, что и
        # на спавне.
        self.store.register_worker(browser_id, process.pid, now=now)
        self.store.assign_proxy(browser_id, proxy["id"])
        self._bind_profile(browser_id, profile)
        self._record_proxy_use(
            proxy["id"],
            browser_id,
            PROXY_USAGE_ROTATED,
            {
                "from_proxy_id": old_proxy_id,
                "pid": process.pid,
                "attempt": worker.rotation_count,
            },
        )

    def _terminate_one(self, worker: _Worker) -> bool:
        """SIGTERM, выдержка, SIGKILL одному воркеру. True — процесс завершён.

        Возвращает флаг, а не исключение: ротация обязана знать, что старый
        процесс пережил обе фазы, иначе поднимет второй на том же browser_id.
        Тот же сигнал, что в ``_stop_all_locked``, только дедлайн свой: ждать
        grace на каждого воркера пула можно, а на одного — тем более.
        """
        if not worker.is_alive():
            return True
        worker.process.terminate()
        deadline = self._clock.time() + self.settings.shutdown_grace_seconds
        while worker.is_alive() and self._clock.time() < deadline:
            self._clock.sleep(0.1)
        if not worker.is_alive():
            return True
        worker.process.kill()
        kill_deadline = self._clock.time() + self.settings.shutdown_grace_seconds
        while worker.is_alive() and self._clock.time() < kill_deadline:
            self._clock.sleep(0.1)
        if worker.is_alive():
            self.store.log(
                "ERROR",
                "proxy",
                "worker process survived SIGKILL during proxy rotation",
                {"pid": worker.process.pid},
                browser_id=worker.browser_id,
            )
        return not worker.is_alive()

    def _spawn_checked(
        self,
        browser_id: str,
        proxy: dict[str, Any] | None,
        profile: dict[str, Any] | None = None,
    ) -> ProcessLike:
        """Запуск с приведением любой ошибки ОС к WorkerSpawnError.

        Подменяемая фабрика (в тестах и в будущем пути запуска) может бросить
        что угодно, а вызывающему нужен один тип ошибки, иначе обработчик
        HTTP отдаст 500 там, где положен 503 с понятным текстом.

        Окружение воркера достраивается здесь, а не в ``env_for``: выбор
        прокси и профиля принимаются до спавна (иначе нечем снабдить процесс),
        а ``env_for`` остаётся точкой подмены для тестов. Обе переменные
        сначала снимаются — наследие из окружения демона не должно обходить
        пулы, — и только потом выставляются назначенные значения.
        """
        env = dict(self._env_for(browser_id))
        env.pop(PROXY_ENV, None)
        env.pop(PROFILE_ENV, None)
        if proxy is not None:
            env[PROXY_ENV] = proxy_env_value(proxy)
        if profile is not None:
            env[PROFILE_ENV] = str(profile["id"])
        try:
            return self._spawn(browser_id, self._command_for(browser_id), env)
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
            # Полный релиз: процесс кончается насовсем, прокси возвращается в
            # пул, а профиль — в очередь на выдачу. Порядок важен:
            # release_assignment обнуляет profile_id, поэтому пул читает
            # ссылку раньше.
            self._release_profile(browser_id)
            self.store.release_assignment(browser_id)

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
        """Один цикл надзора: наблюдение за heartbeat, сбор падений, рестарты.

        Синхронный и возвращающий управление: фоновый поток и HTTP-запрос
        вызывают одно и то же, и обе стороны получают одинаковое поведение.

        Порядок внутри значим: сначала в память переносятся heartbeat'ы,
        записанные воркерами, и только потом те, кто не стучил, помечаются
        зависшими — иначе проверка stale работала бы по устаревшему значению.
        """
        with self._lock:
            self._observe_heartbeats()
            self._mark_stale_workers()
            # Реапер идёт ПОСЛЕ stale-логики и ДО разбора падений: к этому
            # моменту зависший процесс уже погашен, а падение ещё не
            # оформлено — страховка видит ровно то, что видит и stale.
            self._reap_profiles()
            for browser_id, worker in list(self._workers.items()):
                self._reconcile(browser_id, worker)

    def _reap_profiles(self) -> None:
        """Возвращает в пул профили, чьи воркеры мертвы, удалены или протухли.

        Страховка от потери назначения: пул статусов не знает ни о процессах,
        ни о реестре — только супервизор решает, кто жив. Условия живости
        совпадают со stale-логикой: процесс должен быть жив, воркер не должен
        стоять в раскрытой цепи, а heartbeat — не протухнуть.

        ``workers.profile_id`` при этом не трогается (см.
        ``ProfilePool.reap``): это память о прошлом назначении, без которой
        респавн получил бы чужой профиль. Ссылку гасит выдача профиля
        другому воркеру и полный релиз.
        """
        assigned = self.profile_pool.assigned_profile_ids()
        if not assigned:
            return
        live = self._live_profile_ids()
        reaped = self.profile_pool.reap(live)
        if reaped:
            self.store.log(
                "WARNING",
                "browser",
                "profile released: worker is gone",
                {"profile_ids": sorted(assigned - live), "count": reaped},
            )

    def _live_profile_ids(self) -> set[int]:
        """Профили, которые держат живые воркеры, — по реестру и heartbeat."""
        profile_by_browser = {
            row["browser_id"]: row["profile_id"] for row in self.store.list_workers()
        }
        now = self._clock.wall()
        live: set[int] = set()
        for browser_id, worker in self._workers.items():
            if worker.circuit_open or not worker.is_alive():
                continue
            if now - worker.last_heartbeat > self.settings.stale_after_seconds:
                continue
            profile_id = profile_by_browser.get(browser_id)
            if profile_id is not None:
                live.add(profile_id)
        return live

    def _observe_heartbeats(self) -> None:
        """Переносит heartbeat'ы из БД в память. Наблюдение, а не запись.

        ``heartbeat_at`` пишет сам воркер (``StoreWriter.heartbeat``); здесь
        один запрос на тик читает все отметки сразу и двигает in-memory
        ``last_heartbeat`` строго по факту роста значения из БД. «Строго по
        росту» — не формальность: время в памяти не должно «омоложиться»
        само, иначе возраст перестанет расти и детект зависания снова
        перестанет срабатывать.
        """
        if not self._workers:
            return
        stored = self.store.heartbeats()
        for browser_id, worker in self._workers.items():
            seen = stored.get(browser_id)
            if seen is not None and seen > worker.last_heartbeat:
                worker.last_heartbeat = seen

    def _mark_stale_workers(self) -> None:
        """Помечает зависших воркеров, которые не прислали heartbeat вовремя.

        Процесс может быть жив, но бесполезен: браузер не отвечает, сокет
        заблокирован. По одному poll() такой воркер неотличим от здорового, и
        демон ждал бы его вечно. Heartbeat в БД в такой ситуации не растёт —
        его пишет воркер, а супервизор только наблюдает (``_observe_heartbeats``),
        поэтому ``last_heartbeat`` замирает и здесь наступает порог stale.
        Решение принимает _reconcile: он увидит, что heartbeat устарел, и
        переведёт воркера в путь рестарта.
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

        Четыре состояния, и порядок проверок важен:

        1. circuit открыт — ничего не делаем, пользователь должен вмешаться;
        2. процесс жив и помечен ``degraded`` — ротация прокси, и статус
           НЕ затирается на running: сигнал воркера иначе прожил бы ровно
           один тик, и подмена прокси никогда не случилась бы;
        3. процесс жив — обновляем статус на running;
        4. процесс мёртв — либо ждём backoff, либо падение уже записано и
           пора поднимать, либо записываем падение впервые.
        """
        if worker.circuit_open:
            return

        if worker.is_alive():
            stored = self.store.get_worker(browser_id)
            if stored is not None and stored["status"] == WorkerStatus.DEGRADED.value:
                self._handle_degraded(browser_id, worker, stored)
                return
            self._mark_running(browser_id, worker, stored)
            return

        if worker.crash_recorded:
            self._maybe_respawn(browser_id, worker)
            return

        self._handle_crash(browser_id, worker)

    def _mark_running(
        self, browser_id: str, worker: _Worker, stored: dict[str, Any] | None
    ) -> None:
        """Переводит воркера в running и обнуляет счётчик рестартов.

        Обнуление здесь, а не по отдельному таймеру, — потому что момент
        "воркер здоров" наступает ровно тогда, когда супервизор увидел живой
        процесс. Тикает чаще — перевода не происходит, статус уже running.

        ``stored`` приходит из ``_reconcile``: строка уже прочитана там для
        проверки degraded, и второе чтение на каждом тике было бы лишним.
        """
        if stored is None:
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
        """Обнуляет счётчики, если воркер проработал достаточно долго.

        Без этого потолок рестартов копился бы за всю жизнь воркера, и пара
        сбоев за сутки однажды остановила бы его навсегда. Порог — половина
        потолка: показывать пользователю, что демон считает воркера проблемным,
        полезно и до того, как circuit breaker его убьёт.

        Тем же правилом обнуляются счётчик и таймер ротаций: воркер,
        продержавшийся после подмены прокси достаточно долго, заслуживает
        свежего интервала, а не нарастающей задержки от старой деградации.
        """
        if (
            worker.restart_count == 0
            and worker.rotation_count == 0
            and not worker.degraded_recorded
        ):
            return
        if self._clock.wall() - worker.started_at < self.settings.restart_count_reset_after:
            return
        worker.restart_count = 0
        worker.crash_recorded = False
        worker.rotation_count = 0
        worker.next_rotation_at = 0.0
        worker.degraded_recorded = False
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
        # Воркер убит и без явного рестарта не поднимется — значит, прокси и
        # профиль принадлежат уже никому: освобождаем, как и на stop.
        self._release_profile(browser_id)
        self.store.release_assignment(browser_id)
        del self._workers[browser_id]

    def _restart_worker(self, browser_id: str, worker: _Worker) -> None:
        # Профиль: приоритет прошлому назначению — упавший и поднятый заново
        # продолжает работать с тем же аккаунтом. Прокси для пула выбирается
        # заново, а не берётся из строки: назначение могло освободиться или
        # стать мёртвым, пока воркер сидел в backoff; своё же (если оно живо)
        # возвращается — чужое не берётся никогда.
        profile = self._take_profile(browser_id)
        try:
            picked = self._pick_spawn_proxy(browser_id, profile)
            proxy = None if picked is None else picked[0]
            process = self._spawn_checked(browser_id, proxy, profile)
        except BaseException:
            # Исключение из респавна уходит через tick() наружу и останавливает
            # цикл run_idle — реапер-страховки после него не будет, поэтому
            # профиль освобождается здесь же.
            if profile is not None:
                self.profile_pool.release(profile["id"])
            raise
        now = self._clock.wall()
        worker.process = process
        worker.started_at = now
        worker.last_heartbeat = now
        worker.next_start_at = now
        # Сбрасываем флаг, иначе следующее падение не было бы засчитано:
        # тик увидел бы crash_recorded=True и ушёл в ветку backoff без записи.
        worker.crash_recorded = False
        # Свежий процесс начинает деградацию с чистого листа: «exhausted» за
        # прошлую причину падения не должен висеть на новом прокси.
        worker.degraded_recorded = False
        worker.next_rotation_at = 0.0
        self.store.register_worker(browser_id, process.pid, now=now)
        self.store.set_status(browser_id, WorkerStatus.STARTING)
        self._bind_proxy(browser_id, picked)
        self._bind_profile(browser_id, profile)
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
