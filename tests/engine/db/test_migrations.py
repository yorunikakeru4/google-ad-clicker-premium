"""Тесты схемы БД и механики миграций.

Схема — контракт между движком (пишет) и UI (читает), поэтому проверяется
жёстко: набор таблиц, индексы, включённые PRAGMA, идемпотентность и
поведение при рассинхронизации версий.
"""

import sqlite3

import pytest

from engine.db import migrations


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
        (migrations_dir / "002_add_flag.sql").write_text(
            "ALTER TABLE workers ADD COLUMN flag TEXT;"
        )

        migrations.migrate(db_path)
        migrations.migrate(db_path, target_version=2, migrations_dir=migrations_dir)

        with sqlite3.connect(db_path) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            columns = {r[1] for r in conn.execute("PRAGMA table_info(workers)")}

        assert version == 2
        assert "flag" in columns

    def test_applies_several_migrations_in_order(self, db_path, tmp_path):
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "002_first.sql").write_text(
            "ALTER TABLE workers ADD COLUMN first_col TEXT;"
        )
        (migrations_dir / "003_second.sql").write_text(
            "ALTER TABLE workers ADD COLUMN second_col TEXT;"
        )

        migrations.migrate(db_path)
        migrations.migrate(db_path, target_version=3, migrations_dir=migrations_dir)

        with sqlite3.connect(db_path) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            columns = {r[1] for r in conn.execute("PRAGMA table_info(workers)")}

        assert version == 3
        assert {"first_col", "second_col"} <= columns

    def test_skips_migration_that_would_exceed_target_version(self, db_path, tmp_path):
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "002_ok.sql").write_text(
            "ALTER TABLE workers ADD COLUMN applied_col TEXT;"
        )
        (migrations_dir / "003_future.sql").write_text(
            "ALTER TABLE workers ADD COLUMN future_col TEXT;"
        )

        migrations.migrate(db_path)
        migrations.migrate(db_path, target_version=2, migrations_dir=migrations_dir)

        with sqlite3.connect(db_path) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            columns = {r[1] for r in conn.execute("PRAGMA table_info(workers)")}

        assert version == 2
        assert "applied_col" in columns
        assert "future_col" not in columns, "миграция новее цели применена быть не должна"

    def test_preserves_data_across_incremental_migration(self, db_path, tmp_path):
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "002_add_flag.sql").write_text(
            "ALTER TABLE workers ADD COLUMN flag TEXT;"
        )

        migrations.migrate(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?)", ("browser_count", "3")
            )
            conn.commit()

        migrations.migrate(db_path, target_version=2, migrations_dir=migrations_dir)

        with sqlite3.connect(db_path) as conn:
            value = conn.execute("SELECT value FROM kv WHERE key = ?", ("browser_count",)).fetchone()

        assert value is not None, "миграция не должна была потерять данные"
        assert value[0] == "3"

    def test_broken_migration_does_not_advance_version(self, db_path, tmp_path):
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "002_broken.sql").write_text("ALTER TABLE no_such_table ADD COLUMN x TEXT;")

        migrations.migrate(db_path)

        with pytest.raises(sqlite3.OperationalError):
            migrations.migrate(db_path, target_version=2, migrations_dir=migrations_dir)

        with sqlite3.connect(db_path) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]

        assert version == 1, "версия не должна двигаться вперёд при упавшей миграции"
