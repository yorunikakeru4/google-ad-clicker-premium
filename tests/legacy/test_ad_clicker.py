"""Тесты ad_clicker: CLI-контракт, выбор query/proxy и User-Agent прогона.

Сам ``run_scenario`` требует браузера, поэтому целиком не проверяется:
покрыты чистые функции выбора запроса/прокси/User-Agent и диспетчеризация
``main()``, которые и есть поведение, перенесённое в воркер. Прогона с
назначенным профилем касается только строка UA — она проверяется на
подменённой точке создания драйвера, до Chrome и сети.
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


class TestResolveProxyEnv:
    """Env-контракт супервизора: ``ADCLICKER_PROXY`` главнее legacy-источников.

    Супервизор закрепляет прокси за воркером и передаёт его через окружение;
    legacy-путь (файл со списком, ``random.choice``) остаётся для одиночного
    запуска и при пустой переменной.
    """

    def test_env_proxy_wins_over_the_proxy_file(
        self, config, set_paths, sandbox_dir, monkeypatch
    ):
        set_paths(proxy_file=str(sandbox_dir / "proxies.txt"))
        monkeypatch.setenv("ADCLICKER_PROXY", "user:pass@10.0.0.1:9999")

        assert resolve_proxy(parse([])) == "user:pass@10.0.0.1:9999"

    def test_env_proxy_wins_over_the_cli_argument(self, monkeypatch):
        monkeypatch.setenv("ADCLICKER_PROXY", "user:pass@10.0.0.1:9999")

        assert resolve_proxy(parse(["-p", "1.2.3.4:80"])) == "user:pass@10.0.0.1:9999"

    def test_env_proxy_is_trimmed(self, monkeypatch):
        monkeypatch.setenv("ADCLICKER_PROXY", "  user:pass@10.0.0.1:9999  ")

        assert resolve_proxy(parse([])) == "user:pass@10.0.0.1:9999"

    def test_empty_env_keeps_the_legacy_choice(
        self, config, set_paths, sandbox_dir, monkeypatch
    ):
        set_paths(proxy_file=str(sandbox_dir / "proxies.txt"))
        monkeypatch.setenv("ADCLICKER_PROXY", "")

        assert resolve_proxy(parse([])) in {"127.0.0.1:8080", "user:pass@10.0.0.1:3128"}


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


# --- User-Agent прогона: профиль против случайного ---------------------------


class StopBeforeBrowser(Exception):
    """create_webdriver перехвачен: прогон обрывается до запуска Chrome."""


@pytest.fixture
def capture_user_agent(monkeypatch):
    """create_webdriver вместо браузера: запомнить UA и оборвать прогон."""

    captured = {}

    def fake_create_webdriver(proxy, user_agent, plugin_folder_name):
        captured["user_agent"] = user_agent
        raise StopBeforeBrowser()

    monkeypatch.setattr(ad_clicker, "create_webdriver", fake_create_webdriver)
    return captured


@pytest.fixture
def assign_profile(tmp_path, monkeypatch):
    """Профиль в своей БД, назначенный процессу через env супервизора."""

    from engine.db import migrations
    from engine.profile_apply import PROFILE_ID_ENV
    from engine.profile_pool import ProfilePool

    db = tmp_path / "profiles.db"
    migrations.migrate(db)
    monkeypatch.setenv("ADCLICKER_DB", str(db))
    pool = ProfilePool(db)

    def _assign(**fields):
        pool.add_profiles([{"name": "acc", **fields}])
        row = pool.list_profiles()[-1]
        monkeypatch.setenv(PROFILE_ID_ENV, str(row["id"]))
        return row["id"]

    return _assign


class TestProfileUserAgent:
    """UA прогона: назначенный профиль главнее случайного user-agent."""

    def test_profile_user_agent_wins_over_the_random_one(
        self, assign_profile, capture_user_agent, monkeypatch
    ):
        assign_profile(user_agent="UA/Profile")
        monkeypatch.setattr(ad_clicker, "get_random_user_agent_string", lambda: "UA/Random")

        with pytest.raises(StopBeforeBrowser):
            ad_clicker.run_scenario(query="usb hub")

        assert capture_user_agent["user_agent"] == "UA/Profile"

    def test_without_a_profile_the_random_user_agent_is_used(
        self, capture_user_agent, monkeypatch
    ):
        monkeypatch.delenv("ADCLICKER_PROFILE_ID", raising=False)
        monkeypatch.setattr(ad_clicker, "get_random_user_agent_string", lambda: "UA/Random")

        with pytest.raises(StopBeforeBrowser):
            ad_clicker.run_scenario(query="usb hub")

        assert capture_user_agent["user_agent"] == "UA/Random"

    def test_empty_profile_user_agent_falls_back_to_the_random_one(
        self, assign_profile, capture_user_agent, monkeypatch
    ):
        assign_profile(user_agent="   ")
        monkeypatch.setattr(ad_clicker, "get_random_user_agent_string", lambda: "UA/Random")

        with pytest.raises(StopBeforeBrowser):
            ad_clicker.run_scenario(query="usb hub")

        assert capture_user_agent["user_agent"] == "UA/Random"

    def test_profile_without_a_user_agent_keeps_the_random_one(
        self, assign_profile, capture_user_agent, monkeypatch
    ):
        assign_profile(locale="de-DE")
        monkeypatch.setattr(ad_clicker, "get_random_user_agent_string", lambda: "UA/Random")

        with pytest.raises(StopBeforeBrowser):
            ad_clicker.run_scenario(query="usb hub")

        assert capture_user_agent["user_agent"] == "UA/Random"
