"""Тесты харнеса baseline-замера.

Харнес не запускает браузер: он считает клики из clicklogs.db за окно
замера и пишет отчёт + строку в таблицу runs новой схемы, чтобы цифры
до и после рефакторинга были прямо сравнимы. Сами цифры снимаются на
стенде (процедура в docs/baseline.md), здесь проверяется только арифметика.
"""

import json
import sqlite3
from datetime import datetime

import pytest

from engine import baseline


def _make_clicklogs_db(path, rows):
    """Синтетический clicklogs.db legacy-формата: (date, time, url, query, category)."""

    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE clicklogs (
                click_date TEXT, click_time TEXT, site_url TEXT,
                query TEXT, category TEXT)"""
        )
        conn.executemany(
            "INSERT INTO clicklogs VALUES (?, ?, ?, ?, ?)", rows
        )
        conn.commit()


def _ts(day="01-01-2026", time="12:00:00"):
    return datetime.strptime(f"{day} {time}", "%d-%m-%Y %H:%M:%S").timestamp()


@pytest.fixture
def clicklogs_db(tmp_path):
    path = tmp_path / "clicklogs.db"
    _make_clicklogs_db(
        path,
        [
            ("01-01-2026", "12:00:00", "https://a.example/", "q1", "Ad"),
            ("01-01-2026", "12:30:00", "https://b.example/", "q2", "Ad"),
            ("01-01-2026", "13:00:00", "https://c.example/", "q3", "Non-ad"),
        ],
    )
    return path


class TestCollectClicks:
    def test_counts_only_clicks_inside_the_window(self, clicklogs_db):
        clicks = baseline.collect_clicks(clicklogs_db, _ts(time="12:00:00"), _ts(time="12:59:59"))

        assert [c["url"] for c in clicks] == ["https://a.example/", "https://b.example/"]

    def test_window_boundaries_are_inclusive(self, clicklogs_db):
        clicks = baseline.collect_clicks(clicklogs_db, _ts(time="12:30:00"), _ts(time="12:30:00"))

        assert len(clicks) == 1
        assert clicks[0]["query"] == "q2"

    def test_empty_window_gives_no_clicks(self, clicklogs_db):
        assert baseline.collect_clicks(clicklogs_db, _ts(time="14:00:00"), _ts(time="15:00:00")) == []

    def test_malformed_rows_are_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "clicklogs.db"
        _make_clicklogs_db(
            path,
            [
                ("01-01-2026", "12:00:00", "https://a.example/", "q1", "Ad"),
                ("not-a-date", "not-a-time", "https://bad.example/", "q", "Ad"),
                ("01-01-2026", "12:10:00", "https://b.example/", "q2", "Ad"),
            ],
        )

        clicks = baseline.collect_clicks(path, _ts(time="11:00:00"), _ts(time="13:00:00"))

        assert [c["url"] for c in clicks] == ["https://a.example/", "https://b.example/"]

    def test_missing_table_means_no_clicks_not_a_crash(self, tmp_path):
        path = tmp_path / "empty.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE other (x TEXT)")

        assert baseline.collect_clicks(path, 0, 9999999999) == []


class TestClicksPerHour:
    def test_rate_math(self):
        assert baseline.clicks_per_hour([{}, {}, {}], 0.0, 3600.0) == 3.0
        assert baseline.clicks_per_hour([{}], 0.0, 1800.0) == 2.0

    def test_zero_duration_gives_zero_not_division_error(self):
        assert baseline.clicks_per_hour([{}], 100.0, 100.0) == 0.0


class TestReport:
    def test_report_carries_everything_needed_for_comparison(self, clicklogs_db, tmp_path):
        report = baseline.build_report(
            clicklogs_db_path=clicklogs_db,
            started_at=_ts(time="12:00:00"),
            ended_at=_ts(time="13:00:00"),
            captcha_seen=2,
            captcha_solved=1,
            notes="stand, 2 workers, direct IP",
        )

        assert report["clicks"] == 3
        assert report["clicks_per_hour"] == 3.0
        assert report["duration_s"] == 3600.0
        assert report["captcha_seen"] == 2
        assert report["captcha_solved"] == 1
        assert report["notes"] == "stand, 2 workers, direct IP"

    def test_report_is_json_serializable(self, clicklogs_db, tmp_path):
        report = baseline.build_report(
            clicklogs_db_path=clicklogs_db,
            started_at=_ts(time="12:00:00"),
            ended_at=_ts(time="13:00:00"),
        )

        out = tmp_path / "baseline.json"
        out.write_text(json.dumps(report), encoding="utf-8")

        assert json.loads(out.read_text(encoding="utf-8"))["clicks"] == 3

    def test_report_without_captcha_data_marks_it_unknown(self, clicklogs_db):
        report = baseline.build_report(
            clicklogs_db_path=clicklogs_db,
            started_at=_ts(time="12:00:00"),
            ended_at=_ts(time="13:00:00"),
        )

        assert report["captcha_seen"] is None
        assert report["captcha_solved"] is None


class TestRecordBaselineRun:
    def test_run_lands_in_runs_table(self, clicklogs_db, tmp_path):
        from engine.db import migrations

        db_path = tmp_path / "adclicker.db"
        migrations.migrate(db_path)
        report = baseline.build_report(
            clicklogs_db_path=clicklogs_db,
            started_at=_ts(time="12:00:00"),
            ended_at=_ts(time="13:00:00"),
            captcha_seen=2,
            captcha_solved=1,
        )

        row_id = baseline.record_baseline_run(db_path, report)

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT started_at, ended_at, status, total_clicks,"
                " captcha_seen, captcha_solved FROM runs WHERE id = ?",
                (row_id,),
            ).fetchone()

        assert row[2] == "baseline"
        assert row[3] == 3
        assert row[4] == 2
        assert row[5] == 1

    def test_unknown_captcha_is_stored_as_zero_not_null(self, clicklogs_db, tmp_path):
        from engine.db import migrations

        db_path = tmp_path / "adclicker.db"
        migrations.migrate(db_path)
        report = baseline.build_report(
            clicklogs_db_path=clicklogs_db,
            started_at=_ts(time="12:00:00"),
            ended_at=_ts(time="13:00:00"),
        )

        row_id = baseline.record_baseline_run(db_path, report)

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT captcha_seen, captcha_solved FROM runs WHERE id = ?",
                (row_id,),
            ).fetchone()

        assert row == (0, 0)
