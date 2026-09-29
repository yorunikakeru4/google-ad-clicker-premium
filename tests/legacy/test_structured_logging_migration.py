"""Проверки миграции legacy-вызова логгера на структурированные записи.

Один файл на все группы миграции (план.md, фаза 3): здесь фиксируется, что
запись каждого мигрированного модуля попадает в таблицу ``logs`` с правильной
категорией и полями, а зеркало в legacy-лог не потеряло текст.

Записи читаются из общей тестовой БД (``ADCLICKER_DB`` выставляет
``tests/conftest.py``); модульный логгер мигрированного модуля создаётся при
импорте, поэтому фикстура ``isolated_cwd`` на него не влияет.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from engine.log import resolve_db_path


def _rows(message: str) -> list[sqlite3.Row]:
    """Строки logs с данным message, в порядке записи."""

    with sqlite3.connect(resolve_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT level, browser_id, category, message, fields FROM logs "
            "WHERE message = ? ORDER BY id",
            (message,),
        ).fetchall()


def _mirrored(caplog, message: str) -> list[logging.LogRecord]:
    """Записи legacy-логгера (зеркала) с данным началом сообщения."""

    return [
        record
        for record in caplog.records
        if record.name == "logger" and record.getMessage().startswith(message)
    ]


# --- hooks ----------------------------------------------------------------


def test_hook_records_category_and_hook_name():
    import hooks

    hooks.before_search_hook(None)
    hooks.before_ad_click_hook(None)
    hooks.captcha_seen_hook(None)
    hooks.log.flush()

    rows = _rows("Executing before search hook...")
    assert [(row["level"], row["category"]) for row in rows] == [("INFO", "scheduler")]

    rows = _rows("Executing before ad click hook...")
    assert [(row["level"], row["category"]) for row in rows] == [("INFO", "click")]

    rows = _rows("Executing captcha seen hook...")
    assert [(row["level"], row["category"]) for row in rows] == [("INFO", "captcha")]


def test_hook_failure_is_logged_instead_of_raised(monkeypatch, caplog):
    """Хук не должен ронять сценарий: сбой печатается как ERROR с именем хука."""

    import hooks

    def boom(*args, **kwargs):
        raise RuntimeError("hook body broke")

    monkeypatch.setattr(hooks.log, "info", boom)

    with caplog.at_level(logging.DEBUG, logger="logger"):
        hooks.after_browser_close_hook(None)

    records = _mirrored(caplog, "Hook failed")
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].category == "browser"
    assert records[0].fields == {
        "hook": "after_browser_close",
        "error": "hook body broke",
    }


# --- adb ------------------------------------------------------------------


@pytest.fixture
def fake_adb(monkeypatch):
    """subprocess.run, отдающий успешный/неуспешный результат adb."""

    import adb

    class Result:
        def __init__(self, returncode=0, stderr="", stdout=""):
            self.returncode = returncode
            self.stderr = stderr
            self.stdout = stdout

    state = {"returncode": 0, "stderr": "", "stdout": ""}

    def run(command, **kwargs):
        return Result(state["returncode"], state["stderr"], state["stdout"])

    monkeypatch.setattr(adb.subprocess, "run", run)
    return state, adb


def test_adb_open_url_logs_device_and_url(fake_adb):
    state, adb = fake_adb

    adb.ADBController.open_url("https://example.com/p", "emulator-5554")
    adb.log.flush()

    rows = _rows("URL was successfully opened on device")
    assert [(row["level"], row["category"]) for row in rows] == [("INFO", "browser")]
    assert json.loads(rows[-1]["fields"]) == {
        "url": "https://example.com/p",
        "device_id": "emulator-5554",
    }


def test_adb_failure_logs_stderr(fake_adb):
    state, adb = fake_adb
    state["returncode"] = 1
    state["stderr"] = "adb: device offline"

    adb.ADBController.send_swipe(1, 2, 3, 4, 100)
    adb.log.flush()

    rows = _rows("Error during swipe")
    assert [(row["level"], row["category"]) for row in rows] == [("ERROR", "browser")]
    assert json.loads(rows[-1]["fields"]) == {"stderr": "adb: device offline"}


# --- search_controller ----------------------------------------------------


def test_search_controller_logs_filter_words_as_click(make_search_controller):
    import search_controller

    make_search_controller("wireless keyboard@amazon#ebay")
    search_controller.log.flush()

    rows = _rows("Filter words")
    assert rows, "фильтры запроса должны попасть в logs"
    assert rows[-1]["category"] == "click"
    assert json.loads(rows[-1]["fields"]) == {"filter_words": ["amazon", "ebay"]}


def test_search_controller_cache_cleanup_logged_as_cleanup(make_search_controller):
    import search_controller

    controller = make_search_controller()
    controller._delete_cache_and_cookies()
    search_controller.log.flush()

    rows = _rows("Deleting browser cache and cookies...")
    assert rows, "очистка кэша должна попасть в logs"
    assert rows[-1]["category"] == "cleanup"


# --- ad_clicker: --id перебиндывает общий логгер ---------------------------


def test_bound_browser_id_reaches_records_of_every_module(make_search_controller):
    """ad_clicker --id должен попасть в записи всех модулей процесса.

    Модули берут один и тот же инстанс из get_logger(), поэтому bind() в
    main() перебиндывает и их — иначе browser_id в logs остался бы NULL.
    """

    import ad_clicker
    import hooks

    assert ad_clicker.log is hooks.log

    ad_clicker.log.bind("br-42")
    try:
        hooks.before_search_hook(None)
        hooks.log.flush()

        rows = _rows("Executing before search hook...")
        assert any(row["browser_id"] == "br-42" for row in rows)
    finally:
        ad_clicker.log.bind(None)
