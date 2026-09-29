"""Цикл воркера: окно, пауза, очередь запросов, устойчивость к сбоям.

Воркер — единственная оставшаяся точка входа на браузер (``python -m
engine.worker``). Он заменяет собой и ``run_in_loop.py`` (окно запуска и
пауза между проходами), и ``run_ad_clicker.py`` (распределение запросов и
прокси по браузерам): обе функции переехали сюда и в ``engine.scheduler``,
а сам legacy-код драйвера подключается через ``WorkSource``.

Поэтому весь набор ниже работает без Chrome, Selenium и сети: подставляется
фейковый источник работы, настоящая SQLite-база и остановка по ``stop_event``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import time as clock_time

import pytest

from engine.control_plane.state import StateStore
from engine.db import migrations
from engine.log import StructuredLogger
from engine.store import StoreWriter
from engine.worker import (
    EXIT_CONFIG_ERROR,
    EXIT_OK,
    ScenarioRequest,
    ScheduleSettings,
    SourceError,
    WorkerRunner,
    main,
)

# Округло и вне окон: тесты задают окно сами.
NOW = clock_time(12, 30)

# Быстрые паузы: в тестах нельзя ждать реальные 60 секунд.
FAST_POLL = 0.01


def default_settings(**overrides) -> ScheduleSettings:
    values = {
        "interval_start": "00:00",
        "interval_end": "00:00",
        "loop_wait_time": FAST_POLL,
        "wait_factor": 1.0,
        "browser_count": 2,
        "multiprocess_style": 1,
        "send_to_android": False,
    }
    values.update(overrides)
    return ScheduleSettings(**values)


class FakeSource:
    """Источник работы без legacy-импортов: записывает вызовы и чинится снаружи."""

    def __init__(self, settings: ScheduleSettings | None = None):
        self.settings = settings or default_settings()
        self.requests: list[ScenarioRequest] = []
        self.reload_calls = 0
        # BaseException, а не Exception: сценарий обязан переживать и
        # SystemExit, которым legacy сигналит об отсутствии файла запросов.
        self.reload_error: BaseException | None = None
        self.scenario_error: BaseException | None = None
        self.queries_value: list[str] = ["q1", "q2", "q3"]
        self.proxies_value: list[str] = ["p1", "p2", "p3"]
        self.devices_value: list[str] = []
        self.on_reload: Callable[[int], None] | None = None
        self.on_scenario: Callable[[ScenarioRequest], None] | None = None
        # Легальный исход сценария. False моделирует legacy run_scenario,
        # который глотает исключение и просто возвращает управление.
        self.scenario_ok: bool = True

    def reload_settings(self) -> ScheduleSettings:
        self.reload_calls += 1
        # on_reload вызывается ДО проверки ошибки: лечебные хуки в тестах
        # снимают reload_error, иначе источник падал бы вечно.
        if self.on_reload is not None:
            self.on_reload(self.reload_calls)
        if self.reload_error is not None:
            raise self.reload_error
        return self.settings

    def queries(self) -> list[str]:
        return list(self.queries_value)

    def proxies(self) -> list[str]:
        return list(self.proxies_value)

    def devices(self) -> list[str]:
        return list(self.devices_value)

    def run_scenario(self, request: ScenarioRequest) -> bool:
        self.requests.append(request)
        if self.on_scenario is not None:
            self.on_scenario(request)
        if self.scenario_error is not None:
            raise self.scenario_error
        return self.scenario_ok


class ScriptedPauseStore(StateStore):
    """StateStore, у которого пауза ведёт себя по сценарию теста.

    Нужен из-за порядка в цикле: воркер проверяет паузу ДО чтения конфига,
    поэтому ``on_reload`` во время паузы не вызывается и остановить цикл
    хуком в ``reload_settings`` нельзя. Счётчик обращений к флагу даёт и
    «повисеть на паузе ровно N раз», и «проснуться на N-й проверке», без
    таймеров и без реального ожидания.
    """

    def __init__(
        self,
        db_path,
        *,
        hold_for: int,
        stop_event: threading.Event | None = None,
        stop_after_reads: int | None = None,
    ):
        super().__init__(db_path)
        self._hold_for = hold_for
        self._reads = 0
        self._stop_event = stop_event
        self._stop_after_reads = stop_after_reads

    def is_pause_requested(self) -> bool:
        self._reads += 1
        if (
            self._stop_event is not None
            and self._stop_after_reads is not None
            and self._reads >= self._stop_after_reads
        ):
            self._stop_event.set()
        return self._reads <= self._hold_for


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "worker.db"
    migrations.migrate(path)
    return path


@pytest.fixture
def store(db_path):
    return StateStore(db_path)


@pytest.fixture
def writer(db_path):
    handle = StoreWriter(db_path, flush_interval=10.0)
    yield handle
    handle.close()


@pytest.fixture
def logger(writer):
    return StructuredLogger(writer)


@pytest.fixture
def stop_event():
    import threading

    return threading.Event()


def read_logs(db_path) -> list[dict]:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT level, category, message, browser_id FROM logs ORDER BY ts, id"
        ).fetchall()
    return [dict(row) for row in rows]


def make_runner(source, store, logger, stop_event, **overrides) -> WorkerRunner:
    params = {
        "browser_id": "br-1",
        "store": store,
        "source": source,
        "logger": logger,
        "stop_event": stop_event,
        "now": lambda: NOW,
        "pause_poll_seconds": FAST_POLL,
        "outside_wait_seconds": FAST_POLL,
    }
    params.update(overrides)
    return WorkerRunner(**params)


# --- основной цикл --------------------------------------------------------


def test_worker_runs_scenario_and_returns_ok(store, logger, stop_event, db_path):
    source = FakeSource()
    source.on_scenario = lambda request: stop_event.set()

    code = make_runner(source, store, logger, stop_event).run()

    assert code == EXIT_OK
    assert len(source.requests) == 1
    assert source.requests[0].browser_id == "br-1"


def test_worker_does_not_run_scenario_after_stop_is_requested(
    store, logger, stop_event, db_path
):
    source = FakeSource()
    stop_event.set()

    code = make_runner(source, store, logger, stop_event).run()

    assert code == EXIT_OK
    assert source.requests == []


def test_worker_logs_start_and_stop(store, logger, stop_event, db_path, writer):
    source = FakeSource()
    source.on_scenario = lambda request: stop_event.set()

    make_runner(source, store, logger, stop_event).run()
    writer.flush()

    messages = [row["message"] for row in read_logs(db_path)]
    assert "worker started" in messages
    assert "worker stopped" in messages
    assert all(row["browser_id"] == "br-1" for row in read_logs(db_path))


# --- окно запуска ---------------------------------------------------------


def test_worker_idles_outside_running_interval(store, logger, stop_event, db_path, writer):
    source = FakeSource(default_settings(interval_start="18:00", interval_end="23:00"))
    source.on_reload = lambda call: stop_event.set() if call >= 3 else None

    code = make_runner(source, store, logger, stop_event).run()
    writer.flush()

    assert code == EXIT_OK
    assert source.requests == []
    assert any(
        row["level"] == "INFO" and row["category"] == "scheduler" and "running interval" in row["message"]
        for row in read_logs(db_path)
    ), read_logs(db_path)


def test_worker_starts_working_when_interval_opens(store, logger, stop_event, db_path):
    source = FakeSource(default_settings(interval_start="18:00", interval_end="23:00"))
    outside = True

    def switch(call):
        nonlocal outside
        if call >= 3:
            outside = False
            source.settings = default_settings()

    source.on_reload = switch
    source.on_scenario = lambda request: stop_event.set()

    make_runner(source, store, logger, stop_event).run()

    assert len(source.requests) == 1


def test_invalid_interval_keeps_worker_alive_and_logs_error(
    store, logger, stop_event, db_path, writer
):
    source = FakeSource(default_settings(interval_start="12:25", interval_end="12:30"))
    source.on_reload = lambda call: stop_event.set() if call >= 3 else None

    code = make_runner(source, store, logger, stop_event).run()
    writer.flush()

    assert code == EXIT_OK
    assert source.requests == []
    errors = [row for row in read_logs(db_path) if row["level"] == "ERROR"]
    assert errors, "невалидное окно обязано попасть в лог как ERROR"
    assert all(row["category"] == "scheduler" for row in errors)


# --- пауза ----------------------------------------------------------------


def test_worker_stays_idle_while_pause_is_requested(logger, stop_event, db_path):
    # Пауза держится две проверки, затем снимается самим стором: воркер
    # обязан провести их в ожидании и взять сценарий только после resume.
    store = ScriptedPauseStore(db_path, hold_for=2)
    source = FakeSource()
    source.on_scenario = lambda request: stop_event.set()

    make_runner(source, store, logger, stop_event).run()

    assert len(source.requests) == 1, "после resume воркер обязан взять сценарий"
    assert store._reads >= 3, "флаг паузы должен был перечитываться, пока воркер ждал"


def test_worker_never_starts_scenario_while_paused(logger, stop_event, db_path, writer):
    store = ScriptedPauseStore(db_path, hold_for=10**9, stop_event=stop_event, stop_after_reads=3)
    source = FakeSource()

    make_runner(source, store, logger, stop_event).run()
    writer.flush()

    assert source.requests == []
    # Конфиг на паузе не перечитывается: единственный вызов — стаггер до
    # цикла. При возврате порядка «конфиг раньше паузы» счётчик рос бы на
    # каждую проверку, и тест это обязан ловить.
    assert source.reload_calls == 1, (
        f"на паузе reload_settings вызывался {source.reload_calls} раз(а), ожидался 1 (стаггер)"
    )
    messages = [row for row in read_logs(db_path) if row["category"] == "scheduler"]
    assert any(row["level"] == "INFO" and "paused" in row["message"] for row in messages)
    assert not any(
        row["message"] == "scenario finished" for row in read_logs(db_path)
    ), "на паузе сценарий запускаться не должен"


# --- устойчивость ---------------------------------------------------------


def test_config_reload_failure_does_not_kill_the_worker(
    store, logger, stop_event, db_path, writer
):
    source = FakeSource()
    source.reload_error = SourceError("config.json не читается")

    def heal(call):
        if call >= 3:
            source.reload_error = None

    source.on_reload = heal
    source.on_scenario = lambda request: stop_event.set()

    code = make_runner(source, store, logger, stop_event).run()
    writer.flush()

    assert code == EXIT_OK
    assert len(source.requests) == 1, "после починки конфига воркер обязан продолжить"
    assert any(
        row["level"] == "ERROR" and "config reload failed" in row["message"]
        for row in read_logs(db_path)
    )


def test_scenario_failure_is_logged_and_the_loop_continues(
    store, logger, stop_event, db_path, writer
):
    source = FakeSource()
    source.scenario_error = RuntimeError("chrome упал")
    calls = {"n": 0}

    def heal(request):
        calls["n"] += 1
        if calls["n"] >= 2:
            source.scenario_error = None
            stop_event.set()

    source.on_scenario = heal

    code = make_runner(source, store, logger, stop_event).run()
    writer.flush()

    assert code == EXIT_OK
    assert len(source.requests) == 2, "упавший сценарий не должен останавливать цикл"
    assert any(
        row["level"] == "ERROR" and row["category"] == "browser" for row in read_logs(db_path)
    )


def test_failed_but_silent_scenario_is_recorded_as_failure(
    store, logger, stop_event, db_path, writer
):
    """legacy run_scenario глотает исключение — воркер обязан верить флагу."""
    source = FakeSource()
    source.scenario_ok = False
    source.on_scenario = lambda request: stop_event.set()

    make_runner(source, store, logger, stop_event).run()
    writer.flush()

    messages = [row for row in read_logs(db_path) if row["category"] == "browser"]
    assert any(row["level"] == "ERROR" and row["message"] == "scenario failed" for row in messages)
    assert not any(
        row["level"] == "INFO" and row["message"] == "scenario finished" for row in messages
    ), "упавший прогон не должен записываться как успех"


def test_successful_scenario_is_recorded_as_success(
    store, logger, stop_event, db_path, writer
):
    source = FakeSource()
    source.on_scenario = lambda request: stop_event.set()

    make_runner(source, store, logger, stop_event).run()
    writer.flush()

    messages = [row for row in read_logs(db_path) if row["category"] == "browser"]
    assert any(
        row["level"] == "INFO" and row["message"] == "scenario finished" for row in messages
    )


def test_legacy_system_exit_from_scenario_is_contained(store, logger, stop_event, db_path):
    """Legacy бросает SystemExit при отсутствии queries/proxy файла — это не падение процесса."""
    source = FakeSource()
    source.scenario_error = SystemExit()
    source.on_scenario = lambda request: stop_event.set() if len(source.requests) >= 1 else None

    code = make_runner(source, store, logger, stop_event).run()

    assert code == EXIT_OK


# --- распределение работы -------------------------------------------------


def test_query_and_proxy_are_strided_over_the_pool(store, logger, stop_event, db_path):
    source = FakeSource()
    source.queries_value = ["q1", "q2", "q3", "q4", "q5", "q6"]
    source.proxies_value = ["p1", "p2", "p3", "p4", "p5", "p6"]
    source.on_scenario = lambda request: stop_event.set()

    make_runner(source, store, logger, stop_event, browser_id="br-2", pool_size=3).run()

    request = source.requests[0]
    assert (request.worker_index, request.pool_size, request.round_index) == (2, 3, 0)
    assert request.query == "q2"
    assert request.proxy == "p2"


def test_pool_size_falls_back_to_config_browser_count(store, logger, stop_event, db_path):
    source = FakeSource(default_settings(browser_count=4))
    source.on_scenario = lambda request: stop_event.set()

    make_runner(source, store, logger, stop_event, browser_id="br-3").run()

    assert source.requests[0].pool_size == 4
    assert source.requests[0].query == "q3"


def test_multiprocess_style_two_gives_the_same_query_to_every_worker(
    store, logger, stop_event, db_path
):
    source = FakeSource(default_settings(multiprocess_style=2))
    source.queries_value = ["q1", "q2"]
    source.on_scenario = lambda request: stop_event.set()

    make_runner(
        source, store, logger, stop_event, browser_id="br-2", pool_size=3
    ).run()

    assert source.requests[0].query == "q1"


def test_missing_proxy_file_is_not_fatal(store, logger, stop_event, db_path):
    source = FakeSource()
    source.proxies_value = []
    source.on_scenario = lambda request: stop_event.set()

    make_runner(source, store, logger, stop_event).run()

    assert source.requests[0].proxy is None


def test_android_device_is_picked_by_worker_index(store, logger, stop_event, db_path):
    source = FakeSource(default_settings(send_to_android=True))
    source.devices_value = ["dev-a", "dev-b", "dev-c"]
    source.on_scenario = lambda request: stop_event.set()

    make_runner(source, store, logger, stop_event, browser_id="br-3").run()

    assert source.requests[0].device_id == "dev-c"


def test_no_android_device_available_yields_none(store, logger, stop_event, db_path):
    source = FakeSource(default_settings(send_to_android=True))
    source.on_scenario = lambda request: stop_event.set()

    make_runner(source, store, logger, stop_event).run()

    assert source.requests[0].device_id is None


def test_empty_query_file_is_reported_and_does_not_crash_the_worker(
    store, logger, stop_event, db_path, writer
):
    source = FakeSource()
    source.queries_value = []
    source.on_reload = lambda call: stop_event.set() if call >= 3 else None

    code = make_runner(source, store, logger, stop_event).run()
    writer.flush()

    assert code == EXIT_OK
    assert source.requests == []
    assert any(
        row["level"] == "ERROR" and row["category"] == "scheduler"
        for row in read_logs(db_path)
    ), read_logs(db_path)


# --- точка входа ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3", 3),
        ("1", 1),
        ("0", None),
        ("-2", None),
        ("abc", None),
        ("", None),
    ],
)
def test_pool_size_is_parsed_from_environment(raw, expected):
    from engine.worker import pool_size_from_environ

    assert pool_size_from_environ({"ADCLICKER_POOL_SIZE": raw}) == expected


def test_pool_size_is_absent_when_supervisor_did_not_set_it():
    from engine.worker import pool_size_from_environ

    assert pool_size_from_environ({}) is None


def test_main_requires_browser_id(capsys, monkeypatch):
    monkeypatch.delenv("ADCLICKER_BROWSER_ID", raising=False)

    assert main([]) == EXIT_CONFIG_ERROR
    assert "browser-id" in capsys.readouterr().err


def test_main_runs_the_loop_with_the_given_source(tmp_path):
    import threading

    stop = threading.Event()
    source = FakeSource()
    source.on_scenario = lambda request: stop.set()

    code = main(
        ["--browser-id", "br-7", "--db", str(tmp_path / "main.db")],
        source_factory=lambda: source,
        stop_event=stop,
    )

    assert code == EXIT_OK
    assert len(source.requests) == 1
    assert source.requests[0].browser_id == "br-7"


def test_main_reads_browser_id_from_environment(tmp_path, monkeypatch):
    import threading

    monkeypatch.setenv("ADCLICKER_BROWSER_ID", "br-9")
    stop = threading.Event()
    source = FakeSource()
    source.on_scenario = lambda request: stop.set()

    code = main(
        ["--db", str(tmp_path / "env.db")],
        source_factory=lambda: source,
        stop_event=stop,
    )

    assert code == EXIT_OK
    assert source.requests[0].browser_id == "br-9"


def test_main_closes_the_writer_on_the_way_out(tmp_path):
    import threading

    stop = threading.Event()
    source = FakeSource()
    source.on_scenario = lambda request: stop.set()

    main(
        ["--browser-id", "br-1", "--db", str(tmp_path / "close.db")],
        source_factory=lambda: source,
        stop_event=stop,
    )

    # Writer обязан закрыться: иначе его flusher-поток живёт в процессе
    # дольше воркера, а незакрытое соединение держит WAL-файл.
    assert not any(
        thread.name == "store-flusher" and thread.is_alive()
        for thread in threading.enumerate()
    ), "после main() не должно оставаться живого store-flusher"


# --- контракт точки входа -------------------------------------------------


def test_main_restores_previous_signal_handlers(tmp_path):
    """Обработчики обязаны вернуться: иначе pytest и встроенный запуск
    остаются с чужим SIGTERM и трактуют его как остановку своей сессии."""
    import signal as signal_module

    watched = (signal_module.SIGTERM, signal_module.SIGINT)
    previous = {number: signal_module.getsignal(number) for number in watched}

    stop = threading.Event()
    source = FakeSource()
    source.on_scenario = lambda request: stop.set()

    main(
        ["--browser-id", "br-1", "--db", str(tmp_path / "signals.db")],
        source_factory=lambda: source,
        stop_event=stop,
    )

    for number, handler in previous.items():
        assert signal_module.getsignal(number) is handler, f"обработчик {number} не восстановлен"


def test_startup_failure_returns_config_error(monkeypatch, tmp_path, capsys):
    from engine.db import migrations

    def explode(_path):
        raise RuntimeError("схема новее кода")

    monkeypatch.setattr(migrations, "migrate", explode)

    code = main(
        ["--browser-id", "br-1", "--db", str(tmp_path / "broken.db")],
        source_factory=FakeSource,
    )

    assert code == EXIT_CONFIG_ERROR
    assert "не удалось открыть БД" in capsys.readouterr().err


def test_startup_failure_reason_is_written_to_the_db(monkeypatch, tmp_path):
    """stderr воркера супервизор гасит (DEVNULL) — без этой записи в UI
    осталось бы только «exit code 2» без причины."""
    from engine.db import migrations

    db_path = tmp_path / "reason.db"
    migrations.migrate(db_path)

    def explode(_path):
        raise RuntimeError("схема новее кода")

    monkeypatch.setattr(migrations, "migrate", explode)

    code = main(
        ["--browser-id", "br-4", "--db", str(db_path)],
        source_factory=FakeSource,
    )

    assert code == EXIT_CONFIG_ERROR
    rows = [
        row
        for row in read_logs(db_path)
        if row["message"] == "worker failed to start"
    ]
    assert rows, "причина неудачного старта должна попасть в logs"
    assert rows[0]["browser_id"] == "br-4"
