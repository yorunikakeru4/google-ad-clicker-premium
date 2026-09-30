"""Применение схемы БД и инкрементальных миграций.

Схема версионируется через PRAGMA user_version:

- версия 0 (свежая БД) -> применяется engine/db/schema.sql целиком;
- версия N < SCHEMA_VERSION -> применяются файлы engine/db/migrations/NNN_*.sql
  с номером больше текущей версии, по порядку, user_version обновляется после
  каждого;
- версия > SCHEMA_VERSION -> SchemaTooNewError. Это расхождение развёртывания
  (старый бинарник против новой БД), пересоздавать схему в такой ситуации
  нельзя — данные пользователя дороже.

Ограничение executescript: он делает неявный COMMIT и выполняет утверждения в
режиме autocommit, поэтому многосоставная миграция применяется неатомарно —
упавшее третье утверждение оставит первые два применёнными, а user_version
останется на предыдущей версии. Следствие: каждая миграция должна быть
написана так, чтобы её можно было безопасно перезапустить (IF NOT EXISTS,
пересоздание таблиц) либо начинаться с CREATE TABLE, а не с ALTER.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DB_DIR = Path(__file__).parent
SCHEMA_FILE = DB_DIR / "schema.sql"
MIGRATIONS_DIR = DB_DIR / "migrations"

# Повышается только вместе с новым файлом в MIGRATIONS_DIR.
# 2 — колонка profiles.fields и нормализация статуса new → free
# (migrations/002_profile_fields.sql).
# 3 — колонка logs.day (локальная дата от ts), бэкалф истории и индекс
# (day, level, browser_id) (migrations/003_logs_day.sql).
SCHEMA_VERSION = 3

# Движок пишет из нескольких воркеров, UI читает одновременно. WAL снимает
# блокировку между ними, busy_timeout ждёт освобождения вместо ошибки.
BUSY_TIMEOUT_MS = 5000

_MIGRATION_PATTERN = re.compile(r"^(\d+)_.*\.sql$")


class SchemaTooNewError(RuntimeError):
    """БД новее, чем код, который её открыл."""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Подключение к боевой БД с защитными PRAGMA.

    Ожидается, что схема уже применена через migrate().
    """
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def _autocommit(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Подключение без неявной транзакции.

    Миграция меняет схему, а PRAGMA journal_mode и DDL нельзя выполнять
    внутри транзакции — поэтому autocommit, а каждое утверждение применяется
    и фиксируется отдельно.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        yield conn
    finally:
        conn.close()


def _pending_migrations(current_version: int, migrations_dir: Path) -> list[tuple[int, Path]]:
    """Миграции с номером больше текущей версии, по возрастанию номера."""
    if not migrations_dir.is_dir():
        return []

    found: list[tuple[int, Path]] = []
    for path in migrations_dir.iterdir():
        match = _MIGRATION_PATTERN.match(path.name)
        if match is None:
            continue
        version = int(match.group(1))
        if version > current_version:
            found.append((version, path))

    return sorted(found, key=lambda item: item[0])


def _get_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    # PRAGMA не принимает плейсхолдер, значение подставляется только из int.
    conn.execute(f"PRAGMA user_version = {version}")


def _apply_script(conn: sqlite3.Connection, script: str) -> None:
    # executescript сначала делает неявный COMMIT, затем гонит statements.
    conn.executescript(script)


def migrate(
    db_path: str | Path,
    target_version: int | None = None,
    migrations_dir: Path | None = None,
) -> int:
    """Приводит БД к целевой версии схемы. Возвращает итоговую версию.

    Идемпотентна: повторный вызов на актуальной БД ничего не делает и не
    трогает данные.

    target_version и migrations_dir — параметры для тестов: они позволяют
    прогнать инкрементальный путь на настоящем файле миграции, не заводя
    фейковую версию в репозитории ради одного теста.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    target = SCHEMA_VERSION if target_version is None else target_version
    pending_dir = MIGRATIONS_DIR if migrations_dir is None else Path(migrations_dir)

    with _autocommit(db_path) as conn:
        # WAL включается первым и до любых DDL: journal_mode нельзя сменить
        # внутри транзакции, и режим сохраняется в файле навсегда.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")

        current = _get_version(conn)
        if current > target:
            raise SchemaTooNewError(
                f"БД {db_path.name} имеет схему версии {current}, "
                f"код знает только до {target}. "
                "Обновите программу или укажите другой файл БД."
            )

        if current == 0:
            _apply_script(conn, SCHEMA_FILE.read_text(encoding="utf-8"))
            _set_version(conn, target)
            return target

        for version, path in _pending_migrations(current, pending_dir):
            if version > target:
                break
            _apply_script(conn, path.read_text(encoding="utf-8"))
            _set_version(conn, version)

        return _get_version(conn)
