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
from dataclasses import dataclass
from datetime import datetime, timedelta
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

# Значения по умолчанию полей behavior.* хранения логов. Их же берут _SCHEMA
# control plane и config_reader: дефолты обязаны совпадать, иначе старый
# config.json читал бы одно, а демон после сохранения из UI — другое.
DEFAULT_LOG_RETENTION_DAYS = 30
DEFAULT_LOG_FILE_LEVEL = "INFO"
DEFAULT_DB_SIZE_LIMIT_MB = 0

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Имя файла дневного экспорта. Только он подлежит удалению по retention:
# adclicker.log, его ротации и чужие файлы в том же каталоге трогать нельзя.
_EXPORT_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.log$")


@dataclass(frozen=True)
class RetentionResult:
    """Итог одного прогона retention — для лога и для тестов."""

    cutoff: str
    deleted_rows: int
    deleted_files: tuple[Path, ...]


@dataclass(frozen=True)
class DbSizeResult:
    """Итог защиты от роста БД. ``error`` — только имя типа исключения:
    текст ошибки SQLite может содержать путь к файлу пользователя."""

    fits: bool
    limit_bytes: int
    size_bytes: int
    deleted_days: tuple[str, ...]
    error: str | None = None

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


def _next_day(day: str) -> str:
    """Следующие сутки тем же штампом — для «удалить файл этого дня»."""
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def retention_cutoff(today: str, retention_days: int) -> str:
    """Дата-граница retention: ``сегодня - retention_days``.

    Граница строгая: строки (и файлы) с ``day < cutoff`` удаляются, дата
    ``cutoff`` — это ровно ``retention_days`` дней назад и она **остаётся**.
    Сравнение идёт по строкам ``YYYY-MM-DD``, где лексикографический порядок
    совпадает с хронологическим.
    """
    _validate_day(today)
    if (
        isinstance(retention_days, bool)
        or not isinstance(retention_days, int)
        or retention_days < 1
    ):
        raise ValueError(f"retention_days обязан быть целым >= 1, получено {retention_days!r}")
    base = datetime.strptime(today, "%Y-%m-%d")
    return (base - timedelta(days=retention_days)).strftime("%Y-%m-%d")


def purge_logs_before(db_path: str | Path, cutoff_day: str) -> int:
    """Удалить строки ``logs`` строго старше отсечки. Возвращает их число.

    ``day IS NULL`` не трогается: это записи без дня (баг писателя или
    история до бэкалфа), удалять их молча — значит терять данные незаметно.
    """
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        cursor = conn.execute("DELETE FROM logs WHERE day < ?", (cutoff_day,))
        conn.commit()
        return int(cursor.rowcount)
    finally:
        conn.close()


def purge_export_files(export_dir: str | Path, cutoff_day: str) -> list[Path]:
    """Удалить файлы дневного экспорта строго старше отсечки.

    Касаться разрешено только файлов с именем ``YYYY-MM-DD.log``: остальное в
    каталоге (ротационный ``adclicker.log``, случайные записи оператора)
    остаётся нетронутым. Отсутствующий каталог — не ошибка, просто удалять
    нечего.
    """
    directory = Path(export_dir)
    if not directory.is_dir():
        return []
    removed: list[Path] = []
    for path in sorted(directory.iterdir()):
        match = _EXPORT_NAME_RE.match(path.name)
        if match is None or match.group(1) >= cutoff_day:
            continue
        if not path.is_file():
            continue
        path.unlink()
        removed.append(path)
    return removed


def run_retention(
    db_path: str | Path,
    today: str,
    retention_days: int,
    export_dir: str | Path | None = None,
) -> RetentionResult:
    """Удалить строки и файлы экспорта старше N полных дней.

    Граница: ``day < сегодня - N`` уходит, ровно N дней назад остаётся.
    ``export_dir=None`` — ``<cwd>/logs``, тот же каталог, куда пишет
    :func:`export_day`.
    """
    cutoff = retention_cutoff(today, retention_days)
    deleted_rows = purge_logs_before(db_path, cutoff)
    directory = default_export_dir() if export_dir is None else Path(export_dir)
    deleted_files = tuple(purge_export_files(directory, cutoff))
    return RetentionResult(
        cutoff=cutoff, deleted_rows=deleted_rows, deleted_files=deleted_files
    )


def db_size_bytes(db_path: str | Path) -> int:
    """Размер БД и её ``-wal`` вместе: именно это ест место на диске.

    Отсутствующий ``-wal`` (последнее соединение закрылось и его убрали)
    даёт ноль. Другие ошибки чтения не гасятся: защита от роста должна падать
    громко, а не считать размер БД нулём.
    """
    total = 0
    for suffix in ("", "-wal"):
        try:
            total += Path(str(db_path) + suffix).stat().st_size
        except FileNotFoundError:
            continue
    return total


def enforce_db_size_limit(
    db_path: str | Path,
    limit_mb: float,
    export_dir: str | Path | None = None,
) -> DbSizeResult:
    """Держать размер БД в ``limit_mb``: удалять старые дни логов, пока влезет.

    Алгоритм: измерить (БД + ``-wal``) → не влезает → удалить самый старый
    день (строки и его файл экспорта) → освободить место (checkpoint WAL +
    ``VACUUM``) → повторить. ``DELETE`` сам по себе файл не уменьшает —
    освободившиеся страницы остаются в файле, — поэтому без ``VACUUM`` цикл
    выгреб бы все логи и всё равно не влез бы.

    Исходы:

    * ``fits=True`` — влезло (возможно, после удаления нескольких дней);
    * ``fits=False`` + ``error`` — место освободить не удалось (например,
      БД занята другим читателем); данные удалены только за уже выполненные
      итерации, остальные не трогаются — лучше остаться с растущей БД, чем
      выгрести все логи впустую;
    * ``fits=False`` без ``error`` — строки кончились, а БД всё ещё больше
      лимита (например, лимит меньше пустой схемы). Вызывающий код обязан
      сказать об этом WARNING'ом и продолжить принимать записи: защита не
      имеет права блокировать логирование.

    ``limit_mb <= 0`` — ``ValueError``: нулевой лимит означал бы «удалить
    всё», а выключенный режим — это забота вызывающего кода (он проверяет
    ``db_size_limit_mb > 0`` до вызова).
    """
    if (
        isinstance(limit_mb, bool)
        or not isinstance(limit_mb, (int, float))
        or limit_mb <= 0
    ):
        raise ValueError(f"лимит размера БД обязан быть числом МБ > 0, получено {limit_mb!r}")

    limit_bytes = int(limit_mb * 1024 * 1024)
    directory = None if export_dir is None else Path(export_dir)
    deleted: list[str] = []
    while True:
        size = db_size_bytes(db_path)
        if size <= limit_bytes:
            return DbSizeResult(
                fits=True, limit_bytes=limit_bytes, size_bytes=size, deleted_days=tuple(deleted)
            )
        day = _oldest_day(db_path)
        if day is None:
            return DbSizeResult(
                fits=False, limit_bytes=limit_bytes, size_bytes=size, deleted_days=tuple(deleted)
            )
        _delete_day_rows(db_path, day)
        deleted.append(day)
        if directory is not None:
            purge_export_files(directory, _next_day(day))
        try:
            _reclaim_space(db_path)
        except Exception as exc:  # noqa: BLE001 - защита обязана сообщить, а не упасть
            return DbSizeResult(
                fits=False,
                limit_bytes=limit_bytes,
                size_bytes=db_size_bytes(db_path),
                deleted_days=tuple(deleted),
                error=type(exc).__name__,
            )


def _oldest_day(db_path: str | Path) -> str | None:
    """Самый старый день в ``logs``; None — строк с датой не осталось."""
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        row = conn.execute("SELECT MIN(day) FROM logs").fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _delete_day_rows(db_path: str | Path, day: str) -> int:
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        cursor = conn.execute("DELETE FROM logs WHERE day = ?", (day,))
        conn.commit()
        return int(cursor.rowcount)
    finally:
        conn.close()


def _reclaim_space(db_path: str | Path) -> None:
    """Вернуть файлу освободившееся место: checkpoint WAL, затем VACUUM.

    Нужен и для размера (без него файловой системе всё равно, сколько
    страниц свободно), и для дальнейших итераций защиты. Ошибка не гасится
    здесь: вызывающий цикл обязан остановиться, иначе следующее измерение
    соврало бы о размере.

    Checkpoint зовётся дважды: до VACUUM — чтобы работа шла по актуальной
    картине, и после — потому что в WAL-режиме сам VACUUM пишет новый
    образ базы в WAL. Без второго checkpoint'а главный файл продолжал бы
    хранить старые страницы, а сумма «БД + WAL» не уменьшалась бы.
    """
    # isolation_level=None: VACUUM нельзя выполнять внутри транзакции.
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


__all__ = [
    "DEFAULT_DB_SIZE_LIMIT_MB",
    "DEFAULT_LOG_FILE_LEVEL",
    "DEFAULT_LOG_RETENTION_DAYS",
    "EXPORT_DIRNAME",
    "LEVEL_ORDER",
    "DbSizeResult",
    "RetentionResult",
    "db_size_bytes",
    "default_export_dir",
    "enforce_db_size_limit",
    "export_day",
    "level_at_least",
    "level_rank",
    "local_day",
    "purge_export_files",
    "purge_logs_before",
    "retention_cutoff",
    "run_retention",
]
