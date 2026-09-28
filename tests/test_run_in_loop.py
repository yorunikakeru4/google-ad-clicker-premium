"""Тесты run_in_loop._inside_running_interval: проверка окна запуска.

Время "замораживается" подстановкой datetime в модуль run_in_loop, поэтому
проверки не зависят от времени суток, в котором запускается сьют.
"""

from datetime import datetime

import pytest

import run_in_loop


class FrozenDatetime(datetime):
    """datetime с фиксированным now(): 28.09.2026, 12:30:00."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 28, 12, 30, 0)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Подменить datetime в run_in_loop на фиксированное время."""

    monkeypatch.setattr(run_in_loop, "datetime", FrozenDatetime)


def set_interval(monkeypatch, behavior, start, end):
    monkeypatch.setattr(behavior, "running_interval_start", start)
    monkeypatch.setattr(behavior, "running_interval_end", end)


def test_midnight_interval_always_allows_run(monkeypatch, behavior):
    set_interval(monkeypatch, behavior, "00:00", "00:00")

    assert run_in_loop._inside_running_interval() is True


def test_current_time_inside_interval_is_allowed(monkeypatch, behavior):
    set_interval(monkeypatch, behavior, "09:00", "17:00")

    assert run_in_loop._inside_running_interval() is True


def test_current_time_before_interval_is_not_allowed(monkeypatch, behavior):
    set_interval(monkeypatch, behavior, "18:00", "23:00")

    assert run_in_loop._inside_running_interval() is False


def test_current_time_after_interval_is_not_allowed(monkeypatch, behavior):
    set_interval(monkeypatch, behavior, "01:00", "06:00")

    assert run_in_loop._inside_running_interval() is False


def test_interval_start_is_inclusive(monkeypatch, behavior):
    set_interval(monkeypatch, behavior, "12:30", "17:00")

    assert run_in_loop._inside_running_interval() is True


def test_interval_end_is_inclusive(monkeypatch, behavior):
    set_interval(monkeypatch, behavior, "09:00", "12:30")

    assert run_in_loop._inside_running_interval() is True


def test_reversed_interval_terminates_run(monkeypatch, behavior):
    set_interval(monkeypatch, behavior, "23:00", "06:00")

    with pytest.raises(SystemExit) as excinfo:
        run_in_loop._inside_running_interval()

    assert "Start time must be before the end time!" in str(excinfo.value)


def test_equal_start_and_end_terminate_run_as_too_short(monkeypatch, behavior):
    set_interval(monkeypatch, behavior, "12:00", "12:00")

    with pytest.raises(SystemExit) as excinfo:
        run_in_loop._inside_running_interval()

    assert "There should be at least 10 minutes between the start and end!" in str(excinfo.value)


def test_interval_shorter_than_ten_minutes_in_same_hour_terminates_run(monkeypatch, behavior):
    set_interval(monkeypatch, behavior, "12:25", "12:30")

    with pytest.raises(SystemExit) as excinfo:
        run_in_loop._inside_running_interval()

    assert "There should be at least 10 minutes between the start and end!" in str(excinfo.value)


def test_interval_of_fifteen_minutes_in_same_hour_is_evaluated(monkeypatch, behavior):
    # Ровно 15 минут проверка уже не отклоняет, окно просто не покрывает 12:30.
    set_interval(monkeypatch, behavior, "12:00", "12:15")

    assert run_in_loop._inside_running_interval() is False


def test_short_interval_in_different_hours_is_not_rejected(monkeypatch, behavior):
    # Проверка "минимум 10 минут" срабатывает только внутри одного часа,
    # поэтому 5-минутное окно между разными часами проходит проверку.
    set_interval(monkeypatch, behavior, "12:59", "13:04")

    assert run_in_loop._inside_running_interval() is False


@pytest.mark.xfail(
    reason=(
        "Баг legacy-кода: ночной интервал вида 23:00-06:00 (типичный режим "
        "работы ночью) отвергается с 'Start time must be before the end time!', "
        "хотя пользователь явно задал ночное окно"
    ),
    strict=True,
)
def test_overnight_interval_should_be_supported(monkeypatch, behavior):
    set_interval(monkeypatch, behavior, "23:00", "06:00")

    assert run_in_loop._inside_running_interval() is False
