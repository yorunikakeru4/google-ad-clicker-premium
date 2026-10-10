"""Тесты ``engine.exporter`` — выгрузка SQLite → PostgreSQL без реального PG.

Схема такая: чтение из ``adclicker.db`` честное (``tmp_path`` + миграции +
вставленные строки), а ``psycopg`` подменяется фейковым модулем в
``sys.modules`` — он записывает ``connect()``, каждый SQL с параметрами,
коммиты и курсор ``export_state``. Так проверяется контракт плана §13.2:
ленивый импорт, ``enabled=False`` без соединения, курсор ``id > last_id``
с ``LIMIT``, upsert-снимки, NULL в ``proxies.username``/``password`` и
атомарность «данные + курсор» в одном коммите.

Psycopg в nix-среде нет и ставить его нельзя: тесты обязаны работать без
него, а ``ExportError`` на ``ImportError`` — тоже проверяемый контракт.
"""

from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

import pytest

import engine.exporter as exporter_module
from engine.db import migrations
from engine.exporter import (
    DEFAULT_BATCH_SIZE,
    EXPORT_TABLES,
    ExportError,
    ExportSettings,
    Exporter,
)

# Контракт §13.1/§9: ровно эти 11 таблиц уходят в PG, в этом порядке.
EXPECTED_TABLES = (
    "proxies",
    "profiles",
    "workers",
    "metrics_hourly",
    "runs",
    "logs",
    "clicks",
    "network_requests",
    "captcha_events",
    "proxy_usage",
    "diagnostics",
)


def settings(**overrides) -> ExportSettings:
    """Настройки с дефолтами из ``_SCHEMA``; включённые, если не сказали иначе."""
    data = {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 5432,
        "dbname": "adclicker_export",
        "user": "adclicker",
        "password": "secret",
        "sslmode": "prefer",
        "batch_size": DEFAULT_BATCH_SIZE,
    }
    data.update(overrides)
    return ExportSettings(**data)


def sqlite_schema_columns() -> dict[str, list[tuple[str, str]]]:
    """Колонки ``engine/db/schema.sql``: [(имя, тип SQLite), ...] по таблицам."""
    import re

    text = (
        Path(__file__).resolve().parents[2] / "engine" / "db" / "schema.sql"
    ).read_text(encoding="utf-8")
    tables: dict[str, list[tuple[str, str]]] = {}
    current: str | None = None
    for line in text.splitlines():
        header = re.match(r"CREATE TABLE IF NOT EXISTS (\w+) \(", line)
        if header:
            current = header.group(1)
            tables[current] = []
            continue
        if current is None:
            continue
        if line.startswith(");"):
            current = None
            continue
        column = re.match(r"\s+(\w+)\s+(INTEGER|TEXT|REAL)\b", line)
        if column:
            tables[current].append((column.group(1), column.group(2)))
    return tables


def columns_of(table: str) -> tuple[str, ...]:
    """Имена колонок таблицы в порядке ``schema.sql`` — из спецификации модуля."""
    from engine.exporter import _SPECS

    return _SPECS[table].names


class FakeConnection:
    """Соединение-заглушка: журнал событий плюс курсор ``export_state``."""

    def __init__(self, fail_on: str | None = None):
        self.events: list[tuple] = []
        self.state: dict[str, int] = {}
        self.fail_on = fail_on

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.events.append(("commit",))

    def rollback(self) -> None:
        self.events.append(("rollback",))

    def close(self) -> None:
        self.events.append(("close",))

    @property
    def sql_calls(self) -> list[tuple[str, tuple]]:
        return [(event[1], event[2]) for event in self.events if event[0] == "exec"]

    @property
    def commits(self) -> int:
        return sum(1 for event in self.events if event[0] == "commit")

    @property
    def statements(self) -> list[str]:
        return [sql for sql, _ in self.sql_calls]


class FakeCursor:
    """Курсор фейкового psycopg: пишет SQL в журнал соединения."""

    def __init__(self, conn: FakeConnection):
        self.conn = conn
        self._last_sql = ""

    def execute(self, sql, params=None):
        self._last_sql = sql
        self.conn.events.append(("exec", sql, params))
        self._maybe_fail(sql)
        if "INTO export_state" in sql:
            table, last_id, _updated_at = params
            self.conn.state[str(table)] = int(last_id)

    def executemany(self, sql, params_seq):
        for params in params_seq:
            self.conn.events.append(("exec", sql, params))
            self._maybe_fail(sql)

    def fetchall(self):
        if "FROM export_state" in self._last_sql:
            return sorted(self.conn.state.items())
        return []

    def close(self) -> None:
        self.conn.events.append(("cursor-close",))

    def _maybe_fail(self, sql: str) -> None:
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("PostgreSQL недоступен")


class FakePsycopg:
    """Модуль psycopg: ``connect(**kwargs)`` возвращает фейковое соединение."""

    def __init__(self, conn: FakeConnection | None = None, error: Exception | None = None):
        self.conn = conn if conn is not None else FakeConnection()
        self.error = error
        self.connect_calls: list[dict] = []

    def connect(self, **kwargs):
        self.connect_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.conn


def install_psycopg(monkeypatch, fake: FakePsycopg) -> FakePsycopg:
    """Кладёт фейковый модуль в ``sys.modules``: ленивый ``import psycopg`` его возьмёт."""
    module = types.ModuleType("psycopg")
    module.connect = fake.connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", module)
    return fake


def insert(db_path, table: str, **columns) -> None:
    """Одна строка в SQLite: названия колонок — как в schema.sql."""
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"INSERT INTO {table} ({names}) VALUES ({marks})",
            tuple(columns.values()),
        )
        conn.commit()


def seed_one_row_per_table(db_path) -> None:
    """Минимум строк во всех выгружаемых таблицах — для порядка и коммитов."""
    insert(db_path, "proxies", label="main", scheme="http", host="1.2.3.4", port=8080)
    insert(db_path, "profiles", name="p1")
    insert(db_path, "workers", browser_id="br-1", status="running")
    insert(db_path, "metrics_hourly", bucket=1_700_000_000, successes=1)
    insert(db_path, "runs", status="running", started_at=1.0)
    insert(db_path, "logs", ts=1.0, day="2026-10-10", level="INFO", message="ok")
    insert(db_path, "clicks", ts=1.0, url="https://example.test/", query="q")
    insert(db_path, "network_requests", ts=1.0, browser_id="br-1", method="GET", url="https://x")
    insert(db_path, "captcha_events", ts=1.0, browser_id="br-1")
    insert(db_path, "proxy_usage", ts=1.0, browser_id="br-1", result="ok")
    insert(db_path, "diagnostics", ts=1.0, browser_id="br-1", ip="1.2.3.4")


def events_for(conn: FakeConnection, needle: str) -> list[tuple]:
    return [event for event in conn.events if event[0] == "exec" and needle in event[1]]


def params_for(conn: FakeConnection, needle: str) -> list[tuple]:
    return [event[2] for event in events_for(conn, needle)]


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


@pytest.fixture
def sqlite_sql(monkeypatch) -> list[str]:
    """Тексты запросов, которые экспортер реально выполнил в SQLite.

    ``psycopg`` замокан отдельно (журнал ``fake.conn``), а вот выборки-источники
    идут в настоящий sqlite3 — их и ловит trace-колбэк. Параметры в тексте
    уже подставлены (``... WHERE id > 0 ... LIMIT 500``), поэтому в assert'ах
    видно значения, а не плейсхолдеры.
    """
    captured: list[str] = []
    real_connect = exporter_module.migrations.connect

    def connect(db_path):
        conn = real_connect(db_path)
        conn.set_trace_callback(captured.append)
        return conn

    monkeypatch.setattr(exporter_module.migrations, "connect", connect)
    return captured


@pytest.fixture
def fake(monkeypatch):
    """Включённый psycopg с фейковым соединением, ещё ничего не вызывавшим."""
    return install_psycopg(monkeypatch, FakePsycopg())


class TestDisabledExport:
    def test_pass_returns_empty_and_never_connects(self, db_path, fake):
        exporter = Exporter(db_path, settings(enabled=False))

        assert exporter.export_pass() == {}

        assert fake.connect_calls == [], "выключенный экспорт не открывает соединение"

    def test_disabled_exporter_survives_a_missing_psycopg(self, db_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "psycopg", None)  # ImportError при импорте

        exporter = Exporter(db_path, settings(enabled=False))

        assert exporter.export_pass() == {}


class TestConnection:
    def test_dsn_is_built_from_settings(self, db_path, fake):
        Exporter(db_path, settings(host="db.internal", port=6432, dbname="x", sslmode="require"))

        assert fake.connect_calls == [
            {
                "host": "db.internal",
                "port": 6432,
                "dbname": "x",
                "user": "adclicker",
                "password": "secret",
                "sslmode": "require",
                "connect_timeout": 5,
            }
        ]

    def test_missing_psycopg_is_a_readable_export_error(self, db_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "psycopg", None)

        with pytest.raises(ExportError) as excinfo:
            Exporter(db_path, settings())

        assert "psycopg" in str(excinfo.value)

    def test_connection_failure_becomes_export_error(self, db_path, monkeypatch):
        install_psycopg(monkeypatch, FakePsycopg(error=RuntimeError("нет маршрута")))

        with pytest.raises(ExportError) as excinfo:
            Exporter(db_path, settings())

        assert "нет маршрута" in str(excinfo.value)

    def test_close_closes_connection_and_is_idempotent(self, db_path, fake):
        exporter = Exporter(db_path, settings())

        exporter.close()
        exporter.close()

        assert fake.conn.events.count(("close",)) == 1

    def test_pass_after_close_raises_instead_of_silently_doing_nothing(self, db_path, fake):
        exporter = Exporter(db_path, settings())
        exporter.close()

        with pytest.raises(ExportError):
            exporter.export_pass()


class TestCursorExport:
    def test_select_uses_watermark_order_and_limit(self, db_path, fake, sqlite_sql):
        insert(db_path, "logs", ts=1.0, day="2026-10-10", level="INFO", message="ok")
        insert(db_path, "logs", ts=2.0, day="2026-10-10", level="ERROR", message="boom")
        exporter = Exporter(db_path, settings())

        exporter.export_pass(now=1000.0)

        selects = [sql for sql in sqlite_sql if "FROM logs WHERE id >" in sql]
        assert selects == [
            "SELECT id, ts, day, level, browser_id, category, message, fields, created_at "
            f"FROM logs WHERE id > 0 ORDER BY id LIMIT {DEFAULT_BATCH_SIZE}"
        ]

    def test_state_row_is_written_for_the_batch(self, db_path, fake):
        insert(db_path, "logs", ts=1.0, day="2026-10-10", level="INFO", message="ok")
        insert(db_path, "logs", ts=2.0, day="2026-10-10", level="ERROR", message="boom")
        exporter = Exporter(db_path, settings())

        exporter.export_pass(now=1000.0)

        assert params_for(fake.conn, "INTO export_state")[-1][:2] == ("logs", 2)
        assert fake.conn.state["logs"] == 2

    def test_data_and_cursor_share_one_commit(self, db_path, fake):
        insert(db_path, "logs", ts=1.0, day="2026-10-10", level="INFO", message="ok")
        exporter = Exporter(db_path, settings())

        exporter.export_pass(now=1000.0)

        events = fake.conn.events
        data_at = next(
            i for i, e in enumerate(events) if e[0] == "exec" and "INSERT INTO logs" in e[1]
        )
        state_at = next(
            i
            for i, e in enumerate(events)
            if e[0] == "exec" and "INTO export_state" in e[1] and e[2][0] == "logs"
        )
        commit_at = next(i for i, e in enumerate(events[data_at:], data_at) if e[0] == "commit")

        assert data_at < state_at < commit_at, "батч и курсор обязаны фиксироваться вместе"

    def test_second_pass_does_not_repeat_exported_rows(self, db_path, fake):
        insert(db_path, "logs", ts=1.0, day="2026-10-10", level="INFO", message="ok")
        insert(db_path, "logs", ts=2.0, day="2026-10-10", level="ERROR", message="boom")
        exporter = Exporter(db_path, settings())

        first = exporter.export_pass(now=1000.0)
        fake.conn.events.clear()
        second = exporter.export_pass(now=2000.0)

        assert first["logs"] == 2
        assert second["logs"] == 0, "курсор не должен отдавать уже выгруженное"
        assert not any("INSERT INTO logs" in sql for sql in fake.conn.statements)

    def test_summary_covers_every_table_in_the_contract(self, db_path, fake):
        exporter = Exporter(db_path, settings())

        summary = exporter.export_pass(now=1000.0)

        assert set(summary) == set(EXPECTED_TABLES) == set(EXPORT_TABLES)
        assert all(isinstance(count, int) and count == 0 for count in summary.values())

    def test_failed_batch_rolls_back_and_keeps_the_cursor(self, db_path, monkeypatch):
        insert(db_path, "logs", ts=1.0, day="2026-10-10", level="INFO", message="ok")
        conn = FakeConnection(fail_on="INSERT INTO logs")
        install_psycopg(monkeypatch, FakePsycopg(conn=conn))
        exporter = Exporter(db_path, settings())

        with pytest.raises(ExportError) as excinfo:
            exporter.export_pass(now=1000.0)

        assert "PostgreSQL недоступен" in str(excinfo.value)
        assert ("rollback",) in conn.events, "упавшая транзакция обязана быть погашена"
        assert "logs" not in conn.state, "курсор не двигается при сбое"


class TestSnapshots:
    @pytest.mark.parametrize(
        ("table", "conflict"),
        [
            ("proxies", "id"),
            ("profiles", "id"),
            ("workers", "browser_id"),
            ("metrics_hourly", "bucket"),
        ],
    )
    def test_snapshot_tables_are_upserted_whole(self, db_path, fake, table, conflict):
        seed_one_row_per_table(db_path)
        exporter = Exporter(db_path, settings())

        exporter.export_pass(now=1000.0)

        inserts = [sql for sql in fake.conn.statements if sql.startswith(f"INSERT INTO {table} ")]
        assert inserts, f"{table} должна выгружаться как снимок"
        assert f"ON CONFLICT ({conflict}) DO UPDATE SET" in inserts[0]

    def test_snapshot_select_has_no_watermark(self, db_path, fake, sqlite_sql):
        exporter = Exporter(db_path, settings())

        exporter.export_pass(now=1000.0)

        assert not any(
            "FROM workers WHERE id >" in sql for sql in sqlite_sql
        ), "справочники выгружаются целиком, без курсора"

    def test_proxy_credentials_are_never_read_or_written(self, db_path, fake, sqlite_sql):
        insert(
            db_path,
            "proxies",
            label="main",
            scheme="http",
            host="1.2.3.4",
            port=8080,
            username="login",
            password="proxy-secret",
        )
        exporter = Exporter(db_path, settings())

        exporter.export_pass(now=1000.0)

        select = next(sql for sql in sqlite_sql if sql.startswith("SELECT ") and "FROM proxies" in sql)
        assert "username" not in select and "password" not in select

        columns = columns_of("proxies")
        params = params_for(fake.conn, "INSERT INTO proxies ")[0]
        assert params[columns.index("username")] is None
        assert params[columns.index("password")] is None
        assert "proxy-secret" not in repr(params)

    def test_workers_conflict_is_browser_id(self, db_path, fake):
        insert(db_path, "workers", browser_id="br-1", status="running")
        exporter = Exporter(db_path, settings())

        exporter.export_pass(now=1000.0)

        columns = columns_of("workers")
        params = params_for(fake.conn, "INSERT INTO workers ")[0]
        assert params[columns.index("browser_id")] == "br-1"
        assert fake.conn.state["workers"] == params[columns.index("id")]

    def test_metrics_hourly_is_keyed_by_bucket(self, db_path, fake):
        insert(
            db_path,
            "metrics_hourly",
            bucket=1_700_000_000,
            successes=3,
            failures=1,
            requests=40,
            captchas=2,
            uptime_seconds=3600,
        )
        exporter = Exporter(db_path, settings())

        summary = exporter.export_pass(now=1000.0)

        assert summary["metrics_hourly"] == 1
        assert fake.conn.state["metrics_hourly"] == 1_700_000_000


class TestRunningRuns:
    def test_running_rows_are_reexported_with_the_batch_limit(self, db_path, fake, sqlite_sql):
        for index in range(30):
            insert(db_path, "runs", status="running", started_at=float(index))
        exporter = Exporter(db_path, settings(batch_size=25))

        summary = exporter.export_pass(now=1000.0)

        running = [sql for sql in sqlite_sql if "FROM runs WHERE status = 'running'" in sql]
        assert running == [
            "SELECT id, worker_id, started_at, ended_at, status, error, total_clicks, "
            "captcha_seen, captcha_solved, created_at FROM runs "
            "WHERE status = 'running' ORDER BY id LIMIT 25"
        ]
        # Обе порции (курсор и повтор) ограничены batch_size: 25 строк курсора
        # и те же 25 живых запусков — строк в счётчике 25, а не 50.
        assert summary["runs"] == 25

    def test_finished_run_is_moved_by_the_cursor_only(self, db_path, fake, sqlite_sql):
        insert(db_path, "runs", status="ok", started_at=1.0, ended_at=2.0)
        exporter = Exporter(db_path, settings())

        summary = exporter.export_pass(now=1000.0)

        assert summary["runs"] == 1
        # Повтор живых запусков запрос выполняет (это его работа), но строк
        # со status='running' не находит — в PG уходит ровно батч курсора.
        assert any("FROM runs WHERE status = 'running'" in sql for sql in sqlite_sql)
        assert len(params_for(fake.conn, "INSERT INTO runs ")) == 1


class TestOrderAndBatches:
    def test_table_order_follows_the_contract(self, db_path, fake):
        seed_one_row_per_table(db_path)
        exporter = Exporter(db_path, settings())

        exporter.export_pass(now=1000.0)

        first_seen = []
        for sql in fake.conn.statements:
            if not sql.startswith("INSERT INTO ") or "INTO export_state" in sql:
                continue
            table = sql.split()[2]
            if table not in first_seen:
                first_seen.append(table)

        assert first_seen == list(EXPECTED_TABLES)
        assert EXPORT_TABLES == EXPECTED_TABLES

    def test_one_commit_per_non_empty_table(self, db_path, fake):
        insert(db_path, "logs", ts=1.0, day="2026-10-10", level="INFO", message="ok")
        insert(db_path, "clicks", ts=1.0, url="https://x", query="q")
        exporter = Exporter(db_path, settings())

        exporter.export_pass(now=1000.0)

        # Транзакция чтения export_state плюс по одному коммиту на батч.
        assert fake.conn.commits == 3

    def test_idle_pass_touches_only_the_state_select(self, db_path, fake):
        exporter = Exporter(db_path, settings())

        exporter.export_pass(now=1000.0)

        assert not any(sql.startswith("INSERT INTO") for sql in fake.conn.statements)
        assert fake.conn.commits == 1


class TestReferenceDdl:
    """Справочный DDL собран из тех же спецификаций, что и SQL экспорта."""

    @staticmethod
    def _ddl() -> str:
        from engine.exporter import _REFERENCE_DDL

        return "\n".join(_REFERENCE_DDL)

    def test_ddl_covers_every_table_and_the_cursor(self):
        ddl = self._ddl()

        for table in EXPORT_TABLES:
            assert f"CREATE TABLE IF NOT EXISTS {table} (" in ddl
        assert "export_state" in ddl
        assert "last_id bigint NOT NULL DEFAULT 0" in ddl

    def test_ddl_has_no_foreign_keys_and_marks_the_primary_keys(self):
        ddl = self._ddl()

        assert "REFERENCES" not in ddl
        assert "browser_id text PRIMARY KEY" in ddl
        assert "bucket bigint PRIMARY KEY" in ddl

    def test_cursor_tables_declare_id_as_primary_key(self):
        from engine.exporter import _REFERENCE_DDL

        for table in ("logs", "clicks", "runs", "diagnostics"):
            ddl = next(
                statement
                for statement in _REFERENCE_DDL
                if statement.startswith(f"CREATE TABLE IF NOT EXISTS {table} ")
            )
            assert "id bigint PRIMARY KEY" in ddl


class TestSpecsMatchTheSourceSchema:
    """Зеркало в PG обязано быть 1:1 с ``engine/db/schema.sql`` (план §9.2).

    Типы берутся по правилу плана: ``REAL`` → ``double precision``,
    ``INTEGER`` → ``bigint``, ``TEXT`` → ``text``, FK в PG нет. Если в
    schema.sql появится колонка, а в спецификации экспортера — нет, выгрузка
    молча перестанет её зеркалить; этот тест обязан поймать такое первым.
    """

    PG_TYPES = {"INTEGER": "bigint", "REAL": "double precision", "TEXT": "text"}

    @pytest.mark.parametrize("table", sorted(EXPORT_TABLES))
    def test_columns_and_types_follow_schema_sql(self, table):
        from engine.exporter import _SPECS

        source = dict(sqlite_schema_columns())[table]

        assert list(_SPECS[table].columns) == [
            (name, self.PG_TYPES[sqlite_type]) for name, sqlite_type in source
        ]

    @pytest.mark.parametrize("table", sorted(EXPORT_TABLES))
    def test_conflict_column_exists_in_the_table(self, table):
        from engine.exporter import _SPECS

        assert _SPECS[table].conflict in columns_of(table)

    def test_kv_and_legacy_tables_are_not_exported(self):
        assert "kv" not in EXPORT_TABLES
        assert set(EXPORT_TABLES) <= set(sqlite_schema_columns())
