"""Тесты clicklogs_db.ClickLogsDB: журнал кликов в SQLite.

База создаётся в текущем каталоге, а каждый тест выполняется в собственном
tmp_path, поэтому реальный clicklogs.db в репозитории не затрагивается.
"""

import os
from datetime import datetime

import pytest

from clicklogs_db import ClickLogsDB


def today() -> str:
    return datetime.now().strftime("%d-%m-%Y")


def test_save_click_is_visible_for_today(make_db):
    db = make_db()

    db.save_click("http://a.example", "Ad", "usb hub", "10:00:00")

    assert db.query_clicks(today()) == [("http://a.example", 1, "Ad", "10:00:00", "usb hub")]


def test_query_clicks_returns_none_when_no_data(make_db):
    db = make_db()

    assert db.query_clicks(today()) is None


def test_query_clicks_ignores_other_dates(make_db):
    db = make_db()
    db.save_click("http://a.example", "Ad", "usb hub", "10:00:00")

    assert db.query_clicks("01-01-1970") is None


def test_spaces_in_url_are_replaced_with_percent20(make_db):
    db = make_db()
    db.save_click("http://a.example/some path", "Ad", "usb hub", "10:00:00")

    assert db.query_clicks(today()) == [
        ("http://a.example/some%20path", 1, "Ad", "10:00:00", "usb hub")
    ]


def test_repeated_clicks_of_same_link_are_counted(make_db):
    db = make_db()
    for click_time in ("10:00:00", "10:05:00", "11:00:00"):
        db.save_click("http://a.example", "Ad", "usb hub", click_time)

    results = db.query_clicks(today())

    assert len(results) == 1
    assert results[0][1] == 3


def test_same_url_with_different_queries_is_split_into_rows(make_db):
    db = make_db()
    db.save_click("http://a.example", "Ad", "usb hub", "10:00:00")
    db.save_click("http://a.example", "Ad", "webcam", "10:01:00")

    results = db.query_clicks(today())

    assert {(row[0], row[4], row[1]) for row in results} == {
        ("http://a.example", "usb hub", 1),
        ("http://a.example", "webcam", 1),
    }


def test_same_url_with_different_categories_is_split_into_rows(make_db):
    db = make_db()
    db.save_click("http://a.example", "Ad", "usb hub", "10:00:00")
    db.save_click("http://a.example", "Non-ad", "usb hub", "10:01:00")
    db.save_click("http://a.example", "Shopping", "usb hub", "10:02:00")

    categories = {row[2] for row in db.query_clicks(today())}

    assert categories == {"Ad", "Non-ad", "Shopping"}


def test_query_clicks_keeps_query_and_category_columns(make_db):
    db = make_db()
    db.save_click("http://a.example", "Non-ad", "wireless keyboard", "12:34:56")

    url, clicks, category, click_time, query = db.query_clicks(today())[0]

    assert (url, clicks, category, click_time, query) == (
        "http://a.example",
        1,
        "Non-ad",
        "12:34:56",
        "wireless keyboard",
    )


def test_data_survives_reopening_of_the_database(make_db):
    db = make_db()
    db.save_click("http://a.example", "Ad", "usb hub", "10:00:00")

    reopened = ClickLogsDB()

    assert reopened.query_clicks(today()) == [
        ("http://a.example", 1, "Ad", "10:00:00", "usb hub")
    ]


def test_save_click_raises_runtime_error_on_readonly_database(make_db, isolated_cwd, caplog):
    db = make_db()
    db.save_click("http://a.example", "Ad", "usb hub", "10:00:00")

    db_file = isolated_cwd / "clicklogs.db"
    os.chmod(db_file, 0o444)
    try:
        with pytest.raises(RuntimeError):
            db.save_click("http://b.example", "Ad", "usb hub", "10:00:00")
    finally:
        os.chmod(db_file, 0o644)

    # Настоящая причина (ошибка записи) попадает только в лог.
    assert "attempt to write a readonly database" in caplog.text


def test_readonly_database_error_message_mentions_connection(make_db, isolated_cwd):
    # Ошибку записи оборачивают в RuntimeError со текстом про подключение -
    # текущее поведение, которое стоит учитывать при разборе логов.
    db = make_db()
    db_file = isolated_cwd / "clicklogs.db"
    db_file.touch()
    os.chmod(db_file, 0o444)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            db.save_click("http://a.example", "Ad", "usb hub", "10:00:00")

        assert str(excinfo.value) == "Failed to connect to clicklogs database!"
    finally:
        os.chmod(db_file, 0o644)


@pytest.mark.xfail(
    reason=(
        "Баг legacy-кода: контекст-менеджер _clicklogs_db в блоке finally "
        "обращается к clicklogs_db, которого ещё нет, если connect() упал, "
        "поэтому вместо задокументированного RuntimeError вылетает "
        "UnboundLocalError: cannot access local variable"
    ),
    strict=True,
)
def test_database_connection_failure_should_raise_runtime_error(isolated_cwd):
    # Каталог вместо файла БД заставляет sqlite3.connect() упасть.
    (isolated_cwd / "clicklogs.db").mkdir()

    with pytest.raises(RuntimeError) as excinfo:
        ClickLogsDB().save_click("http://a.example", "Ad", "usb hub", "10:00:00")

    assert "Failed to connect to clicklogs database!" in str(excinfo.value)


def test_save_click_also_records_into_shared_clicks(make_db, monkeypatch):
    """Дуал-write: клик уходит и в общую таблицу ``clicks`` общей БД.

    Дашборд фазы 4 читает ``clicks``; без этой записи реальные клики
    оставались только в legacy-журнале (поймано живым e2e-прогоном).
    """
    import clicklogs_db

    recorded: dict = {}

    class Facade:
        def debug(self, *args, **kwargs):
            pass  # legacy-сообщение в журнал тоже пишется через этот объект

        def record_click(self, **kwargs):
            recorded.update(kwargs)

    monkeypatch.setattr(clicklogs_db, "log", Facade())

    db = make_db()
    db.save_click("http://a.example", "Ad", "usb hub", "10:30:15")

    assert recorded["url"].startswith("http://a.example")
    assert recorded["query"] == "usb hub"
    assert recorded["category"] == "Ad"
    assert isinstance(recorded["ts"], float)
