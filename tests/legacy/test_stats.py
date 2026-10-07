"""Тесты stats.SearchStats: компактная сводка статистики клика.

Сводка больше не ASCII-таблица: ``__str__`` даёт одну строку для записи в
лог, ``to_pre_text`` — те же сегменты, но каждый на своей строке внутри
``<pre>``-блока для Telegram. Обе формы строятся из одного списка сегментов,
поэтому обязаны показывать одни и те же значения, а рамок ``+---+`` во
выводе быть не должно.
"""

import pytest

from stats import SearchStats


# (поле, группа сводки, единица): каждый счётчик живёт в своей группе,
# тест проверяет, что он попадает в вывод именно туда.
COUNTER_CASES = [
    ("ads_found", "ads", "found"),
    ("num_filtered_ads", "ads", "filtered"),
    ("num_excluded_ads", "ads", "excluded"),
    ("ads_clicked", "ads", "clicked"),
    ("shopping_ads_found", "shopping", "found"),
    ("num_filtered_shopping_ads", "shopping", "filtered"),
    ("num_excluded_shopping_ads", "shopping", "excluded"),
    ("shopping_ads_clicked", "shopping", "clicked"),
    ("non_ads_clicked", "non-ads", "clicked"),
]

GROUPS = ["ads", "shopping", "captcha", "non-ads"]


def pre_body(text: str) -> str:
    """Содержимое ``<pre>``-блока без обёртки."""

    assert text.startswith("<pre>")
    assert text.endswith("</pre>\n")
    return text[len("<pre>") : -len("</pre>\n")]


def group_part(text: str, group: str) -> str:
    """Сегмент сводки по группе — работает и по ``str``, и по ``<pre>``-блоку."""

    for part in text.replace("\n", " | ").split(" | "):
        if part.startswith(f"{group}:"):
            return part
    raise AssertionError(f"группы {group!r} нет в {text!r}")


# --- to_pre_text ------------------------------------------------------------------


def test_to_pre_text_wraps_output_into_pre_block():
    text = SearchStats().to_pre_text()

    assert text.startswith("<pre>Summary of Statistics")
    assert text.endswith("</pre>\n")


def test_to_pre_text_has_no_table_borders():
    body = pre_body(SearchStats().to_pre_text())

    assert "+" not in body
    assert "---" not in body
    assert "|" not in body


@pytest.mark.parametrize("group", GROUPS)
def test_to_pre_text_contains_every_group(group):
    body = pre_body(SearchStats().to_pre_text())

    assert group_part(body, group)


def test_to_pre_text_hides_browser_id_when_it_is_zero():
    assert "browser" not in pre_body(SearchStats().to_pre_text())


def test_to_pre_text_shows_browser_id_when_it_is_set():
    assert "browser: 4" in pre_body(SearchStats(browser_id=4).to_pre_text())


def test_to_pre_text_shows_browser_id_when_it_is_none():
    # None считается "не задан" так же, как 0: сегмент не выводится.
    assert "browser" not in pre_body(SearchStats(browser_id=None).to_pre_text())


def test_to_pre_text_renders_captcha_flags_as_words():
    body = pre_body(SearchStats(captcha_seen=True, captcha_solved=False).to_pre_text())

    assert group_part(body, "captcha") == "captcha: seen, not solved"


def test_to_pre_text_renders_counters_as_numbers():
    body = pre_body(
        SearchStats(ads_found=7, ads_clicked=2, shopping_ads_clicked=1).to_pre_text()
    )

    assert "7 found" in group_part(body, "ads")
    assert "2 clicked" in group_part(body, "ads")
    assert "1 clicked" in group_part(body, "shopping")


def test_to_pre_text_keeps_browser_id_first_when_present():
    lines = pre_body(SearchStats(browser_id=2).to_pre_text()).splitlines()

    assert lines[0] == "Summary of Statistics"
    assert lines[1] == "browser: 2"


# --- __str__ ----------------------------------------------------------------------


def test_str_is_single_line_without_borders():
    text = str(SearchStats())

    assert len(text.splitlines()) == 1
    assert "+" not in text
    assert "---" not in text
    assert not any(line.startswith("|") for line in text.splitlines())


@pytest.mark.parametrize("group", GROUPS)
def test_str_contains_every_group(group):
    assert group_part(str(SearchStats()), group)


def test_str_hides_browser_id_when_it_is_zero():
    assert "browser" not in str(SearchStats())


def test_str_shows_browser_id_when_it_is_set():
    assert str(SearchStats(browser_id=4)).startswith("browser: 4 |")


def test_str_renders_captcha_flags_as_words():
    assert group_part(str(SearchStats(captcha_seen=True, captcha_solved=True)), "captcha") == (
        "captcha: seen, solved"
    )
    assert group_part(str(SearchStats()), "captcha") == "captcha: not seen, not solved"


def test_str_and_pre_text_report_the_same_parts():
    # Обе формы берут сегменты из одного метода: расхождение здесь означало бы,
    # что отчёт в логе и уведомление в Telegram показывают разные числа.
    stats = SearchStats(browser_id=1, captcha_seen=True, captcha_solved=False, ads_clicked=3)
    pre_parts = pre_body(stats.to_pre_text()).splitlines()[1:]

    assert str(stats).split(" | ") == pre_parts


def test_stats_default_to_zero_and_false():
    stats = SearchStats()

    assert stats.browser_id == 0
    assert stats.captcha_seen is False
    assert stats.ads_found == 0
    assert stats.ads_clicked == 0
    assert stats.non_ads_clicked == 0
    assert stats.shopping_ads_clicked == 0


@pytest.mark.parametrize("counter,group,unit", COUNTER_CASES)
def test_every_counter_is_reported_in_both_forms(counter, group, unit):
    stats = SearchStats(**{counter: 4})

    assert f"4 {unit}" in group_part(str(stats), group)
    assert f"4 {unit}" in group_part(pre_body(stats.to_pre_text()), group)
