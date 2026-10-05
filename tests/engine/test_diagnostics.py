"""Диагностика сессии: разбор сырых ответов, правила согласованности, kv-сигнал.

Сети нет нигде: страницу и echo отдаёт драйвер-заглушка, fetcher'ы подменяются,
список локалей страны подаётся словарём. Проверяется чистая логика, которая
уходит в колонки ``diagnostics`` и в ``suspicion_flags``, — в том числе её
главный инвариант: **нехватка данных не даёт флага** (иначе у половины
воркеров UI светил бы ложный «подозрительный»).
"""

from __future__ import annotations

import json

import pytest

from engine.control_plane.state import StateStore
from engine.db import migrations
from engine.diagnostics import (
    DIAGNOSTICS_FLAG_PREFIX,
    ECHO_URLS,
    DiagnosticSnapshot,
    EchoResult,
    PageSnapshot,
    browser_core_from_ua,
    browser_version,
    check_language_vs_country,
    check_platform_vs_user_agent,
    check_screen_vs_window,
    check_timezone_vs_geo,
    check_ua_version_vs_browser,
    clear_signal,
    collect_snapshot,
    compute_suspicion_flags,
    diagnostics_flag_key,
    fetch_echo,
    fetch_local_ip,
    is_fresh_signal,
    load_country_locales,
    parse_echo_payload,
    parse_page_payload,
    read_signal,
    request_signal,
)

# Каноничный ответ страницы: ровно то, что возвращает PAGE_SNIPPET.
FULL_PAGE = {
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.6778.86",
    "accept_language": "de-DE,de;q=0.9",
    "timezone_id": "Europe/Berlin",
    "screen_w": 1920,
    "screen_h": 1080,
    "window_w": 1600,
    "window_h": 900,
    "platform": "Win32",
    "webdriver": True,
    "hardware_concurrency": 8,
    "device_memory": 8,
    "webgl_vendor": "Google Inc. (NVIDIA)",
    "webgl_renderer": "ANGLE (NVIDIA, GeForce GTX 1080 Direct3D11 vs_5_0 ps_5_0)",
}

FULL_ECHO_BODY = {
    "headers": {
        "Accept": "application/json",
        "Accept-Language": "de-DE,de;q=0.9",
        "Sec-Ch-Ua": '"Chromium";v="131"',
        "User-Agent": FULL_PAGE["user_agent"],
    },
    "origin": "203.0.113.7",
}


class FakeDriver:
    """Драйвер-заглушка: отдаёт заготовленные ответы, сеть не используется.

    ``page_payload`` уходит на ``execute_script`` (снимок страницы),
    ``async_payload`` — на ``execute_async_script`` (echo и внутренний IP).
    """

    def __init__(
        self,
        page_payload=None,
        async_payload=None,
        *,
        capabilities=None,
        page_error=None,
        async_error=None,
    ) -> None:
        self.page_payload = page_payload
        self.async_payload = async_payload
        self.capabilities = {} if capabilities is None else capabilities
        self.page_error = page_error
        self.async_error = async_error
        self.scripts: list[tuple[str, tuple]] = []

    def execute_script(self, script, *args):
        self.scripts.append((script, args))
        if self.page_error is not None:
            raise self.page_error
        return self.page_payload

    def execute_async_script(self, script, *args):
        self.scripts.append((script, args))
        if self.async_error is not None:
            raise self.async_error
        return self.async_payload


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


@pytest.fixture
def store(db_path):
    return StateStore(db_path)


# --- разбор снимка страницы -------------------------------------------------


class TestParsePagePayload:
    def test_full_payload_is_typed_field_by_field(self):
        page = parse_page_payload(dict(FULL_PAGE))

        assert page == PageSnapshot(
            user_agent=FULL_PAGE["user_agent"],
            accept_language="de-DE,de;q=0.9",
            timezone_id="Europe/Berlin",
            screen_w=1920,
            screen_h=1080,
            window_w=1600,
            window_h=900,
            platform="Win32",
            webdriver=True,
            hardware_concurrency=8,
            device_memory=8,
            webgl_vendor="Google Inc. (NVIDIA)",
            webgl_renderer="ANGLE (NVIDIA, GeForce GTX 1080 Direct3D11 vs_5_0 ps_5_0)",
        )

    def test_partial_payload_keeps_what_is_there(self):
        page = parse_page_payload({"user_agent": "UA/1.0", "screen_w": 800})

        assert page.user_agent == "UA/1.0"
        assert page.screen_w == 800
        assert page.screen_h is None
        assert page.webgl_renderer is None
        assert page.timezone_id is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("screen_w", "abc"),
            ("screen_h", [1080]),
            ("window_w", {"w": 800}),
            ("hardware_concurrency", "eight"),
            ("device_memory", "lots"),
            ("platform", 123),
            ("timezone_id", 42),
            ("user_agent", None),
            ("webdriver", "maybe"),
            ("webgl_vendor", 3.5),
        ],
    )
    def test_broken_value_becomes_none_instead_of_raising(self, field, value):
        payload = dict(FULL_PAGE)
        payload[field] = value

        page = parse_page_payload(payload)

        assert getattr(page, field) is None, f"битое поле {field} должно стать None"

    @pytest.mark.parametrize("raw", [None, 42, "not json", ["list"], object()])
    def test_non_object_input_yields_an_empty_snapshot(self, raw):
        page = parse_page_payload(raw)

        assert page == PageSnapshot()

    def test_unknown_keys_never_reach_the_snapshot(self):
        """Снимок — белый список полей: лишнее из ответа страницы (cookies,
        токены, что угодно) не попадает ни в поля объекта, ни в колонки."""
        from dataclasses import asdict

        payload = dict(FULL_PAGE, cookies="session=SECRET", password="hunter2")

        page = parse_page_payload(payload)

        assert set(asdict(page)) == set(asdict(PageSnapshot()))
        dumped = json.dumps(asdict(page))
        assert "SECRET" not in dumped
        assert "hunter2" not in dumped

    def test_json_string_response_is_parsed(self):
        """Драйвер может вернуть JSON-строкой, если JS отдал её как есть."""
        page = parse_page_payload(json.dumps(FULL_PAGE))

        assert page.user_agent == FULL_PAGE["user_agent"]
        assert page.screen_w == 1920

    def test_broken_json_string_yields_an_empty_snapshot(self):
        assert parse_page_payload("{oops") == PageSnapshot()

    @pytest.mark.parametrize(("raw", "expected"), [(True, True), (False, False), (1, True), (0, False)])
    def test_webdriver_flag_is_a_real_boolean(self, raw, expected):
        payload = dict(FULL_PAGE)
        payload["webdriver"] = raw

        assert parse_page_payload(payload).webdriver is expected


# --- ядро и версия браузера --------------------------------------------------


class TestBrowserFacts:
    @pytest.mark.parametrize(
        ("ua", "core"),
        [
            ("Mozilla/5.0 Chrome/131.0.6778.86 Safari/537.36", "chrome"),
            ("Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0", "edge"),
            ("Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36 OPR/116.0.0.0", "opera"),
            ("Mozilla/5.0 Firefox/133.0", "firefox"),
            ("Mozilla/5.0 (Macintosh) Version/17.4 Safari/605.1.15", "safari"),
            ("Mozilla/5.0 (iPhone) OS 17_4 like Mac OS X Safari/604.1", "safari"),
            ("Mozilla/5.0 (X11; Linux x86_64) Chrome/131.0.0.0 Safari/537.36", "chrome"),
            ("SomeBot/1.0 (+https://example.com)", None),
            (None, None),
        ],
    )
    def test_core_is_parsed_from_the_page_user_agent(self, ua, core):
        assert browser_core_from_ua(ua) == core

    def test_capabilities_win_over_the_user_agent(self):
        """Решение: версия — из драйвера, а не из UA.

        UA может быть подменён профилем, и тогда он соврал бы о версии
        реально запущенного Chrome; capabilities сообщает её честно.
        """
        assert (
            browser_version(
                {"browserVersion": "131.0.6778.86"},
                "Mozilla/5.0 Chrome/99.0.0.0 Safari/537.36",
            )
            == "131.0.6778.86"
        )

    @pytest.mark.parametrize(
        ("ua", "version"),
        [
            ("Mozilla/5.0 Chrome/131.0.6778.86 Safari/537.36", "131.0.6778.86"),
            ("Mozilla/5.0 Firefox/133.0", "133.0"),
            ("Mozilla/5.0 (Macintosh) Version/17.4 Safari/605.1.15", "17.4"),
            ("SomeBot/1.0", None),
            (None, None),
        ],
    )
    def test_version_falls_back_to_the_user_agent(self, ua, version):
        assert browser_version({}, ua) == version

    @pytest.mark.parametrize(
        "capabilities", [{}, {"browserVersion": None}, {"browserVersion": 131}]
    )
    def test_useless_capabilities_fall_back_to_the_user_agent(self, capabilities):
        ua = "Mozilla/5.0 Chrome/131.0.6778.86 Safari/537.36"

        assert browser_version(capabilities, ua) == "131.0.6778.86"

    def test_driver_without_capabilities_attribute_is_not_an_error(self):
        assert browser_version(None, "Mozilla/5.0 Chrome/131.0.0.0") == "131.0.0.0"


# --- разбор echo-ответа ------------------------------------------------------


class TestParseEchoPayload:
    def test_headers_and_external_ip_are_taken_from_the_body(self):
        echo = parse_echo_payload({"ok": True, "body": dict(FULL_ECHO_BODY)})

        assert echo.error is None
        assert echo.headers == FULL_ECHO_BODY["headers"]
        assert echo.ip == "203.0.113.7"

    def test_several_origins_keep_only_the_first(self):
        """httpbin отдаёт "ip1, ip2" при цепочке прокси."""
        body = {"headers": {}, "origin": "203.0.113.7, 198.51.100.9"}

        assert parse_echo_payload({"ok": True, "body": body}).ip == "203.0.113.7"

    def test_missing_origin_is_not_an_error(self):
        echo = parse_echo_payload({"ok": True, "body": {"headers": {"A": "b"}}})

        assert echo.ip is None
        assert echo.headers == {"A": "b"}
        assert echo.error is None

    def test_fetch_failure_is_reported_as_error_with_empty_fields(self):
        echo = parse_echo_payload({"ok": False, "error": "TypeError: Failed to fetch"})

        assert echo.headers is None
        assert echo.ip is None
        assert "Failed to fetch" in echo.error

    def test_timeout_reported_by_the_snippet_is_an_error(self):
        echo = parse_echo_payload({"error": "timeout after 5000ms"})

        assert echo.headers is None
        assert "timeout" in echo.error

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            42,
            "not a payload",
            {"ok": True},
            {"ok": True, "body": "text"},
            {"ok": True, "body": {"headers": "nope", "origin": 5}},
        ],
    )
    def test_malformed_payload_never_raises_and_yields_an_error_or_nothing(self, raw):
        echo = parse_echo_payload(raw)

        assert echo.headers is None
        assert echo.ip is None
        if isinstance(raw, dict) and raw.get("ok") is True and isinstance(raw.get("body"), dict):
            assert echo.error is None, "тело-словарь без полей — не ошибка, а просто пусто"
        else:
            assert echo.error

    def test_empty_headers_dict_is_kept_as_a_dict(self):
        echo = parse_echo_payload({"ok": True, "body": {"headers": {}, "origin": ""}})

        assert echo.headers == {}
        assert echo.ip is None, "пустой origin — данных нет, а не пустая строка"


# --- fetcher'ы: запрос идёт из контекста страницы ------------------------------


class TestFetchers:
    def test_echo_request_targets_the_constant_url(self):
        driver = FakeDriver(async_payload={"ok": True, "body": dict(FULL_ECHO_BODY)})

        echo = fetch_echo(driver)

        script, args = driver.scripts[-1]
        assert args[0] == list(ECHO_URLS)
        assert "fetch(" in script
        assert echo.ip == "203.0.113.7"

    def test_echo_driver_failure_becomes_an_error_not_an_exception(self):
        driver = FakeDriver(async_error=RuntimeError("browser is gone"))

        echo = fetch_echo(driver)

        assert echo.headers is None and echo.ip is None
        assert "browser is gone" in echo.error

    def test_local_ip_snippet_returns_the_candidate(self):
        driver = FakeDriver(async_payload="192.168.1.42")

        assert fetch_local_ip(driver) == "192.168.1.42"

    @pytest.mark.parametrize("payload", [None, "", "timeout"])
    def test_local_ip_is_best_effort(self, payload):
        driver = FakeDriver(async_payload=payload)

        assert fetch_local_ip(driver) is None

    def test_local_ip_driver_failure_is_not_fatal(self):
        driver = FakeDriver(async_error=RuntimeError("no peer connection"))

        assert fetch_local_ip(driver) is None


# --- сбор снимка целиком ------------------------------------------------------


class TestCollectSnapshot:
    @staticmethod
    def _echo_ok():
        return EchoResult(headers=dict(FULL_ECHO_BODY["headers"]), ip="203.0.113.7", error=None)

    def test_snapshot_contains_every_column_and_the_context_fields(self):
        driver = FakeDriver(dict(FULL_PAGE), capabilities={"browserVersion": "131.0.6778.86"})
        driver._geo_timezone = "Europe/Berlin"
        driver._geo_country = "DE"

        snapshot = collect_snapshot(
            driver,
            browser_id="br-1",
            ts=1700000000.0,
            proxy_id=7,
            echo_fetcher=lambda _driver: self._echo_ok(),
            local_ip_fetcher=lambda _driver: "10.0.0.5",
            locales={"DE": ["de-DE"]},
        )

        assert snapshot.browser_id == "br-1"
        assert snapshot.ts == 1700000000.0
        assert snapshot.proxy_id == 7
        assert snapshot.ip == "203.0.113.7"
        assert snapshot.country == "DE", "страна берётся из гео прокси, когда своя не задана"
        assert snapshot.user_agent == FULL_PAGE["user_agent"]
        assert snapshot.accept_language == "de-DE,de;q=0.9"
        assert snapshot.timezone_id == "Europe/Berlin"
        assert snapshot.screen_w == 1920 and snapshot.screen_h == 1080
        assert snapshot.window_w == 1600 and snapshot.window_h == 900
        assert snapshot.platform == "Win32"
        assert snapshot.webgl_vendor == FULL_PAGE["webgl_vendor"]
        assert snapshot.webgl_renderer == FULL_PAGE["webgl_renderer"]
        assert snapshot.browser_version == "131.0.6778.86"
        assert snapshot.browser_core == "chrome"
        assert snapshot.webdriver is True
        assert snapshot.hardware_concurrency == 8
        assert snapshot.device_memory == 8
        assert snapshot.local_ip == "10.0.0.5"
        assert snapshot.headers == FULL_ECHO_BODY["headers"]
        assert snapshot.geo_timezone == "Europe/Berlin"
        assert snapshot.suspicion_flags == []

    def test_country_comes_from_the_geo_the_driver_recorded(self):
        """Страна снимка — гео exit-IP, запомненное при старте сессии.

        Пул прокси страну не хранит: единственный источник, который
        отражает именно тот exit-IP, которым шла сессия, — гео,
        записанное драйвером в ``_geo_country``.
        """
        driver = FakeDriver(dict(FULL_PAGE), capabilities={})
        driver._geo_country = "DE"

        snapshot = collect_snapshot(
            driver,
            browser_id="br-1",
            echo_fetcher=lambda _driver: EchoResult(None, None, "нет сети"),
            local_ip_fetcher=lambda _driver: None,
            locales={"DE": ["de-DE"]},
        )

        assert snapshot.country == "DE"

    def test_unavailable_echo_gives_null_fields_and_a_flag(self):
        driver = FakeDriver(dict(FULL_PAGE))

        snapshot = collect_snapshot(
            driver,
            browser_id="br-1",
            echo_fetcher=lambda _driver: EchoResult(None, None, "timeout after 5000ms"),
            local_ip_fetcher=lambda _driver: None,
        )

        assert snapshot.ip is None
        assert snapshot.headers is None
        assert snapshot.local_ip is None
        assert any("echo" in flag for flag in snapshot.suspicion_flags), (
            "недоступный echo обязан оставить след в suspicion_flags"
        )

    def test_broken_page_script_propagates_to_the_caller(self):
        """Мёртвый драйвер — это отказ сбора, а не пустой снимок."""
        driver = FakeDriver(page_error=RuntimeError("session deleted"))

        with pytest.raises(RuntimeError, match="session deleted"):
            collect_snapshot(
                driver,
                browser_id="br-1",
                echo_fetcher=lambda _driver: EchoResult(None, None, "x"),
                local_ip_fetcher=lambda _driver: None,
            )

    def test_ts_defaults_to_now(self):
        import time

        driver = FakeDriver(dict(FULL_PAGE))

        snapshot = collect_snapshot(
            driver,
            browser_id="br-1",
            echo_fetcher=lambda _driver: EchoResult({}, None, None),
            local_ip_fetcher=lambda _driver: None,
        )

        assert abs(snapshot.ts - time.time()) < 5


# --- правила согласованности --------------------------------------------------


LOCALES = {"DE": ["de-DE"], "US": ["en-US", "es-US"], "RU": ["ru-RU"], "BR": ["pt-BR"]}


class TestLanguageAgainstCountry:
    def test_matching_language_and_country_is_ok(self):
        assert check_language_vs_country("de-DE,de;q=0.9", "DE", LOCALES) is None

    def test_matching_short_tag_is_ok(self):
        assert check_language_vs_country("de", "DE", LOCALES) is None

    def test_mismatched_language_is_flagged(self):
        flag = check_language_vs_country("ru-RU,ru;q=0.9", "DE", LOCALES)

        assert flag is not None
        assert "ru" in flag and "DE" in flag

    def test_legacy_python_repr_header_still_parses(self):
        """Legacy складывает список локалей в str() — заголовок приходит
        в виде "['de-DE', 'en-US']", и правило обязано разобрать его так же,
        а не флаговать формат."""
        assert check_language_vs_country("['de-DE', 'en-US']", "DE", LOCALES) is None

    def test_secondary_language_does_not_rescue_a_mismatched_primary(self):
        flag = check_language_vs_country("en-US,de;q=0.9", "DE", LOCALES)

        assert flag is not None

    def test_missing_language_is_not_a_flag(self):
        assert check_language_vs_country(None, "DE", LOCALES) is None
        assert check_language_vs_country("", "DE", LOCALES) is None

    def test_missing_country_is_not_a_flag(self):
        assert check_language_vs_country("ru-RU", None, LOCALES) is None

    def test_country_without_a_known_locale_is_not_a_flag(self):
        """Нет данных о локалях страны — нет и вывода, а не догадка."""
        assert check_language_vs_country("ru-RU", "GB", LOCALES) is None

    def test_empty_locale_table_is_not_a_flag(self):
        assert check_language_vs_country("ru-RU", "DE", {}) is None


class TestTimezoneAgainstGeo:
    def test_equal_timezones_are_ok(self):
        assert check_timezone_vs_geo("Europe/Berlin", "Europe/Berlin") is None

    def test_case_difference_is_not_a_mismatch(self):
        assert check_timezone_vs_geo("europe/berlin", "Europe/Berlin") is None

    def test_mismatched_timezones_are_flagged(self):
        flag = check_timezone_vs_geo("Europe/Paris", "Europe/Berlin")

        assert flag is not None
        assert "Europe/Paris" in flag and "Europe/Berlin" in flag

    @pytest.mark.parametrize(
        ("browser_tz", "geo_tz"),
        [(None, "Europe/Berlin"), ("Europe/Berlin", None), (None, None), ("  ", "Europe/Berlin")],
    )
    def test_missing_side_is_not_a_flag(self, browser_tz, geo_tz):
        assert check_timezone_vs_geo(browser_tz, geo_tz) is None


class TestPlatformAgainstUserAgent:
    WINDOWS_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0"
    MAC_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/131.0.0.0"
    ANDROID_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) Chrome/131.0.0.0 Mobile"
    IOS_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) Safari/604.1"

    @pytest.mark.parametrize(
        ("ua", "platform"),
        [
            (WINDOWS_UA, "Win32"),
            (WINDOWS_UA, "Win64"),
            (MAC_UA, "MacIntel"),
            (ANDROID_UA, "Linux armv8l"),
            (ANDROID_UA, "Android"),
            (IOS_UA, "iPhone"),
            (IOS_UA, "iPad"),
        ],
    )
    def test_consistent_pairs_are_ok(self, ua, platform):
        assert check_platform_vs_user_agent(ua, platform) is None

    @pytest.mark.parametrize(
        ("ua", "platform"),
        [
            (WINDOWS_UA, "MacIntel"),
            (MAC_UA, "Win32"),
            (ANDROID_UA, "Win32"),
            (IOS_UA, "Linux x86_64"),
        ],
    )
    def test_inconsistent_pairs_are_flagged(self, ua, platform):
        flag = check_platform_vs_user_agent(ua, platform)

        assert flag is not None
        assert platform in flag

    @pytest.mark.parametrize("platform", [None, "", 42])
    def test_missing_platform_is_not_a_flag(self, platform):
        assert check_platform_vs_user_agent(self.WINDOWS_UA, platform) is None

    @pytest.mark.parametrize("ua", [None, "", "SomeBot/1.0"])
    def test_user_agent_without_a_known_os_is_not_a_flag(self, ua):
        assert check_platform_vs_user_agent(ua, "Win32") is None


class TestUaVersionAgainstBrowser:
    """Строка-UA ↔ версия браузера: ловит UA из user_agents.txt.

    Снято живым прогоном: при браузере153 в снимках плавали Chrome/136
    (Macintosh) и CriOS/114 (iPhone) — обе строки сервер читает вместе
    с честными Sec-CH-UA и уже по одному этому считает браузер подделанным.
    """

    BROWSER = "153.0.8010.55"

    @pytest.mark.parametrize(
        "ua",
        [
            # честная строка совпадает с реальной версией
            "Mozilla/5.0 (Macintosh) Chrome/153.0.0.0 Safari/537.36",
            # честная редуцированная строка (заморозка UA reduction)
            "Mozilla/5.0 (Macintosh) Chrome/131.0.0.0 Safari/537.36",
        ],
    )
    def test_honest_versions_are_ok(self, ua):
        assert check_ua_version_vs_browser(ua, self.BROWSER) is None

    @pytest.mark.parametrize(
        "ua",
        [
            # живой случай: Chrome/136 при браузере153
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/136.0.0.0 Safari/537.36",
            # живой случай: устаревший мобильный CriOS
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) CriOS/114.0.5735.50 Safari",
        ],
    )
    def test_fabricated_versions_are_flagged(self, ua):
        flag = check_ua_version_vs_browser(ua, self.BROWSER)

        assert flag is not None
        assert "не соответствует" in flag
        assert self.BROWSER in flag

    @pytest.mark.parametrize(
        ("ua", "version"),
        [
            (None, "153.0.8010.55"),
            ("Mozilla/5.0 Chrome/136.0.0.0", None),
            ("", "153.0.8010.55"),
            ("SomeBot/1.0", "153.0.8010.55"),
            ("Mozilla/5.0 Chrome/136.0.0.0", ""),
        ],
    )
    def test_missing_data_or_foreign_ua_is_not_a_flag(self, ua, version):
        assert check_ua_version_vs_browser(ua, version) is None


class TestScreenAgainstWindow:
    @pytest.mark.parametrize(
        ("screen_w", "screen_h", "window_w", "window_h"),
        [
            (1920, 1080, 1600, 900),
            (1920, 1080, 1920, 1080),
            (1280, 720, 1200, 600),
        ],
    )
    def test_window_inside_screen_is_ok(self, screen_w, screen_h, window_w, window_h):
        assert check_screen_vs_window(screen_w, screen_h, window_w, window_h) is None

    @pytest.mark.parametrize(
        ("screen_w", "screen_h", "window_w", "window_h"),
        [
            (1280, 720, 1920, 700),
            (1280, 720, 1200, 900),
            (1280, 720, 1920, 900),
        ],
    )
    def test_window_larger_than_screen_is_flagged(self, screen_w, screen_h, window_w, window_h):
        flag = check_screen_vs_window(screen_w, screen_h, window_w, window_h)

        assert flag is not None

    @pytest.mark.parametrize(
        ("screen_w", "screen_h", "window_w", "window_h"),
        [
            (None, 1080, 1600, 900),
            (1920, None, 1600, 900),
            (1920, 1080, None, 900),
            (1920, 1080, 1600, None),
            (None, None, None, None),
        ],
    )
    def test_missing_side_is_not_a_flag(self, screen_w, screen_h, window_w, window_h):
        assert check_screen_vs_window(screen_w, screen_h, window_w, window_h) is None

    @pytest.mark.parametrize(
        ("screen_w", "screen_h", "window_w", "window_h"),
        [
            (0, 1080, 1600, 900),
            (1920, 0, 1600, 900),
            (-1, 1080, 1600, 900),
            (1920, 1080, 0, 900),
        ],
    )
    def test_non_positive_sizes_are_flagged(self, screen_w, screen_h, window_w, window_h):
        assert check_screen_vs_window(screen_w, screen_h, window_w, window_h) is not None


# --- сборка списка флагов ------------------------------------------------------


def _snapshot(**overrides) -> DiagnosticSnapshot:
    values = {
        "ts": 1700000000.0,
        "browser_id": "br-1",
        "user_agent": FULL_PAGE["user_agent"],
        "accept_language": "de-DE,de;q=0.9",
        "timezone_id": "Europe/Berlin",
        "screen_w": 1920,
        "screen_h": 1080,
        "window_w": 1600,
        "window_h": 900,
        "platform": "Win32",
        "country": "DE",
        "geo_timezone": "Europe/Berlin",
    }
    values.update(overrides)
    return DiagnosticSnapshot(**values)


class TestComputeSuspicionFlags:
    def test_consistent_snapshot_has_no_flags(self):
        assert compute_suspicion_flags(_snapshot(), locales=LOCALES) == []

    def test_every_rule_can_fire_at_once(self):
        snapshot = _snapshot(
            accept_language="ru-RU",
            timezone_id="Europe/Paris",
            platform="MacIntel",
            window_w=2560,
            # мобильный CriOS на десктопе даёт сразу два флага:
            # «платформа не соответствует ОС» и «версия не соответствует»
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_5) CriOS/114.0.5735.50",
            browser_version="153.0.8010.55",
        )

        flags = compute_suspicion_flags(snapshot, locales=LOCALES)

        assert len(flags) == 5, flags

    @pytest.mark.parametrize(
        "overrides",
        [
            {"accept_language": None, "country": None, "geo_timezone": None, "platform": None},
            {},
        ],
    )
    def test_missing_data_never_produces_a_flag(self, overrides):
        """Инвариант: нехватка данных — это не подозрение."""
        snapshot = _snapshot(**{**overrides, "window_w": None, "window_h": None})

        assert compute_suspicion_flags(snapshot, locales=LOCALES) == []

    def test_collection_errors_go_into_the_same_list(self):
        flags = compute_suspicion_flags(_snapshot(), locales=LOCALES, collection_errors=["timeout"])

        assert flags == ["timeout"]

    def test_flag_list_is_stable_and_ordered(self):
        snapshot = _snapshot(accept_language="ru-RU", platform="MacIntel")

        first = compute_suspicion_flags(snapshot, locales=LOCALES)

        assert first == compute_suspicion_flags(snapshot, locales=LOCALES)


# --- таблица локалей -----------------------------------------------------------


class TestCountryLocales:
    def test_repository_file_is_loaded(self):
        locales = load_country_locales()

        assert locales["DE"] == ["de-DE"]
        assert locales["US"] == ["en-US", "es-US"]

    def test_missing_file_yields_no_data_instead_of_raising(self):
        assert load_country_locales("/nonexistent/country_to_locale.json") == {}

    def test_malformed_file_yields_no_data_instead_of_raising(self, tmp_path):
        path = tmp_path / "country_to_locale.json"
        path.write_text("{broken", encoding="utf-8")

        assert load_country_locales(path) == {}


# --- kv-сигнал ------------------------------------------------------------------


class TestSignal:
    def test_flag_key_follows_the_documented_pattern(self):
        """Формат ключа — контракт с API и (потенциально) с UI: литерал
        зафиксирован, а не выведен из той же константы, что и реализация."""
        assert diagnostics_flag_key("br-1") == "DIAGNOSTICS_REQUESTED_br-1"
        assert DIAGNOSTICS_FLAG_PREFIX == "DIAGNOSTICS_REQUESTED_"

    def test_request_sets_a_timestamp_value(self, store):
        value = request_signal(store, "br-1")

        assert float(value) > 0
        assert read_signal(store, "br-1") == value

    def test_request_for_another_worker_does_not_leak(self, store):
        request_signal(store, "br-1")

        assert read_signal(store, "br-2") is None

    def test_read_without_a_request_is_none(self, store):
        assert read_signal(store, "br-1") is None

    def test_clear_removes_the_request(self, store):
        request_signal(store, "br-1")

        clear_signal(store, "br-1")

        assert read_signal(store, "br-1") is None

    @pytest.mark.parametrize(
        ("value", "last_handled", "fresh"),
        [
            (None, None, False),
            ("", None, False),
            ("   ", None, False),
            ("1700000000.5", None, True),
            ("1700000000.5", "1700000000.5", False),
            ("1700000001.5", "1700000000.5", True),
            ("1700000000.5", "1700000001.5", False),
            ("новое-значение", "1700000001.5", True),
        ],
    )
    def test_freshness_compares_against_the_last_handled_value(self, value, last_handled, fresh):
        assert is_fresh_signal(value, last_handled) is fresh
