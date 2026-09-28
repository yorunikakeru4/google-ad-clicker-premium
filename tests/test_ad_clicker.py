"""Тесты ad_clicker.get_arg_parser: контракт CLI запуска кликера.

Сама функция main() требует браузера, поэтому покрывается только построение
парсера аргументов - это чистая функция, через которую проходит весь запуск.
"""

import pytest

from ad_clicker import get_arg_parser


def parse(argv):
    return get_arg_parser().parse_args(argv)


def test_parser_supports_documented_options():
    options = {action.dest for action in get_arg_parser()._actions}

    assert {
        "query",
        "proxy",
        "id",
        "enable_telegram",
        "report_clicks",
        "date",
        "excel",
        "check_stealth",
        "device_id",
    } <= options


def test_query_is_read_from_short_and_long_flags():
    assert parse(["-q", "usb hub"]).query == "usb hub"
    assert parse(["--query", "usb hub"]).query == "usb hub"


def test_proxy_is_read_from_short_and_long_flags():
    assert parse(["-p", "127.0.0.1:8080"]).proxy == "127.0.0.1:8080"
    assert parse(["--proxy", "user:pass@1.2.3.4:8080"]).proxy == "user:pass@1.2.3.4:8080"


def test_optional_values_default_to_none():
    args = parse([])

    assert args.query is None
    assert args.proxy is None
    assert args.id is None
    assert args.date is None
    assert args.device_id is None


def test_boolean_flags_default_to_false():
    args = parse([])

    assert args.enable_telegram is False
    assert args.report_clicks is False
    assert args.excel is False
    assert args.check_stealth is False


def test_boolean_flags_are_set_by_presence_only():
    assert parse(["--enable_telegram"]).enable_telegram is True
    assert parse(["--report_clicks"]).report_clicks is True
    assert parse(["--excel"]).excel is True
    assert parse(["--check_stealth"]).check_stealth is True


def test_browser_id_is_parsed_as_string():
    assert parse(["--id", "3"]).id == "3"


def test_device_id_is_parsed_from_short_and_long_flags():
    assert parse(["-d", "emulator-5554"]).device_id == "emulator-5554"
    assert parse(["--device_id", "emulator-5554"]).device_id == "emulator-5554"


def test_report_date_is_kept_verbatim():
    # Формат даты потом разбирается в datetime.strptime, парсер его не проверяет.
    assert parse(["--report_clicks", "--date", "28-09-2026"]).date == "28-09-2026"


def test_query_with_spaces_survives_parsing():
    assert parse(["-q", "wireless keyboard"]).query == "wireless keyboard"


def test_help_is_disabled_and_points_to_readme():
    assert "See README.md file" in get_arg_parser().format_usage()

    with pytest.raises(SystemExit):
        parse(["-h"])


def test_unknown_option_is_rejected():
    with pytest.raises(SystemExit):
        parse(["--definitely-not-an-option"])
