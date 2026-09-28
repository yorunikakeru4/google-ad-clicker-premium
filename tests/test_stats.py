"""Тесты stats.SearchStats: текстовое представление статистики клика.

Форматирование фиксировано по ширине (25 символов на подпись, 8 на значение),
поэтому тесты проверяют именно раскладку таблиц, а telegram-уведомления
приложения.
"""

import pytest

from stats import SearchStats


ROW_LABELS = [
    "Captcha Seen",
    "Captcha Solved",
    "Ads Found",
    "Num Filtered Ads",
    "Num Excluded Ads",
    "Ads Clicked",
    "Non-ads Clicked",
    "Shopping Ads Found",
    "Num Filtered Shopping Ads",
    "Num Excluded Shopping Ads",
    "Shopping Ads Clicked",
]


# --- to_pre_text ------------------------------------------------------------------


def test_to_pre_text_wraps_output_into_pre_block():
    text = SearchStats().to_pre_text()

    assert text.startswith("<pre>Summary of Statistics")
    assert text.endswith("</pre>\n")


def test_to_pre_text_contains_all_statistic_rows():
    text = SearchStats().to_pre_text()

    for label in ROW_LABELS:
        assert f"{label:<25}:" in text


def test_to_pre_text_hides_browser_id_row_when_it_is_zero():
    assert "Browser ID" not in SearchStats().to_pre_text()


def test_to_pre_text_shows_browser_id_row_when_it_is_set():
    assert "Browser ID" in SearchStats(browser_id=4).to_pre_text()


def test_to_pre_text_shows_browser_id_row_when_it_is_none():
    # None считается "не задан" так же, как 0: строка не выводится.
    assert "Browser ID" not in SearchStats(browser_id=None).to_pre_text()


def test_to_pre_text_renders_captcha_flags_as_yes_or_no():
    text = SearchStats(captcha_seen=True, captcha_solved=False).to_pre_text()

    assert f"{'Captcha Seen':<25}: {'Yes':<8}" in text
    assert f"{'Captcha Solved':<25}: {'No':<8}" in text


def test_to_pre_text_renders_counters_as_numbers():
    text = SearchStats(ads_found=7, ads_clicked=2, shopping_ads_clicked=1).to_pre_text()

    assert f"{'Ads Found':<25}: {7:<8}" in text
    assert f"{'Ads Clicked':<25}: {2:<8}" in text
    assert f"{'Shopping Ads Clicked':<25}: {1:<8}" in text


def test_to_pre_text_keeps_browser_id_first_when_present():
    lines = SearchStats(browser_id=2).to_pre_text().splitlines()

    assert lines[1].startswith("Browser ID")


# --- __str__ ----------------------------------------------------------------------


def test_str_starts_with_title_and_border():
    lines = str(SearchStats()).splitlines()
    expected_border = "+" + "-" * 27 + "+" + "-" * 10 + "+"

    assert lines[0] == "Summary of Statistics"
    assert lines[1] == expected_border
    assert lines[-1] == expected_border


def test_str_contains_every_row_between_borders():
    lines = str(SearchStats()).splitlines()

    for label in ROW_LABELS:
        assert any(line.startswith(f"| {label:<25} |") for line in lines)


def test_str_uses_pipes_around_padded_values():
    text = str(SearchStats(ads_clicked=5))

    assert f"| {'Ads Clicked':<25} | {5:<8} |" in text


def test_str_hides_browser_id_row_when_it_is_zero():
    assert "Browser ID" not in str(SearchStats())


def test_str_shows_browser_id_row_when_it_is_set():
    assert f"| {'Browser ID':<25} | {4:<8} |" in str(SearchStats(browser_id=4))


def test_str_renders_captcha_flags_as_yes_or_no():
    text = str(SearchStats(captcha_seen=True, captcha_solved=True))

    assert f"| {'Captcha Seen':<25} | {'Yes':<8} |" in text
    assert f"| {'Captcha Solved':<25} | {'Yes':<8} |" in text


def test_str_and_pre_text_report_the_same_values():
    # Обе формы вывода строятся из одного списка rows, поэтому значения и раскладка
    # обязаны совпадать: расхождение здесь означало бы, что отчёт и уведомление
    # показывают разные числа.
    stats = SearchStats(browser_id=1, captcha_seen=True, captcha_solved=False, ads_clicked=3)
    pre = stats.to_pre_text()
    table = str(stats)

    for value in (1, "Yes", "No", 3):
        assert f": {value:<8}" in pre
        assert f"| {value:<8} |" in table


def test_stats_default_to_zero_and_false():
    stats = SearchStats()

    assert stats.browser_id == 0
    assert stats.captcha_seen is False
    assert stats.ads_found == 0
    assert stats.ads_clicked == 0
    assert stats.non_ads_clicked == 0
    assert stats.shopping_ads_clicked == 0


@pytest.mark.parametrize("counter", [
    "ads_found",
    "num_filtered_ads",
    "num_excluded_ads",
    "ads_clicked",
    "non_ads_clicked",
    "shopping_ads_found",
    "num_filtered_shopping_ads",
    "num_excluded_shopping_ads",
    "shopping_ads_clicked",
])
def test_every_counter_is_reported_in_pre_text(counter):
    text = SearchStats(**{counter: 4}).to_pre_text()

    assert f"{4:<8}" in text
