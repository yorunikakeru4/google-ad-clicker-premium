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
from engine.log_rotation import export_day, local_day


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
