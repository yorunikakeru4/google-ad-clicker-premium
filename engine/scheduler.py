"""Окно запуска и распределение работы между воркерами.

Модуль заменяет ``run_in_loop.py``: то, что там было логикой отдельного скрипта
с ``while True`` и ``subprocess.run``, стало чистыми функциями, которые вызывает
``engine.worker`` внутри своего цикла. Отдельная точка входа для «запускать
проходами» больше не нужна — воркер сам крутит проходы, а супервизор следит
только за тем, жив ли процесс.

Три решения, которые стоит знать:

1. **Ночное окно — рабочее, а не ошибка.** ``23:00-06:00`` легально: legacy
   отвергал его с «Start time must be before the end time!», из-за чего
   типичный ночной режим запуска был недостижим.
2. **Длительность проверяется по факту, а не по часам.** Правило «минимум
   10 минут» в legacy действовало только когда начало и конец попали в один
   час, поэтому ``12:59-13:04`` проходило, а ``12:25-12:30`` — нет. Здесь
   считается реальная длина окна в любом положении.
3. **Очередь запросов — арифметика, а не shuffle.** Legacy перемешивал список
   в каждом независимом процессе, из-за чего два браузера могли взять один и
   тот же запрос. Стрид по ``worker_index`` гарантирует разные запросы в рамках
   раунда и детерминирован для тестов.
"""

from __future__ import annotations

from datetime import time as clock_time
from typing import Sequence, TypeVar

# Окно считается «всегда включено», когда начало и конец равны полуночи:
# это значение стоит в config.json по умолчанию и в legacy означало то же.
_ALWAYS = ("00:00", "00:00")

# Минимальная длина окна. Меньше — опечатка в конфиге, а не режим работы.
MIN_INTERVAL_SECONDS = 10 * 60

# Сколько секунд ждать между проверками, когда воркер не может работать.
# Взято из legacy ``run_in_loop``: он спал минуту, и менять это без нужды —
# значит переписывать поведение, которое уже отлажено на стенде.
OUTSIDE_INTERVAL_WAIT_SECONDS = 60.0

# Шаг расхода воркеров перед первым сценарием. Legacy разводил запуск на
# 0.5 с на воркер (``start_timeout * wait_factor``), чтобы N процессов не
# боролись за один и тот же chromedriver при патчинге.
START_STAGGER_SECONDS = 0.5

T = TypeVar("T")

__all__ = [
    "MIN_INTERVAL_SECONDS",
    "OUTSIDE_INTERVAL_WAIT_SECONDS",
    "START_STAGGER_SECONDS",
    "IntervalError",
    "inside_running_interval",
    "next_round_item",
    "next_worker_item",
    "stagger_seconds",
    "worker_index_from_browser_id",
]


class IntervalError(ValueError):
    """Окно запуска из конфига непригодно для работы.

    Отдельный тип, а не ``ValueError``: вызывающий код обязан отличать
    «пользователь задал мусор» от «что-то неожиданное сломалось», чтобы
    первое логировать как ошибку конфигурации и не тревожить поддержку
    трейсбеком.
    """


def _parse_clock(value: str, field: str) -> clock_time:
    """ЧЧ:ММ -> ``datetime.time``. Пустое значение сюда не доходит."""
    parts = value.split(":")
    if len(parts) != 2:
        raise IntervalError(f"{field}: ожидался формат ЧЧ:ММ, получено {value!r}")
    hour_text, minute_text = parts
    if not (hour_text.isdigit() and minute_text.isdigit()):
        raise IntervalError(f"{field}: ожидался формат ЧЧ:ММ, получено {value!r}")
    hour, minute = int(hour_text), int(minute_text)
    # Ограничение по разрядам, а не по строке: "9:00" legacy тоже не брал,
    # и молча принять его здесь значило бы разойтись с валидацией конфига.
    if len(hour_text) != 2 or len(minute_text) != 2:
        raise IntervalError(f"{field}: ожидался формат ЧЧ:ММ, получено {value!r}")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise IntervalError(f"{field}: ожидался формат ЧЧ:ММ в пределах 00:00-23:59")
    return clock_time(hour, minute)


def _seconds_of_day(moment: clock_time) -> int:
    return moment.hour * 3600 + moment.minute * 60 + moment.second


def inside_running_interval(now: clock_time, start: str, end: str) -> bool:
    """Попадает ли ``now`` в окно ``start``-``end``.

    Пустое окно (хотя бы одна сторона не задана, обе не заданы) означает
    «ограничения нет»: конфиг по умолчанию пуст, и трактовать это как
    «никогда не запускаться» значило бы молча убить свежую установку.

    ``IntervalError`` — окно задано, но непригодно: возвращать здесь ``False``
    значило бы прятать ошибку конфига за штатным ожиданием, и пользователь
    никогда бы не узнал, что его ``12:59-13:04`` не работает.
    """
    if not start and not end:
        return True
    if bool(start) != bool(end):
        missing = "running_interval_end" if not end else "running_interval_start"
        given = "running_interval_start" if not end else "running_interval_end"
        raise IntervalError(
            f"{missing}: окно задано частично — заполните и {given}, либо очистите оба"
        )

    if (start, end) == _ALWAYS:
        return True

    begin = _parse_clock(start, "running_interval_start")
    finish = _parse_clock(end, "running_interval_end")

    begin_seconds = _seconds_of_day(begin)
    finish_seconds = _seconds_of_day(finish)
    duration = (finish_seconds - begin_seconds) % (24 * 3600)

    if duration < MIN_INTERVAL_SECONDS:
        minutes = duration // 60
        raise IntervalError(
            "окно запуска короче 10 минут: "
            f"{start}-{end} длится {minutes} мин, нужно не меньше 10"
        )

    current = _seconds_of_day(now)
    if begin_seconds <= finish_seconds:
        return begin_seconds <= current <= finish_seconds
    # Окно через полночь: работает либо до конца суток, либо с их начала.
    return current >= begin_seconds or current <= finish_seconds


def next_worker_item(items: Sequence[T], worker_index: int, pool_size: int, round_index: int) -> T:
    """Элемент очереди для воркера ``worker_index`` в раунде ``round_index``.

    Стрид ``pool_size`` вместо ``random``: в рамках одного раунда воркеры
    получают разные элементы, когда список не короче пула, а при нехватке
    элементов честно заворачиваются, а не падают с ``StopIteration`` — legacy
    в этом случае ронял ``ProcessPoolExecutor`` посреди прохода.
    """
    if not items:
        raise ValueError("очередь пуста: нечего распределять по воркерам")
    if pool_size < 1:
        raise ValueError(f"pool_size должен быть не меньше 1, получено {pool_size}")
    if worker_index < 1:
        raise ValueError(f"worker_index должен быть не меньше 1, получено {worker_index}")
    offset = (worker_index - 1) + round_index * pool_size
    return items[offset % len(items)]


def next_round_item(items: Sequence[T], round_index: int) -> T:
    """Элемент, одинаковый для всех воркеров (``multiprocess_style == 2``)."""
    if not items:
        raise ValueError("очередь пуста: нечего распределять по раундам")
    return items[round_index % len(items)]


def worker_index_from_browser_id(browser_id: str) -> int:
    """Порядковый номер воркера, извлечённый из ``browser_id``.

    ``br-3`` -> 3. Суффикс без цифр -> 1: нумерация с единицы совпадает с тем,
    что супервизор раскладывает по ``br-1..br-N``, и одиночный воркер получает
    первый элемент очереди, а не нулевой.
    """
    digits = ""
    for char in reversed(browser_id):
        if not char.isdigit():
            break
        digits = char + digits
    return int(digits) if digits else 1


def stagger_seconds(worker_index: int, wait_factor: float) -> float:
    """Задержка перед первым сценарием воркера.

    Нулевая для первого воркера: ждать того, кто и так единственный, — только
    добавлять латентности старта без всякой пользы.
    """
    if worker_index < 1:
        return 0.0
    return (worker_index - 1) * START_STAGGER_SECONDS * wait_factor
