"""Дневная ротация и хранение логов (план.md, §5 «Фаза 9»).

Здесь живёт всё, что связано с колонкой ``logs.day``: её значение, дневной
экспорт, retention и защита от роста БД. Модуль намеренно лёгкий — только
stdlib и запросы к самой БД: его импортируют и писатели (``engine.store``,
``engine.control_plane.state``), и демон, и тесты, поэтому он не должен
тянуть за собой ни хранилище, ни legacy-логгер.

Три решения, которые стоит знать:

1. **День считается от ``ts``, а не от текущего времени.** Запись могла
   попасть в буфер на минуту и упасть в БД уже в других сутках — привязка к
   моменту события (``local_day(ts)``) делает ``day`` детерминированной и
   пересчитываемой, что и проверяет бэкалф миграции 003.
2. **Локальное время, а не UTC.** План требует закрытия дня в 23:59
   *локального* времени, поэтому и колонка, и экспорт, и отсечка retention
   считают одно и то же — ``time.localtime``/``time.mktime``, ту же libc, что
   и SQLite с модификатором ``unixepoch, localtime`` в миграции 003.
3. **Строковое сравнение дат.** ``YYYY-MM-DD`` сравнимо лексикографически
   точно так же, как хронологически, поэтому отсечки вида
   ``day < cutoff`` не требуют ни функций даты, ни индекса-особника.

Экспорт против БД
-----------------
Файл ``logs/YYYY-MM-DD.log`` — снимок дня, а не источник истины: строки
после вызова ``export_day`` в него не дописываются (поэтому повторный запуск
перезаписывает файл целиком и не плодит дубли), а всё, что записано после
момента закрытия дня в 23:59, остаётся в БД до retention. Это цена
расписания из плана: закрытие — момент, а не ожидание, пока все буферы
воркеров доедут до конца суток. Фильтр по уровню позволяет держать в файле
INFO+ при DEBUG в БД (план §5, фаза 9).
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from engine.db.migrations import BUSY_TIMEOUT_MS

# Порядок уровней по возрастанию серьёзности: сравнение «уровень не ниже
# минимума» — это сравнение позиций в этом кортеже. Он же источник имён для
# enum behavior.log_file_level: словарь значений не должен жить в двух местах.
LEVEL_ORDER = ("DEBUG", "INFO", "WARNING", "ERROR")

_LEVEL_RANK = {name: rank for rank, name in enumerate(LEVEL_ORDER)}

# Ранг неизвестного уровня (в таблицу могла попасть строка от старого кода или
# от прямого INSERT): считается самым серьёзным, чтобы фильтрация по уровню
# не теряла такие записи молча.
_UNKNOWN_LEVEL_RANK = _LEVEL_RANK["ERROR"]

# Каталог дневных экспортов: <cwd>/logs, он же в .gitignore.
EXPORT_DIRNAME = "logs"

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Пустое значение в колонках без привязки: воркер/категория могли не быть
# заданы, и в файле это должно читаться как «нет», а не как пустая строка
# перед двоеточием.
_PLACEHOLDER = "-"


def local_day(ts: float) -> str:
    """Локальная дата unix-времени в формате ``YYYY-MM-DD``.

    Ровно то, что пишется в ``logs.day`` и что сравнивается в retention:
    одна функция на обе задачи, иначе границы дня в писателе и в отсечке
    разъехались бы на границе перехода на летнее время.
    """
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def level_rank(level: str | None) -> int:
    """Позиция уровня в ``LEVEL_ORDER``; неизвестный — как самый серьёзный."""
    if level is None:
        return _UNKNOWN_LEVEL_RANK
    return _LEVEL_RANK.get(level, _UNKNOWN_LEVEL_RANK)


def level_at_least(level: str | None, min_level: str) -> bool:
    """Уровень записи не ниже минимума дневного файла (DEBUG < ... < ERROR)."""
    return level_rank(level) >= level_rank(min_level)


def default_export_dir() -> Path:
    """Каталог дневных экспортов. Считается на каждый вызов, а не при импорте:
    тесты и одноразовые запуски меняют cwd, а каталог привязан к нему."""
    return Path.cwd() / EXPORT_DIRNAME


def _validate_day(day: str) -> None:
    if not isinstance(day, str) or _DAY_RE.match(day) is None:
        raise ValueError(f"день обязан быть YYYY-MM-DD, получено {day!r}")
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"день обязан быть существующей датой YYYY-MM-DD, получено {day!r}") from exc


def _min_level_rank(min_level: str) -> int:
    if not isinstance(min_level, str) or min_level not in _LEVEL_RANK:
        raise ValueError(
            f"неизвестный уровень экспорта {min_level!r}; допустимы {list(LEVEL_ORDER)}"
        )
    return _LEVEL_RANK[min_level]


def _one_line(text: str) -> str:
    """Экранировать переводы строк: одна запись — одна строка файла.

    Файл читают глазами и режут по строкам (grep, tail), поэтому сообщение с
    переводом строки не должно рвать запись на части. Потеря обратима: точный
    текст остаётся в колонке ``message`` БД.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


def _render_line(row: Any) -> str:
    """Строка экспорта: ``ISO-время [LEVEL] browser_id category: message поля``.

    ISO-время берётся из ``ts`` с реальным смещением локальной зоны — файл
    читают с той же машины, что писала запись, и смещение показывает, каким
    днём событие было на самом деле.
    """
    stamp = datetime.fromtimestamp(float(row["ts"])).astimezone().isoformat(timespec="seconds")
    browser_id = row["browser_id"] or _PLACEHOLDER
    category = row["category"] or _PLACEHOLDER
    message = _one_line(row["message"])
    line = f"{stamp} [{row['level']}] {browser_id} {category}: {message}"
    fields = row["fields"]
    if fields:
        line = f"{line} {_one_line(fields)}"
    return line


def _select_day(db_path: str | Path, day: str) -> list[Any]:
    """Строки одного дня в порядке записи. Ошибка чтения бросает: экспорту
    нельзя молча вернуть «пустой файл» — это выглядело бы как закрытый день."""
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT ts, level, browser_id, category, message, fields "
            "FROM logs WHERE day = ? ORDER BY ts, id",
            (day,),
        ).fetchall()
    finally:
        conn.close()


def export_day(
    db_path: str | Path,
    day: str,
    dest_dir: str | Path | None = None,
    min_level: str = "INFO",
) -> Path | None:
    """Выгрузить строки ``logs`` за ``day`` в ``dest_dir/YYYY-MM-DD.log``.

    Формат строки (зафиксирован, документируется здесь и им же проверяется
    тест)::

        <ISO-8601 локальное время> [LEVEL] browser_id category: message <fields JSON>

    Пример::

        2024-04-01T10:20:30+03:00 [WARNING] br-7 proxy: slow proxy {"latency_ms": 900}

    ``browser_id``/``category`` без значения печатаются как ``-``, переводы
    строк внутри ``message``/``fields`` экранируются (``\\n``), чтобы запись
    занимала ровно одну строку. Сортировка — по ``ts``, затем по ``id``.

    ``min_level`` — нижняя граница по известному порядку
    DEBUG < INFO < WARNING < ERROR: записи ниже неё в файл не попадают
    (стандарт — INFO+, «DEBUG остаётся в БД»). Неизвестный уровень записи
    считается самым серьёзным, чтобы не терять его молча; неизвестный
    ``min_level`` — ``ValueError``, это ошибка вызывающего кода.

    Возвращает путь созданного файла или ``None``, если записей нет (или все
    отсеяны по уровню): пустой день не получает файл-индикатор — его
    отсутствие и означает «неэкспортированный день». Повторный вызов
    перезаписывает файл целиком (через временную копию и rename), поэтому
    дублей строк не бывает, а дописанные после первого запуска строки
    попадают в файл.

    ``dest_dir=None`` — ``<cwd>/logs``; каталог создаётся только если есть
    что выгружать.
    """
    _validate_day(day)
    min_rank = _min_level_rank(min_level)
    lines = [
        _render_line(row)
        for row in _select_day(db_path, day)
        if level_rank(row["level"]) >= min_rank
    ]
    if not lines:
        return None

    dest = default_export_dir() if dest_dir is None else Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{day}.log"
    # Временный файл рядом с целевым: os.replace атомарен в пределах файловой
    # системы, и читатель никогда не увидит наполовину записанный день.
    tmp_path = dest / f".{day}.log.tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")
    os.replace(tmp_path, path)
    return path


__all__ = [
    "EXPORT_DIRNAME",
    "LEVEL_ORDER",
    "default_export_dir",
    "export_day",
    "level_at_least",
    "level_rank",
    "local_day",
]
