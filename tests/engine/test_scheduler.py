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
#
# Распределение больше не детерминировано: порядок раунда считается из пары
# «соль запуска + номер раунда». Поэтому проверки свойств, которые обязаны
# выполняться при ЛЮБОЙ соли, идут со значением по умолчанию, а проверки
# «случайности» перебирают явные соли: результат в этом случае вычислен
# заранее и не меняется от машины к машине — тест не может стать флаки.


def test_worker_items_are_distinct_within_a_round():
    """Свойство stride сохранено: в одном раунде воркеры не пересекаются."""
    queries = [f"q{i}" for i in range(1, 9)]

    picked = [next_worker_item(queries, index, 4, 1) for index in range(1, 5)]

    assert len(set(picked)) == 4
    assert set(picked) <= set(queries)


def test_worker_item_is_stable_for_the_same_arguments():
    """Выбор — чистая функция: так разные процессы сходятся без общения."""
    queries = [f"q{i}" for i in range(1, 7)]

    assert next_worker_item(queries, 2, 3, 5) == next_worker_item(queries, 2, 3, 5)


def test_new_starts_do_not_repeat_the_same_first_round():
    """Дефект старта: стрид всегда отдавал ``items[0..N-1]`` нового запуска.

    Соли перечислены явно, а не берутся из ``_RUN_SEED``: ppid тестового
    процесса на каждой машине свой, и свойство обязано быть доказано для
    набора солей, а не для одной случайной.
    """
    queries = [f"q{i}" for i in range(1, 7)]
    starts = [
        tuple(next_worker_item(queries, index, 3, 0, seed=seed) for index in (1, 2, 3))
        for seed in range(60)
    ]

    assert any(start != tuple(queries[:3]) for start in starts), (
        "хотя бы один запуск обязан начинать не с одних и тех же первых запросов"
    )
    assert len({start[0] for start in starts}) > 1, (
        "первый воркер не должен получать один и тот же запрос на всех запусках"
    )
    assert all(len(set(start)) == 3 for start in starts), (
        "даже при другом порядке воркеры одного раунда не пересекаются"
    )


def test_more_workers_than_items_wraps_without_error():
    queries = ["a", "b"]

    picked = [next_worker_item(queries, index, 3, 0) for index in (1, 2, 3)]

    assert sorted(set(picked)) == ["a", "b"], "элементы списка должны исчерпаться"
    assert picked[0] == picked[2], "лишний воркер получает честный повтор"


def test_worker_items_reject_empty_source():
    with pytest.raises(ValueError):
        next_worker_item([], 1, 1, 0)


def test_worker_items_reject_invalid_pool_size():
    with pytest.raises(ValueError):
        next_worker_item(["a"], 1, 0, 0)


def test_worker_items_reject_invalid_worker_index():
    with pytest.raises(ValueError):
        next_worker_item(["a"], 0, 1, 0)


def test_round_item_is_stable_within_a_round():
    """multiprocess_style == 2: один запрос на всех воркеров одного раунда."""
    queries = ["a", "b", "c"]

    assert next_round_item(queries, 2, seed=7) == next_round_item(queries, 2, seed=7)


def test_round_item_is_random_per_run_and_not_a_cycle():
    """Стиль 2 больше не ходит по кругу ``a, b, c, a...`` ни на старте, ни дальше."""
    queries = ["a", "b", "c"]

    starts = {next_round_item(queries, 0, seed=seed) for seed in range(40)}
    sequence = [next_round_item(queries, round_index, seed=7) for round_index in range(6)]

    assert len(starts) > 1, "новый запуск не должен начинать с одного и того же запроса"
    assert set(starts) == set(queries), "за 40 запусков должны встретиться все запросы"
    assert sequence != ["a", "b", "c", "a", "b", "c"]
    assert set(sequence) <= set(queries)


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
