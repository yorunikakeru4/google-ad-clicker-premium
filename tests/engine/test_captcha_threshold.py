"""Тесты доли CAPTCHA и политики порога (план §5, фаза 8).

Формула — чистая функция над ``captcha_events``/``network_requests``: ровно ту
же долю считает ``captcha_share`` метрик дашборда
(``ui/src-tauri/src/metrics.rs``), поэтому дашборд и политика не могут иметь
двух разных значений. Знаменатель 0 — ``None``, а не 0% и не деление.

Политика — edge-триггер: действие выполняется один раз на переход «доля ниже
порога → порог превышен», а не на каждом тике с превышающей долёй. Простая
гистерезис-семантика: состояние сбрасывается только по доле, которая реально
опустилась ниже порога (и по ``None`` — отсутствию данных), поэтому следующее
превышение снова триггерит, а залипшего «превышения» не бывает.
"""

from __future__ import annotations

import json
import time

import pytest

from engine.captcha_threshold import (
    CAPTCHA_SHARE_WINDOW_SECONDS,
    CAPTCHA_THRESHOLD_ACTIONS,
    DEFAULT_CAPTCHA_THRESHOLD_ACTION,
    DEFAULT_CAPTCHA_THRESHOLD_PERCENT,
    CaptchaThresholdPolicy,
    captcha_share,
)
from engine.control_plane.state import StateStore
from engine.control_plane.supervisor import Supervisor, SupervisorSettings
from engine.db import migrations
from engine.proxy_pool import ProxyPool
from tests.engine.control_plane.test_supervisor import (
    FakeClock,
    FakeProcessRegistry,
    assign_profile,
    usage_rows,
)

# Фиксированное «сейчас»: тесты не зависят от машинного времени, а окно
# считается от него же (политика получает clock).
NOW = 1_700_000_000.0


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


@pytest.fixture
def store(db_path):
    return StateStore(db_path)


def seed(db_path, *, requests: int, captchas: int, ts: float = NOW) -> None:
    """Окно с заданным числом строк в обеих таблицах (предыдущие стираются)."""
    conn = migrations.connect(db_path)
    try:
        conn.execute("DELETE FROM network_requests")
        conn.execute("DELETE FROM captcha_events")
        for _ in range(requests):
            conn.execute(
                "INSERT INTO network_requests (ts, browser_id, method, url) "
                "VALUES (?, 'br-1', 'GET', 'https://example.test/')",
                (ts,),
            )
        for _ in range(captchas):
            conn.execute(
                "INSERT INTO captcha_events (ts, browser_id) VALUES (?, 'br-1')",
                (ts,),
            )
        conn.commit()
    finally:
        conn.close()


def captcha_logs(store) -> list[dict]:
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT level, category, message, fields FROM logs "
            "WHERE category = 'captcha' ORDER BY id"
        ).fetchall()
    logs = [dict(row) for row in rows]
    for row in logs:
        row["fields"] = json.loads(row["fields"]) if row["fields"] else {}
    return logs


class FakePolicySupervisor:
    """Заглушка супервизора: считает вызовы действий и умеет падать."""

    def __init__(self, error: Exception | None = None):
        self.pause_calls = 0
        self.rotate_reasons: list[str] = []
        self.error = error

    def pause(self) -> None:
        self.pause_calls += 1
        if self.error is not None:
            raise self.error

    def rotate_all(self, *, reason: str) -> int:
        self.rotate_reasons.append(reason)
        if self.error is not None:
            raise self.error
        return 1


def make_policy(db_path, supervisor=None, *, clock=lambda: NOW) -> CaptchaThresholdPolicy:
    return CaptchaThresholdPolicy(
        StateStore(db_path),
        supervisor if supervisor is not None else FakePolicySupervisor(),
        clock=clock,
    )


# --- формула доли -----------------------------------------------------------


class TestCaptchaShare:
    """Доля CAPTCHA за окно ``since..now``: границы включительно, 0 запросов — None."""

    def test_window_is_one_hour(self):
        """Окно — константа модуля, то же, что в дашборде ([now-3600, now])."""
        assert CAPTCHA_SHARE_WINDOW_SECONDS == 3600.0

    def test_empty_database_gives_none(self, db_path):
        """Знаменатель 0 — ``None``: ложные 0% выглядели бы как здоровье."""
        assert captcha_share(db_path, 0.0) is None

    def test_captcha_without_requests_is_none(self, db_path):
        """Капча без запросов — делить не на что, а не 100%."""
        seed(db_path, requests=0, captchas=3)

        assert captcha_share(db_path, 0.0) is None

    def test_requests_without_captchas_is_zero(self, db_path):
        seed(db_path, requests=8, captchas=0)

        assert captcha_share(db_path, 0.0) == 0.0

    def test_share_is_captchas_over_requests(self, db_path):
        seed(db_path, requests=8, captchas=2)

        assert captcha_share(db_path, 0.0) == pytest.approx(0.25)

    def test_window_start_boundary_is_inclusive(self, db_path):
        """Строка ровно на ``since`` попадает в окно обеих таблиц."""
        seed(db_path, requests=4, captchas=4, ts=NOW - CAPTCHA_SHARE_WINDOW_SECONDS)

        assert captcha_share(db_path, NOW - CAPTCHA_SHARE_WINDOW_SECONDS) == 1.0

    def test_rows_older_than_since_are_ignored(self, db_path):
        seed(db_path, requests=4, captchas=4, ts=NOW - CAPTCHA_SHARE_WINDOW_SECONDS - 1.0)

        assert captcha_share(db_path, NOW - CAPTCHA_SHARE_WINDOW_SECONDS) is None

    def test_since_cuts_both_tables_the_same_way(self, db_path):
        """Старая капча не смешивается с окном запросов — одно since на обе."""
        conn = migrations.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO captcha_events (ts) VALUES (?)",
                (NOW - CAPTCHA_SHARE_WINDOW_SECONDS - 1.0,),
            )
            conn.execute("INSERT INTO network_requests (ts) VALUES (?)", (NOW,))
            conn.commit()
        finally:
            conn.close()

        assert captcha_share(db_path, NOW - CAPTCHA_SHARE_WINDOW_SECONDS) == 0.0

    def test_future_rows_inside_the_window_are_counted(self, db_path):
        """Верхняя граница — не фильтр: окно задаётся одним since, как в Rust."""
        seed(db_path, requests=2, captchas=1, ts=NOW + 100.0)

        assert captcha_share(db_path, NOW) == pytest.approx(0.5)


# --- поля конфига: контракт модуля -----------------------------------------


class TestThresholdConstants:
    """Дефолты и enum живут здесь, а не копируются в config.py."""

    def test_default_threshold_is_five_percent(self):
        assert DEFAULT_CAPTCHA_THRESHOLD_PERCENT == 5.0

    def test_default_action_is_warn(self):
        assert DEFAULT_CAPTCHA_THRESHOLD_ACTION == "warn"

    def test_allowed_actions_are_exactly_the_policy_set(self):
        assert CAPTCHA_THRESHOLD_ACTIONS == frozenset({"warn", "pause", "rotate"})


# --- edge-trigger -----------------------------------------------------------


class TestEdgeTrigger:
    """Один переход — одно действие; возврат ниже порога разрешает следующее."""

    def test_share_below_threshold_does_nothing(self, db_path):
        seed(db_path, requests=100, captchas=1)  # 1% < 5%
        policy = make_policy(db_path)

        assert policy.check(5.0, "warn") is None
        assert policy.exceeded is False
        assert captcha_logs(StateStore(db_path)) == []

    def test_share_exactly_at_the_threshold_is_an_exceedance(self, db_path):
        """Граница включительно: ровно 5% при пороге 5 — уже превышение."""
        seed(db_path, requests=20, captchas=1)  # 5.0%
        policy = make_policy(db_path)

        assert policy.check(5.0, "warn") == "warn"
        assert policy.exceeded is True

    def test_share_above_the_threshold_triggers(self, db_path):
        seed(db_path, requests=20, captchas=2)  # 10%
        policy = make_policy(db_path)

        assert policy.check(5.0, "warn") == "warn"

    def test_series_of_exceeding_ticks_performs_one_action(self, db_path):
        """Edge, а не level: на серии тиков с превышением — одно действие."""
        seed(db_path, requests=20, captchas=2)
        policy = make_policy(db_path)

        results = [policy.check(5.0, "warn") for _ in range(5)]

        assert results == ["warn", None, None, None, None]
        logs = captcha_logs(StateStore(db_path))
        assert len(logs) == 1, "предупреждение не должно дублироваться на каждом тике"

    def test_returning_below_the_threshold_rearms_the_trigger(self, db_path):
        seed(db_path, requests=20, captchas=2)
        policy = make_policy(db_path)
        assert policy.check(5.0, "warn") == "warn"

        seed(db_path, requests=20, captchas=0)
        assert policy.check(5.0, "warn") is None
        assert policy.exceeded is False
        assert policy.check(5.0, "warn") is None, "серия ниже порога не триггерит"

        seed(db_path, requests=20, captchas=2)
        assert policy.check(5.0, "warn") == "warn", "новое превышение обязано сработать снова"
        assert len(captcha_logs(StateStore(db_path))) == 2

    def test_none_never_triggers(self, db_path):
        """Нет запросов — нет и превышения: ложного срабатывания не бывает."""
        policy = make_policy(db_path)

        for _ in range(3):
            assert policy.check(5.0, "rotate") is None
        assert policy.exceeded is False
        assert captcha_logs(StateStore(db_path)) == []

    def test_none_rearms_the_trigger_instead_of_sticking(self, db_path):
        """``None`` — отсутствие данных, а не залипшее «превышение»."""
        seed(db_path, requests=20, captchas=2)
        policy = make_policy(db_path)
        assert policy.check(5.0, "warn") == "warn"

        seed(db_path, requests=0, captchas=0)
        assert policy.check(5.0, "warn") is None
        assert policy.exceeded is False

        seed(db_path, requests=20, captchas=2)
        assert policy.check(5.0, "warn") == "warn"

    def test_exceedance_is_logged_once_with_share_threshold_and_action(self, db_path):
        seed(db_path, requests=20, captchas=2)
        policy = make_policy(db_path)

        policy.check(5.0, "pause")
        policy.check(5.0, "pause")

        logs = captcha_logs(StateStore(db_path))
        assert len(logs) == 1
        entry = logs[0]
        assert entry["level"] == "WARNING"
        assert entry["message"] == "captcha threshold exceeded"
        assert entry["fields"]["share"] == pytest.approx(0.1)
        assert entry["fields"]["threshold_percent"] == 5.0
        assert entry["fields"]["action"] == "pause"


# --- действия ---------------------------------------------------------------


class TestActions:
    """warn — только лог; pause — kv-флаг; rotate — подмена прокси живых воркеров."""

    def test_warn_does_not_touch_the_supervisor(self, db_path):
        seed(db_path, requests=20, captchas=2)
        supervisor = FakePolicySupervisor()
        policy = CaptchaThresholdPolicy(StateStore(db_path), supervisor, clock=lambda: NOW)

        assert policy.check(5.0, "warn") == "warn"

        assert supervisor.pause_calls == 0
        assert supervisor.rotate_reasons == []
        assert len(captcha_logs(StateStore(db_path))) == 1

    def test_pause_sets_the_kv_flag_like_the_button_in_the_ui(self, db_path):
        """Пауза идёт тем же механизмом, что кнопка Pause: kv PAUSE_REQUESTED."""
        registry = FakeProcessRegistry()
        supervisor = Supervisor(
            store=StateStore(db_path),
            settings=SupervisorSettings(
                heartbeat_interval=0.01,
                shutdown_grace_seconds=0.05,
                restart_backoff_base=0.01,
                restart_backoff_max=0.02,
            ),
            spawn=registry,
            clock=FakeClock(),
        )
        supervisor.start(1)
        seed(db_path, requests=20, captchas=2)
        policy = CaptchaThresholdPolicy(StateStore(db_path), supervisor, clock=lambda: NOW)

        try:
            assert policy.check(5.0, "pause") == "pause"

            assert supervisor.store.is_pause_requested() is True
            assert supervisor.store.get_run_state() == "paused"
            assert len(registry.created) == 1, "пауза не убивает процессы"
        finally:
            supervisor.stop()

    def test_rotate_replaces_the_process_with_a_reserve_proxy(self, db_path):
        registry = FakeProcessRegistry()
        store = StateStore(db_path)
        pool = ProxyPool(db_path)
        pool.add_lines(
            ["alice:s3cr3t@10.0.0.1:8080", "bob:hunter2@10.0.0.2:9090"]
        )
        ids = [row["id"] for row in pool.list_proxies()]
        supervisor = Supervisor(
            store=store,
            settings=SupervisorSettings(
                heartbeat_interval=0.01,
                shutdown_grace_seconds=0.05,
                restart_backoff_base=0.01,
                restart_backoff_max=0.02,
            ),
            spawn=registry,
            clock=FakeClock(),
            proxy_pool=pool,
        )
        supervisor.start(1)
        assert store.get_worker("br-1")["proxy_id"] == ids[0]
        profile_id = assign_profile(store, "br-1")
        seed(db_path, requests=20, captchas=2)
        policy = CaptchaThresholdPolicy(store, supervisor, clock=lambda: NOW)

        try:
            assert policy.check(5.0, "rotate") == "rotate"

            assert len(registry.created) == 2, "ротация обязана поднять новый процесс"
            assert registry.created[0].poll() is not None, "старый процесс должен быть погашен"
            assert store.get_worker("br-1")["proxy_id"] == ids[1]
            assert store.get_worker("br-1")["restart_count"] == 0
            assert store.get_worker("br-1")["profile_id"] == profile_id, (
                "ротация из политики обязана сохранять профиль воркера"
            )
            assert [(row["proxy_id"], row["result"]) for row in usage_rows(store)] == [
                (ids[0], "assigned"),
                (ids[1], "rotated"),
            ]
        finally:
            supervisor.stop()

    def test_rotate_passes_a_reason_to_the_supervisor(self, db_path):
        seed(db_path, requests=20, captchas=2)
        supervisor = FakePolicySupervisor()
        policy = CaptchaThresholdPolicy(StateStore(db_path), supervisor, clock=lambda: NOW)

        policy.check(5.0, "rotate")

        assert supervisor.rotate_reasons == ["captcha_threshold"]

    def test_failed_action_is_logged_instead_of_raising(self, db_path):
        """Пул остановлен: pause падает, но проверка не умирает и видит ошибку."""
        seed(db_path, requests=20, captchas=2)
        supervisor = FakePolicySupervisor(RuntimeError("нет воркеров"))
        policy = CaptchaThresholdPolicy(StateStore(db_path), supervisor, clock=lambda: NOW)

        assert policy.check(5.0, "pause") == "pause"

        logs = captcha_logs(StateStore(db_path))
        failures = [row for row in logs if row["message"] == "captcha threshold action failed"]
        assert len(failures) == 1
        assert failures[0]["level"] == "ERROR"
        assert failures[0]["fields"] == {"action": "pause", "error": "RuntimeError"}

    def test_unknown_action_is_logged_instead_of_raising(self, db_path):
        seed(db_path, requests=20, captchas=2)
        policy = make_policy(db_path)

        assert policy.check(5.0, "explode") == "explode"

        logs = captcha_logs(StateStore(db_path))
        failures = [row for row in logs if row["message"] == "captcha threshold action failed"]
        assert len(failures) == 1
        assert failures[0]["fields"]["action"] == "explode"
        assert failures[0]["fields"]["error"] == "ValueError"


# --- проводка: политика читает часы ----------------------------------------


class TestClock:
    def test_window_is_measured_from_the_injected_clock(self, db_path):
        """Окно — ``now - 3600`` по часам политики, а не по машинному времени."""
        seed(db_path, requests=4, captchas=4, ts=NOW - CAPTCHA_SHARE_WINDOW_SECONDS)
        policy = CaptchaThresholdPolicy(
            StateStore(db_path),
            FakePolicySupervisor(),
            clock=lambda: NOW,
        )

        assert policy.share() == 1.0

        older = CaptchaThresholdPolicy(
            StateStore(db_path),
            FakePolicySupervisor(),
            clock=lambda: NOW + CAPTCHA_SHARE_WINDOW_SECONDS,
        )
        assert older.share() is None, "строки должны выпадать из окна по мере старения"

    def test_wall_clock_is_the_default_source(self, db_path):
        seed(db_path, requests=4, captchas=4, ts=time.time())
        policy = CaptchaThresholdPolicy(StateStore(db_path), FakePolicySupervisor())

        assert policy.share() == 1.0
