"""Окно запуска и распределение работы между воркерами.

Это то, что делал ``run_in_loop.py`` как отдельная точка входа, перенесённое в
чистую функцию: окно проверяется по переданному ``now``, а не по ``datetime.now()``,
поэтому тесты не зависят от времени суток, в котором их запускают.

Два свойства, которых в legacy не было и которые здесь закреплены:

* ночное окно ``23:00-06:00`` — рабочий режим, а не ошибка;
* требование «минимум 10 минут» проверяется по фактической длительности окна,
  а не только внутри одного часа.
"""

from __future__ import annotations

from datetime import time as clock_time

import pytest

from engine.scheduler import (
    IntervalError,
    inside_running_interval,
    next_round_item,
    next_worker_item,
    stagger_seconds,
    worker_index_from_browser_id,
)

# Момент, на котором проверяется окно: 12:30:00.
NOW = clock_time(12, 30)


# --- окно запуска ---------------------------------------------------------


def test_midnight_interval_means_always_on():
    assert inside_running_interval(NOW, "00:00", "00:00") is True


def test_unset_interval_means_always_on():
    """Конфиг по умолчанию пуст: окно не задано, а не «никогда»."""
    assert inside_running_interval(NOW, "", "") is True


def test_current_time_inside_interval_is_allowed():
    assert inside_running_interval(NOW, "09:00", "17:00") is True


def test_current_time_before_interval_is_not_allowed():
    assert inside_running_interval(NOW, "18:00", "23:00") is False


def test_current_time_after_interval_is_not_allowed():
    assert inside_running_interval(NOW, "01:00", "06:00") is False


def test_interval_start_is_inclusive():
    assert inside_running_interval(clock_time(12, 30), "12:30", "17:00") is True


def test_interval_end_is_inclusive():
    assert inside_running_interval(clock_time(12, 30), "09:00", "12:30") is True


def test_overnight_interval_is_supported():
    """Окно через полночь — норма, legacy отвергал его как перевёрнутое."""
    assert inside_running_interval(clock_time(23, 30), "23:00", "06:00") is True
    assert inside_running_interval(clock_time(5, 30), "23:00", "06:00") is True
    assert inside_running_interval(NOW, "23:00", "06:00") is False


def test_only_one_side_of_interval_set_is_rejected():
    with pytest.raises(IntervalError) as excinfo:
        inside_running_interval(NOW, "09:00", "")
    assert "running_interval_end" in str(excinfo.value)

    with pytest.raises(IntervalError) as excinfo:
        inside_running_interval(NOW, "", "17:00")
    assert "running_interval_start" in str(excinfo.value)


def test_malformed_interval_is_rejected():
    with pytest.raises(IntervalError) as excinfo:
        inside_running_interval(NOW, "9:00", "17:00")
    assert "ЧЧ:ММ" in str(excinfo.value)


def test_equal_non_midnight_interval_is_rejected_as_too_short():
    with pytest.raises(IntervalError) as excinfo:
        inside_running_interval(NOW, "12:00", "12:00")
    assert "минут" in str(excinfo.value)


def test_interval_shorter_than_ten_minutes_is_rejected():
    with pytest.raises(IntervalError):
        inside_running_interval(NOW, "12:25", "12:30")


def test_short_interval_across_hour_boundary_is_rejected():
    """Legacy проверял длительность только внутри одного часа — здесь всегда."""
    with pytest.raises(IntervalError):
        inside_running_interval(NOW, "12:59", "13:04")


def test_interval_of_ten_minutes_is_accepted():
    assert inside_running_interval(clock_time(12, 25), "12:20", "12:30") is True
    assert inside_running_interval(clock_time(12, 31), "12:20", "12:30") is False


def test_interval_of_fifteen_minutes_is_accepted_but_does_not_cover_now():
    assert inside_running_interval(NOW, "12:00", "12:15") is False


# --- распределение работы -------------------------------------------------


def test_worker_items_are_strided_over_the_pool():
    queries = [f"q{i}" for i in range(1, 7)]

    round_zero = [next_worker_item(queries, index, 3, 0) for index in (1, 2, 3)]
    round_one = [next_worker_item(queries, index, 3, 1) for index in (1, 2, 3)]

    assert round_zero == ["q1", "q2", "q3"]
    assert round_one == ["q4", "q5", "q6"]


def test_worker_items_wrap_around_the_list():
    queries = ["a", "b"]

    assert next_worker_item(queries, 3, 3, 0) == "a"
    assert next_worker_item(queries, 3, 3, 1) == "b"


def test_worker_items_are_distinct_within_a_round_when_pool_fits():
    queries = [f"q{i}" for i in range(1, 9)]

    picked = [next_worker_item(queries, index, 4, 1) for index in range(1, 5)]

    assert len(set(picked)) == 4
    assert picked == ["q5", "q6", "q7", "q8"]


def test_more_workers_than_items_wraps_without_error():
    queries = ["a", "b"]

    picked = [next_worker_item(queries, index, 3, 0) for index in (1, 2, 3)]

    assert picked == ["a", "b", "a"]


def test_worker_items_reject_empty_source():
    with pytest.raises(ValueError):
        next_worker_item([], 1, 1, 0)


def test_worker_items_reject_invalid_pool_size():
    with pytest.raises(ValueError):
        next_worker_item(["a"], 1, 0, 0)


def test_worker_items_reject_invalid_worker_index():
    with pytest.raises(ValueError):
        next_worker_item(["a"], 0, 1, 0)


def test_round_item_is_the_same_for_every_worker():
    """multiprocess_style == 2: один запрос на всех, очередь движется по раундам."""
    queries = ["a", "b", "c"]

    assert [next_round_item(queries, round_index) for round_index in range(4)] == [
        "a",
        "b",
        "c",
        "a",
    ]


def test_round_item_rejects_empty_source():
    with pytest.raises(ValueError):
        next_round_item([], 0)


# --- служебное ------------------------------------------------------------


@pytest.mark.parametrize(
    ("browser_id", "expected"),
    [
        ("br-1", 1),
        ("br-3", 3),
        ("br-10", 10),
        ("worker-2", 2),
        ("br-", 1),
        ("", 1),
        ("no-digits-here", 1),
    ],
)
def test_worker_index_is_parsed_from_browser_id(browser_id, expected):
    assert worker_index_from_browser_id(browser_id) == expected


def test_stagger_grows_with_worker_index():
    """Первый воркер не ждёт, остальные расходятся, чтобы не рвать один chromedriver."""
    assert stagger_seconds(1, wait_factor=1.0) == 0.0
    assert stagger_seconds(2, wait_factor=1.0) == 0.5
    assert stagger_seconds(3, wait_factor=0.2) == pytest.approx(0.2)


def test_stagger_is_zero_for_unknown_index():
    assert stagger_seconds(0, wait_factor=1.0) == 0.0
