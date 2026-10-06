"""Тесты engine/wordlist.py: CRUD списков в файлах.

Списки запросов и доменов живут в файлах (source of truth для движка),
поэтому здесь проверяется ровно то, что ломает эксплуатацию: нормализация
строк так же, как читает legacy, регистронезависимая дедупликация,
best-effort-удаление с отчётом и атомарная запись без рваных файлов.
"""

from __future__ import annotations

import pytest

from engine.wordlist import (
    WordlistError,
    add_items,
    clean_domain,
    clean_query,
    delete_items,
    read_items,
    write_items,
)


# --- нормализация строк ---------------------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        ("wireless keyboard", "wireless keyboard"),
        ("  wireless keyboard  ", "wireless keyboard"),
        ("'wireless keyboard'", "wireless keyboard"),
        ('"wireless keyboard"', "wireless keyboard"),
        ("wireless 'keyboard'", "wireless keyboard"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_clean_query_matches_legacy_reader(line, expected):
    """Строка в списке обязана совпадать с тем, что уходит в поиск.

    ``utils.get_queries`` вырезает кавычки целиком — если бы здесь было
    иначе, UI показывал бы список, отличающийся от реального.
    """
    assert clean_query(line) == expected


@pytest.mark.parametrize(
    "line,expected",
    [
        ("edelind.de", "edelind.de"),
        ("www.edelind.de", "edelind.de"),
        ("https://www.edelind.de/goldkette", "edelind.de"),
        ("  Edelind.DE  ", "edelind.de"),
        ("'edelind.de'", "edelind.de"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_clean_domain_normalizes_to_host(line, expected):
    assert clean_domain(line) == expected


# --- чтение ---------------------------------------------------------------------


def test_read_items_of_missing_file_is_empty(tmp_path):
    assert read_items(tmp_path / "nope.txt") == []


def test_read_items_skips_blanks_and_applies_clean(tmp_path):
    path = tmp_path / "domains.txt"
    path.write_text("www.edelind.de\n\n  \nbooking.com\n", encoding="utf-8")

    assert read_items(path, clean_domain) == ["edelind.de", "booking.com"]


def test_write_items_is_atomic_and_replaces_content(tmp_path):
    path = tmp_path / "queries.txt"
    path.write_text("old\n", encoding="utf-8")

    write_items(path, ["new-1", "new-2"])

    assert path.read_text(encoding="utf-8") == "new-1\nnew-2\n"
    assert list(tmp_path.glob(".wordlist-*")) == [], "временный файл не подчистился"


def test_write_items_creates_missing_parents(tmp_path):
    path = tmp_path / "nested" / "dir" / "queries.txt"

    write_items(path, ["q"])

    assert path.read_text(encoding="utf-8") == "q\n"


def test_write_items_failure_raises_wordlist_error(tmp_path):
    directory = tmp_path / "as-file"
    directory.write_text("занято", encoding="utf-8")

    with pytest.raises(WordlistError) as excinfo:
        write_items(directory / "queries.txt", ["q"])

    assert "не удалось записать" in str(excinfo.value)
    assert excinfo.value.status == 500


# --- добавление -----------------------------------------------------------------


def test_add_items_appends_and_keeps_order(tmp_path):
    path = tmp_path / "queries.txt"
    path.write_text("first\n", encoding="utf-8")

    result = add_items(path, ["second", "third"], clean=clean_query)

    assert result == {"added": 2, "skipped": 0, "problems": []}
    assert read_items(path, clean_query) == ["first", "second", "third"]


def test_add_items_creates_missing_file(tmp_path):
    path = tmp_path / "queries.txt"

    result = add_items(path, ["only"], clean=clean_query)

    assert result["added"] == 1
    assert path.read_text(encoding="utf-8") == "only\n"


def test_add_items_dedupes_case_insensitively(tmp_path):
    path = tmp_path / "queries.txt"
    path.write_text("Wireless Keyboard\n", encoding="utf-8")

    result = add_items(path, ["wireless keyboard", "usb hub"], clean=clean_query)

    assert result["added"] == 1
    assert result["skipped"] == 1
    assert result["problems"] == ["уже есть: wireless keyboard"]
    assert read_items(path, clean_query) == ["Wireless Keyboard", "usb hub"]


def test_add_items_skips_empty_lines_with_problem(tmp_path):
    path = tmp_path / "queries.txt"

    result = add_items(path, ["", "   ", "  ok  "], clean=clean_query)

    assert result["added"] == 1
    assert result["skipped"] == 2
    assert result["problems"] == ["пустая строка не добавляется"] * 2
    assert read_items(path, clean_query) == ["ok"]


def test_add_items_normalizes_domains_before_dedup(tmp_path):
    path = tmp_path / "domains.txt"

    result = add_items(path, ["https://www.edelind.de/x", "edelind.de"], clean=clean_domain)

    assert result == {"added": 1, "skipped": 1, "problems": ["уже есть: edelind.de"]}
    assert read_items(path, clean_domain) == ["edelind.de"]


def test_add_items_with_nothing_to_add_does_not_touch_file(tmp_path):
    path = tmp_path / "queries.txt"
    path.write_text("keep\n", encoding="utf-8")

    result = add_items(path, ["keep"], clean=clean_query)

    assert result["added"] == 0
    assert path.read_text(encoding="utf-8") == "keep\n"


# --- удаление -------------------------------------------------------------------


def test_delete_items_by_value(tmp_path):
    path = tmp_path / "queries.txt"
    path.write_text("first\nsecond\nthird\n", encoding="utf-8")

    result = delete_items(path, ["second"], clean=clean_query)

    assert result == {"deleted": 1, "skipped": 0, "problems": []}
    assert read_items(path, clean_query) == ["first", "third"]


def test_delete_items_is_best_effort_for_missing_values(tmp_path):
    path = tmp_path / "queries.txt"
    path.write_text("first\nsecond\n", encoding="utf-8")

    result = delete_items(path, ["first", "ghost"], clean=clean_query)

    assert result["deleted"] == 1
    assert result["skipped"] == 1
    assert result["problems"] == ["не найдено: ghost"]
    assert read_items(path, clean_query) == ["second"]


def test_delete_items_matches_case_insensitively(tmp_path):
    path = tmp_path / "queries.txt"
    path.write_text("Wireless Keyboard\n", encoding="utf-8")

    result = delete_items(path, ["wireless keyboard"], clean=clean_query)

    assert result["deleted"] == 1
    assert read_items(path, clean_query) == []


def test_delete_all_empties_the_file(tmp_path):
    path = tmp_path / "domains.txt"
    path.write_text("a.de\nb.de\n", encoding="utf-8")

    result = delete_items(path, [], clean=clean_domain, delete_all=True)

    assert result == {"deleted": 2, "skipped": 0, "problems": []}
    assert path.read_text(encoding="utf-8") == ""


def test_delete_all_on_missing_file_reports_it(tmp_path):
    result = delete_items(tmp_path / "nope.txt", [], clean=clean_query, delete_all=True)

    assert result["deleted"] == 0
    problems = result["problems"]
    assert isinstance(problems, list)
    assert problems[0].startswith("файл не найден:")


def test_delete_of_missing_file_reports_it_even_with_targets(tmp_path):
    result = delete_items(tmp_path / "nope.txt", ["ghost"], clean=clean_query)

    assert result == {
        "deleted": 0,
        "skipped": 0,
        "problems": [f"файл не найден: {tmp_path / 'nope.txt'}"],
    }
