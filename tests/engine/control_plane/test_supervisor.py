"""Тесты супервизора воркеров.

Супервизор — единственное место, где принимаются решения о жизни и смерти
процессов, поэтому проверяется он на подменяемой фабрике процессов: реальный
Chrome не запускается никогда. Подмена живёт в памяти теста и ведёт себя как
настоящий дочерний процесс с той стороны, которая важна супервизору: жив/мёртв,
terminate, kill, код возврата.

Управление временем — через явный ``now`` в supervisor.tick() и через
подставленные часы, поэтому тесты не спят и не мигают.
"""

import threading
import time

import pytest

from engine.control_plane import supervisor as sup
from engine.control_plane.state import StateStore, WorkerStatus


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
        self.start_calls.append({"browser_id": browser_id, "command": list(command)})
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
    """Heartbeat в БД каждые 5 секунд."""

    def test_start_writes_initial_heartbeat(self, store, registry, clock, settings):
        """Регистрация сама ставит heartbeat — UI не должен ждать первого тика,
        чтобы понять, что воркер только что стартовал."""
        supervisor = make_supervisor(store, registry, clock, settings)

        supervisor.start(1)

        assert store.get_worker("br-1")["heartbeat_at"] == clock.wall()

    def test_heartbeat_uses_supervisor_clock_not_real_time(self, store, registry, clock, settings):
        """Иначе тесты heartbeat'а зависели бы от системных часов."""
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        clock.advance(sup.HEARTBEAT_INTERVAL_SECONDS)

        supervisor.tick()

        assert store.get_worker("br-1")["heartbeat_at"] == clock.wall() == 1_700_000_005.0


    def test_no_heartbeat_before_interval_elapses(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        first = store.get_worker("br-1")["heartbeat_at"]

        clock.advance(sup.HEARTBEAT_INTERVAL_SECONDS - 1)
        supervisor.tick()

        assert store.get_worker("br-1")["heartbeat_at"] == first, "писать heartbeat раньше срока нельзя"

    def test_heartbeat_written_after_interval(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(1)
        first = store.get_worker("br-1")["heartbeat_at"]

        clock.advance(sup.HEARTBEAT_INTERVAL_SECONDS)
        supervisor.tick()

        assert store.get_worker("br-1")["heartbeat_at"] == clock.wall() > first

    def test_heartbeat_refreshed_for_every_worker(self, store, registry, clock, settings):
        supervisor = make_supervisor(store, registry, clock, settings)
        supervisor.start(3)
        clock.advance(sup.HEARTBEAT_INTERVAL_SECONDS)

        supervisor.tick()

        assert {w["heartbeat_at"] for w in store.list_workers()} == {clock.wall()}

    def test_default_heartbeat_interval_is_five_seconds(self):
        assert sup.HEARTBEAT_INTERVAL_SECONDS == 5.0


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
