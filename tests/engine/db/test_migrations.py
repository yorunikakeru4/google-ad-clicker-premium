"""Тесты схемы БД и механики миграций.

Схема — контракт между движком (пишет) и UI (читает), поэтому проверяется
жёстко: набор таблиц, индексы, включённые PRAGMA, идемпотентность и
поведение при рассинхронизации версий.
"""

import os
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from engine.db import migrations


# Следующая версия схемы относительно текущей. Тесты инкрементального пути
# считают от SCHEMA_VERSION, а не от захардкоженной единицы: с повышением
# версии они продолжают проверять ровно то же поведение — «настоящая БД
# подтягивается следующим файлом миграции» — и не требуют правки при каждой
# новой миграции (так эти тесты и ломались при SCHEMA_VERSION 1 → 2).
NEXT_VERSION = migrations.SCHEMA_VERSION + 1

EXPECTED_TABLES = {
    "workers",
    "runs",
    "logs",
    "clicks",
    "captcha_events",
    "proxies",
    "proxy_usage",
    "profiles",
    "diagnostics",
    "network_requests",
    "metrics_hourly",
    "kv",
}

# Индексы, на которые UI опирается при фильтрации: по времени и по browser_id
EXPECTED_INDEXES = {
    "idx_logs_ts",
    "idx_logs_browser_id",
    "idx_logs_day_level_browser",
    "idx_clicks_ts",
    "idx_clicks_browser_id",
    "idx_captcha_events_ts",
    "idx_network_requests_ts",
    "idx_proxy_usage_ts",
    "idx_diagnostics_ts",
}


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "adclicker.db"


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


def _index_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    return {r[0] for r in rows}


def _user_version(db_path) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, dict]:
    """Имя колонки → её описание из PRAGMA table_info."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {
        row[1]: {"notnull": row[3], "default": row[4], "pk": row[5]} for row in rows
    }


# Снимок CREATE TABLE profiles из schema.sql версии 1 — намеренно не
# синхронизируется с текущей схемой: именно этот DDL должна пережить
# инкрементальная миграция, и любое его «обновление» свело бы тест к
# проверке схемы против самой себя.
_LEGACY_PROFILES_DDL = """
CREATE TABLE profiles (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL UNIQUE,
    key_ref      TEXT,
    proxy_id     INTEGER REFERENCES proxies (id) ON DELETE SET NULL,
    user_agent   TEXT,
    locale       TEXT,
    timezone     TEXT,
    status       TEXT    NOT NULL DEFAULT 'new',
    last_used_at REAL,
    created_at   REAL    NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL))
);
"""

# Строки, как они лежали в старой БД: default 'new' до миграции был рабочим.
_LEGACY_PROFILES = [
    ("legacy-a", "key-a", "new"),
    ("legacy-b", "key-b", "free"),
    ("legacy-c", "key-c", "new"),
]

# Снимок таблицы logs ДО миграции 003 — без колонки day. Тот же DDL нужен и
# для БД версии 1, и для версии 2: logs не менялась миграцией 002, поэтому
# «очередная» БД до 003 выглядит именно так.
_LEGACY_LOGS_DDL = """
CREATE TABLE logs (
    id         INTEGER PRIMARY KEY,
    ts         REAL    NOT NULL,
    level      TEXT    NOT NULL DEFAULT 'INFO',
    browser_id TEXT,
    category   TEXT,
    message    TEXT    NOT NULL,
    fields     TEXT,
    created_at REAL    NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL))
);
"""


def _recreate_logs_without_day(conn: sqlite3.Connection) -> None:
    """Вернуть logs в вид до миграции 003 (без колонки day).

    Свежая migrate() уже создала колонку: без пересоздания накат 003 на такую
    БД упал бы на duplicate column, и тест проверял бы миграцию против самой
    себя. Индексы logs уходят вместе с таблицей и создаются заново — иначе
    DROP TABLE оставил бы висячие idx_logs_*.
    """
    conn.execute("DROP TABLE logs")
    conn.execute(_LEGACY_LOGS_DDL)
    conn.execute("CREATE INDEX idx_logs_ts ON logs (ts)")
    conn.execute("CREATE INDEX idx_logs_browser_id ON logs (browser_id)")


def _make_version_database(db_path, version: int) -> None:
    """Собрать БД указанной версии из текущей схемы.

    Текущая схема создаётся целиком, затем таблицы, изменённые миграциями
    новее ``version``, пересоздаются в их прежнем виде, а user_version
    откатывается — ровно то, что увидит демон у пользователя, обновившегося
    с прошлой версии. Порядок важен: сначала migrate() создаёт все таблицы и
    индексы (иначе внешние ключи profiles останутся висеть на
    несуществующих proxies/workers).
    """
    migrations.migrate(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        # 002 меняла profiles, 003 — logs: чем моложе целевая версия, тем
        # больше таблиц надо вернуть в прежний вид.
        if version < 3:
            _recreate_logs_without_day(conn)
        if version < 2:
            conn.execute("DROP TABLE profiles")
            conn.execute(_LEGACY_PROFILES_DDL)
            conn.executemany(
                "INSERT INTO profiles (name, key_ref, status) VALUES (?, ?, ?)",
                _LEGACY_PROFILES,
            )
        conn.execute(f"PRAGMA user_version = {version}")
    finally:
        conn.close()


def _make_v1_database(db_path) -> None:
    """БД версии 1: profiles со status 'new' и logs без колонки day."""
    _make_version_database(db_path, 1)


def _make_v2_database(db_path) -> None:
    """БД версии 2: текущая схема, но logs ещё без колонки day."""
    _make_version_database(db_path, 2)


class TestFreshInstall:
    """БД создаётся с нуля одним вызовом."""

    def test_creates_every_table_from_the_data_model(self, db_path):
        migrations.migrate(db_path)

        with sqlite3.connect(db_path) as conn:
            names = _table_names(conn)

        assert EXPECTED_TABLES <= names

    def test_sets_user_version_to_current_schema_version(self, db_path):
        migrations.migrate(db_path)

        with sqlite3.connect(db_path) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]

        assert version == migrations.SCHEMA_VERSION

    def test_enables_wal_for_parallel_engine_writes_and_ui_reads(self, db_path):
        migrations.migrate(db_path)

        with sqlite3.connect(db_path) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

        assert mode.lower() == "wal"

    def test_creates_indexes_for_time_and_browser_filters(self, db_path):
        migrations.migrate(db_path)

        with sqlite3.connect(db_path) as conn:
            names = _index_names(conn)

        assert EXPECTED_INDEXES <= names


class TestIdempotency:
    """Повторный запуск не должен ни падать, ни ломать данные."""

    def test_running_twice_is_a_no_op(self, db_path):
        migrations.migrate(db_path)
        migrations.migrate(db_path)

        with sqlite3.connect(db_path) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            names = _table_names(conn)

        assert version == migrations.SCHEMA_VERSION
        assert EXPECTED_TABLES <= names

    def test_running_twice_preserves_existing_rows(self, db_path):
        migrations.migrate(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?)", ("retention_days", "30")
            )
            conn.commit()

        migrations.migrate(db_path)

        with sqlite3.connect(db_path) as conn:
            value = conn.execute("SELECT value FROM kv WHERE key = ?", ("retention_days",)).fetchone()

        assert value is not None, "повторная миграция не должна стирать данные"
        assert value[0] == "30"


class TestVersionMismatch:
    """БД новее кода — это ошибка развёртывания, а не повод пересоздавать схему."""

    def test_rejects_database_newer_than_code(self, db_path):
        migrations.migrate(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(f"PRAGMA user_version = {migrations.SCHEMA_VERSION + 5}")
            conn.commit()

        with pytest.raises(migrations.SchemaTooNewError):
            migrations.migrate(db_path)

    def test_rejection_does_not_touch_the_database(self, db_path):
        migrations.migrate(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(f"PRAGMA user_version = {migrations.SCHEMA_VERSION + 5}")
            conn.commit()

        with pytest.raises(migrations.SchemaTooNewError):
            migrations.migrate(db_path)

        with sqlite3.connect(db_path) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            names = _table_names(conn)

        assert version == migrations.SCHEMA_VERSION + 5, "версия не должна была сдвинуться"
        assert EXPECTED_TABLES <= names, "таблицы не должны были пропасть"


class TestConnectionSetup:
    """Подключение к боевой БД должно выставлять защитные PRAGMA."""

    def test_connection_enables_foreign_keys(self, db_path):
        migrations.migrate(db_path)

        with migrations.connect(db_path) as conn:
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_connection_sets_busy_timeout_for_concurrent_writers(self, db_path):
        migrations.migrate(db_path)

        with migrations.connect(db_path) as conn:
            timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]

        assert timeout_ms >= migrations.BUSY_TIMEOUT_MS


class TestPendingMigrations:
    """Отбор файлов миграций: по номеру, по имени, по порядку."""

    def test_selects_only_files_newer_than_current_version(self, tmp_path):
        (tmp_path / "001_initial.sql").write_text("SELECT 1;")
        (tmp_path / "002_add_clicks_index.sql").write_text("SELECT 1;")
        (tmp_path / "003_add_runs_column.sql").write_text("SELECT 1;")

        pending = migrations._pending_migrations(1, tmp_path)

        assert [version for version, _ in pending] == [2, 3]

    def test_returns_nothing_when_all_applied(self, tmp_path):
        (tmp_path / "001_initial.sql").write_text("SELECT 1;")

        assert migrations._pending_migrations(1, tmp_path) == []

    def test_ignores_files_not_matching_the_pattern(self, tmp_path):
        (tmp_path / "README.md").write_text("not a migration")
        (tmp_path / "002_ok.sql").write_text("SELECT 1;")
        (tmp_path / "backup.sql").write_text("SELECT 1;")
        (tmp_path / "002_copy.sql.bak").write_text("SELECT 1;")

        pending = migrations._pending_migrations(0, tmp_path)

        assert [version for version, _ in pending] == [2]

    def test_missing_directory_is_not_an_error(self, tmp_path):
        assert migrations._pending_migrations(0, tmp_path / "nope") == []


class TestIncrementalMigration:
    """Путь «БД уже создана, накатываем следующую миграцию».

    Именно этот путь сработает на первом же изменении схемы, поэтому он
    проверяется на настоящем файле миграции, а не остаётся мёртвым кодом.
    """

    def test_applies_migration_file_on_top_of_existing_database(self, db_path, tmp_path):
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / f"{NEXT_VERSION}_add_flag.sql").write_text(
            "ALTER TABLE workers ADD COLUMN flag TEXT;"
        )

        migrations.migrate(db_path)
        migrations.migrate(db_path, target_version=NEXT_VERSION, migrations_dir=migrations_dir)

        with sqlite3.connect(db_path) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            columns = {r[1] for r in conn.execute("PRAGMA table_info(workers)")}

        assert version == NEXT_VERSION
        assert "flag" in columns

    def test_applies_several_migrations_in_order(self, db_path, tmp_path):
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / f"{NEXT_VERSION}_first.sql").write_text(
            "ALTER TABLE workers ADD COLUMN first_col TEXT;"
        )
        (migrations_dir / f"{NEXT_VERSION + 1}_second.sql").write_text(
            "ALTER TABLE workers ADD COLUMN second_col TEXT;"
        )

        migrations.migrate(db_path)
        migrations.migrate(db_path, target_version=NEXT_VERSION + 1, migrations_dir=migrations_dir)

        with sqlite3.connect(db_path) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            columns = {r[1] for r in conn.execute("PRAGMA table_info(workers)")}

        assert version == NEXT_VERSION + 1
        assert {"first_col", "second_col"} <= columns

    def test_skips_migration_that_would_exceed_target_version(self, db_path, tmp_path):
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / f"{NEXT_VERSION}_ok.sql").write_text(
            "ALTER TABLE workers ADD COLUMN applied_col TEXT;"
        )
        (migrations_dir / f"{NEXT_VERSION + 1}_future.sql").write_text(
            "ALTER TABLE workers ADD COLUMN future_col TEXT;"
        )

        migrations.migrate(db_path)
        migrations.migrate(db_path, target_version=NEXT_VERSION, migrations_dir=migrations_dir)

        with sqlite3.connect(db_path) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            columns = {r[1] for r in conn.execute("PRAGMA table_info(workers)")}

        assert version == NEXT_VERSION
        assert "applied_col" in columns
        assert "future_col" not in columns, "миграция новее цели применена быть не должна"

    def test_preserves_data_across_incremental_migration(self, db_path, tmp_path):
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / f"{NEXT_VERSION}_add_flag.sql").write_text(
            "ALTER TABLE workers ADD COLUMN flag TEXT;"
        )

        migrations.migrate(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?)", ("browser_count", "3")
            )
            conn.commit()

        migrations.migrate(db_path, target_version=NEXT_VERSION, migrations_dir=migrations_dir)

        with sqlite3.connect(db_path) as conn:
            value = conn.execute("SELECT value FROM kv WHERE key = ?", ("browser_count",)).fetchone()

        assert value is not None, "миграция не должна была потерять данные"
        assert value[0] == "3"

    def test_broken_migration_does_not_advance_version(self, db_path, tmp_path):
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / f"{NEXT_VERSION}_broken.sql").write_text(
            "ALTER TABLE no_such_table ADD COLUMN x TEXT;"
        )

        migrations.migrate(db_path)
        version_before = _user_version(db_path)

        with pytest.raises(sqlite3.OperationalError):
            migrations.migrate(db_path, target_version=NEXT_VERSION, migrations_dir=migrations_dir)

        assert _user_version(db_path) == version_before, (
            "версия не должна двигаться вперёд при упавшей миграции"
        )


class TestProfileFieldsMigration:
    """Миграция 002: колонка profiles.fields и нормализация статуса.

    Оба пути обязаны работать: свежая БД собирается из schema.sql (там же
    default 'free'), существующая подтягивается файлом миграции без потери
    строк. Проверяется не «версия равна константе», а конкретные изменения:
    колонка, её default, сохранность данных и переход new → free.
    """

    def test_fresh_database_has_fields_column_with_empty_object_default(self, db_path):
        migrations.migrate(db_path)

        with sqlite3.connect(db_path) as conn:
            columns = _columns(conn, "profiles")

        assert "fields" in columns, "свежая БД обязана получить колонку fields"
        assert columns["fields"]["notnull"] == 1
        assert columns["fields"]["default"] == "'{}'"

    def test_fresh_database_defaults_status_to_free(self, db_path):
        migrations.migrate(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute("INSERT INTO profiles (name) VALUES ('fresh')")
            status = conn.execute("SELECT status FROM profiles").fetchone()[0]

        assert status == "free", "default 'new' больше не является статусом профиля"

    def test_incremental_migration_is_pending_on_a_version_one_database(self, db_path):
        _make_v1_database(db_path)

        pending = migrations._pending_migrations(1, migrations.MIGRATIONS_DIR)

        # Все миграции после версии 1, по порядку: тест не должен ломаться
        # с каждой новой, ему важно, что цепочка до текущей версии цела.
        assert [version for version, _ in pending] == list(
            range(2, migrations.SCHEMA_VERSION + 1)
        ), "цепочка миграций от БД версии 1 до текущей должна быть полной"

    def test_incremental_migration_adds_the_column_and_advances_version(self, db_path):
        _make_v1_database(db_path)
        assert _user_version(db_path) == 1

        migrations.migrate(db_path)

        assert _user_version(db_path) == migrations.SCHEMA_VERSION
        with sqlite3.connect(db_path) as conn:
            columns = _columns(conn, "profiles")
        assert "fields" in columns
        assert columns["fields"]["notnull"] == 1
        assert columns["fields"]["default"] == "'{}'"

    def test_incremental_migration_keeps_rows_and_normalizes_new_status(self, db_path):
        _make_v1_database(db_path)

        migrations.migrate(db_path)

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT name, key_ref, status, fields FROM profiles ORDER BY id"
            ).fetchall()

        assert [(row[0], row[1]) for row in rows] == [
            (name, key_ref) for name, key_ref, _ in _LEGACY_PROFILES
        ], "строки профилей не должны теряться"
        assert [row[2] for row in rows] == ["free", "free", "free"], (
            "статус 'new' обязан стать 'free' независимо от того, было ли значение"
            " проставлено явно или получено из старого default"
        )
        assert [row[3] for row in rows] == ["{}", "{}", "{}"], (
            "существующие строки получают default новой колонки"
        )

    def test_incremental_migration_normalizes_only_new_status(self, db_path):
        """Остальные статусы — данные оператора, миграция их не трогает."""
        _make_v1_database(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO profiles (name, status) VALUES ('operator-blocked', 'blocked')"
            )
            conn.commit()

        migrations.migrate(db_path)

        with sqlite3.connect(db_path) as conn:
            status = conn.execute(
                "SELECT status FROM profiles WHERE name = 'operator-blocked'"
            ).fetchone()[0]

        assert status == "blocked"

    def test_repeated_migration_is_a_no_op_for_data(self, db_path):
        _make_v1_database(db_path)
        migrations.migrate(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO profiles (name, status) VALUES ('post-migration', 'assigned')"
            )
            conn.commit()

        migrations.migrate(db_path)

        assert _user_version(db_path) == migrations.SCHEMA_VERSION
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT name, status FROM profiles ORDER BY id").fetchall()
        assert rows == [
            ("legacy-a", "free"),
            ("legacy-b", "free"),
            ("legacy-c", "free"),
            ("post-migration", "assigned"),
        ]


def _local_ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> float:
    """Unix-время для заданного локального времени (секунды не нужны точнее)."""
    return time.mktime((year, month, day, hour, minute, second, 0, 0, -1))


def _insert_logs(db_path, ts_values) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO logs (ts, level, message) VALUES (?, 'INFO', ?)",
            [(ts, f"msg-{index}") for index, ts in enumerate(ts_values)],
        )
        conn.commit()


def _log_days(db_path) -> list[str | None]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT day FROM logs ORDER BY id").fetchall()
    return [row[0] for row in rows]


def _logs_columns(db_path) -> dict[str, dict]:
    with sqlite3.connect(db_path) as conn:
        return _columns(conn, "logs")


@pytest.fixture
def fixed_timezone():
    """Перевести процесс на UTC-12 и корректно вернуть обратно.

    Нужен, чтобы различать локальную дату и UTC: бэкалф обязан считать по
    localtime, а не по UTC. Зона Etc/GMT+12 — это UTC-12: знак в имени
    зон tzatabase инвертирован к POSIX. tzset() зовётся и при входе, и при выходе — libc
    кэширует зону, и без сброса последующие тесты увидели бы чужую.
    """
    original = os.environ.get("TZ")
    os.environ["TZ"] = "Etc/GMT+12"
    time.tzset()
    yield
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


class TestLogsDayMigration:
    """Миграция 003: колонка logs.day, бэкалф локальной даты и индекс.

    Оба пути обязаны работать: свежая БД получает колонку и индекс из
    schema.sql, существующая — из файла миграции вместе с бэкалфом из ts.
    """

    def test_fresh_database_has_nullable_day_column(self, db_path):
        migrations.migrate(db_path)

        columns = _logs_columns(db_path)

        assert "day" in columns, "свежая БД обязана получить колонку day"
        assert columns["day"]["notnull"] == 0, (
            "day заполняется писателем от ts, а не DEFAULT: у SQLite нет "
            "выражения DEFAULT от другой колонки таблицы"
        )

    def test_fresh_database_indexes_day_level_browser(self, db_path):
        migrations.migrate(db_path)

        with sqlite3.connect(db_path) as conn:
            names = _index_names(conn)

        assert "idx_logs_day_level_browser" in names

    def test_incremental_migration_adds_column_and_index_on_a_version_two_db(
        self, db_path
    ):
        _make_v2_database(db_path)
        assert "day" not in _logs_columns(db_path)

        migrations.migrate(db_path)

        assert "day" in _logs_columns(db_path)
        with sqlite3.connect(db_path) as conn:
            names = _index_names(conn)
        assert "idx_logs_day_level_browser" in names
        assert _user_version(db_path) == migrations.SCHEMA_VERSION

    def test_incremental_backfill_splits_rows_on_the_local_day_boundary(self, db_path):
        """23:59:59 и 00:00:00 локально — соседние сутки, а не один день."""
        just_before_midnight = _local_ts(2024, 3, 31, 23, 59, 59)
        midnight = _local_ts(2024, 4, 1, 0, 0, 0)
        _make_v2_database(db_path)
        _insert_logs(db_path, [just_before_midnight, midnight])

        migrations.migrate(db_path)

        assert _log_days(db_path) == ["2024-03-31", "2024-04-01"]

    def test_incremental_backfill_uses_local_date_not_utc(self, db_path, fixed_timezone):
        """С зоной UTC-12 (Etc/GMT+12) запись в 00:30 UTC уходит в предыдущий день."""
        instant = datetime(2024, 7, 1, 0, 30, tzinfo=timezone.utc).timestamp()
        if time.strftime("%Y-%m-%dT%H", time.localtime(instant)) != "2024-06-30T12":
            pytest.skip("зона Etc/GMT+12 недоступна в этом окружении")
        _make_v2_database(db_path)
        _insert_logs(db_path, [instant])

        migrations.migrate(db_path)

        assert _log_days(db_path) == ["2024-06-30"], (
            "бэкалф обязан считать localtime, а не UTC"
        )

    def test_backfill_matches_python_localtime_for_every_inserted_row(self, db_path):
        """Опора на одну и ту же localtime: SQL-бэкалф против time.localtime."""
        timestamps = [
            _local_ts(2023, 12, 31, 23, 59, 59),
            _local_ts(2024, 1, 1, 0, 0, 0),
            _local_ts(2024, 6, 15, 12, 0, 0),
            _local_ts(2024, 12, 31, 23, 59, 59),
        ]
        _make_v2_database(db_path)
        _insert_logs(db_path, timestamps)

        migrations.migrate(db_path)

        assert _log_days(db_path) == [
            time.strftime("%Y-%m-%d", time.localtime(ts)) for ts in timestamps
        ]

    def test_incremental_logs_schema_matches_a_fresh_database(self, db_path, tmp_path):
        fresh = tmp_path / "fresh.db"
        migrations.migrate(fresh)
        _make_v2_database(db_path)

        migrations.migrate(db_path)

        assert _logs_columns(db_path) == _logs_columns(fresh), (
            "оба пути к схеме обязаны дать один и тот же logs"
        )

    def test_repeated_migration_is_a_no_op_for_backfilled_days(self, db_path):
        _make_v2_database(db_path)
        _insert_logs(db_path, [_local_ts(2024, 4, 1, 10, 0, 0)])
        migrations.migrate(db_path)
        first = _log_days(db_path)

        migrations.migrate(db_path)

        assert _log_days(db_path) == first, "повторная миграция не должна пересчитывать day"
        assert _user_version(db_path) == migrations.SCHEMA_VERSION
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0] == 1
