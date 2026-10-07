"""Тесты структурированного логгера (engine/log.py).

Контракт:

- категории и уровни — закрытый словарь, неизвестная категория/уровень —
  ошибка программирования (ValueError);
- fields уходят в БД JSON-строкой, несериализуемое превращается в repr;
- слишком длинные строковые значения полей обрезаются по
  ``FIELD_VALUE_LIMIT`` одинаково и в БД, и в файловое зеркало: message
  и ``exc_info`` при этом не трогаются;
- запись метрики ``network_requests`` идёт через тот же store и биндинг
  ``browser_id``, ошибки гасятся так же, а в зеркало не дублируется;
- путь логгирования никогда не бросает исключений: сломанный store под
  логгером — счётчик + последняя ошибка, воркер продолжает работать.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

import pytest

from engine.db import migrations
from engine.log import (
    CATEGORIES,
    FIELD_VALUE_LIMIT,
    LEVELS,
    StructuredLogger,
    encode_fields,
    legacy_mirror,
    render_mirror_message,
)
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


def _network(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT ts, browser_id, method, url, resource_type, status FROM network_requests"
        ).fetchall()


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

    def record_network_request(
        self,
        method: str,
        url: str,
        resource_type: str | None = None,
        status: int | None = None,
        browser_id: str | None = None,
        ts: float | None = None,
    ) -> None:
        raise RuntimeError("disk full")

    def record_diagnostic(self, **kwargs: Any) -> None:
        raise RuntimeError("disk full")

    def record_captcha_event(self, **kwargs: Any) -> None:
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


class TestFieldTruncation:
    """Длинные значения полей обрезаются одинаково в БД и в зеркале.

    Стек-трейс из прода (~2КБ) целиком уходил в поле ``error`` и делал
    запись нечитаемой. Лимит берётся такой, чтобы первая строка ошибки —
    она и есть суть — проходила целиком.
    """

    STACK_TRACE = (
        "Message: element not interactable\n"
        "(Session info: chrome=153.0.8010.55)\n" + "0 undetected_chromedriver 0x0000000103306b4a\n" * 50
    )

    @staticmethod
    def _truncated(value: str) -> str:
        return value[:FIELD_VALUE_LIMIT] + f"... [+{len(value) - FIELD_VALUE_LIMIT} символов]"

    def test_stack_trace_fixture_is_longer_than_limit(self):
        assert len(self.STACK_TRACE) > FIELD_VALUE_LIMIT

    def test_long_field_is_truncated_in_encode_fields(self):
        parsed = json.loads(encode_fields({"error": self.STACK_TRACE}))

        assert parsed["error"] == self._truncated(self.STACK_TRACE)
        assert parsed["error"].startswith("Message: element not interactable\n")

    def test_long_field_is_truncated_in_render_mirror_message(self):
        rendered = render_mirror_message("Direct typing failed", {"error": self.STACK_TRACE})

        assert rendered.startswith("Direct typing failed ")
        # Зеркало печатает значение через repr — маркер обязан совпасть с БД.
        assert f"error={self._truncated(self.STACK_TRACE)!r}" in rendered

    def test_truncation_is_identical_in_db_and_mirror(self):
        # Одиночная строка без кавычек и переводов: repr совпадает со значением,
        # и маркер можно сравнивать напрямую.
        value = "element not interactable " + "x" * 500
        in_db = json.loads(encode_fields({"error": value}))["error"]
        in_mirror = render_mirror_message("m", {"error": value})

        assert in_db == self._truncated(value)
        assert f"error={in_db!r}" in in_mirror

    def test_short_field_is_not_truncated(self):
        fields = {"error": "element not interactable", "n": 3, "ok": True}

        assert json.loads(encode_fields(fields)) == fields
        assert "error='element not interactable'" in render_mirror_message("m", fields)

    def test_field_exactly_at_limit_is_not_truncated(self):
        value = "y" * FIELD_VALUE_LIMIT

        assert json.loads(encode_fields({"e": value})) == {"e": value}

    def test_nested_long_value_is_truncated(self):
        value = "z" * (FIELD_VALUE_LIMIT + 10)

        parsed = json.loads(encode_fields({"nested": {"error": value}}))

        assert parsed["nested"]["error"] == self._truncated(value)

    def test_message_stays_intact_while_field_value_is_cut(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")
        message = "m" * (FIELD_VALUE_LIMIT * 2)
        error = "e" * (FIELD_VALUE_LIMIT + 5)

        logger.info("click", message, fields={"error": error})
        writer.flush()

        row = _logs(db_path)[0]
        assert row["message"] == message
        assert json.loads(row["fields"])["error"] == self._truncated(error)


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


class TestNetworkRecords:
    """Запись метрики через логгер: browser_id берётся из биндинга, ошибки гасятся."""

    def test_record_network_request_uses_bound_browser_id(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")

        logger.record_network_request(
            method="GET",
            url="https://site.test/a",
            resource_type="Document",
            status=200,
            ts=1700000020.0,
        )
        writer.flush()

        rows = _network(db_path)
        assert [
            (r["browser_id"], r["method"], r["url"], r["resource_type"], r["status"], r["ts"])
            for r in rows
        ] == [("br-1", "GET", "https://site.test/a", "Document", 200, 1700000020.0)]

    def test_explicit_browser_id_overrides_bound_one(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")

        logger.record_network_request(
            method="GET", url="https://site.test/a", browser_id="br-2"
        )
        writer.flush()

        assert [r["browser_id"] for r in _network(db_path)] == ["br-2"]

    def test_broken_store_counts_loss_and_never_raises(self):
        logger = StructuredLogger(_BrokenStore(), browser_id="br-1")

        logger.record_network_request(method="GET", url="https://site.test/a")

        assert logger.dropped == 1
        assert logger.last_error is not None
        assert "disk full" in logger.last_error

    def test_worker_keeps_recording_after_store_failure(self):
        logger = StructuredLogger(_BrokenStore(), browser_id="br-1")

        logger.record_network_request(method="GET", url="https://site.test/a")
        logger.record_network_request(method="POST", url="https://site.test/b")

        assert logger.dropped == 2

    def test_metric_row_does_not_mirror_into_legacy_log(self, writer, db_path, caplog):
        """Метрика живёт в таблице, а не в логах: полный URL в логи не уходит."""

        legacy = __import__("logger")
        logger = StructuredLogger(writer, browser_id="br-1", mirror=legacy_mirror)

        with caplog.at_level(logging.DEBUG, logger=legacy.__name__):
            logger.record_network_request(
                method="GET", url="https://site.test/p?token=SECRET123"
            )
        writer.flush()

        rows = _network(db_path)
        assert [r["url"] for r in rows] == ["https://site.test/p?token=SECRET123"]
        assert [r for r in caplog.records if r.name == legacy.__name__] == []
        assert "SECRET123" not in caplog.text


def _workers(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT browser_id, status, last_error FROM workers ORDER BY browser_id"
        ).fetchall()


class TestMarkDegraded:
    """Деградация доходит до ``workers`` через логгер: биндинг и политика ошибок."""

    def test_uses_bound_browser_id(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")

        logger.mark_degraded("cdp connection lost")

        assert [(r["browser_id"], r["status"], r["last_error"]) for r in _workers(db_path)] == [
            ("br-1", "degraded", "cdp connection lost"),
        ]

    def test_explicit_browser_id_overrides_bound_one(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")

        logger.mark_degraded("cdp connection lost", browser_id="br-2")

        assert [(r["browser_id"], r["status"]) for r in _workers(db_path)] == [
            ("br-2", "degraded"),
        ]

    def test_without_browser_id_writes_nothing_and_does_not_raise(self, writer, db_path):
        """CLI-прогон без --id: писать некуда, но падать нельзя."""

        logger = StructuredLogger(writer)

        logger.mark_degraded("cdp connection lost")

        assert _workers(db_path) == []
        assert logger.dropped == 0
        assert logger.last_error is None

    def test_broken_store_counts_loss_and_never_raises(self):
        logger = StructuredLogger(_BrokenStore(), browser_id="br-1")

        logger.mark_degraded("cdp connection lost")

        assert logger.dropped == 1
        assert logger.last_error is not None

    def test_unavailable_store_explains_itself_in_last_error(self):
        from engine.log import _UnavailableStore

        logger = StructuredLogger(_UnavailableStore("хранилище недоступно"), browser_id="br-1")

        logger.mark_degraded("cdp connection lost")

        assert logger.dropped == 1
        assert logger.last_error is not None
        assert "хранилище недоступно" in logger.last_error

    def test_state_write_does_not_mirror_into_legacy_log(self, writer, db_path, caplog):
        """Причина деградации живёт в ``workers``, а не дублируется в логи."""

        legacy = __import__("logger")
        logger = StructuredLogger(writer, browser_id="br-1", mirror=legacy_mirror)

        with caplog.at_level(logging.DEBUG, logger=legacy.__name__):
            logger.mark_degraded("cdp connection lost")
        writer.flush()

        assert [(r["status"], r["last_error"]) for r in _workers(db_path)] == [
            ("degraded", "cdp connection lost"),
        ]
        assert [r for r in caplog.records if r.name == legacy.__name__] == []


def _diagnostics(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT browser_id, ip, headers, suspicion_flags FROM diagnostics "
            "ORDER BY browser_id"
        ).fetchall()


class TestRecordDiagnostic:
    """Снимок диагностики через логгер: биндинг и политика ошибок.

    Путь воркера именно такой: в ``ad_clicker`` уже есть общий логгер
    процесса, и снимок пишется через него — как и ``mark_degraded``, без
    отдельного соединения и без зеркалирования в legacy-лог.
    """

    def test_uses_bound_browser_id_and_writes_a_row(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")

        logger.record_diagnostic(
            ip="203.0.113.7",
            headers={"Accept": "application/json"},
            suspicion_flags=["платформа не соответствует ОС"],
        )

        rows = _diagnostics(db_path)
        assert [(r["browser_id"], r["ip"]) for r in rows] == [("br-1", "203.0.113.7")]
        assert json.loads(rows[0]["headers"]) == {"Accept": "application/json"}
        assert json.loads(rows[0]["suspicion_flags"]) == ["платформа не соответствует ОС"]

    def test_explicit_browser_id_overrides_bound_one(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")

        logger.record_diagnostic(browser_id="br-2", ip="198.51.100.1")

        assert [r["browser_id"] for r in _diagnostics(db_path)] == ["br-2"]

    def test_without_browser_id_writes_nothing_and_does_not_raise(self, writer, db_path):
        """CLI-прогон без --id: писать некуда, но падать нельзя."""

        logger = StructuredLogger(writer)

        logger.record_diagnostic(ip="203.0.113.7")

        assert _diagnostics(db_path) == []
        assert logger.dropped == 0
        assert logger.last_error is None

    def test_broken_store_counts_loss_and_never_raises(self):
        logger = StructuredLogger(_BrokenStore(), browser_id="br-1")

        logger.record_diagnostic(ip="203.0.113.7")

        assert logger.dropped == 1
        assert logger.last_error is not None

    def test_unavailable_store_explains_itself_in_last_error(self):
        from engine.log import _UnavailableStore

        logger = StructuredLogger(_UnavailableStore("хранилище недоступно"), browser_id="br-1")

        logger.record_diagnostic(ip="203.0.113.7")

        assert logger.dropped == 1
        assert "хранилище недоступно" in logger.last_error

    def test_snapshot_row_does_not_mirror_into_legacy_log(self, writer, db_path, caplog):
        """Снимок живёт в ``diagnostics``, а не дублируется в логи."""

        legacy = __import__("logger")
        logger = StructuredLogger(writer, browser_id="br-1", mirror=legacy_mirror)

        with caplog.at_level(logging.DEBUG, logger=legacy.__name__):
            logger.record_diagnostic(ip="203.0.113.7")
        writer.flush()

        assert [r["ip"] for r in _diagnostics(db_path)] == ["203.0.113.7"]
        assert [r for r in caplog.records if r.name == legacy.__name__] == []


def _captcha_events(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT browser_id, page_url, sitekey, screenshot_path, solved, solver, "
            "elapsed_ms FROM captcha_events ORDER BY id"
        ).fetchall()


class TestRecordCaptchaEvent:
    """Событие CAPTCHA через логгер: биндинг и политика ошибок.

    Путь воркера такой же, что у снимка диагностики: общий логгер процесса
    уже привязан к воркеру, поэтому событие не требует отдельного соединения.
    """

    def test_uses_bound_browser_id_and_writes_a_row(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")

        logger.record_captcha_event(
            page_url="https://www.google.com/search?q=x",
            sitekey="6L-sitekey",
            screenshot_path="/shots/br-1_1.png",
            solved=True,
            solver="2captcha",
            elapsed_ms=1500,
        )

        rows = _captcha_events(db_path)
        assert [(r["browser_id"], r["sitekey"], r["solved"]) for r in rows] == [
            ("br-1", "6L-sitekey", 1)
        ]
        assert rows[0]["solver"] == "2captcha"
        assert rows[0]["elapsed_ms"] == 1500

    def test_explicit_browser_id_overrides_bound_one(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")

        logger.record_captcha_event(browser_id="br-2", solved=False)

        assert [r["browser_id"] for r in _captcha_events(db_path)] == ["br-2"]

    def test_without_browser_id_writes_a_null_row_and_does_not_raise(self, writer, db_path):
        """CLI-прогон без --id: событие пишется с NULL, а не теряется."""

        logger = StructuredLogger(writer)

        logger.record_captcha_event(page_url="https://x.test/", solved=False)

        rows = _captcha_events(db_path)
        assert [(r["browser_id"], r["page_url"]) for r in rows] == [
            (None, "https://x.test/")
        ]
        assert logger.dropped == 0
        assert logger.last_error is None

    def test_broken_store_counts_loss_and_never_raises(self):
        logger = StructuredLogger(_BrokenStore(), browser_id="br-1")

        logger.record_captcha_event(solved=False)

        assert logger.dropped == 1
        assert logger.last_error is not None

    def test_unavailable_store_explains_itself_in_last_error(self):
        from engine.log import _UnavailableStore

        logger = StructuredLogger(_UnavailableStore("хранилище недоступно"), browser_id="br-1")

        logger.record_captcha_event(solved=False)

        assert logger.dropped == 1
        assert logger.last_error is not None
        assert "хранилище недоступно" in logger.last_error

    def test_event_row_does_not_mirror_into_legacy_log(self, writer, db_path, caplog):
        """Событие живёт в ``captcha_events``, а не дублируется в файловый лог."""

        legacy = __import__("logger")
        logger = StructuredLogger(writer, browser_id="br-1", mirror=legacy_mirror)

        with caplog.at_level(logging.DEBUG, logger=legacy.__name__):
            logger.record_captcha_event(solved=False)
        writer.flush()

        assert len(_captcha_events(db_path)) == 1
        assert [r for r in caplog.records if r.name == legacy.__name__] == []


def _clicks(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT ts, browser_id, url, query, category FROM clicks"
        ).fetchall()


class TestRecordClickFacade:
    """Дуал-write клика в общую таблицу ``clicks``.

    ``clicklogs_db.save_click`` до этого писал только в legacy-журнал —
    дашборд (клики/час, графики) читал пустую таблицу, хотя клики реально
    происходили (поймано живым e2e-прогоном: 2 клика в clicklogs.db и 0 в
    clicks).
    """

    def test_uses_bound_browser_id_and_writes_a_row(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")

        logger.record_click(
            url="https://www.google.com/goto?url=x", query="kette gold", category="Ad"
        )
        writer.flush()  # клики батчатся в отличие от diagnostic/captcha

        rows = _clicks(db_path)
        assert [(r["browser_id"], r["query"], r["category"]) for r in rows] == [
            ("br-1", "kette gold", "Ad")
        ]
        assert rows[0]["url"].startswith("https://www.google.com")

    def test_explicit_browser_id_overrides_bound_one(self, writer, db_path):
        logger = StructuredLogger(writer, browser_id="br-1")

        logger.record_click(url="https://example.com/", browser_id="br-2")
        writer.flush()

        assert [r["browser_id"] for r in _clicks(db_path)] == ["br-2"]

    def test_without_browser_id_writes_a_null_row(self, writer, db_path):
        """CLI-прогон без --id: клик ценнее пустой привязки — пишется с NULL."""

        logger = StructuredLogger(writer)

        logger.record_click(url="https://example.com/", query="q", category="Ad")
        writer.flush()

        rows = _clicks(db_path)
        assert len(rows) == 1
        assert rows[0]["browser_id"] is None

    def test_broken_store_never_raises(self, db_path):
        logger = StructuredLogger(_BrokenStore())

        logger.record_click(url="https://example.com/")

        assert logger.dropped >= 1
        assert logger.last_error is not None
