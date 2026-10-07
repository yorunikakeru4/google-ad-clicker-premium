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
3. **Очередь запросов — случайная перестановка, но общая для раунда.** У
   старых решений были ровно свои изъяны: legacy перемешивал список в каждом
   независимом процессе, из-за чего два браузера брали один и тот же запрос,
   а заменивший его стрид по ``worker_index`` был детерминирован — на каждом
   старте воркеры получали одни и те же ``items[0..N-1]``, и программа
   запускалась с одинаковой связкой «запрос + прокси», которую капча легко
   узнавала. Здесь раунд берёт перестановку, посчитанную из пары «соль
   запуска + номер раунда»: воркеры одного раунда получают разные элементы,
   процессы сходятся без общения (выбор — чистая функция аргументов), а
   новый запуск начинает с другого порядка.
"""

from __future__ import annotations

import os
import random
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

# Соль запуска: ``ppid`` процесса, импортировавшего модуль.
#
# Воркеры — отдельные процессы и поговорить о «случайном порядке» между
# собой не могут, поэтому выбор обязан быть чистой функцией аргументов, а
# внешняя случайность приходит отсюда. ``ppid`` взят потому, что супервизор
# порождает всех воркеров одного пула сам (``Popen`` не меняет родителя даже
# с ``start_new_session``): внутри запуска соль у всех одинакова, а новый
# демон получает новый pid — и новый порядок. Замораживается при импорте,
# а не пересчитывается на каждый вызов: пересчёт разъехал бы воркеров,
# переживших смерть супервизора (их родителем стал бы init, ``ppid == 1``).
_RUN_SEED = os.getppid()

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


def _round_rng(seed: int, round_index: int) -> random.Random:
    """Генератор раунда: зерно складывается из соли запуска и номера раунда.

    Одна и та же пара даёт одну и ту же последовательность у любого
    участника — другого способа сойтись у процессов нет: воркеры не
    обмениваются ни состоянием, ни результатом выбора.
    """
    return random.Random(f"{seed}:{round_index}")


def next_worker_item(
    items: Sequence[T],
    worker_index: int,
    pool_size: int,
    round_index: int,
    *,
    seed: int | None = None,
) -> T:
    """Элемент очереди для воркера ``worker_index`` в раунде ``round_index``.

    Раунд берёт случайную перестановку списка, а воркер идёт по ней стридом
    ``round_index * pool_size + worker_index - 1`` — старая арифметика
    осталась, но считается уже по перемешанному массиву. Что это даёт:

    * в рамках одного раунда разные воркеры получают разные элементы, пока
      список длиннее пула (перестановка не повторяет индексы);
    * при нехватке элементов смещение заворачивается по длине списка —
      честные повторы вместо ``StopIteration``, которым legacy ронял
      ``ProcessPoolExecutor`` посреди прохода;
    * порядок меняется и от раунда к раунду, и от запуска к запуску: раньше
      ``round_index == 0`` на старте всегда отдавал воркерам ``items[0..N-1]``,
      и каждый перезапуск начинался с одних и тех же запросов и прокси.

    ``seed`` — соль запуска (по умолчанию :data:`_RUN_SEED`): её передают
    тесты, чтобы показать «разные запуски — разный порядок», не полагаясь на
    время и на вероятность.
    """
    if not items:
        raise ValueError("очередь пуста: нечего распределять по воркерам")
    if pool_size < 1:
        raise ValueError(f"pool_size должен быть не меньше 1, получено {pool_size}")
    if worker_index < 1:
        raise ValueError(f"worker_index должен быть не меньше 1, получено {worker_index}")
    order = list(range(len(items)))
    _round_rng(_RUN_SEED if seed is None else seed, round_index).shuffle(order)
    offset = (worker_index - 1) + round_index * pool_size
    return items[order[offset % len(items)]]


def next_round_item(
    items: Sequence[T], round_index: int, *, seed: int | None = None
) -> T:
    """Элемент, одинаковый для всех воркеров (``multiprocess_style == 2``).

    Смысл стиля сохранён: в раунде все воркеры ищут один и тот же запрос,
    поэтому выбор остаётся чистой функцией ``(seed, round_index)`` и
    повторяется от вызова к вызову. Меняется источник: не цикл
    ``items[round_index % len]``, а случайный индекс того же раунда —
    программа больше не проходит список по порядку с каждого старта.
    """
    if not items:
        raise ValueError("очередь пуста: нечего распределять по раундам")
    rng = _round_rng(_RUN_SEED if seed is None else seed, round_index)
    return items[rng.randrange(len(items))]


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
