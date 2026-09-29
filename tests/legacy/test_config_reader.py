"""Тесты config_reader.ConfigReader: чтение и валидация config.json.

Фикстура make_config пишет конфиг в tmp_path, поэтому тесты не зависят от
настоящего config.json в корне репозитория.
"""

import multiprocessing

import pytest


def test_read_parameters_fills_paths_section_from_config(make_config, base_config):
    base_config["paths"] = {
        "query_file": "/tmp/q.txt",
        "proxy_file": "/tmp/p.txt",
        "user_agents": "/tmp/ua.txt",
        "filtered_domains": "/tmp/d.txt",
    }

    paths = make_config(base_config).paths

    assert paths.query_file == "/tmp/q.txt"
    assert paths.proxy_file == "/tmp/p.txt"
    assert paths.user_agents == "/tmp/ua.txt"
    assert paths.filtered_domains == "/tmp/d.txt"


def test_read_parameters_maps_every_webdriver_option(make_config, base_config):
    base_config["paths"]["proxy_file"] = ""  # иначе конфликтует с webdriver.proxy
    base_config["webdriver"] = {
        "proxy": "127.0.0.1:8080",
        "auth": False,
        "incognito": True,
        "country_domain": True,
        "language_from_proxy": False,
        "ss_on_exception": True,
        "window_size": "1280,720",
        "shift_windows": True,
        "use_seleniumbase": True,
        "proxy_transport": "direct",
    }

    webdriver = make_config(base_config).webdriver

    assert webdriver.proxy == "127.0.0.1:8080"
    assert webdriver.auth is False
    assert webdriver.incognito is True
    assert webdriver.country_domain is True
    assert webdriver.language_from_proxy is False
    assert webdriver.ss_on_exception is True
    assert webdriver.window_size == "1280,720"
    assert webdriver.shift_windows is True
    assert webdriver.use_seleniumbase is True
    assert webdriver.proxy_transport == "direct"


def test_proxy_transport_defaults_to_cdp_auth_when_the_key_is_absent(
    make_config, base_config
):
    """Старый config.json без ключа обязан читаться, дефолт — cdp_auth."""

    assert "proxy_transport" not in base_config["webdriver"]

    assert make_config(base_config).webdriver.proxy_transport == "cdp_auth"


def test_proxy_transport_default_is_the_constant_from_proxy_auth(make_config, base_config):
    """Дефолт legacy-чтения не должен разъезжаться с engine.proxy_auth."""

    from engine.proxy_auth import DEFAULT_PROXY_TRANSPORT

    assert make_config(base_config).webdriver.proxy_transport == DEFAULT_PROXY_TRANSPORT


def test_read_parameters_maps_every_behavior_option(make_config, base_config):
    base_config["paths"]["query_file"] = ""  # иначе конфликтует с behavior.query
    base_config["behavior"] = {
        "query": "usb hub",
        "ad_page_min_wait": 1,
        "ad_page_max_wait": 2,
        "nonad_page_min_wait": 3,
        "nonad_page_max_wait": 4,
        "max_scroll_limit": 5,
        "check_shopping_ads": False,
        "excludes": "amazon,ebay",
        "random_mouse": True,
        "custom_cookies": True,
        "click_order": 3,
        "browser_count": 7,
        "multiprocess_style": 2,
        "loop_wait_time": 30,
        "wait_factor": 0.5,
        "running_interval_start": "09:00",
        "running_interval_end": "17:30",
        "2captcha_apikey": "key-42",
        "hooks_enabled": True,
        "telegram_enabled": True,
        "send_to_android": True,
        "request_boost": True,
        "captcha_policy": "solve",
    }

    behavior = make_config(base_config).behavior

    assert behavior.query == "usb hub"
    assert behavior.ad_page_min_wait == 1
    assert behavior.ad_page_max_wait == 2
    assert behavior.nonad_page_min_wait == 3
    assert behavior.nonad_page_max_wait == 4
    assert behavior.max_scroll_limit == 5
    assert behavior.check_shopping_ads is False
    assert behavior.excludes == "amazon,ebay"
    assert behavior.random_mouse is True
    assert behavior.custom_cookies is True
    assert behavior.click_order == 3
    assert behavior.multiprocess_style == 2
    assert behavior.loop_wait_time == 30
    assert behavior.wait_factor == 0.5
    assert behavior.running_interval_start == "09:00"
    assert behavior.running_interval_end == "17:30"
    assert behavior.twocaptcha_apikey == "key-42"
    assert behavior.hooks_enabled is True
    assert behavior.telegram_enabled is True
    assert behavior.send_to_android is True
    assert behavior.request_boost is True
    assert behavior.captcha_policy == "solve"


def test_captcha_policy_defaults_to_stop_when_the_key_is_absent(make_config, base_config):
    """Старый config.json без ключа обязан читаться, дефолт — stop.

    Дефолт по плану (§5, фаза 8): остановка ждёт оператора, авто-решение
    включается только явным ``solve``/``both``.
    """

    assert "captcha_policy" not in base_config["behavior"]

    assert make_config(base_config).behavior.captcha_policy == "stop"


def test_captcha_policy_default_is_the_constant_from_captcha_policy(
    make_config, base_config
):
    """Дефолт legacy-чтения не должен разъезжаться с engine.captcha_policy."""

    from engine.captcha_policy import DEFAULT_CAPTCHA_POLICY

    assert make_config(base_config).behavior.captcha_policy == DEFAULT_CAPTCHA_POLICY


def test_twocaptcha_key_is_read_from_numeric_prefixed_json_key(make_config, base_config):
    # В JSON ключ называется "2captcha_apikey", а в dataclass - twocaptcha_apikey.
    # Похожий, но неверный "captcha_apikey" должен быть проигнорирован.
    base_config["behavior"]["captcha_apikey"] = "wrong-key-name"
    base_config["behavior"]["2captcha_apikey"] = "right-key-name"

    behavior = make_config(base_config).behavior

    assert behavior.twocaptcha_apikey == "right-key-name"


def test_browser_count_zero_is_replaced_with_cpu_count(make_config, base_config):
    base_config["behavior"]["browser_count"] = 0

    behavior = make_config(base_config).behavior

    assert behavior.browser_count == multiprocessing.cpu_count()
    assert behavior.browser_count > 0


def test_browser_count_is_kept_as_is_when_nonzero(make_config, base_config):
    base_config["behavior"]["browser_count"] = 3

    assert make_config(base_config).behavior.browser_count == 3


def test_both_proxy_file_and_webdriver_proxy_are_rejected(make_config, caplog, base_config):
    base_config["paths"]["proxy_file"] = "/tmp/proxies.txt"
    base_config["webdriver"]["proxy"] = "127.0.0.1:8080"

    with pytest.raises(SystemExit):
        make_config(base_config)

    assert "Either 'proxy_file' or 'proxy' parameter should be empty." in caplog.text


def test_only_proxy_file_is_allowed(make_config, base_config):
    base_config["paths"]["proxy_file"] = "/tmp/proxies.txt"
    base_config["webdriver"]["proxy"] = ""

    cfg = make_config(base_config)

    assert cfg.paths.proxy_file == "/tmp/proxies.txt"
    assert cfg.webdriver.proxy == ""


def test_only_webdriver_proxy_is_allowed(make_config, base_config):
    base_config["paths"]["proxy_file"] = ""
    base_config["webdriver"]["proxy"] = "user:pass@1.2.3.4:8080"

    cfg = make_config(base_config)

    assert cfg.paths.proxy_file == ""
    assert cfg.webdriver.proxy == "user:pass@1.2.3.4:8080"


def test_both_query_file_and_query_are_rejected(make_config, caplog, base_config):
    base_config["paths"]["query_file"] = "/tmp/queries.txt"
    base_config["behavior"]["query"] = "usb hub"

    with pytest.raises(SystemExit):
        make_config(base_config)

    assert "Either 'query_file' or 'query' parameter should be empty." in caplog.text


def test_malformed_json_terminates_with_readable_error(make_config, caplog):
    with pytest.raises(SystemExit):
        make_config(raw="{not a json")

    assert "Failed to read config file. Check format and try again." in caplog.text


def test_missing_key_in_behavior_exits_with_readable_error(make_config, caplog, base_config):
    del base_config["behavior"]["click_order"]

    with pytest.raises(SystemExit):
        make_config(base_config)

    assert (
        "Failed to read config file. Missing 'click_order' parameter in 'behavior' section."
        in caplog.text
    )


def test_missing_key_in_webdriver_exits_with_readable_error(make_config, caplog, base_config):
    del base_config["webdriver"]["auth"]

    with pytest.raises(SystemExit):
        make_config(base_config)

    assert (
        "Failed to read config file. Missing 'auth' parameter in 'webdriver' section."
        in caplog.text
    )


def test_missing_key_in_paths_exits_with_readable_error(make_config, caplog, base_config):
    del base_config["paths"]["query_file"]

    with pytest.raises(SystemExit):
        make_config(base_config)

    assert (
        "Failed to read config file. Missing 'query_file' parameter in 'paths' section."
        in caplog.text
    )


def test_missing_section_exits_with_readable_error(make_config, caplog, base_config):
    del base_config["webdriver"]

    with pytest.raises(SystemExit):
        make_config(base_config)

    assert "Failed to read config file. Missing 'webdriver' section." in caplog.text


def test_section_of_wrong_type_exits_with_readable_error(make_config, caplog, base_config):
    base_config["webdriver"] = ["proxy", "127.0.0.1:8080"]

    with pytest.raises(SystemExit):
        make_config(base_config)

    assert "Failed to read config file. 'webdriver' must be a JSON object." in caplog.text


def test_top_level_json_list_exits_with_readable_error(make_config, caplog):
    with pytest.raises(SystemExit):
        make_config(raw='["paths", "webdriver", "behavior"]')

    assert "Failed to read config file. Check format and try again." in caplog.text


def test_empty_paths_and_query_are_allowed(make_config, base_config):
    base_config["paths"]["query_file"] = ""
    base_config["paths"]["proxy_file"] = ""
    base_config["behavior"]["query"] = ""

    cfg = make_config(base_config)

    assert cfg.paths.query_file == ""
    assert cfg.paths.proxy_file == ""
    assert cfg.behavior.query == ""


def test_read_parameters_can_be_repeated_on_same_reader(make_config, base_config):
    reader = make_config(base_config)

    reader.read_parameters()
    first_paths = reader.paths
    reader.read_parameters()

    assert reader.paths is not first_paths
    assert reader.paths.query_file == first_paths.query_file
    assert reader.behavior is not None
