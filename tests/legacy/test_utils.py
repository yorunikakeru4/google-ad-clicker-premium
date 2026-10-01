"""Тесты utils.py: чтение файлов, паузы, разбор ответов 2captcha, отчёты.

Все файлы данных тесты создают сами в tmp_path, поэтому реальные queries.txt,
user_agents.txt, domains.txt и cookies.txt из репозитория не используются.
"""

import json
import platform
import random
import time

import openpyxl
import pytest

import utils


# --- get_random_sleep -------------------------------------------------------------


def test_get_random_sleep_stays_inside_given_range():
    for _ in range(50):
        assert 0.5 <= utils.get_random_sleep(0.5, 1.5) <= 1.5


def test_get_random_sleep_rounds_to_two_decimals(monkeypatch):
    monkeypatch.setattr(random, "uniform", lambda start, end: 1.23456789)

    assert utils.get_random_sleep(1, 2) == 1.23


def test_get_random_sleep_for_equal_bounds_returns_that_value(monkeypatch):
    monkeypatch.setattr(random, "uniform", lambda start, end: start)

    assert utils.get_random_sleep(2, 2) == 2.0


def test_get_random_sleep_uses_both_bounds(monkeypatch):
    seen = {}

    def fake_uniform(start, end):
        seen["range"] = (start, end)
        return start

    monkeypatch.setattr(random, "uniform", fake_uniform)

    utils.get_random_sleep(3, 9)

    assert seen["range"] == (3, 9)


# --- _check_error -----------------------------------------------------------------


@pytest.mark.parametrize(
    "response_text",
    [
        "ERROR_WRONG_USER_KEY",
        "ERROR_KEY_DOES_NOT_EXIST",
        "ERROR_ZERO_BALANCE",
        "IP_BANNED",
        "ERROR_GOOGLEKEY",
    ],
)
def test_check_error_in_php_exits_on_fatal_errors(response_text):
    exit_, cont, brk = utils._check_error(response_text)

    assert exit_ is True
    assert cont is False
    assert brk is False


def test_check_error_in_php_continues_when_no_slots_available(monkeypatch):
    monkeypatch.setattr(utils.config.behavior, "wait_factor", 0.0)

    exit_, cont, brk = utils._check_error("ERROR_NO_SLOT_AVAILABLE")

    assert (exit_, cont, brk) == (False, True, False)


def test_check_error_in_php_breaks_on_task_id(monkeypatch):
    monkeypatch.setattr(utils.config.behavior, "wait_factor", 0.0)

    exit_, cont, brk = utils._check_error("OK|2122988149")

    assert (exit_, cont, brk) == (False, False, True)


def test_check_error_in_php_fatal_error_wins_over_continue_error(monkeypatch):
    monkeypatch.setattr(utils.config.behavior, "wait_factor", 0.0)

    exit_, cont, brk = utils._check_error("ERROR_ZERO_BALANCE ERROR_NO_SLOT_AVAILABLE")

    assert (exit_, cont, brk) == (True, False, False)


def test_check_error_res_php_exits_on_wrong_user_key(monkeypatch):
    monkeypatch.setattr(utils.config.behavior, "wait_factor", 0.0)

    assert utils._check_error("ERROR_WRONG_USER_KEY", "res_php") == (True, False, False)


def test_check_error_res_php_exits_on_unsolvable_captcha(monkeypatch):
    monkeypatch.setattr(utils.config.behavior, "wait_factor", 0.0)

    assert utils._check_error("ERROR_CAPTCHA_UNSOLVABLE", "res_php") == (True, False, False)


def test_check_error_res_php_continues_while_captcha_not_ready(monkeypatch):
    monkeypatch.setattr(utils.config.behavior, "wait_factor", 0.0)

    assert utils._check_error("CAPCHA_NOT_READY", "res_php") == (False, True, False)


def test_check_error_res_php_breaks_on_solved_captcha(monkeypatch):
    monkeypatch.setattr(utils.config.behavior, "wait_factor", 0.0)

    assert utils._check_error("OK|abc123", "res_php") == (False, False, True)


def test_check_error_unknown_request_type_returns_no_flags(caplog):
    assert utils._check_error("OK|1", "unknown_type") == (False, False, False)

    # Текст запроса ушёл в поля, а не в message: проверяем структурную
    # запись в зеркале legacy-логгера, а не склеенную строку.
    record = next(
        entry for entry in caplog.records if entry.getMessage().startswith("Wrong request type")
    )
    assert record.category == "captcha"
    assert record.fields == {"request_type": "unknown_type"}


def test_check_error_in_php_treats_res_php_captcha_not_ready_as_success(monkeypatch):
    # Ошибка res.php не обрабатывается в режиме in.php и считается успехом:
    # это текущее поведение, которое важно не потерять при рефакторинге.
    monkeypatch.setattr(utils.config.behavior, "wait_factor", 0.0)

    assert utils._check_error("CAPCHA_NOT_READY") == (False, False, True)


def test_check_error_wait_is_scaled_by_wait_factor(monkeypatch):
    # error_wait = 5 * wait_factor; две точки фиксируют коэффициент 5.
    monkeypatch.setattr(utils.config.behavior, "wait_factor", 0.08)
    started = time.monotonic()
    utils._check_error("ERROR_NO_SLOT_AVAILABLE")
    assert time.monotonic() - started >= 0.4

    monkeypatch.setattr(utils.config.behavior, "wait_factor", 0.2)
    started = time.monotonic()
    utils._check_error("CAPCHA_NOT_READY", "res_php")
    assert time.monotonic() - started >= 1.0


def test_check_error_does_not_wait_when_wait_factor_is_zero(monkeypatch):
    monkeypatch.setattr(utils.config.behavior, "wait_factor", 0.0)

    started = time.monotonic()
    utils._check_error("ERROR_NO_SLOT_AVAILABLE")
    elapsed = time.monotonic() - started

    assert elapsed < 0.1


# --- Чтение списков из файлов ----------------------------------------------------


def test_get_queries_strips_whitespace_and_quotes(set_paths, tmp_path):
    query_file = tmp_path / "queries.txt"
    query_file.write_text("  'wireless keyboard'  \n\"bluetooth headphones\"\n", "utf-8")
    set_paths(query_file=query_file)

    assert utils.get_queries() == ["wireless keyboard", "bluetooth headphones"]


def test_get_queries_skips_blank_lines(set_paths, tmp_path):
    # Пустая строка в файле запросов стала бы пустым поисковым запросом:
    # кликер бы искал по пустой строке и впустую тратил трафик.
    query_file = tmp_path / "queries.txt"
    query_file.write_text("usb hub\n\n\nwebcam\n", "utf-8")
    set_paths(query_file=query_file)

    assert utils.get_queries() == ["usb hub", "webcam"]


def test_get_queries_skips_lines_of_whitespace_and_quotes(set_paths, tmp_path):
    query_file = tmp_path / "queries.txt"
    query_file.write_text("usb hub\n   \n''\n\"\"\nwebcam\n", "utf-8")
    set_paths(query_file=query_file)

    assert utils.get_queries() == ["usb hub", "webcam"]


def test_get_queries_returns_empty_list_when_file_has_only_blank_lines(set_paths, tmp_path):
    query_file = tmp_path / "queries.txt"
    query_file.write_text("\n\n \n", "utf-8")
    set_paths(query_file=query_file)

    assert utils.get_queries() == []


def test_get_domains_skips_blank_lines(set_paths, tmp_path):
    domains_file = tmp_path / "domains.txt"
    domains_file.write_text("www.booking.com\n\n  \nwww.sixt.com\n", "utf-8")
    set_paths(filtered_domains=domains_file)

    assert utils.get_domains() == ["www.booking.com", "www.sixt.com"]


def test_get_domains_returns_empty_list_when_file_has_only_blank_lines(set_paths, tmp_path):
    domains_file = tmp_path / "domains.txt"
    domains_file.write_text("\n''\n", "utf-8")
    set_paths(filtered_domains=domains_file)

    assert utils.get_domains() == []


def test_get_user_agents_skips_blank_lines(tmp_path):
    ua_file = tmp_path / "user_agents.txt"
    ua_file.write_text("Mozilla/5.0 Windows\n\n  \nMozilla/5.0 Linux\n", "utf-8")

    assert utils._get_user_agents(ua_file) == ["Mozilla/5.0 Windows", "Mozilla/5.0 Linux"]


def test_get_user_agents_returns_empty_list_when_file_has_only_blank_lines(tmp_path):
    ua_file = tmp_path / "user_agents.txt"
    ua_file.write_text("\n\"\"\n", "utf-8")

    assert utils._get_user_agents(ua_file) == []


def test_get_queries_exits_when_file_is_missing(set_paths, tmp_path):
    set_paths(query_file=tmp_path / "nope.txt")

    with pytest.raises(SystemExit) as excinfo:
        utils.get_queries()

    assert "Couldn't find queries file" in str(excinfo.value)


def test_get_domains_strips_whitespace_and_quotes(set_paths, tmp_path):
    domains_file = tmp_path / "domains.txt"
    domains_file.write_text(" 'www.booking.com' \n\"www.sixt.com\"\n", "utf-8")
    set_paths(filtered_domains=domains_file)

    assert utils.get_domains() == ["www.booking.com", "www.sixt.com"]


def test_get_domains_exits_when_file_is_missing(set_paths, tmp_path):
    set_paths(filtered_domains=tmp_path / "nope.txt")

    with pytest.raises(SystemExit) as excinfo:
        utils.get_domains()

    assert "Couldn't find domains file" in str(excinfo.value)


def test_get_user_agents_strips_whitespace_and_quotes(tmp_path):
    ua_file = tmp_path / "user_agents.txt"
    ua_file.write_text(" 'Mozilla/5.0 Windows' \n\"Mozilla/5.0 Linux\"\n", "utf-8")

    assert utils._get_user_agents(ua_file) == ["Mozilla/5.0 Windows", "Mozilla/5.0 Linux"]


def test_get_user_agents_exits_when_file_is_missing(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        utils._get_user_agents(tmp_path / "nope.txt")

    assert "Couldn't find user agents file" in str(excinfo.value)


# --- get_locale_language ----------------------------------------------------------


def test_get_locale_language_returns_list_for_known_country():
    assert utils.get_locale_language("US") == ["en-US", "en"]


def test_get_locale_language_keeps_all_locales_for_multilingual_country():
    assert utils.get_locale_language("AF") == ["ps-AF", "uz-AF"]


def test_get_locale_language_falls_back_to_english_for_unknown_country():
    assert utils.get_locale_language("ZZ") == ["en"]


def test_get_locale_language_falls_back_to_module_dir_when_cwd_has_no_file(
    isolated_cwd,
):
    # Упакованный запуск: cwd — каталог данных без этого файла, локали
    # обязаны браться из каталога модуля (в frozen это _MEIPASS, где файл
    # лежит в бандле sidecar).
    (isolated_cwd / "country_to_locale.json").unlink()

    assert utils.get_locale_language("US") == ["en-US", "es-US"], (
        "значения должны прийти из настоящего country_to_locale.json рядом с utils.py"
    )


def test_get_locale_language_raises_when_mapping_file_is_missing(
    isolated_cwd, monkeypatch
):
    # «Пропал» в обоих местах: нет в cwd и нет рядом с модулем — иначе
    # фолбэк спрятал бы отсутствие файла, которое здесь и проверяем.
    import engine.diagnostics as diagnostics

    (isolated_cwd / "country_to_locale.json").unlink()
    monkeypatch.setattr(
        diagnostics, "COUNTRY_LOCALES_FILE", isolated_cwd / "absent.json"
    )

    with pytest.raises(FileNotFoundError):
        utils.get_locale_language("US")


# --- Direction --------------------------------------------------------------------


def test_direction_members_keep_their_values():
    assert utils.Direction.UP.value == "UP"
    assert utils.Direction.DOWN.value == "DOWN"
    assert utils.Direction.LEFT.value == "LEFT"
    assert utils.Direction.RIGHT.value == "RIGHT"
    assert utils.Direction.BOTH.value == "BOTH"


# --- resolve_redirect -------------------------------------------------------------


@pytest.mark.parametrize(
    "broken_url",
    ["htp:/broken-url", "not-a-url", "://missing-scheme"],
)
def test_resolve_redirect_returns_original_url_when_request_fails(broken_url, caplog):
    # Сетевых обращений здесь нет: requests падает на разборе URL до выхода в сеть.
    assert utils.resolve_redirect(broken_url) == broken_url
    assert "Error resolving URL redirection" in caplog.text


# --- add_cookies ------------------------------------------------------------------


def test_add_cookies_normalizes_samesite_values(isolated_cwd, recording_cookie_driver):
    (isolated_cwd / "cookies.txt").write_text(
        json.dumps(
            [
                {"name": "a", "sameSite": "strict", "secure": True},
                {"name": "b", "sameSite": "lax", "secure": False},
                {"name": "c", "sameSite": "no_restriction", "secure": True},
                {"name": "d", "sameSite": "anything", "secure": False},
            ]
        ),
        encoding="utf-8",
    )

    utils.add_cookies(recording_cookie_driver)

    same_site = {cookie["name"]: cookie["sameSite"] for cookie in recording_cookie_driver.cookies}
    assert same_site == {"a": "Strict", "b": "Lax", "c": "None", "d": "Lax"}


def test_add_cookies_keeps_all_other_cookie_fields(isolated_cwd, recording_cookie_driver):
    (isolated_cwd / "cookies.txt").write_text(
        json.dumps(
            [{"name": "session", "value": "42", "domain": ".example.com", "sameSite": "lax"}]
        ),
        encoding="utf-8",
    )

    utils.add_cookies(recording_cookie_driver)

    assert recording_cookie_driver.cookies[0]["name"] == "session"
    assert recording_cookie_driver.cookies[0]["value"] == "42"
    assert recording_cookie_driver.cookies[0]["domain"] == ".example.com"


@pytest.mark.xfail(
    reason=(
        "Баг legacy-кода: add_cookies обращается к cookie['sameSite'] и "
        "cookie['secure'] напрямую, поэтому cookie без этих полей (например "
        "выгруженный не из Chrome) роняет кликер с голым KeyError"
    ),
    strict=True,
)
def test_add_cookies_should_tolerate_cookie_without_samesite(
    isolated_cwd, recording_cookie_driver
):
    (isolated_cwd / "cookies.txt").write_text(
        json.dumps([{"name": "session", "value": "42", "domain": ".example.com"}]),
        encoding="utf-8",
    )

    utils.add_cookies(recording_cookie_driver)

    assert recording_cookie_driver.cookies[0]["name"] == "session"


def test_add_cookies_exits_when_cookies_file_is_missing(isolated_cwd, recording_cookie_driver):
    (isolated_cwd / "cookies.txt").unlink()

    with pytest.raises(SystemExit) as excinfo:
        utils.add_cookies(recording_cookie_driver)

    assert "Missing cookies.txt file!" in str(excinfo.value)


def test_add_cookies_exits_on_malformed_json(isolated_cwd, recording_cookie_driver, caplog):
    (isolated_cwd / "cookies.txt").write_text("{oops", encoding="utf-8")

    with pytest.raises(SystemExit):
        utils.add_cookies(recording_cookie_driver)

    assert "Failed to read cookies file. Check format and try again." in caplog.text


# --- generate_click_report --------------------------------------------------------


def test_generate_click_report_writes_xlsx_with_headers(isolated_cwd):
    results = [("http://example.com", 3, "Ad", "10:11:12", "usb hub")]

    utils.generate_click_report(results, "28-09-2026")

    sheet = openpyxl.load_workbook(isolated_cwd / "click_report_28-09-2026.xlsx").active
    headers = [sheet["A1"].value, sheet["B1"].value, sheet["C1"].value, sheet["D1"].value,
               sheet["E1"].value]
    assert headers == ["URL", "Query", "Clicks", "Time", "Category"]


def test_generate_click_report_orders_columns_url_query_clicks_time_category(isolated_cwd):
    results = [("http://example.com", 3, "Ad", "10:11:12", "usb hub")]

    utils.generate_click_report(results, "28-09-2026")

    sheet = openpyxl.load_workbook(isolated_cwd / "click_report_28-09-2026.xlsx").active
    assert [sheet["A2"].value, sheet["B2"].value, sheet["C2"].value, sheet["D2"].value,
            sheet["E2"].value] == [
        "http://example.com",
        "usb hub",
        3,
        "28-09-2026 10:11:12",
        "Ad",
    ]


def test_generate_click_report_writes_one_row_per_result(isolated_cwd):
    results = [
        ("http://a.example", 1, "Ad", "10:00:00", "usb hub"),
        ("http://b.example", 2, "Non-ad", "11:00:00", "webcam"),
        ("http://c.example", 4, "Shopping", "12:00:00", "monitor"),
    ]

    utils.generate_click_report(results, "28-09-2026")

    sheet = openpyxl.load_workbook(isolated_cwd / "click_report_28-09-2026.xlsx").active
    assert sheet.max_row == 4
    assert [row[4] for row in sheet.iter_rows(min_row=2, values_only=True)] == [
        "Ad",
        "Non-ad",
        "Shopping",
    ]


def test_generate_click_report_creates_only_headers_without_results(isolated_cwd):
    utils.generate_click_report([], "28-09-2026")

    sheet = openpyxl.load_workbook(isolated_cwd / "click_report_28-09-2026.xlsx").active
    assert sheet.max_row == 1
    assert sheet["A1"].value == "URL"


def test_generate_click_report_sets_column_widths(isolated_cwd):
    utils.generate_click_report([], "28-09-2026")

    sheet = openpyxl.load_workbook(isolated_cwd / "click_report_28-09-2026.xlsx").active
    widths = {letter: sheet.column_dimensions[letter].width for letter in "ABCDE"}
    assert widths == {"A": 80, "B": 25, "C": 15, "D": 20, "E": 15}


# --- get_random_user_agent_string -------------------------------------------------


def test_get_random_user_agent_string_filters_by_linux(set_paths, tmp_path, monkeypatch):
    ua_file = tmp_path / "user_agents.txt"
    ua_file.write_text("Mozilla/5.0 (X11; Linux x86_64) Chrome/118\n"
                       "Mozilla/5.0 (Windows NT 10.0) Chrome/120\n", encoding="utf-8")
    set_paths(user_agents=ua_file)
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    for _ in range(20):
        assert utils.get_random_user_agent_string() == "Mozilla/5.0 (X11; Linux x86_64) Chrome/118"


def test_get_random_user_agent_string_filters_by_windows(set_paths, tmp_path, monkeypatch):
    ua_file = tmp_path / "user_agents.txt"
    ua_file.write_text("Mozilla/5.0 (X11; Linux x86_64) Chrome/118\n"
                       "Mozilla/5.0 (Windows NT 10.0) Chrome/120\n", encoding="utf-8")
    set_paths(user_agents=ua_file)
    monkeypatch.setattr(platform, "system", lambda: "Windows")

    assert utils.get_random_user_agent_string() == "Mozilla/5.0 (Windows NT 10.0) Chrome/120"


def test_get_random_user_agent_string_keeps_ios_agents_on_macos(set_paths, tmp_path, monkeypatch):
    ua_file = tmp_path / "user_agents.txt"
    ua_file.write_text("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari\n"
                       "Mozilla/5.0 (Windows NT 10.0) Chrome/120\n", encoding="utf-8")
    set_paths(user_agents=ua_file)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")

    assert utils.get_random_user_agent_string().startswith("Mozilla/5.0 (iPhone")


def test_get_random_user_agent_string_counts_android_as_linux(set_paths, tmp_path, monkeypatch):
    ua_file = tmp_path / "user_agents.txt"
    ua_file.write_text("Mozilla/5.0 (Linux; Android 13) Chrome/117\n", encoding="utf-8")
    set_paths(user_agents=ua_file)
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    assert utils.get_random_user_agent_string() == "Mozilla/5.0 (Linux; Android 13) Chrome/117"


def test_get_random_user_agent_string_falls_back_to_all_agents_on_unknown_os(
    set_paths, tmp_path, monkeypatch
):
    ua_file = tmp_path / "user_agents.txt"
    ua_file.write_text("Mozilla/5.0 (Windows NT 10.0) Chrome/120\n", encoding="utf-8")
    set_paths(user_agents=ua_file)
    monkeypatch.setattr(platform, "system", lambda: "Plan9")

    assert utils.get_random_user_agent_string() == "Mozilla/5.0 (Windows NT 10.0) Chrome/120"


def test_get_random_user_agent_string_exits_when_no_agent_matches_os(
    set_paths, tmp_path, monkeypatch
):
    ua_file = tmp_path / "user_agents.txt"
    ua_file.write_text("Mozilla/5.0 (Windows NT 10.0) Chrome/120\n", encoding="utf-8")
    set_paths(user_agents=ua_file)
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    with pytest.raises(IndexError):
        utils.get_random_user_agent_string()


@pytest.mark.xfail(
    reason=(
        "Баг legacy-кода: если в user_agents.txt нет строки под текущую ОС, "
        "random.choice() падает с голым IndexError без подсказки, что нужно "
        "добавить user-agent'ы для своей ОС"
    ),
    strict=True,
)
def test_get_random_user_agent_string_should_exit_with_readable_error(
    set_paths, tmp_path, monkeypatch, caplog
):
    ua_file = tmp_path / "user_agents.txt"
    ua_file.write_text("Mozilla/5.0 (Windows NT 10.0) Chrome/120\n", encoding="utf-8")
    set_paths(user_agents=ua_file)
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    with pytest.raises(SystemExit):
        utils.get_random_user_agent_string()

    assert "user agents" in caplog.text.lower()
