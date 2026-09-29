"""Тесты супервизора воркеров.

Супервизор — единственное место, где принимаются решения о жизни и смерти
процессов, поэтому проверяется он на подменяемой фабрике процессов: реальный
Chrome не запускается никогда. Подмена живёт в памяти теста и ведёт себя как
настоящий дочерний процесс с той стороны, которая важна супервизору: жив/мёртв,
terminate, kill, код возврата.

Управление временем — через явный ``now`` в supervisor.tick() и через
подставленные часы, поэтому тесты не спят и не мигают.
"""

import json
import threading

import pytest

from engine.control_plane import supervisor as sup
from engine.control_plane.state import StateStore, WorkerStatus
from engine.db import migrations
from engine.profile_pool import ProfilePool
from engine.proxy_pool import ProxyPool
from engine.store import StoreWriter


class FakeProcess:
    """Процесс, которым управляет тест.

    Повторяет контракт subprocess.Popen в той части, которую использует
    супервизор: ``pid``, ``poll()``, ``terminate()``, ``kill()``, ``wait()``.
    Никакой настоящей ОС здесь не задействовано.
    """

    def __init__(self, pid: int, registry: "FakeProcessRegistry", browser_id: str):
        self.pid = pid
        self._registry = registry
        self.browser_id = browser_id
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.spawned_at = 0.0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self._registry.terminated.append(self.browser_id)
        if not self._registry.ignores_sigterm:
            self.returncode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self._registry.killed.append(self.browser_id)
        if self._registry.survives_kill:
            # Процесс, переживший и SIGTERM, и SIGKILL: в реальности это
            # D-состояние в ядре. Проверяет, что супервизор не зацикливается.
            self.returncode = None
        else:
            self.returncode = -9

    def exit(self, code: int = 1) -> None:
        """Моделирует неожиданное падение воркера."""
        self.returncode = code


class FakeProcessRegistry:
    """Фабрика процессов: выдаёт FakeProcess и запоминает выданные."""

    def __init__(
        self,
        exit_immediately: bool = False,
        spawn_error: Exception | None = None,
        ignores_sigterm: bool = False,
        survives_kill: bool = False,
    ):
        self.created: list[FakeProcess] = []
        self.terminated: list[str] = []
        self.killed: list[str] = []
        self.start_calls: list[dict] = []
        self.ignores_sigterm = ignores_sigterm
        self.survives_kill = survives_kill
        self.exit_immediately = exit_immediately
        self.spawn_error = spawn_error
        self._next_pid = 1000
        self._lock = threading.Lock()


    def __call__(self, browser_id: str, command: list[str], env: dict) -> FakeProcess:
        with self._lock:
            self._next_pid += 1
            pid = self._next_pid
        if self.spawn_error is not None:
            raise self.spawn_error
        process = FakeProcess(pid, self, browser_id)
        self.created.append(process)
        self.start_calls.append(
            {"browser_id": browser_id, "command": list(command), "env": dict(env)}
        )
        if self.exit_immediately:
            process.exit(1)
        return process

    def alive(self) -> list[FakeProcess]:
        return [p for p in self.created if p.poll() is None]


class FakeClock:
    """Монотонные часы, которые двигает тест."""

    def __init__(self, start: float = 0.0):
        self.value = start
        self._lock = threading.Lock()

    def time(self) -> float:
        with self._lock:
            return self.value

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.value += seconds

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.value += seconds

    def wall(self) -> float:
        return 1_700_000_000.0 + self.value


@pytest.fixture
def db_path(tmp_path):
    from engine.db import migrations

    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


@pytest.fixture
def store(db_path):
    return StateStore(db_path)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def registry():
    return FakeProcessRegistry()


@pytest.fixture
def settings():
    return sup.SupervisorSettings(
        heartbeat_interval=sup.HEARTBEAT_INTERVAL_SECONDS,
        shutdown_grace_seconds=sup.SHUTDOWN_GRACE_SECONDS,
        restart_backoff_base=sup.RESTART_BACKOFF_BASE_SECONDS,
        restart_backoff_max=sup.RESTART_BACKOFF_MAX_SECONDS,
        max_restarts=sup.DEFAULT_MAX_RESTARTS,
    )


def make_supervisor(store, registry, clock, settings):
    return sup.Supervisor(
        store=store,
        settings=settings,
        spawn=registry,
        clock=clock,
        browser_ids=lambda n: [f"br-{i + 1}" for i in range(n)],
    )


def worker_beats(store, clock, *browser_ids):
    """Пишет heartbeat ровно тем кодом, что и настоящий воркер.

    ``StoreWriter.heartbeat`` — production-путь записи из ``engine.worker``;
    ``now`` подставляется из часов теста, чтобы наблюдение супервизора и
    stale-порог сравнивались с одним источником времени. Без этой записи
    фейковый воркер в тестах выглядел бы зависшим: heartbeat пишет воркер,
    а не супервизор.
    """
    writer = StoreWriter(store.db_path)
    try:
        for browser_id in browser_ids:
            writer.heartbeat(browser_id, now=clock.wall())
    finally:
        writer.close()


class TestStart:
    """Запуск воркеров и отказ при повторном старте."""

    def test_start_spawns_requested_number_of_workers(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(3)

        assert len(registry.created) == 3
        assert [c["browser_id"] for c in registry.start_calls] == ["br-1", "br-2", "br-3"]

    def test_start_registers_pids_in_database(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(2)

        workers = store.list_workers()
        assert [w["browser_id"] for w in workers] == ["br-1", "br-2"]
        assert workers[0]["pid"] == registry.created[0].pid
        assert workers[1]["pid"] == registry.created[1].pid

    def test_start_marks_workers_running_after_first_successful_tick(
        self, store, registry, clock, settings
    ):
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(1)
        supervisor.tick()

        assert store.get_worker("br-1")["status"] == WorkerStatus.RUNNING.value

    def test_start_sets_run_state_running(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(1)

        assert store.get_run_state() == "running"

    def test_start_opens_a_run_record_per_worker(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(2)

        assert all(store.latest_run_id(w["id"]) is not None for w in store.list_workers())

    def test_start_while_running_raises_already_running(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(2)

        with pytest.raises(sup.AlreadyRunningError):
            supervisor.start(1)

    def test_failed_second_start_does_not_spawn_extra_processes(self, store, registry, clock, settings):
        """Проверка «Start при работающих воркерах — ошибка, а не тихий no-op»."""
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(2)

        with pytest.raises(sup.AlreadyRunningError):
            supervisor.start(3)

        assert len(registry.created) == 2, "повторный start не должен плодить процессы"

    def test_start_with_zero_workers_raises(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)

        with pytest.raises(sup.InvalidWorkerCountError):
            supervisor.start(0)

    def test_start_rejects_more_workers_than_settings_allow(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)

        with pytest.raises(sup.InvalidWorkerCountError):
            supervisor.start(settings.max_workers + 1)

    def test_start_after_stop_works(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        supervisor.stop()

        supervisor.start(1)

        assert store.get_run_state() == "running"

    def test_spawn_failure_is_recorded_and_raises(self, store, clock, settings):
        registry = FakeProcessRegistry(spawn_error=OSError("no such binary"))
        supervisor = make_supervisor(store, registry, clock, settings)

        with pytest.raises(sup.WorkerSpawnError):
            supervisor.start(1)

        assert store.get_run_state() == "stopped", "неудачный старт не должен оставить state=running"


class TestHeartbeat:
    """Heartbeat пишет воркер, супервизор наблюдает.

    Раньше эти тесты проверяли, что супервизор сам обновляет ``heartbeat_at``
    каждые 5 секунд: из-за этого детект зависания был мёртв — ``tick()``
    обновлял отметку перед проверкой stale, и возраст в памяти не превышал
    интервал. Теперь запись принадлежит воркеру (``StoreWriter.heartbeat``),
    а тесты проверяют наблюдение: тик читает БД одним запросом и двигает
    in-memory ``last_heartbeat`` только по факту роста значения.
    """

    def test_start_writes_initial_heartbeat(self, store, registry, clock, settings):
        """Регистрация сама ставит heartbeat — UI не должен ждать первого тика,
        чтобы понять, что воркер только что стартовал."""
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(1)

        assert store.get_worker("br-1")["heartbeat_at"] == clock.wall()

    def test_tick_does_not_write_heartbeat_for_the_worker(
        self, store, registry, clock, settings
    ):
        """Было «супервизор пишет heartbeat» — стало «супервизор не пишет».

        Писал бы супервизор — обновлял бы отметку сам и обнулял возраст
        перед проверкой stale, отбрасывая зависшего воркера как здорового.
        """
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        first = store.get_worker("br-1")["heartbeat_at"]

        clock.advance(sup.HEARTBEAT_INTERVAL_SECONDS * 3)
        supervisor.tick()

        assert store.get_worker("br-1")["heartbeat_at"] == first, (
            "писать heartbeat от имени воркера супервизору нельзя"
        )
        assert supervisor._workers["br-1"].last_heartbeat == first, (
            "наблюдение не должно двигаться само, без записи воркера"
        )

    def test_worker_write_is_observed_on_the_next_tick(
        self, store, registry, clock, settings
    ):
        """Запись воркера попадает в память на тике, часы — супервизорские.

        Иначе тесты heartbeat'а зависели бы от системных часов. Значение
        читается из БД, а не берётся из ``time.time()``: воркер и демон
        смотрят на одну и ту же колонку.
        """
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        first = store.get_worker("br-1")["heartbeat_at"]
        clock.advance(sup.HEARTBEAT_INTERVAL_SECONDS)
        worker_beats(store, clock, "br-1")

        # Запись уже в БД, но без тика наблюдение не состоялось.
        assert supervisor._workers["br-1"].last_heartbeat == first

        supervisor.tick()

        assert store.get_worker("br-1")["heartbeat_at"] == clock.wall() == 1_700_000_005.0
        assert supervisor._workers["br-1"].last_heartbeat == clock.wall()

    def test_observed_heartbeat_covers_every_worker(
        self, store, registry, clock, settings
    ):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(3)
        clock.advance(sup.HEARTBEAT_INTERVAL_SECONDS)

        worker_beats(store, clock, "br-1", "br-2", "br-3")
        supervisor.tick()

        assert {w["browser_id"]: w["heartbeat_at"] for w in store.list_workers()} == {
            "br-1": clock.wall(),
            "br-2": clock.wall(),
            "br-3": clock.wall(),
        }
        assert all(
            worker.last_heartbeat == clock.wall()
            for worker in supervisor._workers.values()
        ), "наблюдение обязано увидеть запись каждого воркера одним тиком"

    def test_default_heartbeat_interval_is_five_seconds(self):
        assert sup.HEARTBEAT_INTERVAL_SECONDS == 5.0


class TestStaleDetection:
    """Зависший воркер: процесс жив по ``poll()``, heartbeat не приходит.

    Heartbeat в БД пишет сам воркер (``StoreWriter.heartbeat``), супервизор
    только наблюдает за ростом ``heartbeat_at`` и обязан по его отсутствию
    признать воркера зависшим — несмотря на живой процесс. Раньше этого не
    происходило: ``_heartbeat_due`` писал heartbeat от имени воркера прямо в
    ``tick()`` перед проверкой stale, и возраст в памяти не превышал интервал.
    """

    def test_tick_terminates_worker_that_stopped_beating(
        self, store, registry, clock, settings
    ):
        """Красный тест на дефект: зависание должно находиться полным ``tick()``."""
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        supervisor.tick()

        clock.advance(settings.stale_after_seconds + 1)
        supervisor.tick()

        assert registry.terminated == ["br-1"], (
            "зависший процесс обязан получить SIGTERM: docstring "
            "_mark_stale_workers обещает это ровно, а порядок "
            "heartbeat -> stale в tick() гасил детект"
        )
        assert store.get_worker("br-1")["status"] == WorkerStatus.BACKOFF.value
        assert "worker heartbeat is stale" in all_log_text(store)

    def test_worker_that_keeps_beating_is_not_marked_stale(
        self, store, registry, clock, settings
    ):
        """Обратная сторона детекта: штатные 5 секунд не дают ложных срабатываний."""
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        supervisor.tick()

        for _ in range(3):
            clock.advance(sup.HEARTBEAT_INTERVAL_SECONDS)
            worker_beats(store, clock, "br-1")
            supervisor.tick()

        assert registry.terminated == [], "стучащий воркер не может быть признан зависшим"
        assert store.get_worker("br-1")["status"] == WorkerStatus.RUNNING.value

    def test_grace_after_spawn_covers_the_first_heartbeat(
        self, store, registry, clock, settings
    ):
        """Первое наблюдение за новым воркером приходит не сразу.

        Регистрация ставит ``last_heartbeat=now``, а воркер пишет первый
        heartbeat через свой интервал — до порога stale должно оставаться
        запас, иначе каждый свежий спавн гиб бы от ложного срабатывания.
        """
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)

        clock.advance(settings.stale_after_seconds - 1)
        supervisor.tick()

        assert registry.terminated == []
        assert store.get_worker("br-1")["status"] == WorkerStatus.RUNNING.value

    def test_restart_resets_the_stale_observation(
        self, store, registry, clock, settings
    ):
        """Рестарт начинает отсчёт заново: новый процесс не наследует
        возраст heartbeat'а старого, иначе первое наблюдение после
        рестарта выглядело бы зависанием сразу после старта."""
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        worker_beats(store, clock, "br-1")
        registry.created[0].exit(1)
        supervisor.tick()
        clock.advance(sup.RESTART_BACKOFF_BASE_SECONDS)
        supervisor.tick()

        clock.advance(settings.stale_after_seconds - 1)
        supervisor.tick()

        assert registry.terminated == [], "после рестарта возраст heartbeat'а начинается заново"
        assert len(registry.created) == 2, "воркер должен быть перезапущен, а не убит снова"


class TestCrashDetection:
    """Обнаружение падения и запись ошибки."""

    def test_tick_detects_dead_process(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        supervisor.tick()
        registry.created[0].exit(1)

        supervisor.tick()

        assert store.get_worker("br-1")["status"] == WorkerStatus.BACKOFF.value

    def test_exit_code_is_recorded_in_last_error(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        registry.created[0].exit(7)

        supervisor.tick()

        assert "7" in store.get_worker("br-1")["last_error"]

    def test_crash_is_logged(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        registry.created[0].exit(1)

        supervisor.tick()

        with store._connect() as conn:
            rows = conn.execute(
                "SELECT message, level FROM logs WHERE browser_id = ?", ("br-1",)
            ).fetchall()
        assert any("crash" in row["message"].lower() for row in rows)
        assert any(row["level"] == "ERROR" for row in rows)

    def test_dead_worker_keeps_its_row_for_history(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        registry.created[0].exit(1)

        supervisor.tick()

        assert store.get_worker("br-1") is not None, "история рестартов нужна UI"


class TestAutoRestart:
    """Перезапуск упавшего воркера с экспоненциальной задержкой."""

    def test_no_respawn_before_backoff_elapses(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        registry.created[0].exit(1)

        supervisor.tick()
        clock.advance(sup.RESTART_BACKOFF_BASE_SECONDS - 0.5)
        supervisor.tick()

        assert len(registry.created) == 1, "рестарт раньше backoff недопустим"

    def test_respawn_after_backoff(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        registry.created[0].exit(1)

        supervisor.tick()
        clock.advance(sup.RESTART_BACKOFF_BASE_SECONDS)
        supervisor.tick()

        assert len(registry.created) == 2
        assert store.get_worker("br-1")["restart_count"] == 1

    def test_backoff_doubles_each_failure(self, store, registry, clock, settings):
        """Второе падение ждёт вдвое дольше первого.

        Проверяется на числе поднятых процессов, а не на restart_count:
        счётчик растёт в момент обнаружения падения, и по нему нельзя
        отличить "ждёт backoff" от "уже перезапустился".
        """
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)

        registry.created[0].exit(1)
        supervisor.tick()
        clock.advance(sup.RESTART_BACKOFF_BASE_SECONDS)
        supervisor.tick()
        assert len(registry.created) == 2, "первый рестарт после base-секунды"
        assert store.get_worker("br-1")["restart_count"] == 1

        registry.created[1].exit(1)
        supervisor.tick()
        assert store.get_worker("br-1")["status"] == WorkerStatus.BACKOFF.value

        # Первая задержка уже не подходит: её не хватает вдвое.
        clock.advance(sup.RESTART_BACKOFF_BASE_SECONDS)
        supervisor.tick()
        assert len(registry.created) == 2, "backoff не вырос: рестарт слишком рано"

        clock.advance(sup.RESTART_BACKOFF_BASE_SECONDS)
        supervisor.tick()
        assert len(registry.created) == 3, "после удвоенной задержки рестарт обязателен"
        assert store.get_worker("br-1")["restart_count"] == 2


    def test_backoff_is_capped(self, store, registry, clock, settings):
        assert sup.backoff_delay(30, base=2.0, maximum=10.0) == 10.0

    def test_backoff_formula(self, store, registry, clock, settings):
        assert sup.backoff_delay(1, base=2.0, maximum=100.0) == 2.0
        assert sup.backoff_delay(2, base=2.0, maximum=100.0) == 4.0
        assert sup.backoff_delay(3, base=2.0, maximum=100.0) == 8.0

    def test_backoff_zero_attempts_is_zero(self, store, registry, clock, settings):
        assert sup.backoff_delay(0, base=2.0, maximum=100.0) == 0.0

    def test_restart_reuses_same_browser_id(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        registry.created[0].exit(1)
        supervisor.tick()
        clock.advance(sup.RESTART_BACKOFF_BASE_SECONDS)

        supervisor.tick()

        assert registry.created[1].browser_id == "br-1"
        assert store.get_worker("br-1")["pid"] == registry.created[1].pid

    def test_restart_count_resets_after_long_healthy_stretch(self, store, registry, clock, settings):
        """Воркер, отработавший дольше reset_after, считается здоровым.

        Иначе воркер, честно работающий сутки с двумя мелкими сбоями, утром
        упёрся бы в circuit breaker и больше не поднялся бы.
        """
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        registry.created[0].exit(1)
        supervisor.tick()
        clock.advance(sup.RESTART_BACKOFF_BASE_SECONDS)
        supervisor.tick()
        assert store.get_worker("br-1")["restart_count"] == 1

        clock.advance(settings.restart_count_reset_after + 1)
        # Воркер всё это время работал и стучил сам: к тику его последняя
        # запись свежая, и детект зависания не должен вмешаться в сброс
        # счётчика. Без записи фейковый воркер выглядел бы зависшим.
        worker_beats(store, clock, "br-1")
        supervisor.tick()

        assert store.get_worker("br-1")["restart_count"] == 0


class TestCircuitBreaker:
    """Потолок рестартов: после него воркер не поднимается."""

    def test_worker_stops_after_max_restarts(self, store, clock):
        registry = FakeProcessRegistry()
        settings = sup.SupervisorSettings(
            heartbeat_interval=5.0,
            shutdown_grace_seconds=1.0,
            restart_backoff_base=1.0,
            restart_backoff_max=4.0,
            max_restarts=2,
            restart_count_reset_after=3600.0,
            max_workers=4,
        )
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)

        # Каждый цикл: упал -> отработал backoff -> поднялся снова.
        for expected_count in (1, 2):
            registry.created[-1].exit(1)
            supervisor.tick()
            clock.advance(10.0)
            supervisor.tick()
            assert store.get_worker("br-1")["restart_count"] == expected_count

        # Третье падение уходит за потолок.
        registry.created[-1].exit(1)
        supervisor.tick()
        clock.advance(60.0)
        supervisor.tick()

        assert store.get_worker("br-1")["status"] == WorkerStatus.CIRCUIT_OPEN.value
        assert len(registry.created) == 3, "1 старт + 2 рестарта, дальше — тишина"

    def test_circuit_open_records_last_error(self, store, clock):
        registry = FakeProcessRegistry()
        settings = sup.SupervisorSettings(
            heartbeat_interval=5.0,
            shutdown_grace_seconds=1.0,
            restart_backoff_base=1.0,
            restart_backoff_max=4.0,
            max_restarts=1,
            restart_count_reset_after=3600.0,
            max_workers=4,
        )
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)

        for _ in range(2):
            registry.created[-1].exit(1)
            supervisor.tick()
            clock.advance(60.0)
            supervisor.tick()

        worker = store.get_worker("br-1")
        assert worker["status"] == WorkerStatus.CIRCUIT_OPEN.value
        assert "restart" in worker["last_error"].lower()
        assert str(settings.max_restarts) in worker["last_error"]

    def test_circuit_open_clears_pid(self, store, clock):
        registry = FakeProcessRegistry()
        settings = sup.SupervisorSettings(
            heartbeat_interval=5.0,
            shutdown_grace_seconds=1.0,
            restart_backoff_base=1.0,
            restart_backoff_max=1.0,
            max_restarts=1,
            restart_count_reset_after=3600.0,
            max_workers=4,
        )
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        for _ in range(2):
            registry.created[-1].exit(1)
            supervisor.tick()
            clock.advance(60.0)
            supervisor.tick()

        assert store.get_worker("br-1")["pid"] is None

    def test_circuit_open_is_logged(self, store, clock):
        registry = FakeProcessRegistry()
        settings = sup.SupervisorSettings(
            heartbeat_interval=5.0,
            shutdown_grace_seconds=1.0,
            restart_backoff_base=1.0,
            restart_backoff_max=1.0,
            max_restarts=1,
            restart_count_reset_after=3600.0,
            max_workers=4,
        )
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        for _ in range(2):
            registry.created[-1].exit(1)
            supervisor.tick()
            clock.advance(60.0)
            supervisor.tick()

        with store._connect() as conn:
            messages = [
                row[0]
                for row in conn.execute("SELECT message FROM logs WHERE level = 'ERROR'").fetchall()
            ]
        assert any("circuit" in message.lower() for message in messages)

    def test_circuit_open_worker_survives_further_ticks_without_respawn(self, store, clock):
        registry = FakeProcessRegistry()
        settings = sup.SupervisorSettings(
            heartbeat_interval=5.0,
            shutdown_grace_seconds=1.0,
            restart_backoff_base=1.0,
            restart_backoff_max=1.0,
            max_restarts=1,
            restart_count_reset_after=3600.0,
            max_workers=4,
        )
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        for _ in range(2):
            registry.created[-1].exit(1)
            supervisor.tick()
            clock.advance(60.0)
            supervisor.tick()
        created_before = len(registry.created)

        for _ in range(10):
            clock.advance(60.0)
            supervisor.tick()

        assert len(registry.created) == created_before
        assert store.get_worker("br-1")["status"] == WorkerStatus.CIRCUIT_OPEN.value

    def test_default_max_restarts_is_five(self):
        assert sup.DEFAULT_MAX_RESTARTS == 5

    def test_one_failing_worker_does_not_stop_the_others(self, store, registry, clock):
        """Изоляция воркеров: упавший не должен утаскивать остальных."""
        settings = sup.SupervisorSettings(
            heartbeat_interval=5.0,
            shutdown_grace_seconds=1.0,
            restart_backoff_base=1.0,
            restart_backoff_max=1.0,
            max_restarts=2,
            restart_count_reset_after=3600.0,
            max_workers=4,
        )
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(3)
        supervisor.tick()

        registry.created[0].exit(1)
        supervisor.tick()
        assert store.get_worker("br-1")["status"] == WorkerStatus.BACKOFF.value
        clock.advance(1.0)
        supervisor.tick()
        supervisor.tick()

        assert store.get_worker("br-1")["status"] == WorkerStatus.RUNNING.value
        assert store.get_worker("br-2")["status"] == WorkerStatus.RUNNING.value
        assert store.get_worker("br-3")["status"] == WorkerStatus.RUNNING.value

    def test_reaching_limit_on_one_worker_leaves_others_running(self, store, registry, clock):
        settings = sup.SupervisorSettings(
            heartbeat_interval=5.0,
            shutdown_grace_seconds=1.0,
            restart_backoff_base=1.0,
            restart_backoff_max=1.0,
            max_restarts=1,
            restart_count_reset_after=3600.0,
            max_workers=4,
        )
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(2)
        supervisor.tick()

        # Убиваем именно текущий процесс br-1: после рестарта это уже другой
        # объект в registry, и убийство первого ничего бы не значило.
        for _ in range(3):
            current = [p for p in registry.created if p.browser_id == "br-1" and p.poll() is None]
            for process in current:
                process.exit(1)
            supervisor.tick()
            clock.advance(60.0)
            # br-2 и br-3 всё это время работают и стучат heartbeat'ом сами:
            # иначе супервизор признал бы их зависшими, а тест проверяет
            # изоляцию воркеров, а не детект зависания.
            worker_beats(store, clock, "br-2", "br-3")
            supervisor.tick()

        assert store.get_worker("br-1")["status"] == WorkerStatus.CIRCUIT_OPEN.value
        assert store.get_worker("br-2")["status"] == WorkerStatus.RUNNING.value



class TestPauseResume:
    """Пауза через флаг в kv; воркер дорабатывает сценарий сам."""

    def test_pause_sets_flag_and_state(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(2)

        supervisor.pause()

        assert store.is_pause_requested() is True
        assert store.get_run_state() == "paused"

    def test_pause_does_not_terminate_workers(self, store, registry, clock, settings):
        """Пауза — это «доработай текущий сценарий», а не убийство."""
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(2)
        supervisor.tick()

        supervisor.pause()

        assert registry.terminated == []
        assert len(registry.alive()) == 2

    def test_pause_twice_is_idempotent(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)

        supervisor.pause()
        supervisor.pause()

        assert store.get_run_state() == "paused"
        assert store.is_pause_requested() is True

    def test_pause_without_running_workers_raises(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)

        with pytest.raises(sup.NotRunningError):
            supervisor.pause()

    def test_resume_clears_flag_and_state(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        supervisor.pause()

        supervisor.resume()

        assert store.is_pause_requested() is False
        assert store.get_run_state() == "running"

    def test_resume_without_pause_raises(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)

        with pytest.raises(sup.NotPausedError):
            supervisor.resume()

    def test_resume_does_not_spawn_new_workers(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(2)
        supervisor.pause()

        supervisor.resume()

        assert len(registry.created) == 2


class TestGracefulShutdown:
    """SIGTERM -> ожидание -> SIGKILL."""

    def test_stop_terminates_all_workers(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(3)
        supervisor.tick()

        supervisor.stop()

        assert sorted(registry.terminated) == ["br-1", "br-2", "br-3"]

    def test_stop_does_not_kill_cooperative_workers(self, store, registry, clock, settings):
        """Воркер, послушный SIGTERM, SIGKILL не получает."""
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(2)
        supervisor.tick()

        supervisor.stop()

        assert registry.killed == []

    def test_stop_kills_worker_that_ignores_sigterm(self, store, registry, clock, settings):
        registry.ignores_sigterm = True
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(2)
        supervisor.tick()

        supervisor.stop()

        assert sorted(registry.killed) == ["br-1", "br-2"]

    def test_stop_kills_worker_that_exits_only_after_the_kill(self, store, registry, clock, settings):
        """Худший случай: процесс переживает и SIGTERM, и весь grace."""
        registry.ignores_sigterm = True
        registry.survives_kill = True
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        supervisor.tick()

        supervisor.stop()

        assert registry.killed == ["br-1"]

    def test_stop_waits_grace_before_killing(self, store, registry, clock, settings):
        registry.ignores_sigterm = True
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        supervisor.tick()
        start = clock.time()

        supervisor.stop()

        assert clock.time() - start >= settings.shutdown_grace_seconds


    def test_stop_marks_workers_stopped(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(2)
        supervisor.tick()

        supervisor.stop()

        assert {w["status"] for w in store.list_workers()} == {WorkerStatus.STOPPED.value}

    def test_stop_clears_pids(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        supervisor.tick()

        supervisor.stop()

        assert all(w["pid"] is None for w in store.list_workers())

    def test_stop_closes_run_records(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(2)
        supervisor.tick()

        supervisor.stop()

        with store._connect() as conn:
            rows = conn.execute("SELECT status, ended_at FROM runs").fetchall()
        assert {row["status"] for row in rows} == {"stopped"}
        assert all(row["ended_at"] is not None for row in rows)

    def test_stop_sets_run_state_stopped(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        supervisor.tick()

        supervisor.stop()

        assert store.get_run_state() == "stopped"

    def test_stop_clears_pause_flag(self, store, registry, clock, settings):
        """Иначе демон, перезапущенный вручную, стартовал бы сразу с паузой."""
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        supervisor.pause()

        supervisor.stop()

        assert store.is_pause_requested() is False

    def test_stop_is_logged(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        supervisor.tick()

        supervisor.stop()

        with store._connect() as conn:
            messages = [row[0] for row in conn.execute("SELECT message FROM logs").fetchall()]
        assert any("stop" in message.lower() for message in messages)

    def test_stop_without_start_is_noop(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.stop()

        assert registry.terminated == []
        assert store.get_run_state() == "stopped"

    def test_stop_twice_is_idempotent(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        supervisor.tick()
        supervisor.stop()
        terminated_first = list(registry.terminated)

        supervisor.stop()

        assert registry.terminated == terminated_first, "повторный stop не должен слать сигналы снова"

    def test_start_after_stop_reaps_old_processes(self, store, registry, clock, settings):
        """Старые PID не должны числиться воркерами нового запуска."""
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        old_pid = registry.created[0].pid
        supervisor.stop()

        supervisor.start(1)

        assert store.get_worker("br-1")["pid"] != old_pid
        assert store.get_worker("br-1")["pid"] == registry.created[-1].pid


class TestRestart:
    """restart = stop + start, и это два отдельных шага."""

    def test_restart_replaces_processes(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(2)
        supervisor.tick()
        old_pids = {p.pid for p in registry.created}

        supervisor.restart(2)

        new_pids = {p.pid for p in registry.created} - old_pids
        assert len(new_pids) == 2
        assert sorted(registry.terminated) == ["br-1", "br-2"]

    def test_restart_without_running_workers_raises(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)

        with pytest.raises(sup.NotRunningError):
            supervisor.restart(1)

    def test_restart_keeps_run_state_running(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        supervisor.tick()

        supervisor.restart(1)

        assert store.get_run_state() == "running"
        assert store.get_worker("br-1")["status"] == WorkerStatus.STARTING.value

    def test_restart_picks_up_new_worker_count(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        supervisor.tick()

        supervisor.restart(3)

        assert len(registry.alive()) == 3
        assert store.snapshot()["worker_count"] == 3


class TestIdleLoop:
    """Спящий цикл: тикает, не начиная новых воркеров, пока идёт backoff."""

    def test_idle_loop_advances_clock(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        before = clock.time()

        supervisor.run_idle(stop_after=3)

        assert clock.time() > before

    def test_idle_loop_promotes_worker_to_running(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)

        supervisor.run_idle(stop_after=1)

        assert store.get_worker("br-1")["status"] == WorkerStatus.RUNNING.value

    def test_idle_loop_restarts_crashed_worker_over_time(self, store, registry, clock, settings):
        """Главный сценарий демона: воркер упал, демон заметил и поднял."""
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        registry.created[0].exit(1)

        # Тиков много, часы идут: за это время backoff истечёт.
        supervisor.run_idle(stop_after=4)

        assert len(registry.created) == 2, "демон обязан поднять упавшего воркера без HTTP-запросов"

    def test_idle_loop_stops_on_event(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        stop_event = threading.Event()
        stop_event.set()

        supervisor.run_idle(stop_event=stop_event)

        assert store.get_worker("br-1")["status"] == WorkerStatus.STARTING.value, (
            "уже установленное событие остановки должно прекратить цикл до первого тика"
        )

    def test_stale_heartbeat_counts_as_dead(self, store, registry, clock, settings):
        """Демон обязан замечать зависший воркер, даже если процесс формально жив."""
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        supervisor.tick()

        clock.advance(settings.stale_after_seconds + 1)
        supervisor._mark_stale_workers()

        assert store.get_worker("br-1")["status"] == WorkerStatus.BACKOFF.value


class TestConcurrency:
    """Параллельная работа: 2-3 воркера, тики из другого потока."""

    def test_tick_from_another_thread_handles_three_workers(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(3)
        errors = []

        def worker_thread():
            try:
                for _ in range(5):
                    supervisor.tick()
            except Exception as exc:  # noqa: BLE001 - тест обязан увидеть причину
                errors.append(exc)

        thread = threading.Thread(target=worker_thread)
        thread.start()
        thread.join(timeout=10)

        assert errors == []
        assert all(w["heartbeat_at"] is not None for w in store.list_workers())

    def test_simultaneous_http_read_and_tick_does_not_corrupt_state(
        self, store, registry, clock, settings
    ):
        """Сценарий "UI читает /state, пока супервизор тикает"."""
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(3)
        snapshots = []
        errors = []

        def ticker():
            try:
                for _ in range(20):
                    supervisor.tick()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def reader():
            try:
                for _ in range(20):
                    snapshots.append(store.snapshot())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=ticker), threading.Thread(target=reader)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert errors == []
        assert len(snapshots) == 20
        assert all(s["worker_count"] == 3 for s in snapshots)

    def test_concurrent_ticks_do_not_double_spawn(self, store, registry, clock, settings):
        """Гонка двух тиков не должна удвоить число процессов."""
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        registry.created[0].exit(1)
        supervisor.tick()
        clock.advance(10.0)
        barrier = threading.Barrier(2)
        errors = []

        def tick_thread():
            try:
                barrier.wait()
                supervisor.tick()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=tick_thread) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert errors == []
        assert len(registry.created) == 2, "после одного рестарта должен быть ровно один новый процесс"


class TestBackoffFunction:
    """Чистая функция задержки проверяется отдельно от супервизора."""

    def test_first_restart_uses_base(self):
        assert sup.backoff_delay(1, base=1.0, maximum=60.0) == 1.0

    def test_delay_is_monotonic_in_attempts(self):
        delays = [sup.backoff_delay(n, base=1.0, maximum=1000.0) for n in range(1, 6)]

        assert delays == sorted(delays)
        assert len(set(delays)) == len(delays)

    def test_caps_at_maximum(self):
        assert sup.backoff_delay(50, base=1.0, maximum=30.0) == 30.0

    def test_does_not_explode_on_huge_attempt_count(self):
        """Потолок спасает и от переполнения: 10**6 попыток — всё равно maximum."""
        assert sup.backoff_delay(10**6, base=1.0, maximum=60.0) == 60.0


class TestWorkerEnvironment:
    """Переменные, которые супервизор передаёт воркеру через окружение.

    Воркер — отдельный процесс, памятью с демоном не делится, поэтому канал
    «факт о пуле -> воркер» обязан быть атомарным со спавном. Файл-маркер
    ``.MULTI_BROWSERS_IN_USE`` раньше заводил ``run_ad_clicker.py``; после
    удаления этой точки входа источником стал супервизор.
    """

    def test_pool_size_is_exported_into_spawned_workers(
        self, store, registry, clock, settings
    ):
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(3)

        assert [call["env"]["ADCLICKER_POOL_SIZE"] for call in registry.start_calls] == [
            "3",
            "3",
            "3",
        ]

    def test_single_worker_is_not_marked_multi_browser(
        self, store, registry, clock, settings
    ):
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(1)

        env = registry.start_calls[0]["env"]
        assert env["ADCLICKER_POOL_SIZE"] == "1"
        assert env["ADCLICKER_MULTI_BROWSERS"] == "0"

    def test_multi_browser_marker_is_set_when_pool_exceeds_one(
        self, store, registry, clock, settings
    ):
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(2)

        assert {call["env"]["ADCLICKER_MULTI_BROWSERS"] for call in registry.start_calls} == {
            "1"
        }

    def test_pool_size_is_reset_after_stop(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(2)

        supervisor.stop()

        env = supervisor._default_env("br-1")
        assert env["ADCLICKER_POOL_SIZE"] == "0"
        assert env["ADCLICKER_MULTI_BROWSERS"] == "0"

    def test_failed_start_does_not_leave_a_stale_pool_size(
        self, store, registry, clock, settings
    ):
        """Откат пула обязан сбрасывать иначе следующий спавн соврал бы о размере."""
        supervisor = make_supervisor(store, registry, clock, settings)
        registry.spawn_error = sup.WorkerSpawnError("нет места")

        with pytest.raises(sup.WorkerSpawnError):
            supervisor.start(3)

        assert supervisor._default_env("br-1")["ADCLICKER_POOL_SIZE"] == "0"

    def test_restart_updates_pool_size_for_the_new_pool(
        self, store, registry, clock, settings
    ):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(2)

        supervisor.restart(3)

        assert supervisor._default_env("br-1")["ADCLICKER_POOL_SIZE"] == "3"


class TestParallelShutdown:
    """Остановка пула должна стоить два grace, а не grace на каждого воркера.

    Порядок из плана: «SIGTERM -> 10 с -> SIGKILL **по всем PID**». Обе фазы
    рассылают сигналы всем до общего дедлайна, поэтому счёт для N воркеров —
    ``2 * grace`` (SIGTERM-выдержка плюс SIGKILL-выдержка), а не
    ``N * grace``. Последовательная реализация дала бы при N=3 тридцать
    секунд и не влезла бы в ``STOP_GRACE`` Tauri-хоста: тот SIGKILL'ил бы
    демон посреди остановки, а воркеры (собственная сессия у каждого)
    пережили бы его как сироты с открытыми Chrome.

    Оба прогона нужны: с ``ignores_sigterm`` меряется фаза SIGTERM, с
    ``survives_kill`` — фаза SIGKILL, и только вместе они ловят возврат
    «дедлайн на каждого» в любой из фаз.
    """

    def test_sigterm_phase_costs_a_single_grace_for_the_whole_pool(
        self, store, clock, settings
    ):
        registry = FakeProcessRegistry(ignores_sigterm=True)
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(3)

        supervisor.stop()

        limit = settings.shutdown_grace_seconds
        assert clock.value <= limit * 2 + 1.0, (
            f"остановка 3 воркеров заняла {clock.value:.1f}s при grace {limit}s — "
            "сигналы, похоже, ждутся последовательно"
        )
        assert sorted(set(registry.terminated)) == ["br-1", "br-2", "br-3"], (
            "SIGTERM должен уйти каждому воркеру"
        )
        assert sorted(set(registry.killed)) == ["br-1", "br-2", "br-3"], (
            "не послушавшие SIGTERM обязаны получить SIGKILL"
        )

    def test_sigkill_phase_shares_one_deadline_too(self, store, clock, settings):
        """Кто пережил SIGKILL, всё равно ждёт по общему дедлайну."""
        registry = FakeProcessRegistry(ignores_sigterm=True, survives_kill=True)
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(3)

        supervisor.stop()

        limit = settings.shutdown_grace_seconds
        # 2 * grace (SIGTERM-выдержка + SIGKILL-выдержка) и ни дольше:
        # последовательная схема дала бы здесь 3 * grace.
        assert clock.value <= limit * 2 + 1.0, (
            f"фаза SIGKILL заняла {clock.value:.1f}s при grace {limit}s — "
            "дедлайн, похоже, считается на каждого воркера отдельно"
        )
        assert sorted(set(registry.killed)) == ["br-1", "br-2", "br-3"]
        for worker in store.list_workers():
            assert worker["status"] == WorkerStatus.STOPPED.value


# --- назначение и ротация прокси ------------------------------------------


def make_pool(db_path) -> ProxyPool:
    """Пул прокси над той же БД, что и супервизор (у супервизора свой
    экземпляр, но файл один — состояние общее, как в демоне)."""
    return ProxyPool(db_path)


def hold_proxy(store, proxy_id, browser_id, status="running", pid=4242):
    """Строка чужого живого воркера, уже держащего прокси."""
    store.register_worker(browser_id, pid)
    with store._connect() as conn:
        conn.execute(
            "UPDATE workers SET proxy_id = ?, status = ? WHERE browser_id = ?",
            (proxy_id, status, browser_id),
        )
        conn.commit()


def degrade(db_path, browser_id, reason="cdp connection lost"):
    """Пишет в ``workers`` ровно то, что пишет воркер в on_proxy_dead.

    ``StoreWriter.mark_degraded`` — реальный сигнал воркера (ветка transport),
    поэтому тест ротации проверяет путь «воркер сообщил → супервизор заметил»,
    а не SQL, скопированный в тест.
    """
    writer = StoreWriter(db_path)
    try:
        writer.mark_degraded(browser_id, reason)
    finally:
        writer.close()


def usage_rows(store):
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT proxy_id, browser_id, result FROM proxy_usage ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def proxy_logs(store, level=None):
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT level, category, browser_id, message, fields FROM logs "
            "WHERE category = 'proxy' ORDER BY id"
        ).fetchall()
    logs = [dict(row) for row in rows]
    if level is None:
        return logs
    return [row for row in logs if row["level"] == level]


def all_log_text(store) -> str:
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT level, category, browser_id, message, fields FROM logs ORDER BY id"
        ).fetchall()
    return "\n".join(
        f"{row['level']} {row['category']} {row['browser_id']} {row['message']} {row['fields']}"
        for row in rows
    )


def assign_profile(store, browser_id, name="default"):
    """Назначает профиль воркера ровно в том виде, в каком это делает пул:
    строка ``profiles`` в статусе ``assigned`` и ссылка ``workers.profile_id``."""
    with store._connect() as conn:
        conn.execute("INSERT INTO profiles (name, status) VALUES (?, 'assigned')", (name,))
        profile_id = conn.execute(
            "SELECT id FROM profiles WHERE name = ?", (name,)
        ).fetchone()["id"]
        conn.execute(
            "UPDATE workers SET profile_id = ? WHERE browser_id = ?",
            (profile_id, browser_id),
        )
        conn.commit()
    return profile_id


def profile_status(db_path, profile_id) -> str | None:
    conn = migrations.connect(db_path)
    try:
        row = conn.execute("SELECT status FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        return None if row is None else row["status"]
    finally:
        conn.close()


def profile_rows(db_path) -> list[dict]:
    conn = migrations.connect(db_path)
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()]
    finally:
        conn.close()


def browser_logs(store, level=None):
    """Логи профилей (категория browser) — зеркало proxy_logs."""
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT level, category, browser_id, message, fields FROM logs "
            "WHERE category = 'browser' ORDER BY id"
        ).fetchall()
    logs = [dict(row) for row in rows]
    if level is None:
        return logs
    return [row for row in logs if row["level"] == level]


class TestProxyAssignment:
    """Назначение прокси при спавне: живые строки, дележ, пустой пул.

    Супервизор — единственный, кто выдаёт ``ADCLICKER_PROXY``: выбор делается
    по живым (``is_alive=1``) и не занятым другим живым воркером строкам, а
    факт выдачи уходит в ``workers.proxy_id``, ``proxy_usage`` и лог с
    категорией ``proxy`` без кредов.
    """

    def test_spawn_assigns_alive_free_proxy_and_passes_it_via_env(
        self, store, registry, clock, settings, db_path
    ):
        pool = make_pool(db_path)
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        proxy_id = pool.list_proxies()[0]["id"]
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(1)

        assert store.get_worker("br-1")["proxy_id"] == proxy_id
        assert registry.start_calls[0]["env"]["ADCLICKER_PROXY"] == (
            "alice:s3cr3t@10.0.0.1:8080"
        )

    def test_spawn_does_not_duplicate_live_assignments(
        self, store, registry, clock, settings, db_path
    ):
        """Прокси, занятый живым воркером, не достаётся никому ещё."""
        pool = make_pool(db_path)
        pool.add_lines(
            [
                "alice:s3cr3t@10.0.0.1:8080",
                "alice:s3cr3t@10.0.0.2:8080",
                "alice:s3cr3t@10.0.0.3:8080",
            ]
        )
        ids = [row["id"] for row in pool.list_proxies()]
        hold_proxy(store, ids[0], "br-9")
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(2)

        assigned = [store.get_worker(b)["proxy_id"] for b in ("br-1", "br-2")]
        assert ids[0] not in assigned, "живое назначение br-9 должно уцелеть"
        assert assigned[0] != assigned[1], "свободных прокси хватает — дележа нет"
        assert [call["env"]["ADCLICKER_PROXY"] for call in registry.start_calls] == [
            "alice:s3cr3t@10.0.0.2:8080",
            "alice:s3cr3t@10.0.0.3:8080",
        ]

    def test_spawn_shares_the_only_alive_proxy_with_a_warning(
        self, store, registry, clock, settings, db_path
    ):
        """Свободных нет — дележ, но старт блокировать нельзя."""
        pool = make_pool(db_path)
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        proxy_id = pool.list_proxies()[0]["id"]
        hold_proxy(store, proxy_id, "br-9")
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(1)

        assert store.get_worker("br-1")["proxy_id"] == proxy_id
        assert registry.start_calls[0]["env"]["ADCLICKER_PROXY"] == (
            "alice:s3cr3t@10.0.0.1:8080"
        )
        assert [(row["proxy_id"], row["result"]) for row in usage_rows(store)] == [
            (proxy_id, "shared")
        ]
        warnings = proxy_logs(store, level="WARNING")
        assert warnings, "дележ обязан быть виден в логе"
        assert all(row["browser_id"] == "br-1" for row in warnings)
        assert all("shared" in row["message"] for row in warnings)

    def test_spawn_with_an_empty_pool_keeps_worker_without_proxy(
        self, store, registry, clock, settings, db_path
    ):
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(2)

        assert len(registry.created) == 2, "пустой пул не должен мешать старту"
        assert all(
            "ADCLICKER_PROXY" not in call["env"] for call in registry.start_calls
        )
        assert all(store.get_worker(b)["proxy_id"] is None for b in ("br-1", "br-2"))
        assert usage_rows(store) == []

    def test_dead_proxy_is_never_assigned(self, store, registry, clock, settings, db_path):
        """Жив только ``is_alive=1``: мёртвый прокси не выдаётся даже в дележе."""
        pool = make_pool(db_path)
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        proxy_id = pool.list_proxies()[0]["id"]
        pool.record_check_result(proxy_id, alive=False, error="timeout")
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(1)

        assert "ADCLICKER_PROXY" not in registry.start_calls[0]["env"]
        assert store.get_worker("br-1")["proxy_id"] is None
        assert proxy_logs(store, level="WARNING"), (
            "непустой, но мёртвый пул должен быть заметен в логе"
        )

    def test_assignment_is_logged_without_credentials(
        self, store, registry, clock, settings, db_path
    ):
        pool = make_pool(db_path)
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(1)

        logs = proxy_logs(store)
        assert logs, "назначение прокси обязано попадать в лог"
        assert logs[0]["category"] == "proxy"
        assert logs[0]["browser_id"] == "br-1"
        assert any(row["message"] == "proxy assigned" for row in logs)
        assert "s3cr3t" not in all_log_text(store), "креды не должны утекать в логи"

    def test_stop_releases_proxy_and_profile(self, store, registry, clock, settings, db_path):
        pool = make_pool(db_path)
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        profile_id = assign_profile(store, "br-1")

        supervisor.stop()

        worker = store.get_worker("br-1")
        assert worker["proxy_id"] is None, "после stop прокси должен быть свободен"
        assert worker["profile_id"] is None, "профиль должен освободиться при остановке"
        assert profile_status(db_path, profile_id) == "free", (
            "полный релиз на stop обязан вернуть профиль в пул"
        )

    def test_open_circuit_releases_proxy(self, store, clock, db_path):
        pool = make_pool(db_path)
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        registry = FakeProcessRegistry()
        settings = sup.SupervisorSettings(
            heartbeat_interval=5.0,
            shutdown_grace_seconds=0.2,
            restart_backoff_base=0.1,
            restart_backoff_max=0.2,
            max_restarts=1,
            restart_count_reset_after=3600.0,
            max_workers=4,
        )
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        assert store.get_worker("br-1")["proxy_id"] is not None, "спавн должен назначить"

        # Два падения подряд при max_restarts=1 — дальше circuit open.
        for _ in range(2):
            registry.created[-1].exit(1)
            supervisor.tick()
            clock.advance(60.0)
            supervisor.tick()

        worker = store.get_worker("br-1")
        assert worker["status"] == WorkerStatus.CIRCUIT_OPEN.value
        assert worker["proxy_id"] is None, "убитый воркер не должен держать прокси"

    def test_restart_after_stop_assigns_a_fresh_proxy_row(
        self, store, registry, clock, settings, db_path
    ):
        pool = make_pool(db_path)
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080", "alice:s3cr3t@10.0.0.2:8080"])
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        supervisor.stop()

        supervisor.start(1)

        worker = store.get_worker("br-1")
        assert worker["proxy_id"] is not None
        assert worker["profile_id"] is None, "новый воркер стартует с чистым профилем"
        assert "ADCLICKER_PROXY" in registry.start_calls[-1]["env"]


class TestProxyRotation:
    """Ротация на ``degraded``: подмена, backoff, отказ при отсутствии резерва.

    Деградацию пишет воркер (``StoreWriter.mark_degraded``), решение принимает
    ``tick()``. Ротация — не падение воркера: ``restart_count`` не растёт, а
    темп задаётся тем же backoff'ом, что и у рестартов, чтобы подмена не
    превращалась в плотный цикл.
    """

    def test_degraded_worker_is_respawned_with_a_reserve_proxy(
        self, store, registry, clock, settings, db_path
    ):
        pool = make_pool(db_path)
        pool.add_lines(
            ["alice:s3cr3t@10.0.0.1:8080", "bob:hunter2@10.0.0.2:9090"]
        )
        ids = [row["id"] for row in pool.list_proxies()]
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        assert store.get_worker("br-1")["proxy_id"] == ids[0]

        degrade(db_path, "br-1")
        supervisor.tick()

        assert len(registry.created) == 2, "ротация обязана поднять новый процесс"
        assert registry.created[0].poll() is not None, "старый процесс должен быть погашен"
        assert store.get_worker("br-1")["proxy_id"] == ids[1]
        assert store.get_worker("br-1")["status"] == WorkerStatus.STARTING.value
        assert registry.start_calls[1]["env"]["ADCLICKER_PROXY"] == (
            "bob:hunter2@10.0.0.2:9090"
        )
        assert registry.start_calls[1]["env"]["ADCLICKER_PROXY"] != (
            registry.start_calls[0]["env"]["ADCLICKER_PROXY"]
        )

    def test_rotation_records_usage_and_keeps_restart_count_clean(
        self, store, registry, clock, settings, db_path
    ):
        """Ротация — не вина воркера: счётчик падений и circuit breaker молчат.

        Наблюдаемость обеспечивают ``proxy_usage`` и лог с категорией ``proxy``,
        а не ``restart_count``: иначе несколько смен прокси убили бы здорового
        воркера потолком рестартов.
        """
        pool = make_pool(db_path)
        pool.add_lines(
            ["alice:s3cr3t@10.0.0.1:8080", "bob:hunter2@10.0.0.2:9090"]
        )
        ids = [row["id"] for row in pool.list_proxies()]
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)

        degrade(db_path, "br-1")
        supervisor.tick()

        worker = store.get_worker("br-1")
        assert worker["restart_count"] == 0
        assert worker["status"] == WorkerStatus.STARTING.value
        assert [(row["proxy_id"], row["browser_id"], row["result"]) for row in usage_rows(store)] == [
            (ids[0], "br-1", "assigned"),
            (ids[0], "br-1", "exhausted"),
            (ids[1], "br-1", "rotated"),
        ]
        assert "s3cr3t" not in all_log_text(store)
        assert "hunter2" not in all_log_text(store)

    def test_rotation_waits_backoff_before_the_next_rotation(
        self, store, registry, clock, settings, db_path
    ):
        pool = make_pool(db_path)
        pool.add_lines(
            ["alice:s3cr3t@10.0.0.1:8080", "bob:hunter2@10.0.0.2:9090"]
        )
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)

        degrade(db_path, "br-1")
        supervisor.tick()
        assert len(registry.created) == 2

        # Новый прокси тоже оказался плохим: подряд ротировать нельзя.
        degrade(db_path, "br-1")
        supervisor.tick()
        assert len(registry.created) == 2, "вторая ротация раньше backoff — плотный цикл"

        clock.advance(sup.RESTART_BACKOFF_BASE_SECONDS)
        supervisor.tick()
        assert len(registry.created) == 3, "после backoff повторная ротация обязательна"

    def test_degraded_worker_without_reserve_stays_degraded(
        self, store, registry, clock, settings, db_path
    ):
        """Резерва нет — воркер остаётся degraded и пробует снова по интервалу."""

        def postponed():
            return [
                row
                for row in proxy_logs(store, level="WARNING")
                if "rotation postponed" in row["message"]
            ]

        def exhausted():
            return [row for row in proxy_logs(store) if row["message"] == "proxy exhausted"]

        pool = make_pool(db_path)
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)

        degrade(db_path, "br-1")
        supervisor.tick()

        worker = store.get_worker("br-1")
        assert worker["status"] == WorkerStatus.DEGRADED.value, (
            "без резерва статус обязан остаться degraded, а не уйти в running/backoff"
        )
        assert worker["proxy_id"] is not None
        assert registry.created[0].poll() is None, "процесс без резерва не убиваем"
        assert len(registry.created) == 1
        assert len(postponed()) == 1
        assert len(exhausted()) == 1, "исчерпание фиксируется один раз на деградацию"

        # Тики без времени не должны плодить предупреждения и попытки.
        supervisor.tick()
        supervisor.tick()
        assert len(postponed()) == 1
        assert len(exhausted()) == 1
        assert len(registry.created) == 1

        clock.advance(sup.RESTART_BACKOFF_BASE_SECONDS)
        supervisor.tick()
        assert len(postponed()) == 2, (
            "попытка обязана повторяться по интервалу, а не затихнуть"
        )
        assert len(exhausted()) == 1, "повторная попытка не должна дублировать запись"
        assert len(registry.created) == 1, "пока резерва нет — респавнов не бывает"

    def test_rotation_never_takes_another_workers_proxy(
        self, store, registry, clock, settings, db_path
    ):
        pool = make_pool(db_path)
        pool.add_lines(
            ["alice:s3cr3t@10.0.0.1:8080", "bob:hunter2@10.0.0.2:9090"]
        )
        ids = [row["id"] for row in pool.list_proxies()]
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        hold_proxy(store, ids[1], "br-9")

        degrade(db_path, "br-1")
        supervisor.tick()

        worker = store.get_worker("br-1")
        assert worker["status"] == WorkerStatus.DEGRADED.value
        assert worker["proxy_id"] == ids[0], "чужой занятый прокси не годится в резерв"
        assert len(registry.created) == 1

    def test_rotation_keeps_the_profile_and_leaves_other_workers_alone(
        self, store, registry, clock, settings, db_path
    ):
        """Ротация меняет только прокси.

        Мотивация правки фазы 5: ``release_assignment`` чистил оба поля, и
        подмена прокси сбрасывала профиль воркера — воркер продолжал работу
        уже без аккаунта, а профиль уходил в пул к чужому браузеру. Профиль
        переживает ротацию; полный релиз остаётся на stop/circuit-open.
        """
        pool = make_pool(db_path)
        pool.add_lines(
            [
                "alice:s3cr3t@10.0.0.1:8080",
                "alice:s3cr3t@10.0.0.2:8080",
                "alice:s3cr3t@10.0.0.3:8080",
            ]
        )
        ids = [row["id"] for row in pool.list_proxies()]
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(2)
        profile_id = assign_profile(store, "br-1")
        second_pid = store.get_worker("br-2")["pid"]

        degrade(db_path, "br-1")
        supervisor.tick()

        rotated = store.get_worker("br-1")
        assert rotated["proxy_id"] == ids[2], "резерв — свободный живой прокси"
        assert rotated["profile_id"] == profile_id, (
            "ротация прокси не должна сбрасывать профиль воркера"
        )
        assert profile_status(db_path, profile_id) == "assigned"
        untouched = store.get_worker("br-2")
        assert untouched["proxy_id"] == ids[1]
        assert untouched["status"] == WorkerStatus.RUNNING.value
        assert untouched["pid"] == second_pid, "ротация не должна трогать соседа"
        assert len(registry.created) == 3, "новый процесс только у деградировавшего"

    def test_rotation_is_logged_with_proxy_category_and_no_credentials(
        self, store, registry, clock, settings, db_path
    ):
        pool = make_pool(db_path)
        pool.add_lines(
            ["alice:s3cr3t@10.0.0.1:8080", "bob:hunter2@10.0.0.2:9090"]
        )
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)

        degrade(db_path, "br-1")
        supervisor.tick()

        logs = proxy_logs(store)
        assert any(row["message"] == "proxy exhausted" for row in logs)
        assert any(row["message"] == "proxy rotated" for row in logs)
        assert all(row["category"] == "proxy" for row in logs)
        assert all(row["browser_id"] == "br-1" for row in logs)
        assert "s3cr3t" not in all_log_text(store)
        assert "hunter2" not in all_log_text(store)

    def test_rotation_does_not_happen_before_the_first_degrade(
        self, store, registry, clock, settings, db_path
    ):
        pool = make_pool(db_path)
        pool.add_lines(
            ["alice:s3cr3t@10.0.0.1:8080", "bob:hunter2@10.0.0.2:9090"]
        )
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)

        supervisor.tick()
        supervisor.tick()

        assert len(registry.created) == 1, "здоровый воркер не должен ротироваться"
        assert store.get_worker("br-1")["status"] == WorkerStatus.RUNNING.value
        assert [(row["result"]) for row in usage_rows(store)] == ["assigned"]

    def test_crash_respawn_keeps_its_own_proxy(
        self, store, registry, clock, settings, db_path
    ):
        """Падение — не повод менять прокси: подмена бывает только по degraded.

        Иначе любое падение (диск, сайт, OOM) тихо меняло бы назначение, а
        план привязывает ротацию именно к сигналу воркера о мёртвом прокси.
        """
        pool = make_pool(db_path)
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080", "alice:s3cr3t@10.0.0.2:8080"])
        ids = [row["id"] for row in pool.list_proxies()]
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        assert store.get_worker("br-1")["proxy_id"] == ids[0]

        registry.created[0].exit(1)
        supervisor.tick()
        clock.advance(sup.RESTART_BACKOFF_BASE_SECONDS)
        supervisor.tick()

        worker = store.get_worker("br-1")
        assert worker["proxy_id"] == ids[0], "свой прокси должен вернуться"
        assert registry.start_calls[1]["env"]["ADCLICKER_PROXY"] == (
            "alice:s3cr3t@10.0.0.1:8080"
        )
        assert [(row["proxy_id"], row["result"]) for row in usage_rows(store)] == [
            (ids[0], "assigned"),
            (ids[0], "assigned"),
        ]


# --- профили ----------------------------------------------------------------


def add_profiles(db_path, *names):
    """Свободные профили через пул, возвращает их id по порядку."""
    pool = ProfilePool(db_path)
    result = pool.add_profiles([{"name": name} for name in names])
    assert result["added"] == len(names), result
    return [row["id"] for row in profile_rows(db_path)]


def env_of(registry, call=-1):
    return registry.start_calls[call]["env"]


class TestProfileAssignment:
    """Выдача профиля при спавне: env, профильный прокси, лог, откат.

    Контракт: супервизор передаёт ``ADCLICKER_PROFILE_ID`` (десятичный id) и,
    если у профиля есть свой прокси, делает его ``ADCLICKER_PROXY`` —
    приоритетнее выбора из пула. Нет профиля — переменной нет вовсе, даже
    если она досталась демону из окружения.
    """

    def test_spawn_takes_a_profile_and_passes_its_id_via_env(
        self, store, registry, clock, settings, db_path
    ):
        (profile_id,) = add_profiles(db_path, "alice")
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(1)

        worker = store.get_worker("br-1")
        assert worker["profile_id"] == profile_id
        assert env_of(registry)["ADCLICKER_PROFILE_ID"] == str(profile_id)
        assert profile_status(db_path, profile_id) == "assigned"

    def test_spawn_without_profiles_leaves_the_worker_profileless(
        self, store, registry, clock, settings, db_path
    ):
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(1)

        assert "ADCLICKER_PROFILE_ID" not in env_of(registry)
        assert store.get_worker("br-1")["profile_id"] is None
        assert profile_rows(db_path) == []

    def test_profile_env_inherited_from_the_daemon_is_dropped(
        self, store, registry, clock, settings, db_path, monkeypatch
    ):
        """Наследие окружения демона не должно обходить пул, как ADCLICKER_PROXY."""
        monkeypatch.setenv("ADCLICKER_PROFILE_ID", "999")
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(1)

        assert "ADCLICKER_PROFILE_ID" not in env_of(registry)

    def test_each_worker_gets_its_own_profile(
        self, store, registry, clock, settings, db_path
    ):
        ids = add_profiles(db_path, "a", "b")
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(2)

        assert [store.get_worker(b)["profile_id"] for b in ("br-1", "br-2")] == ids
        assert [env_of(registry, call)["ADCLICKER_PROFILE_ID"] for call in (0, 1)] == [
            str(ids[0]),
            str(ids[1]),
        ]

    def test_profile_proxy_wins_over_the_pool(
        self, store, registry, clock, settings, db_path
    ):
        """Свой прокси профиля приоритетнее пула: аккаунт не уезжает в чужую страну."""
        pool = make_pool(db_path)
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080", "bob:hunter2@10.0.0.2:9090"])
        proxy_ids = [row["id"] for row in pool.list_proxies()]
        profiles = ProfilePool(db_path)
        profiles.add_profiles([{"name": "alice", "proxy_id": proxy_ids[1]}])
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(1)

        assert store.get_worker("br-1")["proxy_id"] == proxy_ids[1]
        assert env_of(registry)["ADCLICKER_PROXY"] == "bob:hunter2@10.0.0.2:9090"
        assert [(row["proxy_id"], row["result"]) for row in usage_rows(store)] == [
            (proxy_ids[1], "assigned")
        ], "прокси пула не должен расходоваться, когда у профиля есть свой"

    def test_profile_without_proxy_falls_back_to_the_pool(
        self, store, registry, clock, settings, db_path
    ):
        pool = make_pool(db_path)
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        proxy_id = pool.list_proxies()[0]["id"]
        add_profiles(db_path, "alice")
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(1)

        assert store.get_worker("br-1")["proxy_id"] == proxy_id
        assert env_of(registry)["ADCLICKER_PROXY"] == "alice:s3cr3t@10.0.0.1:8080"

    def test_dead_profile_proxy_falls_back_to_the_pool_with_a_warning(
        self, store, registry, clock, settings, db_path
    ):
        pool = make_pool(db_path)
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080", "bob:hunter2@10.0.0.2:9090"])
        proxy_ids = [row["id"] for row in pool.list_proxies()]
        pool.record_check_result(proxy_ids[0], alive=False, error="timeout")
        profiles = ProfilePool(db_path)
        profiles.add_profiles([{"name": "alice", "proxy_id": proxy_ids[0]}])
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(1)

        assert store.get_worker("br-1")["proxy_id"] == proxy_ids[1], (
            "мёртвый прокси профиля не должен попадать в env — пул даёт живой"
        )
        assert env_of(registry)["ADCLICKER_PROXY"] == "bob:hunter2@10.0.0.2:9090"
        assert proxy_logs(store, level="WARNING"), "подмена должна быть видна в логе"

    def test_spawn_logs_the_profile_without_secrets(
        self, store, registry, clock, settings, db_path
    ):
        pool = make_pool(db_path)
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        proxy_id = pool.list_proxies()[0]["id"]
        ProfilePool(db_path).add_profiles(
            [{"name": "alice", "key_ref": "hooks:SECRET-KEY", "proxy_id": proxy_id}]
        )
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(1)

        logs = browser_logs(store)
        assert logs, "выдача профиля обязана попасть в лог"
        assert logs[0]["category"] == "browser"
        assert logs[0]["browser_id"] == "br-1"
        fields = json.loads(logs[0]["fields"])
        assert fields["profile_id"] == 1
        assert fields["proxy_id"] == proxy_id
        assert "SECRET-KEY" not in all_log_text(store), "key_ref не пишется в логи"
        assert "s3cr3t" not in all_log_text(store), "креды прокси не пишутся в логи"

    def test_failed_spawn_does_not_leave_the_profile_assigned(
        self, store, clock, settings, db_path
    ):
        (profile_id,) = add_profiles(db_path, "alice")
        registry = FakeProcessRegistry(spawn_error=OSError("no such binary"))
        supervisor = make_supervisor(store, registry, clock, settings)

        with pytest.raises(sup.WorkerSpawnError):
            supervisor.start(1)

        assert profile_status(db_path, profile_id) == "free", (
            "профиль нельзя оставить назначенным на воркер, которого нет"
        )

    def test_respawn_after_a_crash_keeps_the_same_profile(
        self, store, registry, clock, settings, db_path
    ):
        """Падение — не повод менять профиль: реапер освобождает, респавн забирает."""
        ids = add_profiles(db_path, "a", "b")
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        assert store.get_worker("br-1")["profile_id"] == ids[0]

        registry.created[0].exit(1)
        supervisor.tick()

        assert profile_status(db_path, ids[0]) == "free", (
            "реапер освобождает профиль мёртвого воркера — иначе пул потерял бы строку"
        )
        assert store.get_worker("br-1")["profile_id"] == ids[0], (
            "ссылка переживает реапер: именно она возвращает профиль респавну"
        )

        clock.advance(sup.RESTART_BACKOFF_BASE_SECONDS)
        supervisor.tick()

        worker = store.get_worker("br-1")
        assert worker["profile_id"] == ids[0]
        assert profile_status(db_path, ids[0]) == "assigned"
        assert env_of(registry, -1)["ADCLICKER_PROFILE_ID"] == str(ids[0])

    def test_rotation_passes_the_profile_to_the_new_process(
        self, store, registry, clock, settings, db_path
    ):
        pool = make_pool(db_path)
        pool.add_lines(
            ["alice:s3cr3t@10.0.0.1:8080", "bob:hunter2@10.0.0.2:9090"]
        )
        (profile_id,) = add_profiles(db_path, "alice")
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        assert env_of(registry, 0)["ADCLICKER_PROFILE_ID"] == str(profile_id)

        degrade(db_path, "br-1")
        supervisor.tick()

        assert env_of(registry, 1)["ADCLICKER_PROFILE_ID"] == str(profile_id), (
            "новый процесс ротации обязан получить тот же профиль"
        )
        assert store.get_worker("br-1")["profile_id"] == profile_id
        assert profile_status(db_path, profile_id) == "assigned"

    def test_open_circuit_releases_the_profile(self, store, clock, db_path):
        (profile_id,) = add_profiles(db_path, "alice")
        registry = FakeProcessRegistry()
        settings = sup.SupervisorSettings(
            heartbeat_interval=5.0,
            shutdown_grace_seconds=0.2,
            restart_backoff_base=0.1,
            restart_backoff_max=0.2,
            max_restarts=1,
            restart_count_reset_after=3600.0,
            max_workers=4,
        )
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        assert store.get_worker("br-1")["profile_id"] == profile_id

        for _ in range(2):
            registry.created[-1].exit(1)
            supervisor.tick()
            clock.advance(60.0)
            supervisor.tick()

        worker = store.get_worker("br-1")
        assert worker["status"] == WorkerStatus.CIRCUIT_OPEN.value
        assert worker["profile_id"] is None, "раскрытая цепь — полный релиз"
        assert profile_status(db_path, profile_id) == "free"

    def test_stop_releases_the_profile_taken_from_the_pool(
        self, store, registry, clock, settings, db_path
    ):
        (profile_id,) = add_profiles(db_path, "alice")
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        assert store.get_worker("br-1")["profile_id"] == profile_id

        supervisor.stop()

        assert store.get_worker("br-1")["profile_id"] is None
        assert profile_status(db_path, profile_id) == "free"


class TestProfileReaper:
    """Реапер в tick(): назначение без живого воркера не висит вечно.

    Страховка от потери назначения: если воркер умер, удалён из реестра или
    не стучал heartbeat'ом, профиль возвращается в пул. Ссылка
    ``workers.profile_id`` при этом сохраняется — это память, благодаря
    которой респавн получает тот же профиль.
    """

    def test_profile_of_a_live_worker_survives_ticks(
        self, store, registry, clock, settings, db_path
    ):
        (profile_id,) = add_profiles(db_path, "alice")
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        worker_beats(store, clock, "br-1")

        supervisor.tick()
        supervisor.tick()

        assert profile_status(db_path, profile_id) == "assigned"
        assert store.get_worker("br-1")["profile_id"] == profile_id

    def test_profile_is_freed_when_the_process_dies(
        self, store, registry, clock, settings, db_path
    ):
        (profile_id,) = add_profiles(db_path, "alice")
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)

        registry.created[0].exit(1)
        supervisor.tick()

        assert profile_status(db_path, profile_id) == "free"
        assert store.get_worker("br-1")["profile_id"] == profile_id, (
            "ссылка остаётся памятью о прошлом назначении"
        )

    def test_profile_is_freed_when_the_worker_row_is_gone(
        self, store, registry, clock, settings, db_path
    ):
        (profile_id,) = add_profiles(db_path, "alice")
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        store.delete_worker("br-1")

        supervisor.tick()

        assert profile_status(db_path, profile_id) == "free"

    def test_profile_is_freed_when_the_heartbeat_goes_stale(
        self, store, registry, clock, settings, db_path
    ):
        (profile_id,) = add_profiles(db_path, "alice")
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        worker_beats(store, clock, "br-1")

        # Heartbeat замер: воркер перестал стучить, дальше — stale-логика
        # (процесс гасится) и реапер (профиль освобождается).
        clock.advance(sup.STALE_MULTIPLIER * sup.HEARTBEAT_INTERVAL_SECONDS + 1.0)
        supervisor.tick()

        assert store.get_worker("br-1")["status"] == WorkerStatus.BACKOFF.value
        assert profile_status(db_path, profile_id) == "free"

    def test_reaping_is_logged_with_the_profile_id(
        self, store, registry, clock, settings, db_path
    ):
        (profile_id,) = add_profiles(db_path, "alice")
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        worker_beats(store, clock, "br-1")
        clock.advance(sup.STALE_MULTIPLIER * sup.HEARTBEAT_INTERVAL_SECONDS + 1.0)

        supervisor.tick()

        logs = browser_logs(store, level="WARNING")
        assert logs, "реапер обязан оставить след в логе"
        assert profile_id in [json.loads(row["fields"])["profile_ids"][0] for row in logs]
        assert all(row["category"] == "browser" for row in logs)

    def test_profiles_of_other_live_workers_are_untouched(
        self, store, registry, clock, settings, db_path
    ):
        ids = add_profiles(db_path, "a", "b")
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(2)
        worker_beats(store, clock, "br-1", "br-2")

        registry.created[0].exit(1)
        supervisor.tick()

        assert profile_status(db_path, ids[0]) == "free"
        assert profile_status(db_path, ids[1]) == "assigned"
