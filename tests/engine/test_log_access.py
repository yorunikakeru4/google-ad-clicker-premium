"""Тесты accessor'а ``get_logger`` и dual-write (план.md, фаза 3).

Контракт:

- ``get_logger()`` один инстанс на путь: первый вызов открывает StoreWriter
  и применяет миграции, повторный возвращает тот же объект;
- ``browser_id`` биндится по умолчанию, явный ``browser_id=`` в вызове
  перебивает;
- ошибка создания хранилища не роняет вызывающего: причина остаётся в
  ``dropped``/``last_error`` логгера, записи продолжают приниматься;
- каждая запись, сделанная через ``get_logger()``, уходит в store **и** в
  legacy stdlib-логгер из ``logger.py`` (файловый ротационный лог и консоль
  работают как раньше);
- упавшее зеркало не роняет вызывающего и не отменяет запись в store.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from engine.db import migrations
from engine.log import StructuredLogger, get_logger, legacy_mirror
from engine.store import StoreWriter

# Имя модуля legacy-логгера: записи зеркала попадают в caplog под ним.
LEGACY_LOGGER_NAME = "logger"


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


def _logs(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT ts, level, browser_id, category, message, fields FROM logs ORDER BY id"
        ).fetchall()


def _legacy_records(caplog):
    return [record for record in caplog.records if record.name == LEGACY_LOGGER_NAME]


class TestAccessor:
    def test_same_path_returns_same_instance(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ADCLICKER_DB", str(tmp_path / "same.db"))

        first = get_logger()
        second = get_logger()

        assert first is second

    def test_different_paths_return_different_instances(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ADCLICKER_DB", str(tmp_path / "a.db"))
        first = get_logger()
        monkeypatch.setenv("ADCLICKER_DB", str(tmp_path / "b.db"))
        second = get_logger()

        assert first is not second

    def test_first_call_migrates_and_record_lands_in_logs(self, tmp_path, monkeypatch):
        path = tmp_path / "fresh.db"
        assert not path.exists()
        monkeypatch.setenv("ADCLICKER_DB", str(path))

        log = get_logger(browser_id="br-1")
        log.info("click", "opened", fields={"url": "https://example.com", "n": 2})
        log.flush()

        rows = _logs(path)
        assert len(rows) == 1
        assert rows[0]["level"] == "INFO"
        assert rows[0]["browser_id"] == "br-1"
        assert rows[0]["category"] == "click"
        assert rows[0]["message"] == "opened"
        assert json.loads(rows[0]["fields"]) == {"url": "https://example.com", "n": 2}

    def test_browser_id_bound_by_default_and_explicit_argument_wins(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("ADCLICKER_DB", str(tmp_path / "bind.db"))

        log = get_logger(browser_id="br-1")
        log.info("click", "bound")
        log.info("click", "overridden", browser_id="br-2")
        log.flush()

        rows = _logs(tmp_path / "bind.db")
        assert [row["browser_id"] for row in rows] == ["br-1", "br-2"]

    def test_binding_rebinds_the_shared_instance(self, tmp_path, monkeypatch):
        """Вызов с browser_id в середине жизни процесса перебиндивает общий
        логгер — так ad_clicker подставляет --id уже после импорта модулей."""

        monkeypatch.setenv("ADCLICKER_DB", str(tmp_path / "rebind.db"))

        first = get_logger()
        bound = get_logger(browser_id="br-9")

        assert bound is first
        assert first.browser_id == "br-9"

    def test_unavailable_store_does_not_raise_and_counts_loss(self, tmp_path, monkeypatch):
        # Путь указывает на каталог: и migrate(), и sqlite3.connect() падают.
        broken = tmp_path / "im-a-directory"
        broken.mkdir()
        monkeypatch.setenv("ADCLICKER_DB", str(broken))

        log = get_logger()
        log.info("click", "lost")
        log.warning("proxy", "also lost")

        assert log.dropped == 2
        assert log.last_error is not None
        assert "im-a-directory" in log.last_error

    def test_unavailable_store_failure_is_cached_not_retried(self, tmp_path, monkeypatch):
        broken = tmp_path / "im-a-directory"
        broken.mkdir()
        monkeypatch.setenv("ADCLICKER_DB", str(broken))

        first = get_logger()
        second = get_logger()

        assert first is second
        # Счётчики остаются нулевыми, пока никто не писал: ошибка создания
        # всплывает при первой же записи, а не в момент создания.
        assert first.dropped == 0

    def test_default_path_comes_from_env(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("ADCLICKER_DB", raising=False)

        log = get_logger()
        log.info("scheduler", "tick")
        log.flush()

        # Дефолт — adclicker.db в текущем каталоге, как задумано для скриптов.
        assert len(_logs(tmp_path / "adclicker.db")) == 1


class TestDualWrite:
    @pytest.mark.parametrize(
        ("level", "legacy_level"),
        [
            ("debug", logging.DEBUG),
            ("info", logging.INFO),
            ("warning", logging.WARNING),
            ("error", logging.ERROR),
        ],
    )
    def test_record_goes_to_store_and_legacy_logger(
        self, tmp_path, monkeypatch, caplog, level, legacy_level
    ):
        path = tmp_path / f"dual-{level}.db"
        monkeypatch.setenv("ADCLICKER_DB", str(path))
        legacy = __import__(LEGACY_LOGGER_NAME)

        with caplog.at_level(logging.DEBUG, logger=legacy.__name__):
            log = get_logger(browser_id="br-3")
            getattr(log, level)("click", "opened", fields={"url": "https://example.com", "n": 2})
            log.flush()

        rows = _logs(path)
        assert len(rows) == 1
        assert rows[0]["level"] == level.upper()
        assert json.loads(rows[0]["fields"]) == {"url": "https://example.com", "n": 2}

        mirrored = _legacy_records(caplog)
        assert len(mirrored) == 1
        assert mirrored[0].levelno == legacy_level
        # Поля приклеиваются к сообщению в читаемом виде.
        assert mirrored[0].getMessage() == "opened url='https://example.com' n=2"
        # И остаются доступными структурно для тестов и обвязки.
        assert mirrored[0].category == "click"
        assert mirrored[0].fields == {"url": "https://example.com", "n": 2}

    def test_mirror_failure_keeps_store_record_and_is_reported(self, writer, db_path):
        def broken_mirror(*args, **kwargs):
            raise RuntimeError("handlers gone")

        log = StructuredLogger(writer, browser_id="br-1", mirror=broken_mirror)

        log.info("click", "kept")

        writer.flush()

        rows = _logs(db_path)
        assert len(rows) == 1, "упавшее зеркало не должно отменять запись в store"
        assert log.dropped == 0
        assert log.last_error is not None
        assert "зеркало" in log.last_error

    def test_broken_store_still_reaches_legacy_logger(self, caplog):
        class BrokenStore:
            def log(self, **kwargs):
                raise RuntimeError("disk full")

            def flush(self) -> None:
                raise RuntimeError("disk full")

        legacy = __import__(LEGACY_LOGGER_NAME)
        with caplog.at_level(logging.DEBUG, logger=legacy.__name__):
            log = StructuredLogger(BrokenStore(), mirror=legacy_mirror)
            log.error("proxy", "down", fields={"code": 502})

        mirrored = _legacy_records(caplog)
        assert [record.getMessage() for record in mirrored] == ["down code=502"]
        assert log.dropped == 1
        assert "disk full" in log.last_error

    def test_exc_info_reaches_mirror_only(self, writer, db_path):
        captured = {}

        def mirror(level, message, *, exc_info=None, extra=None):
            captured.update(level=level, message=message, exc_info=exc_info, extra=extra)

        log = StructuredLogger(writer, mirror=mirror)
        try:
            raise ValueError("unexpected kill failure")
        except ValueError as exc:
            log.debug("browser", str(exc), fields={"error_type": "ValueError"}, exc_info=exc)
        writer.flush()

        rows = _logs(db_path)
        assert rows[0]["level"] == "DEBUG"
        # Трассировка не попадает в таблицу: там только JSON-поля.
        assert json.loads(rows[0]["fields"]) == {"error_type": "ValueError"}
        assert "Traceback" not in rows[0]["message"]

        assert isinstance(captured["exc_info"], ValueError)

    def test_without_mirror_only_store_is_written(self, writer, db_path, caplog):
        legacy = __import__(LEGACY_LOGGER_NAME)

        log = StructuredLogger(writer, browser_id="br-1")
        with caplog.at_level(logging.DEBUG, logger=legacy.__name__):
            log.info("click", "quiet")
        writer.flush()

        assert len(_logs(db_path)) == 1
        assert _legacy_records(caplog) == []

    def test_mirror_survives_poison_fields(self, writer, db_path, caplog):
        """Ядовитое значение в fields не роняет ни store, ни зеркало."""

        class Poison:
            def __repr__(self) -> str:
                raise RuntimeError("repr is broken")

        legacy = __import__(LEGACY_LOGGER_NAME)
        with caplog.at_level(logging.DEBUG, logger=legacy.__name__):
            log = StructuredLogger(writer, browser_id="br-1", mirror=legacy_mirror)
            log.info("browser", "weird", fields={"p": Poison()})
        writer.flush()

        rows = _logs(db_path)
        assert len(rows) == 1
        assert json.loads(rows[0]["fields"]) == {"p": "<unrepresentable> Poison"}
        assert log.dropped == 0

        mirrored = _legacy_records(caplog)
        assert len(mirrored) == 1
        assert "<unrepresentable>" in mirrored[0].getMessage()
