"""Харнес baseline-замера: клики/час и капча на текущем коде.

Не запускает браузер. Считает клики из legacy clicklogs.db за окно замера
и собирает отчёт, который пишется в baseline.json и в таблицу runs новой
схемы — чтобы цифры до и после рефакторинга сравнивались напрямую.

Сами цифры снимаются на стенде с реальным Chrome, прокси и запросами
(процедура в docs/baseline.md): здесь только арифметика подсчёта.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_LEGACY_DATETIME_FORMAT = "%d-%m-%Y %H:%M:%S"


def _parse_legacy_timestamp(click_date: str, click_time: str) -> float | None:
    """Unix-время строки legacy-формата. Мусор в БД — не повод ронять замер."""

    try:
        return datetime.strptime(
            f"{click_date} {click_time}", _LEGACY_DATETIME_FORMAT
        ).timestamp()
    except (ValueError, TypeError):
        logger.warning("Пропущена битая строка clicklogs: %r %r", click_date, click_time)
        return None


def collect_clicks(
    clicklogs_db_path: str | Path, started_at: float, ended_at: float
) -> list[dict]:
    """Клики из clicklogs.db внутри окна [started_at, ended_at], границы включительно."""

    clicks: list[dict] = []
    with sqlite3.connect(clicklogs_db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "clicklogs" not in tables:
            return []

        for click_date, click_time, site_url, query, category in conn.execute(
            "SELECT click_date, click_time, site_url, query, category FROM clicklogs"
        ):
            ts = _parse_legacy_timestamp(click_date, click_time)
            if ts is None:
                continue
            if started_at <= ts <= ended_at:
                clicks.append(
                    {
                        "ts": ts,
                        "url": site_url,
                        "query": query,
                        "category": category,
                    }
                )

    return clicks


def clicks_per_hour(clicks: list[dict], started_at: float, ended_at: float) -> float:
    """Средняя скорость. Нулевая длительность — ноль, а не деление на ноль."""

    duration_s = ended_at - started_at
    if duration_s <= 0:
        return 0.0
    return len(clicks) / (duration_s / 3600.0)


def build_report(
    clicklogs_db_path: str | Path,
    started_at: float,
    ended_at: float,
    captcha_seen: int | None = None,
    captcha_solved: int | None = None,
    notes: str = "",
) -> dict:
    """Отчёт замера. Капча — из финальной статистики стенда, если её сняли.

    Legacy-код держит captcha_seen/solved только в памяти контроллера, в БД
    они не пишутся. Поэтому харнес их не выдумывает: нет данных — None,
    а не ноль. Ноль означал бы «капчи не было», None — «не измерялось».
    """

    clicks = collect_clicks(clicklogs_db_path, started_at, ended_at)
    return {
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_s": ended_at - started_at,
        "clicks": len(clicks),
        "clicks_per_hour": clicks_per_hour(clicks, started_at, ended_at),
        "captcha_seen": captcha_seen,
        "captcha_solved": captcha_solved,
        "notes": notes,
    }


def record_baseline_run(db_path: str | Path, report: dict) -> int:
    """Строка в таблице runs новой схемы. Возвращает id строки.

    Ожидается, что схема уже применена через engine.db.migrations.migrate().
    """

    from engine.db import migrations

    with migrations.connect(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO runs
               (started_at, ended_at, status, total_clicks, captcha_seen, captcha_solved)
               VALUES (?, ?, 'baseline', ?, ?, ?)""",
            (
                report["started_at"],
                report["ended_at"],
                report["clicks"],
                report["captcha_seen"] or 0,
                report["captcha_solved"] or 0,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
