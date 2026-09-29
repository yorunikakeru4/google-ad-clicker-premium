"""Тесты дневного экспорта логов (engine/log_rotation.py).

Экспорт — единственное место, которое превращает строки ``logs`` за день в
``logs/YYYY-MM-DD.log``. Здесь проверяется то, что нельзя сломать незаметно:
формат строки, граница уровня, идемпотентность (повторный запуск
перезаписывает, а не дописывает), изоляция одного дня и поведение на пустом
дне.

Файлы экспорта всегда создаются внутри ``tmp_path``: тест не имеет права
оставить ничего в ``logs/`` репозитория.
"""

from __future__ import annotations

import re
import sqlite3
import time

import pytest

from engine.db import migrations
from engine.log_rotation import (
    db_size_bytes,
    enforce_db_size_limit,
    export_day,
    local_day,
    retention_cutoff,
    run_retention,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


@pytest.fixture
def export_dir(tmp_path):
    path = tmp_path / "logs"
    return path


def _insert(
    db_path,
    *,
    day,
    ts,
    level="INFO",
    message="message",
    browser_id="br-1",
    category="click",
    fields=None,
):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO logs (ts, day, level, browser_id, category, message, fields) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, day, level, browser_id, category, message, fields),
        )
        conn.commit()


# Формат строки: ISO-время с смещением зоны, уровень в квадратных скобках,
# воркер, категория и сообщение. Поля JSON, если есть, приклеиваются в конец.
LINE_RE = re.compile(
    r"^(?P<iso>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}) "
    r"\[(?P<level>[A-Z]+)\] (?P<browser>\S+) (?P<category>\S+): (?P<rest>.*)$"
)


class TestExportDay:
    def test_writes_one_file_named_after_the_day(self, db_path, export_dir):
        _insert(db_path, day="2024-04-01", ts=1711954800.0, message="hello")

        path = export_day(db_path, "2024-04-01", export_dir)

        assert path == export_dir / "2024-04-01.log"
        assert path.is_file()
        assert [p.name for p in export_dir.iterdir()] == ["2024-04-01.log"]

    def test_line_matches_the_documented_format(self, db_path, export_dir):
        _insert(
            db_path,
            day="2024-04-01",
            ts=1711954800.0,
            level="WARNING",
            message="slow proxy",
            browser_id="br-7",
            category="proxy",
            fields='{"latency_ms": 900}',
        )

        path = export_day(db_path, "2024-04-01", export_dir)
        lines = path.read_text(encoding="utf-8").splitlines()

        assert len(lines) == 1
        match = LINE_RE.match(lines[0])
        assert match is not None, f"строка не соответствует формату: {lines[0]!r}"
        assert match["iso"].startswith("2024-04-01"), "время берётся из ts записи"
        assert match["level"] == "WARNING"
        assert match["browser"] == "br-7"
        assert match["category"] == "proxy"
        assert match["rest"] == 'slow proxy {"latency_ms": 900}'

    def test_optional_columns_render_as_placeholders(self, db_path, export_dir):
        _insert(
            db_path,
            day="2024-04-01",
            ts=1711954800.0,
            browser_id=None,
            category=None,
            fields=None,
        )

        path = export_day(db_path, "2024-04-01", export_dir)
        line = path.read_text(encoding="utf-8").splitlines()[0]

        assert re.search(r"\[INFO\] - -: message$", line), line

    def test_min_level_keeps_info_and_above(self, db_path, export_dir):
        for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            _insert(
                db_path,
                day="2024-04-01",
                ts=1711954800.0,
                level=level,
                message=f"msg-{level}",
            )

        path = export_day(db_path, "2024-04-01", export_dir, min_level="INFO")
        body = path.read_text(encoding="utf-8")

        assert "msg-DEBUG" not in body, "DEBUG должен остаться только в БД"
        assert "msg-INFO" in body
        assert "msg-WARNING" in body
        assert "msg-ERROR" in body

    def test_min_level_debug_exports_everything(self, db_path, export_dir):
        for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            _insert(
                db_path,
                day="2024-04-01",
                ts=1711954800.0,
                level=level,
                message=f"msg-{level}",
            )

        path = export_day(db_path, "2024-04-01", export_dir, min_level="DEBUG")
        body = path.read_text(encoding="utf-8")

        assert all(f"msg-{level}" in body for level in ("DEBUG", "INFO", "WARNING", "ERROR"))

    def test_min_level_error_keeps_only_errors(self, db_path, export_dir):
        for level in ("INFO", "ERROR"):
            _insert(
                db_path,
                day="2024-04-01",
                ts=1711954800.0,
                level=level,
                message=f"msg-{level}",
            )

        path = export_day(db_path, "2024-04-01", export_dir, min_level="ERROR")
        body = path.read_text(encoding="utf-8")

        assert "msg-INFO" not in body
        assert "msg-ERROR" in body

    def test_unknown_level_is_kept_as_the_most_serious(self, db_path, export_dir):
        """Строка не из словаря уровней не должна молча выпасть из экспорта."""
        _insert(db_path, day="2024-04-01", ts=1711954800.0, level="CRITICAL", message="odd")

        path = export_day(db_path, "2024-04-01", export_dir, min_level="ERROR")

        assert "odd" in path.read_text(encoding="utf-8")

    def test_only_the_requested_day_is_exported(self, db_path, export_dir):
        _insert(db_path, day="2024-03-31", ts=1711868400.0, message="yesterday")
        _insert(db_path, day="2024-04-01", ts=1711954800.0, message="today")
        _insert(db_path, day="2024-04-02", ts=1712041200.0, message="tomorrow")

        path = export_day(db_path, "2024-04-01", export_dir)
        body = path.read_text(encoding="utf-8")

        assert body.count("\n") == 1
        assert "today" in body
        assert "yesterday" not in body
        assert "tomorrow" not in body

    def test_rows_come_out_ordered_by_timestamp(self, db_path, export_dir):
        _insert(db_path, day="2024-04-01", ts=1711962000.0, message="second")
        _insert(db_path, day="2024-04-01", ts=1711954800.0, message="first")

        path = export_day(db_path, "2024-04-01", export_dir)
        body = path.read_text(encoding="utf-8")

        assert body.index("first") < body.index("second")

    def test_repeated_export_rewrites_instead_of_appending(self, db_path, export_dir):
        _insert(db_path, day="2024-04-01", ts=1711954800.0, message="once")

        first = export_day(db_path, "2024-04-01", export_dir)
        first_body = first.read_text(encoding="utf-8")
        second = export_day(db_path, "2024-04-01", export_dir)

        assert second == first
        assert second.read_text(encoding="utf-8") == first_body
        assert first_body.count("once") == 1, "повторный запуск не должен дублировать строки"

    def test_export_picks_up_rows_written_after_the_first_run(self, db_path, export_dir):
        _insert(db_path, day="2024-04-01", ts=1711954800.0, message="first")
        export_day(db_path, "2024-04-01", export_dir)
        _insert(db_path, day="2024-04-01", ts=1711954900.0, message="late")

        path = export_day(db_path, "2024-04-01", export_dir)
        body = path.read_text(encoding="utf-8")

        assert "first" in body
        assert "late" in body

    def test_empty_day_creates_nothing_and_returns_none(self, db_path, export_dir):
        _insert(db_path, day="2024-04-02", ts=1712041200.0, message="other day")

        path = export_day(db_path, "2024-04-01", export_dir)

        assert path is None, "пустой день не должен оставлять файл-индикатор"
        assert not export_dir.exists() or list(export_dir.iterdir()) == []

    def test_day_filtered_out_by_level_is_treated_as_empty(self, db_path, export_dir):
        _insert(db_path, day="2024-04-01", ts=1711954800.0, level="DEBUG", message="quiet")

        path = export_day(db_path, "2024-04-01", export_dir, min_level="INFO")

        assert path is None
        assert not export_dir.exists() or list(export_dir.iterdir()) == []

    def test_malformed_day_is_rejected(self, db_path, export_dir):
        for bad in ("01-04-2024", "2024-4-1", "2024-04-01 ", "yesterday"):
            with pytest.raises(ValueError):
                export_day(db_path, bad, export_dir)

    def test_unknown_min_level_is_rejected(self, db_path, export_dir):
        _insert(db_path, day="2024-04-01", ts=1711954800.0)

        with pytest.raises(ValueError):
            export_day(db_path, "2024-04-01", export_dir, min_level="TRACE")

    def test_message_newlines_do_not_break_one_record_per_line(self, db_path, export_dir):
        _insert(db_path, day="2024-04-01", ts=1711954800.0, message="first\nsecond")

        path = export_day(db_path, "2024-04-01", export_dir)
        lines = path.read_text(encoding="utf-8").splitlines()

        assert len(lines) == 1
        assert "first\\nsecond" in lines[0]


class TestLocalDay:
    def test_formats_the_local_date_of_the_timestamp(self):
        ts = 1711954800.0

        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", local_day(ts))
        assert local_day(ts) == local_day(ts + 0.5)

    def test_differs_across_local_midnight(self):
        before = time.mktime((2024, 3, 31, 23, 59, 59, 0, 0, -1))
        after = time.mktime((2024, 4, 1, 0, 0, 0, 0, 0, -1))

        assert local_day(before) == "2024-03-31"
        assert local_day(after) == "2024-04-01"


class TestRetentionCutoff:
    """Отсечка retention: ровно N полных дней назад ещё живут."""

    def test_cutoff_is_n_days_before_today(self):
        assert retention_cutoff("2024-04-30", 30) == "2024-03-31"

    def test_cutoff_crosses_month_and_year_boundaries(self):
        assert retention_cutoff("2024-01-01", 1) == "2023-12-31"
        assert retention_cutoff("2024-03-01", 29) == "2024-02-01"

    def test_single_day_retention_keeps_yesterday(self):
        assert retention_cutoff("2024-04-30", 1) == "2024-04-29"

    def test_non_positive_retention_is_rejected(self):
        for days in (0, -1):
            with pytest.raises(ValueError):
                retention_cutoff("2024-04-30", days)


class TestRunRetention:
    """Граница удаления: строго старше N дней — в мусор, ровно N — остаётся."""

    @pytest.fixture
    def dated_db(self, db_path):
        for day in ("2024-03-29", "2024-03-30", "2024-03-31", "2024-04-01"):
            _insert(db_path, day=day, ts=1711954800.0, message=f"row-{day}")
        return db_path

    @pytest.fixture
    def export_files(self, export_dir):
        export_dir.mkdir(parents=True)
        for name in ("2024-03-29.log", "2024-03-30.log", "2024-03-31.log", "2024-04-01.log"):
            (export_dir / name).write_text("day\n", encoding="utf-8")
        (export_dir / "adclicker.log").write_text("legacy\n", encoding="utf-8")
        (export_dir / "adclicker.log.1").write_text("legacy rotated\n", encoding="utf-8")
        (export_dir / "notes.txt").write_text("не лог\n", encoding="utf-8")
        return export_dir

    def _days_left(self, db_path):
        with sqlite3.connect(db_path) as conn:
            return [row[0] for row in conn.execute("SELECT DISTINCT day FROM logs ORDER BY day")]

    def test_rows_exactly_n_days_old_survive(self, dated_db, export_dir):
        """N=30 при сегодня 2024-04-30: 2024-03-31 — ровно 30 дней, остаётся."""
        result = run_retention(dated_db, "2024-04-30", 30, export_dir)

        assert result.cutoff == "2024-03-31"
        assert "2024-03-31" in self._days_left(dated_db)

    def test_rows_strictly_older_than_n_days_are_deleted(self, dated_db, export_dir):
        result = run_retention(dated_db, "2024-04-30", 30, export_dir)

        assert self._days_left(dated_db) == ["2024-03-31", "2024-04-01"]
        assert result.deleted_rows == 2, "строки двух удалённых дней"

    def test_export_files_follow_the_same_boundary(self, dated_db, export_files):
        run_retention(dated_db, "2024-04-30", 30, export_files)

        assert sorted(p.name for p in export_files.iterdir()) == [
            "2024-03-31.log",
            "2024-04-01.log",
            "adclicker.log",
            "adclicker.log.1",
            "notes.txt",
        ]

    def test_files_and_rows_are_reported_together(self, dated_db, export_files):
        result = run_retention(dated_db, "2024-04-30", 30, export_files)

        assert sorted(p.name for p in result.deleted_files) == [
            "2024-03-29.log",
            "2024-03-30.log",
        ]
        assert result.deleted_rows == 2

    def test_one_day_retention_deletes_only_the_day_before_yesterday(
        self, dated_db, export_dir
    ):
        result = run_retention(dated_db, "2024-04-01", 1, export_dir)

        assert result.cutoff == "2024-03-31"
        assert self._days_left(dated_db) == ["2024-03-31", "2024-04-01"]

    def test_nothing_to_delete_is_a_quiet_no_op(self, db_path, export_dir):
        _insert(db_path, day="2024-04-01", ts=1711954800.0, message="fresh")

        result = run_retention(db_path, "2024-04-01", 30, export_dir)

        assert result.deleted_rows == 0
        assert result.deleted_files == []
        assert self._days_left(db_path) == ["2024-04-01"]

    def test_missing_export_dir_is_not_an_error(self, db_path, tmp_path):
        _insert(db_path, day="2024-04-01", ts=1711954800.0)

        result = run_retention(db_path, "2024-04-01", 30, tmp_path / "nope")

        assert result.deleted_files == []

    def test_rows_without_day_are_not_silently_dropped(self, db_path):
        """NULL в day — баг писателя, а не повод удалять запись молча."""
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO logs (ts, day, level, message) VALUES (?, NULL, 'INFO', 'x')",
                (1711954800.0,),
            )
            conn.commit()

        result = run_retention(db_path, "2024-04-01", 1, None)

        assert result.deleted_rows == 0

    def test_non_positive_retention_is_rejected(self, db_path):
        with pytest.raises(ValueError):
            run_retention(db_path, "2024-04-01", 0)


class TestDbSizeLimit:
    """Защита от роста: старые дни логов уходят, пока БД не влезет в лимит."""

    def _payload_db(self, db_path, days):
        """БД с указанными днями (~1 МБ строк на день); возвращает её размер."""
        with sqlite3.connect(db_path) as conn:
            for day in days:
                ts = time.mktime((2024, 4, 1, 12, 0, 0, 0, 0, -1))
                conn.executemany(
                    "INSERT INTO logs (ts, day, level, browser_id, category, message) "
                    "VALUES (?, ?, 'INFO', 'br-1', 'click', ?)",
                    [(ts + i, day, "x" * 500) for i in range(2500)],
                )
            conn.commit()
        return db_size_bytes(db_path)

    def _reference_db(self, tmp_path, name, days):
        """Отдельная БД с теми же днями — источник правдоподобного размера.

        Лимит в тестах выбирается между измеренными размерами разных наборов
        дней, а не вычисляется из «примерно мегабайт»: так тест не зависит от
        того, сколько страниц займут строки на данной машине.
        """
        path = tmp_path / name
        migrations.migrate(path)
        return self._payload_db(path, days)

    def test_size_counts_the_database_and_its_wal(self, tmp_path):
        db_path = tmp_path / "sized.db"
        migrations.migrate(db_path)
        base = db_size_bytes(db_path)
        (tmp_path / "sized.db-wal").write_bytes(b"0" * 4096)

        assert db_size_bytes(db_path) == base + 4096

    def test_limit_zero_is_rejected(self, db_path):
        with pytest.raises(ValueError):
            enforce_db_size_limit(db_path, 0)

    def test_database_already_within_the_limit_is_untouched(self, db_path):
        _insert(db_path, day="2024-04-01", ts=1711954800.0)

        result = enforce_db_size_limit(db_path, 100)

        assert result.fits is True
        assert result.deleted_days == ()
        assert result.error is None

    def test_deletes_the_oldest_day_and_stops_when_it_fits(self, tmp_path, db_path, export_dir):
        days = ("2024-04-01", "2024-04-02", "2024-04-03")
        export_dir.mkdir(parents=True)
        for day in days:
            (export_dir / f"{day}.log").write_text("day\n", encoding="utf-8")
        initial = self._payload_db(db_path, days)
        without_oldest = self._reference_db(tmp_path, "two.db", days[1:])
        # Лимит между размерами «три дня» и «два дня»: в БД не влезает, но
        # выкинуть достаточно ровно один день.
        limit_mb = (initial + without_oldest) / 2 / (1024 * 1024)
        assert initial > without_oldest

        result = enforce_db_size_limit(db_path, limit_mb, export_dir=export_dir)

        assert result.fits is True, result
        assert result.deleted_days == ("2024-04-01",), "удаляются только самые старые дни"
        assert result.size_bytes <= result.limit_bytes
        assert not (export_dir / "2024-04-01.log").exists()
        assert (export_dir / "2024-04-02.log").exists()
        with sqlite3.connect(db_path) as conn:
            left = {row[0] for row in conn.execute("SELECT DISTINCT day FROM logs")}
        assert left == {"2024-04-02", "2024-04-03"}

    def test_keeps_deleting_until_the_database_fits(self, tmp_path, db_path):
        days = ("2024-04-01", "2024-04-02", "2024-04-03")
        initial = self._payload_db(db_path, days)
        two_days = self._reference_db(tmp_path, "two.db", days[1:])
        one_day = self._reference_db(tmp_path, "one.db", days[2:])
        # Лимит между «двумя днями» и «одним»: одного удалённого дня мало.
        limit_mb = (two_days + one_day) / 2 / (1024 * 1024)
        assert initial > two_days > one_day

        result = enforce_db_size_limit(db_path, limit_mb)

        assert result.fits is True, result
        assert result.deleted_days == ("2024-04-01", "2024-04-02")
        with sqlite3.connect(db_path) as conn:
            left = {row[0] for row in conn.execute("SELECT DISTINCT day FROM logs")}
        assert left == {"2024-04-03"}

    def test_reports_failure_when_even_an_empty_database_does_not_fit(self, db_path):
        result = enforce_db_size_limit(db_path, 0.001)

        assert result.fits is False, "лимит мельче пустой БД — защита обязана сказать об этом"
        assert result.deleted_days == (), "удалять нечего: строк дня нет"
        assert result.size_bytes > result.limit_bytes

    def test_deletes_every_day_when_nothing_fits_and_reports_it(self, db_path, export_dir):
        export_dir.mkdir(parents=True)
        for day in ("2024-04-01", "2024-04-02"):
            _insert(db_path, day=day, ts=1711954800.0, message="row")
            (export_dir / f"{day}.log").write_text("day\n", encoding="utf-8")

        result = enforce_db_size_limit(db_path, 0.001, export_dir=export_dir)

        assert result.fits is False
        assert result.deleted_days == ("2024-04-01", "2024-04-02")
        assert sorted(p.name for p in export_dir.iterdir()) == []
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0] == 0

    def test_negative_limit_is_rejected(self, db_path):
        with pytest.raises(ValueError):
            enforce_db_size_limit(db_path, -1)
