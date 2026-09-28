"""Тесты структурированного логгера (engine/log.py).

Контракт:

- категории и уровни — закрытый словарь, неизвестная категория/уровень —
  ошибка программирования (ValueError);
- fields уходят в БД JSON-строкой, несериализуемое превращается в repr;
- путь логгирования никогда не бросает исключений: сломанный store под
  логгером — счётчик + последняя ошибка, воркер продолжает работать.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from engine.db import migrations
from engine.log import CATEGORIES, LEVELS, StructuredLogger
from engine.store import StoreWriter


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


@pytest.fixture
def writer(db_path):
    writer = StoreWriter(db_path, batch_size=100, flush_interval=60.0)
    yield writer
    writer.close()


def _logs(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT ts, level, browser_id, category, message, fields FROM logs").fetchall()


class _BrokenStore(StoreWriter):
    """Store, у которого путь записи всегда падает."""

    def __init__(self) -> None:
        pass

    def log(
        self,
        level: str = "INFO",
        category: str = "",
        message: str = "",
        browser_id: str | None = None,
        fields: str | dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> None:
        raise RuntimeError("disk full")


class TestRowShape:
    def test_info_writes_exact_row(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")

        logger.info("click", "done", fields={"url": "https://example.com", "n": 3})
        writer.flush()

        rows = _logs(db_path)
        assert len(rows) == 1
        assert rows[0]["level"] == "INFO"
        assert rows[0]["browser_id"] == "br-1"
        assert rows[0]["category"] == "click"
        assert rows[0]["message"] == "done"
        assert json.loads(rows[0]["fields"]) == {"url": "https://example.com", "n": 3}
        assert rows[0]["ts"] > 0

    def test_explicit_browser_id_overrides_bound_one(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")

        logger.info("click", "done", browser_id="br-2")
        writer.flush()

        assert _logs(db_path)[0]["browser_id"] == "br-2"

    def test_no_browser_id_writes_null(self, writer, db_path):
        logger = StructuredLogger(writer)

        logger.info("scheduler", "tick")
        writer.flush()

        assert _logs(db_path)[0]["browser_id"] is None

    def test_no_fields_writes_null(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")

        logger.info("cleanup", "sweep")
        writer.flush()

        assert _logs(db_path)[0]["fields"] is None


class TestLevels:
    def test_all_levels_written_verbatim(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")

        logger.debug("proxy", "d")
        logger.info("proxy", "i")
        logger.warning("proxy", "w")
        logger.error("proxy", "e")
        writer.flush()

        assert [r["level"] for r in _logs(db_path)] == ["DEBUG", "INFO", "WARNING", "ERROR"]

    def test_levels_constant_matches_spec(self):
        assert set(LEVELS) == {"DEBUG", "INFO", "WARNING", "ERROR"}

    def test_unknown_level_raises(self, writer):
        logger = StructuredLogger(writer, browser_id="br-1")

        with pytest.raises(ValueError):
            logger.log("TRACE", "click", "x")

    def test_lowercase_level_raises(self, writer):
        logger = StructuredLogger(writer, browser_id="br-1")

        with pytest.raises(ValueError):
            logger.log("info", "click", "x")


class TestCategories:
    def test_all_spec_categories_accepted(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")

        for category in ("proxy", "browser", "captcha", "click", "cleanup", "scheduler"):
            logger.info(category, "ok")
        writer.flush()

        assert sorted(r["category"] for r in _logs(db_path)) == [
            "browser",
            "captcha",
            "cleanup",
            "click",
            "proxy",
            "scheduler",
        ]

    def test_categories_constant_matches_spec(self):
        assert set(CATEGORIES) == {
            "proxy",
            "browser",
            "captcha",
            "click",
            "cleanup",
            "scheduler",
        }

    def test_unknown_category_raises(self, writer):
        logger = StructuredLogger(writer, browser_id="br-1")

        with pytest.raises(ValueError):
            logger.info("network", "x")


class TestFieldsSanitization:
    def test_non_serializable_value_becomes_repr(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")
        marker = object()

        logger.info("browser", "weird", fields={"obj": marker})
        writer.flush()

        rows = _logs(db_path)
        assert len(rows) == 1
        assert json.loads(rows[0]["fields"]) == {"obj": repr(marker)}

    def test_nested_non_serializable_sanitized(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")
        marker = object()

        logger.info("captcha", "nested", fields={"items": [1, marker, {"k": marker}]})
        writer.flush()

        parsed = json.loads(_logs(db_path)[0]["fields"])
        assert parsed == {"items": [1, repr(marker), {"k": repr(marker)}]}

    def test_non_string_keys_become_strings(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")

        logger.info("proxy", "keys", fields={1: "one"})
        writer.flush()

        assert json.loads(_logs(db_path)[0]["fields"]) == {"1": "one"}

    def test_serializable_values_pass_through_unchanged(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")
        payload = {"s": "a", "i": 1, "f": 1.5, "b": True, "n": None, "l": [1, 2], "d": {"x": 1}}

        logger.info("click", "ok", fields=payload)
        writer.flush()

        assert json.loads(_logs(db_path)[0]["fields"]) == payload


class TestNeverRaises:
    def test_broken_store_does_not_raise_and_counts(self):
        logger = StructuredLogger(_BrokenStore(), browser_id="br-1")

        logger.info("click", "m1")

        assert logger.dropped == 1
        assert logger.last_error is not None
        assert "disk full" in logger.last_error

    def test_worker_keeps_working_after_store_failure(self):
        logger = StructuredLogger(_BrokenStore(), browser_id="br-1")

        logger.info("click", "m1")
        logger.error("browser", "m2", fields={"code": 500})

        assert logger.dropped == 2
        assert logger.last_error is not None

    def test_poison_fields_do_not_raise(self, writer, db_path):
        class Poison:
            def __repr__(self) -> str:
                raise RuntimeError("repr is broken")

        logger = StructuredLogger(writer, browser_id="br-1")

        logger.info("click", "m", fields={"p": Poison()})
        writer.flush()

        assert logger.dropped == 0
        assert len(_logs(db_path)) == 1
