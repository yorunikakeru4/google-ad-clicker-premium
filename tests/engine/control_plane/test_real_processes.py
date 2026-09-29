"""Супервизор на настоящих лёгких процессах.

Фейки из test_supervisor.py проверяют решения супервизора, но ничего не
говорят о контракте, через который он разговаривает с ОС: настоящий SIGTERM,
настоящий SIGKILL, настоящие PID, настоящие коды возврата и то, что pid в
базе соответствует живому процессу, а не просто строке.

Роль воркера играет короткоживущий процесс на sys.executable: он пишет свой
PID в файл и засыпает, а по сигналу выходит с нужным кодом. Chrome и Selenium
не запускаются никогда — вся проверка обходится без браузера, потому что
супервизору всё равно, что внутри процесса.

Ожидание сделано на предикате с дедлайном, а не на фиксированном sleep:
фиксированная пауза либо медлит, либо делает прогон flaky на загруженной
машине, а предикат с дедлайном даёт и скорость, и стабильность.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import psutil
import pytest

from engine.control_plane import supervisor as sup
from engine.control_plane.state import StateStore, WorkerStatus
from engine.proxy_pool import ProxyPool

# Пишет "<browser_id>:<pid>" построчно и засыпает. Файл — это проверка того,
# что все трое стартовали, а не только то, что супервизор про них записал.
RECORDER_SLEEPER = """
import os, sys, time
with open(sys.argv[1], "a", encoding="utf-8") as handle:
    handle.write(f"{sys.argv[2]}:{os.getpid()}\\n")
time.sleep(300)
"""

# Тот же процесс, но SIGTERM игнорирует: проверяет ветку SIGKILL.
SIGTERM_IGNORING_SLEEPER = """
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(sys.argv[1], "a", encoding="utf-8") as handle:
    handle.write(f"{sys.argv[2]}:{os.getpid()}\\n")
time.sleep(300)
"""

# Отвечает SIGTERM и выходит с 0: так завершается воркер по требованию.
SIGTERM_EXITING_SLEEPER = """
import os, signal, sys, time
def _bye(signum, frame):
    raise SystemExit(0)
signal.signal(signal.SIGTERM, _bye)
with open(sys.argv[1], "a", encoding="utf-8") as handle:
    handle.write(f"{sys.argv[2]}:{os.getpid()}\\n")
time.sleep(300)
"""

# Падает сразу с кодом 1, ничего не записав: сценарий "воркер сломан".
IMMEDIATE_FAILURE = "raise SystemExit(1)\n"

# Стучит heartbeat'ом через настоящий StoreWriter — ровно то, что делает
# engine.worker фоновым потоком. DB и репозиторий приходят аргументами:
# заглушка живёт в отдельном процессе и не может импортировать тестовые
# константы. Пишется каждые 50 мс, чтобы тест не ждал боевой интервал.
HEARTBEATING_SLEEPER = """
import os, sys, time
with open(sys.argv[1], "a", encoding="utf-8") as handle:
    handle.write(f"{sys.argv[2]}:{os.getpid()}\\n")
sys.path.insert(0, sys.argv[4])
from engine.store import StoreWriter
writer = StoreWriter(sys.argv[3])
while True:
    writer.heartbeat(sys.argv[2])
    time.sleep(0.05)
"""

BROWSER_IDS = ["br-1", "br-2", "br-3"]


def _wait_until(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _tick_until(supervisor, predicate, timeout: float = 20.0) -> bool:
    """Тикает, пока предикат не станет истиной.

    Отдельная функция, а не ``_wait_until``: супервизор замечает падение только
    на тике, а в бою за него тикает фоновый поток. Здесь тикает тест — так видно
    и решение, и его цену, а ожидание без тиков проверяло бы лишь то, что
    процесс кем-то убит.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        supervisor.tick()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _alive(pid: int) -> bool:
    return pid is not None and psutil.pid_exists(pid)


def _recorder_command(pid_file, browser_id: str, source: str = RECORDER_SLEEPER) -> list[str]:
    return [sys.executable, "-c", source, str(pid_file), browser_id]


@pytest.fixture
def db_path(tmp_path):
    from engine.db import migrations

    path = tmp_path / "real.db"
    migrations.migrate(path)
    return path


@pytest.fixture
def store(db_path):
    return StateStore(db_path)


@pytest.fixture
def pid_file(tmp_path):
    return tmp_path / "started.txt"


@pytest.fixture
def cleanup_pids(store):
    """Гасит всё, что пережило тест.

    Осиротевший процесс здесь — не просто грязный прогон: он держал бы
    tmp_path и продолжал бы писать туда после того, как pytest уберёт каталог.
    """
    yield
    for worker in store.list_workers():
        pid = worker["pid"]
        if not _alive(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            psutil.Process(pid).wait(timeout=5)
        except (OSError, psutil.Error):
            pass


def make_real_supervisor(
    store,
    pid_file,
    command_for,
    settings: sup.SupervisorSettings | None = None,
) -> sup.Supervisor:
    """Супервизор с настоящей фабрикой процессов.

    spawn не подменяется: здесь проверяется именно spawn_subprocess и весь
    контракт с ОС за ним. Подменяется только команда — чтобы вместо
    ``python -m engine.worker`` (а его в фазе 1 ещё нет) запустился
    управляемый процесс-заглушка.
    """
    return sup.Supervisor(
        store=store,
        settings=settings or sup.SupervisorSettings(
            heartbeat_interval=0.05,
            shutdown_grace_seconds=1.0,
            restart_backoff_base=0.05,
            restart_backoff_max=0.2,
            max_restarts=2,
            restart_count_reset_after=3600.0,
        ),
        browser_ids=lambda n: BROWSER_IDS[:n],
        command_for=command_for,
    )


def started_records(pid_file) -> list[str]:
    if not pid_file.exists():
        return []
    return [line for line in pid_file.read_text(encoding="utf-8").splitlines() if line]


def db_pids(store) -> list[int]:
    return [w["pid"] for w in store.list_workers() if w["pid"] is not None]


class TestRealWorkersRunTogether:
    """Ключевое требование фазы: 2-3 воркера живы одновременно."""

    def test_three_real_processes_are_alive_at_the_same_moment(self, store, pid_file, cleanup_pids):
        supervisor = make_real_supervisor(
            store, pid_file, lambda bid: _recorder_command(pid_file, bid)
        )

        supervisor.start(3)

        assert _wait_until(lambda: len(started_records(pid_file)) == 3), (
            f"ожидались 3 запущенных процесса, в файле: {started_records(pid_file)}"
        )
        pids = db_pids(store)
        assert len(pids) == 3
        assert len(set(pids)) == 3, f"PID должны быть разными, получились {pids}"
        alive = [pid for pid in pids if _alive(pid)]
        assert len(alive) == 3, f"все трое должны быть живы разом, живы только {alive}"

        supervisor.stop()

    def test_each_real_worker_got_its_own_browser_id(self, store, pid_file, cleanup_pids):
        supervisor = make_real_supervisor(
            store, pid_file, lambda bid: _recorder_command(pid_file, bid)
        )

        supervisor.start(3)
        assert _wait_until(lambda: len(started_records(pid_file)) == 3)

        recorded = dict(record.split(":", 1) for record in started_records(pid_file))
        for browser_id in BROWSER_IDS:
            assert browser_id in recorded, f"{browser_id} не сообщил свой PID"
            assert int(recorded[browser_id]) in db_pids(store), (
                f"PID из файла для {browser_id} не совпал с тем, что в БД"
            )

        supervisor.stop()

    def test_worker_is_a_session_leader_so_daemon_signal_misses_its_children(
        self, store, pid_file, cleanup_pids
    ):
        """start_new_session обязателен, иначе Chrome пережил бы остановку демона.

        SIGTERM, посланный демону, доходит до всей группы процессов. Если бы
        воркер остался в группе демона, его дочерние браузеры либо получили бы
        этот сигнал, либо (при перезапуске демона) остались бы висеть сиротами.
        """
        supervisor = make_real_supervisor(
            store, pid_file, lambda bid: _recorder_command(pid_file, bid)
        )

        supervisor.start(3)
        assert _wait_until(lambda: len(started_records(pid_file)) == 3)
        pids = db_pids(store)
        own_group = [os.getpgid(pid) for pid in pids]

        supervisor.stop()

        assert own_group == pids, (
            f"каждый воркер должен возглавлять собственную группу процессов, "
            f"получено pgid={own_group} при pid={pids}"
        )
        assert os.getpgid(0) not in own_group, (
            "группа воркера не должна совпадать с группой демона"
        )

    def test_browser_id_is_exported_into_the_worker_environment(self, store, pid_file, cleanup_pids):
        """Воркер узнаёт, кто он, только из окружения — память демона ему недоступна."""
        supervisor = make_real_supervisor(
            store, pid_file, lambda bid: _recorder_command(pid_file, bid)
        )
        default_env = supervisor._default_env("br-2")

        supervisor.start(1)
        assert _wait_until(lambda: len(started_records(pid_file)) == 1)
        supervisor.stop()

        assert default_env["ADCLICKER_BROWSER_ID"] == "br-2"


class TestRealHeartbeat:
    """Heartbeat доходит до БД по настоящим PID.

    Пишет его воркер (``StoreWriter.heartbeat``), супервизор только наблюдает.
    Обе стороны контракта проверяются на настоящих процессах: рост
    ``heartbeat_at`` от самого процесса и отсутствие записей от супервизора.
    """

    def test_worker_writes_growing_heartbeat_for_every_real_worker(
        self, store, db_path, pid_file, cleanup_pids
    ):
        repo_root = str(Path(__file__).resolve().parents[3])
        supervisor = make_real_supervisor(
            store,
            pid_file,
            lambda bid: [
                sys.executable,
                "-c",
                HEARTBEATING_SLEEPER,
                str(pid_file),
                bid,
                str(db_path),
                repo_root,
            ],
        )

        supervisor.start(3)
        assert _wait_until(lambda: len(started_records(pid_file)) == 3)
        supervisor.tick()
        before = {w["browser_id"]: w["heartbeat_at"] for w in store.list_workers()}
        assert len(before) == 3
        assert all(value is not None for value in before.values())
        assert all(w["status"] == WorkerStatus.RUNNING.value for w in store.list_workers())

        assert _wait_until(
            lambda: all(
                worker["heartbeat_at"] > before[worker["browser_id"]]
                for worker in store.list_workers()
            )
        ), f"воркеры обязаны писать heartbeat в рабочем цикле: {store.list_workers()}"

        supervisor.tick()

        assert all(
            supervisor._workers[browser_id].last_heartbeat
            == store.get_worker(browser_id)["heartbeat_at"]
            for browser_id in before
        ), "наблюдение супервизора обязано увидеть запись воркера"

        supervisor.stop()

    def test_supervisor_does_not_write_heartbeat_for_a_silent_worker(
        self, store, pid_file, cleanup_pids
    ):
        """Воркер молчит — супервизор не пишет за него.

        Писал бы — обновлял бы отметку сам и обнулял возраст перед проверкой
        stale, и зависший процесс остался бы незамеченным.
        """
        supervisor = make_real_supervisor(
            store, pid_file, lambda bid: _recorder_command(pid_file, bid)
        )
        supervisor.start(1)
        assert _wait_until(lambda: len(started_records(pid_file)) == 1)
        first = store.get_worker("br-1")["heartbeat_at"]

        time.sleep(supervisor.settings.heartbeat_interval * 3)
        supervisor.tick()
        supervisor.tick()

        assert store.get_worker("br-1")["heartbeat_at"] == first, (
            "супервизор наблюдает за heartbeat, а не пишет его от имени воркера"
        )
        supervisor.stop()

    def test_stale_real_worker_is_marked_backoff_and_restarted(self, store, pid_file, cleanup_pids):
        """Настоящий зависший воркер обязан быть найден по heartbeat, а не по poll()."""
        supervisor = make_real_supervisor(
            store,
            pid_file,
            lambda bid: _recorder_command(pid_file, bid),
            settings=sup.SupervisorSettings(
                heartbeat_interval=0.05,
                shutdown_grace_seconds=1.0,
                restart_backoff_base=0.05,
                restart_backoff_max=0.2,
                max_restarts=5,
                stale_after_seconds=0.0,
                restart_count_reset_after=3600.0,
            ),
        )
        supervisor.start(1)
        assert _wait_until(lambda: len(started_records(pid_file)) == 1)
        first_pid = db_pids(store)[0]

        def respawned() -> bool:
            worker = store.get_worker("br-1")
            return worker["restart_count"] >= 1 and worker["pid"] not in (None, first_pid)

        assert _tick_until(supervisor, respawned), (
            f"зависший воркер должен быть перезапущен, состояние: {store.get_worker('br-1')}"
        )

        supervisor.stop()


class TestRealGracefulShutdown:
    """SIGTERM, затем SIGKILL — на настоящих процессах."""

    def test_stop_terminates_every_real_process_with_sigterm(self, store, pid_file, cleanup_pids):
        supervisor = make_real_supervisor(
            store, pid_file, lambda bid: _recorder_command(pid_file, bid)
        )
        supervisor.start(3)
        assert _wait_until(lambda: len(started_records(pid_file)) == 3)
        pids = db_pids(store)

        supervisor.stop()

        assert _wait_until(lambda: not any(_alive(pid) for pid in pids)), (
            f"после stop() процессы должны быть завершены, живы: "
            f"{[pid for pid in pids if _alive(pid)]}"
        )

    def test_responsive_worker_exits_on_sigterm_without_sigkill(
        self, store, pid_file, cleanup_pids
    ):
        """Воркер, который слушает SIGTERM, гасится им, а не SIGKILL'ом."""
        supervisor = make_real_supervisor(
            store, pid_file, lambda bid: _recorder_command(pid_file, bid, SIGTERM_EXITING_SLEEPER)
        )
        supervisor.start(2)
        assert _wait_until(lambda: len(started_records(pid_file)) == 2)
        pids = db_pids(store)

        supervisor.stop()

        assert _wait_until(lambda: not any(_alive(pid) for pid in pids))
        assert all(
            worker["status"] == WorkerStatus.STOPPED.value for worker in store.list_workers()
        )
        assert all(worker["pid"] is None for worker in store.list_workers()), (
            "после остановки PID не должен оставаться в БД: по нему судят, жив ли воркер"
        )

    def test_worker_ignoring_sigterm_is_killed_after_grace_period(
        self, store, pid_file, cleanup_pids
    ):
        """Кто не послушался SIGTERM, получает SIGKILL — иначе демон не встанет."""
        supervisor = make_real_supervisor(
            store,
            pid_file,
            lambda bid: _recorder_command(pid_file, bid, SIGTERM_IGNORING_SLEEPER),
            settings=sup.SupervisorSettings(
                heartbeat_interval=0.05,
                shutdown_grace_seconds=0.3,
                restart_backoff_base=0.05,
                restart_backoff_max=0.2,
                max_restarts=2,
                restart_count_reset_after=3600.0,
            ),
        )
        supervisor.start(2)
        assert _wait_until(lambda: len(started_records(pid_file)) == 2)
        pids = db_pids(store)
        assert len(pids) == 2

        supervisor.stop()

        assert _wait_until(lambda: not any(_alive(pid) for pid in pids)), (
            f"воркер, игнорирующий SIGTERM, должен быть убит через SIGKILL, "
            f"живы: {[pid for pid in pids if _alive(pid)]}"
        )

    def test_stop_is_safe_when_no_workers_were_started(self, store, pid_file, cleanup_pids):
        supervisor = make_real_supervisor(
            store, pid_file, lambda bid: _recorder_command(pid_file, bid)
        )

        supervisor.stop()

        assert store.list_workers() == []
        assert store.get_run_state() == "stopped"


class TestRealCrashHandling:
    """Падение настоящего процесса: рестарт и circuit breaker."""

    def test_real_worker_that_exits_is_restarted(self, store, pid_file, cleanup_pids):
        supervisor = make_real_supervisor(
            store, pid_file, lambda bid: _recorder_command(pid_file, bid)
        )
        supervisor.start(1)
        assert _wait_until(lambda: len(started_records(pid_file)) == 1)
        first_pid = db_pids(store)[0]

        os.kill(first_pid, signal.SIGKILL)

        assert _tick_until(supervisor, lambda: len(started_records(pid_file)) >= 2), (
            "супервизор обязан поднять упавшего настоящего воркера"
        )
        assert store.get_worker("br-1")["restart_count"] == 1

        supervisor.stop()

    def test_persistent_real_failure_opens_circuit_and_stops_spawning(
        self, store, pid_file, cleanup_pids
    ):
        """Потолок рестартов на настоящих процессах: дальше — circuit_open."""
        supervisor = make_real_supervisor(
            store,
            pid_file,
            lambda bid: [sys.executable, "-c", IMMEDIATE_FAILURE],
            settings=sup.SupervisorSettings(
                heartbeat_interval=0.01,
                shutdown_grace_seconds=0.2,
                restart_backoff_base=0.01,
                restart_backoff_max=0.05,
                max_restarts=3,
                restart_count_reset_after=3600.0,
            ),
        )
        supervisor.start(1)
        assert _wait_until(lambda: len(started_records(pid_file)) == 0)
        # max_restarts=3 плюс первоначальный запуск: демон не должен поднять
        # больше, чем успевает до потолка, сколько бы тиков ни прошло.
        _tick_until(
            supervisor,
            lambda: store.get_worker("br-1")["status"] == WorkerStatus.CIRCUIT_OPEN.value,
        )

        worker = store.get_worker("br-1")
        assert worker["status"] == WorkerStatus.CIRCUIT_OPEN.value
        assert worker["pid"] is None, "у открытой цепи не должно оставаться PID"
        assert worker["last_error"] is not None
        assert "restart limit reached" in worker["last_error"]
        assert worker["restart_count"] == 3

        supervisor.stop()
        assert not _alive(any([w["pid"] for w in store.list_workers() if w["pid"]]))


class TestRealSpawnFailures:
    """Ошибки запуска настоящего процесса."""

    def test_missing_executable_becomes_worker_spawn_error(self, store, pid_file, cleanup_pids):
        supervisor = make_real_supervisor(
            store, pid_file, lambda bid: ["/nonexistent/definitely-not-a-binary"]
        )

        with pytest.raises(sup.WorkerSpawnError) as excinfo:
            supervisor.start(1)

        assert "br-1" in str(excinfo.value)

    def test_failed_start_leaves_no_half_running_pool(self, store, pid_file, cleanup_pids):
        """Частично поднятый пул — худшее состояние, откатываем целиком."""
        calls = {"n": 0}

        def command_for(browser_id: str) -> list[str]:
            calls["n"] += 1
            if calls["n"] == 1:
                return _recorder_command(pid_file, browser_id)
            return ["/nonexistent/definitely-not-a-binary"]

        supervisor = make_real_supervisor(store, pid_file, command_for)

        with pytest.raises(sup.WorkerSpawnError):
            supervisor.start(3)

        assert supervisor.is_running() is False
        assert store.get_run_state() == "stopped"
        assert not any(_alive(pid) for pid in db_pids(store)), (
            "процесс, поднятый до сбоя, обязан быть погашен"
        )


# Воркер, который сразу сигнализирует деградацию (реальным StoreWriter, как
# on_proxy_dead/on_connection_lost) и засыпает. PID пишется до сигнала, чтобы
# тест дождался и строки в БД, и самого процесса.
DEGRADING_SLEEPER = """
import os, sys, time
with open(sys.argv[1], "a", encoding="utf-8") as handle:
    handle.write(f"{sys.argv[2]}:{os.getpid()}\\n")
sys.path.insert(0, sys.argv[4])
from engine.store import StoreWriter
writer = StoreWriter(sys.argv[3])
try:
    writer.mark_degraded(sys.argv[2], "proxy rejected credentials")
finally:
    writer.close()
time.sleep(300)
"""


class TestRealProxyRotation:
    """Ротация на настоящих процессах.

    Решение принимает супервизор, но проверяется оно по тому, что получает
    НОВЫЙ процесс: ``ADCLICKER_PROXY`` читается напрямую из окружения живого
    PID (``psutil``), а не из логов и не из записей супервизора — иначе тест
    подтвердил бы только то, что демон себе записал.
    """

    def test_degraded_real_worker_respawns_with_a_different_proxy_env(
        self, store, db_path, pid_file, cleanup_pids
    ):
        pool = ProxyPool(db_path)
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080", "bob:hunter2@10.0.0.2:9090"])
        first_id, second_id = [row["id"] for row in pool.list_proxies()]
        repo_root = str(Path(__file__).resolve().parents[3])
        supervisor = make_real_supervisor(
            store,
            pid_file,
            lambda bid: [
                sys.executable,
                "-c",
                DEGRADING_SLEEPER,
                str(pid_file),
                bid,
                str(db_path),
                repo_root,
            ],
            settings=sup.SupervisorSettings(
                heartbeat_interval=0.05,
                shutdown_grace_seconds=1.0,
                restart_backoff_base=0.05,
                restart_backoff_max=0.2,
                max_restarts=5,
                restart_count_reset_after=3600.0,
                # Воркер-заглушка не стучит heartbeat'ом: зависанием его
                # считать нельзя, иначе тест проверял бы устаревание.
                stale_after_seconds=3600.0,
            ),
        )

        supervisor.start(1)
        assert _wait_until(lambda: len(started_records(pid_file)) == 1)
        assert _wait_until(lambda: store.get_worker("br-1")["status"] == "degraded")

        first_pid = store.get_worker("br-1")["pid"]
        assert psutil.Process(first_pid).environ()["ADCLICKER_PROXY"] == (
            "alice:s3cr3t@10.0.0.1:8080"
        )
        with store._connect() as conn:
            conn.execute("INSERT INTO profiles (name) VALUES ('default')")
            conn.execute(
                "UPDATE workers SET profile_id = (SELECT id FROM profiles WHERE name = 'default') "
                "WHERE browser_id = 'br-1'"
            )
            conn.commit()

        supervisor.tick()

        assert _wait_until(lambda: len(started_records(pid_file)) >= 2), (
            "ротация обязана поднять новый процесс"
        )
        second_pid = int(started_records(pid_file)[-1].split(":", 1)[1])
        assert second_pid != first_pid
        assert not _alive(first_pid), "старый процесс должен быть погашен ротацией"
        assert psutil.Process(second_pid).environ()["ADCLICKER_PROXY"] == (
            "bob:hunter2@10.0.0.2:9090"
        )
        worker = store.get_worker("br-1")
        assert worker["proxy_id"] == second_id
        assert worker["profile_id"] is None, "профиль освобождается при ротации"

        with store._connect() as conn:
            rows = conn.execute(
                "SELECT proxy_id, browser_id, result FROM proxy_usage ORDER BY id"
            ).fetchall()
        assert [(row["proxy_id"], row["browser_id"], row["result"]) for row in rows] == [
            (first_id, "br-1", "assigned"),
            (first_id, "br-1", "exhausted"),
            (second_id, "br-1", "rotated"),
        ]

        supervisor.stop()

        worker = store.get_worker("br-1")
        assert worker["proxy_id"] is None, "после stop прокси должен быть свободен"
        assert worker["profile_id"] is None
