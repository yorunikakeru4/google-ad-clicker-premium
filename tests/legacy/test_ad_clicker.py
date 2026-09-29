"""Тесты ad_clicker: CLI-контракт и выбор query/proxy для прогона.

Сам ``run_scenario`` требует браузера, поэтому не проверяется: покрыты
чистые функции выбора запроса и прокси и диспетчеризация ``main()``,
которые и есть поведение, перенесённое в воркер.
"""

import contextlib
import io
import sys

import pytest

import ad_clicker
from ad_clicker import get_arg_parser, resolve_proxy, resolve_query


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


class TestResolveQuery:
    """Выбор запроса для прогона: аргумент CLI против config.json."""

    def test_argument_wins_over_config(self, config):
        config.behavior.query = "from config"

        assert resolve_query(parse(["-q", "from cli"])) == "from cli"

    def test_config_is_used_when_no_argument(self, config):
        config.behavior.query = "from config"

        assert resolve_query(parse([])) == "from config"

    def test_empty_query_everywhere_is_an_error(self, config):
        config.behavior.query = ""

        with pytest.raises(SystemExit):
            resolve_query(parse([]))


class TestResolveProxy:
    """Выбор прокси для прогона: аргумент, файл со списком, конфиг, ничего."""

    def test_argument_wins_over_config(self, config):
        config.webdriver.proxy = "10.0.0.1:8080"

        assert resolve_proxy(parse(["-p", "1.2.3.4:80"])) == "1.2.3.4:80"

    def test_proxy_file_is_read_and_respected(self, config, set_paths, sandbox_dir):
        set_paths(proxy_file=str(sandbox_dir / "proxies.txt"))
        config.webdriver.proxy = "10.0.0.1:8080"

        # Файл главнее одиночного прокси: так же это решает и воркер.
        assert resolve_proxy(parse([])) in {"127.0.0.1:8080", "user:pass@10.0.0.1:3128"}

    def test_single_proxy_from_config_when_no_file(self, config, set_paths):
        set_paths(proxy_file="")
        config.webdriver.proxy = "10.0.0.1:8080"

        assert resolve_proxy(parse([])) == "10.0.0.1:8080"

    def test_nothing_configured_yields_none(self, config, set_paths):
        set_paths(proxy_file="")
        config.webdriver.proxy = ""

        assert resolve_proxy(parse([])) is None


class TestMainDispatch:
    """main() разбирает argv и передаёт управление в run_scenario."""

    def test_main_hands_over_to_run_scenario(self, monkeypatch, config, set_paths):
        monkeypatch.setattr(sys, "argv", ["ad_clicker.py"])
        set_paths(proxy_file="")
        config.webdriver.proxy = ""
        config.behavior.query = "usb hub"
        captured = {}

        def fake_run_scenario(**kwargs):
            captured.update(kwargs)
            return True

        monkeypatch.setattr(ad_clicker, "run_scenario", fake_run_scenario)

        ad_clicker.main()

        assert captured["query"] == "usb hub"
        assert captured["proxy"] is None
        assert captured["browser_id"] is None
        assert captured["device_id"] is None
        assert captured["check_stealth"] is False

    def test_report_mode_does_not_run_a_scenario(self, monkeypatch, config):
        monkeypatch.setattr(sys, "argv", ["ad_clicker.py", "--report_clicks"])
        calls = []
        monkeypatch.setattr(ad_clicker, "run_scenario", lambda **kwargs: calls.append(kwargs))

        with contextlib.redirect_stdout(io.StringIO()):
            ad_clicker.main()

        assert calls == [], "отчёт не должен запускать сценарий"
