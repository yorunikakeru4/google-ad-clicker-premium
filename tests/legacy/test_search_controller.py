"""Тесты search_controller.py: разбор запроса, паузы, статистика, отбор ссылок.

SearchController создаётся с фальшивым драйвером (никакого Chrome и сети), но
внутри он по-прежнему работает с настоящими config, ClickLogsDB и разбором
ссылок - покрывается именно логика, а не заглушки.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

import search_controller
from clicklogs_db import ClickLogsDB
from conftest import FakeDriver, FakeElement
from engine.profile_apply import load_profile_cookies, profile_cookies_path
from search_controller import SearchController
from stats import SearchStats


def make_link(url, **overrides):
    """Ссылка, которая по умолчанию проходит все фильтры _get_non_ad_links."""

    attributes = {
        "href": url,
        "role": "presentation",
        "jsname": "UWckNb",
        "data-ved": "2eVUEQ",
    }
    attributes.update(overrides)
    return FakeElement(attributes=attributes)


# --- _process_query: разбор строки запроса ----------------------------------------


def test_process_query_without_filters_returns_query_as_is():
    assert SearchController._process_query("wireless keyboard") == ("wireless keyboard", [])


def test_process_query_trims_spaces_around_query():
    assert SearchController._process_query("  wireless keyboard  ") == (
        "wireless keyboard",
        [],
    )


def test_process_query_splits_filter_words_by_hash():
    assert SearchController._process_query("wireless keyboard@amazon#ebay") == (
        "wireless keyboard",
        ["amazon", "ebay"],
    )


def test_process_query_trims_spaces_around_filter_words():
    assert SearchController._process_query("bluetooth headphones @ sony # amazon  #bose") == (
        "bluetooth headphones",
        ["sony", "amazon", "bose"],
    )


def test_process_query_lowercases_filter_words():
    assert SearchController._process_query("keyboard@Amazon#EBAY") == (
        "keyboard",
        ["amazon", "ebay"],
    )


def test_process_query_keeps_case_of_search_query():
    assert SearchController._process_query("Wireless Keyboard@Amazon") == (
        "Wireless Keyboard",
        ["amazon"],
    )


def test_process_query_returns_empty_query_when_only_filters_given():
    # Такое поведение сейчас молча приводит к поиску по пустой строке.
    assert SearchController._process_query("@amazon#ebay") == ("", ["amazon", "ebay"])


def test_process_query_ignores_text_after_second_at_sign():
    # Берётся только участок между первым и вторым "@": "aliexpress" теряется.
    assert SearchController._process_query("keyboard@amazon#ebay@aliexpress") == (
        "keyboard",
        ["amazon", "ebay"],
    )


def test_process_query_keeps_hash_without_at_sign_in_query():
    assert SearchController._process_query("usb#c hub") == ("usb#c hub", [])


def test_process_query_ignores_dangling_at_sign():
    # Пустой элемент фильтра содержится в любом тексте ("", in text - всегда
    # True), поэтому висячий "@" должен исчезнуть, а не отфильтровать всё.
    assert SearchController._process_query("wireless keyboard@") == ("wireless keyboard", [])


def test_process_query_ignores_at_sign_followed_by_spaces():
    assert SearchController._process_query("wireless keyboard@   ") == ("wireless keyboard", [])


def test_process_query_ignores_empty_word_between_hashes():
    assert SearchController._process_query("keyboard@amazon##ebay") == (
        "keyboard",
        ["amazon", "ebay"],
    )


def test_process_query_ignores_at_sign_with_empty_words():
    assert SearchController._process_query("keyboard@ # ") == ("keyboard", [])


# --- __init__: перенос настроек конфигурации --------------------------------------


def test_controller_opens_google_main_page_on_init(make_search_controller):
    driver = FakeDriver()

    make_search_controller(driver=driver)

    assert driver.visited == ["https://www.google.com"]


def test_controller_splits_query_and_filters_on_init(make_search_controller):
    controller = make_search_controller(query="usb hub@amazon#ebay")

    assert controller._search_query == "usb hub"
    assert controller._filter_words == ["amazon", "ebay"]


def test_controller_copies_wait_ranges_from_config(make_search_controller, monkeypatch, behavior):
    monkeypatch.setattr(behavior, "ad_page_min_wait", 3)
    monkeypatch.setattr(behavior, "ad_page_max_wait", 4)
    monkeypatch.setattr(behavior, "nonad_page_min_wait", 7)
    monkeypatch.setattr(behavior, "nonad_page_max_wait", 8)

    controller = make_search_controller()

    assert controller._ad_page_min_wait == 3
    assert controller._ad_page_max_wait == 4
    assert controller._nonad_page_min_wait == 7
    assert controller._nonad_page_max_wait == 8


def test_controller_copies_behavior_flags_from_config(make_search_controller, monkeypatch, behavior):
    monkeypatch.setattr(behavior, "random_mouse", True)
    monkeypatch.setattr(behavior, "custom_cookies", True)
    monkeypatch.setattr(behavior, "hooks_enabled", True)
    monkeypatch.setattr(behavior, "max_scroll_limit", 5)
    monkeypatch.setattr(behavior, "twocaptcha_apikey", "key-42")

    controller = make_search_controller()

    assert controller._random_mouse_enabled is True
    assert controller._use_custom_cookies is True
    assert controller._hooks_enabled is True
    assert controller._max_scroll_limit == 5
    assert controller._twocaptcha_apikey == "key-42"


def test_controller_has_no_exclude_list_when_excludes_is_empty(make_search_controller):
    assert make_search_controller()._exclude_list is None


def test_controller_splits_excludes_by_comma(make_search_controller, monkeypatch, behavior):
    monkeypatch.setattr(behavior, "excludes", " amazon ,ebay ,, aliexpress ")

    controller = make_search_controller()

    assert controller._exclude_list == ["amazon", "ebay", "", "aliexpress"]


def test_controller_starts_with_zeroed_stats(make_search_controller):
    assert make_search_controller().stats == SearchStats()


def test_controller_uses_country_domain_url_for_mapped_country(make_search_controller):
    driver = FakeDriver()

    make_search_controller(country_code="DE", driver=driver)

    assert driver.visited == ["https://www.google.de"]


def test_controller_falls_back_to_google_com_for_unmapped_country(make_search_controller):
    driver = FakeDriver()

    make_search_controller(country_code="ZZ", driver=driver)

    assert driver.visited == ["https://www.google.com"]


def test_country_url_override_does_not_leak_into_class_attribute(make_search_controller):
    # _set_start_url вешает URL на экземпляр: класс не должен поменяться.
    controller = make_search_controller(country_code="DE")

    assert controller.URL == "https://www.google.de"
    assert SearchController.URL == "https://www.google.com"


def test_controller_uses_seleniumbase_loader_when_configured(
    make_search_controller, monkeypatch, config
):
    monkeypatch.setattr(config.webdriver, "use_seleniumbase", True)
    driver = FakeDriver()
    driver.uc_open_with_reconnect = lambda url, reconnect_time: driver.visited.append(
        (url, reconnect_time)
    )

    make_search_controller(driver=driver)

    assert driver.visited == [("https://www.google.com", 3)]


# --- _get_wait_time ---------------------------------------------------------------


def test_wait_time_for_ad_stays_in_ad_range(make_search_controller):
    controller = make_search_controller()

    for _ in range(50):
        assert 10 <= controller._get_wait_time(True) <= 14


def test_wait_time_for_non_ad_stays_in_non_ad_range(make_search_controller):
    controller = make_search_controller()

    for _ in range(50):
        assert 15 <= controller._get_wait_time(False) <= 19


def test_wait_time_never_reaches_configured_maximum(make_search_controller, monkeypatch, behavior):
    # range(min, max) не включает max - верхняя граница конфига недостижима.
    monkeypatch.setattr(behavior, "ad_page_min_wait", 10)
    monkeypatch.setattr(behavior, "ad_page_max_wait", 11)
    controller = make_search_controller()

    for _ in range(50):
        assert controller._get_wait_time(True) == 10


def test_wait_time_for_single_value_range_is_the_lower_bound(
    make_search_controller, monkeypatch, behavior
):
    monkeypatch.setattr(behavior, "nonad_page_min_wait", 42)
    monkeypatch.setattr(behavior, "nonad_page_max_wait", 43)
    controller = make_search_controller()

    assert controller._get_wait_time(False) == 42


def test_wait_time_with_equal_bounds_crashes_with_indexerror(
    make_search_controller, monkeypatch, behavior
):
    # min == max даёт пустой range(...), на котором random.choice падает.
    monkeypatch.setattr(behavior, "ad_page_min_wait", 5)
    monkeypatch.setattr(behavior, "ad_page_max_wait", 5)
    controller = make_search_controller()

    with pytest.raises(IndexError):
        controller._get_wait_time(True)


# --- _update_click_stats ----------------------------------------------------------


def today() -> str:
    return datetime.now().strftime("%d-%m-%Y")


def test_click_on_ad_increments_ads_clicked(make_search_controller):
    controller = make_search_controller()

    controller._update_click_stats("http://a.example", "10:00:00", "Ad")

    assert controller.stats.ads_clicked == 1
    assert controller.stats.non_ads_clicked == 0
    assert controller.stats.shopping_ads_clicked == 0


def test_click_on_non_ad_increments_non_ads_clicked(make_search_controller):
    controller = make_search_controller()

    controller._update_click_stats("http://a.example", "10:00:00", "Non-ad")

    assert controller.stats.non_ads_clicked == 1
    assert controller.stats.ads_clicked == 0


def test_click_on_shopping_ad_increments_shopping_counter(make_search_controller):
    controller = make_search_controller()

    controller._update_click_stats("http://a.example", "10:00:00", "Shopping")

    assert controller.stats.shopping_ads_clicked == 1
    assert controller.stats.ads_clicked == 0


def test_click_with_unknown_category_increments_nothing(make_search_controller):
    controller = make_search_controller()

    controller._update_click_stats("http://a.example", "10:00:00", "Unknown")

    assert controller.stats.ads_clicked == 0
    assert controller.stats.non_ads_clicked == 0
    assert controller.stats.shopping_ads_clicked == 0


def test_click_is_saved_to_database_with_search_query_and_date(make_search_controller):
    controller = make_search_controller(query="usb hub@amazon")
    controller._update_click_stats("http://a.example", "10:11:12", "Ad")

    results = ClickLogsDB().query_clicks(today())

    assert results == [("http://a.example", 1, "Ad", "10:11:12", "usb hub")]


def test_repeated_clicks_are_counted_per_category(make_search_controller):
    controller = make_search_controller()

    controller._update_click_stats("http://a.example", "10:00:00", "Ad")
    controller._update_click_stats("http://a.example", "10:05:00", "Ad")
    controller._update_click_stats("http://b.example", "10:06:00", "Non-ad")

    assert controller.stats.ads_clicked == 2
    assert controller.stats.non_ads_clicked == 1
    # Порядок строк после GROUP BY не задан, поэтому сравниваем множества.
    assert set(ClickLogsDB().query_clicks(today())) == {
        ("http://a.example", 2, "Ad", "10:00:00", "wireless keyboard"),
        ("http://b.example", 1, "Non-ad", "10:06:00", "wireless keyboard"),
    }


# --- set_browser_id / assign_android_device / stats -------------------------------


def test_set_browser_id_is_reflected_in_stats(make_search_controller):
    controller = make_search_controller()

    controller.set_browser_id(7)

    assert controller.stats.browser_id == 7


def test_assign_android_device_stores_device_id(make_search_controller):
    controller = make_search_controller()

    controller.assign_android_device("emulator-5554")

    assert controller._android_device_id == "emulator-5554"


def test_assign_android_device_logs_browser_id(make_search_controller, caplog):
    controller = make_search_controller()
    controller.set_browser_id(3)

    controller.assign_android_device("emulator-5554")

    # Устройство и браузер ушли в поля записи, а не в склеенную строку:
    # проверяем структурную запись в зеркале legacy-логгера.
    record = next(
        entry
        for entry in caplog.records
        if entry.getMessage().startswith("Assigning device")
    )
    assert record.category == "browser"
    assert record.fields == {"device_id": "emulator-5554", "browser_id": 3}
    assert "emulator-5554" in record.getMessage()


# --- _extract_link_info -----------------------------------------------------------


def test_extract_link_info_unpacks_ad_tuple(make_search_controller):
    controller = make_search_controller()
    ad_element = FakeElement()

    assert controller._extract_link_info((ad_element, "http://a.example", "Title"), True) == (
        ad_element,
        "http://a.example",
        "Title",
    )


def test_extract_link_info_reads_href_for_non_ad_element(make_search_controller):
    controller = make_search_controller()
    element = FakeElement(attributes={"href": "http://b.example"})

    element_result, url, title = controller._extract_link_info(element, False)

    assert element_result is element
    assert url == "http://b.example"
    assert title is None


# --- _get_non_ad_links: правила отбора ссылок ------------------------------------


def test_get_non_ad_links_keeps_plain_external_link(make_search_controller):
    link = make_link("http://shop.example/product")
    controller = make_search_controller(driver=FakeDriver(links=[link]))

    assert controller._get_non_ad_links([]) == [link]


@pytest.mark.parametrize("role", ["link", "button", "menuitem", "menuitemradio"])
def test_get_non_ad_links_skips_service_roles(make_search_controller, role):
    link = make_link("http://shop.example/product", role=role)
    controller = make_search_controller(driver=FakeDriver(links=[link]))

    assert controller._get_non_ad_links([]) == []


def test_get_non_ad_links_skips_ad_elements(make_search_controller):
    ad = make_link("http://ads.example/product")
    controller = make_search_controller(driver=FakeDriver(links=[ad]))

    assert controller._get_non_ad_links([(ad, "http://ads.example/product", "Ad")]) == []


def test_get_non_ad_links_skips_link_without_href(make_search_controller):
    link = make_link("http://shop.example/product", href=None)
    controller = make_search_controller(driver=FakeDriver(links=[link]))

    assert controller._get_non_ad_links([]) == []


def test_get_non_ad_links_skips_link_without_jsname(make_search_controller):
    link = make_link("http://shop.example/product", jsname=None)
    controller = make_search_controller(driver=FakeDriver(links=[link]))

    assert controller._get_non_ad_links([]) == []


def test_get_non_ad_links_skips_link_without_data_ved(make_search_controller):
    link = make_link("http://shop.example/product", **{"data-ved": None})
    controller = make_search_controller(driver=FakeDriver(links=[link]))

    assert controller._get_non_ad_links([]) == []


def test_get_non_ad_links_skips_link_with_data_rw(make_search_controller):
    link = make_link("http://shop.example/product", **{"data-rw": "https://x.example"})
    controller = make_search_controller(driver=FakeDriver(links=[link]))

    assert controller._get_non_ad_links([]) == []


@pytest.mark.parametrize(
    "url",
    [
        "http://www.google.com/maps/place/x",
        "http://www.google.com/search?q=usb+hub",
        "http://pagead2.googlesyndication.com/googleadservices/x",
        "https://www.google.com/webhp",
        "/relative/path",
    ],
)
def test_get_non_ad_links_skips_google_internal_links(make_search_controller, url):
    link = make_link(url)
    controller = make_search_controller(driver=FakeDriver(links=[link]))

    assert controller._get_non_ad_links([]) == []


def test_get_non_ad_links_skips_links_containing_svg_icon(make_search_controller):
    link = make_link("http://shop.example/product")
    link.svg_count = 1
    controller = make_search_controller(driver=FakeDriver(links=[link]))

    assert controller._get_non_ad_links([]) == []


def test_get_non_ad_links_keeps_link_without_svg_children(make_search_controller):
    link = make_link("http://shop.example/product")
    link.svg_count = 0
    controller = make_search_controller(driver=FakeDriver(links=[link]))

    assert controller._get_non_ad_links([]) == [link]


def test_get_non_ad_links_filters_by_given_domains(make_search_controller):
    wanted = make_link("http://www.booking.com/hotel")
    other = make_link("http://shop.example/product")
    controller = make_search_controller(driver=FakeDriver(links=[wanted, other]))

    assert controller._get_non_ad_links([], ["www.booking.com"]) == [wanted]


def test_get_non_ad_links_returns_every_matching_link_when_domains_given(make_search_controller):
    first = make_link("http://www.booking.com/hotel/1")
    second = make_link("http://www.booking.com/hotel/2")
    controller = make_search_controller(driver=FakeDriver(links=[first, second]))

    assert controller._get_non_ad_links([], ["booking"]) == [first, second]


def test_get_non_ad_links_caps_result_to_three_links_without_domain_filter(
    make_search_controller,
):
    links = [make_link(f"http://shop{index}.example/product") for index in range(6)]
    controller = make_search_controller(driver=FakeDriver(links=links))

    result = controller._get_non_ad_links([])

    assert len(result) == 3
    assert all(any(item is original for original in links) for item in result)


def test_get_non_ad_links_keeps_all_links_when_three_or_fewer(make_search_controller):
    links = [make_link(f"http://shop{index}.example/product") for index in range(3)]
    controller = make_search_controller(driver=FakeDriver(links=links))

    assert controller._get_non_ad_links([]) == links


def test_get_non_ad_links_does_not_cap_matching_links_when_domain_filter_given(
    make_search_controller,
):
    links = [make_link(f"http://www.booking.com/hotel/{index}") for index in range(6)]
    controller = make_search_controller(driver=FakeDriver(links=links))

    assert len(controller._get_non_ad_links([], ["www.booking.com"])) == 6


def test_get_non_ad_links_does_not_duplicate_link_for_two_matching_domains(make_search_controller):
    link = make_link("http://www.booking.com/hotel")
    controller = make_search_controller(driver=FakeDriver(links=[link]))

    assert controller._get_non_ad_links([], ["booking", "www.booking.com"]) == [link]


# --- end_search -------------------------------------------------------------------


def test_end_search_deletes_cache_cookies_and_quits(make_search_controller):
    driver = FakeDriver()
    calls = []
    driver.execute_cdp_cmd = lambda command, payload: calls.append(command)
    controller = make_search_controller(driver=driver)

    controller.end_search()

    assert calls == ["Network.clearBrowserCache", "Network.clearBrowserCookies"]
    assert driver.cookies_deleted == 1
    assert driver.scripts == [
        "window.localStorage.clear();",
        "window.sessionStorage.clear();",
    ]
    assert "quit" in driver.visited
    assert controller._driver is None


def test_end_search_swallows_devtools_disconnect_error(make_search_controller):
    driver = FakeDriver()
    controller = make_search_controller(driver=driver)

    def raise_disconnect(command, payload):
        raise RuntimeError("not connected to DevTools")

    driver.execute_cdp_cmd = raise_disconnect

    controller.end_search()

    assert driver.cookies_deleted == 1
    assert controller._driver is None


class AdContainer:
    """Контейнер объявлений, отдающий заранее подготовленный список."""

    def __init__(self, ads):
        self._ads = ads

    def find_elements(self, by, value=None):
        return self._ads


class ScrolledAdsDriver(FakeDriver):
    """Драйвер с одним объявлением, который прокручивается ровно один раз."""

    def __init__(self, ad):
        super().__init__()
        self.ad = ad
        self.scroll_checks = 0

    def find_elements(self, by, value=None):
        return [AdContainer([self.ad])]

    def execute_script(self, script, *args):
        # Первая проверка прокрутки сообщает "ещё не в конце", вторая - "в конце",
        # поэтому цикл прокрутки отрабатывает ровно один раз.
        self.scroll_checks += 1

        if "scrollHeight" in script:
            return 1000

        return 0 if self.scroll_checks == 2 else 1000


def make_ad(title, link="https://ads.example.com/kb"):
    return FakeElement(attributes={"href": link, "data-pcu": "shop.example.com"}, text=title)


# --- _get_ad_links: фильтрация объявлений по filter_words ------------------------


def test_ad_links_pass_when_query_has_no_filter_words(make_search_controller):
    driver = ScrolledAdsDriver(make_ad("Wireless Keyboard Sale"))
    controller = make_search_controller(query="wireless keyboard", driver=driver)

    assert controller._get_ad_links() == [
        (driver.ad, "https://ads.example.com/kb", "Wireless Keyboard Sale")
    ]
    assert controller._stats.num_filtered_ads == 0


def test_ad_links_are_kept_when_filter_word_matches(make_search_controller):
    # Фильтр работает как белый список: кликаются объявления, в ссылке или
    # заголовке которых есть слово из запроса.
    driver = ScrolledAdsDriver(make_ad("Wireless Keyboard Sale"))
    controller = make_search_controller(query="wireless keyboard@shop.example", driver=driver)

    assert controller._get_ad_links() == [
        (driver.ad, "https://ads.example.com/kb", "Wireless Keyboard Sale")
    ]
    assert controller._stats.num_filtered_ads == 1


def test_ad_links_are_dropped_when_filter_word_does_not_match(make_search_controller):
    driver = ScrolledAdsDriver(make_ad("Wireless Keyboard Sale"))
    controller = make_search_controller(query="wireless keyboard@amazon", driver=driver)

    assert controller._get_ad_links() == []
    assert controller._stats.num_filtered_ads == 0


def test_ad_links_are_dropped_when_query_ends_with_empty_filter_word(make_search_controller):
    # Регрессия: пустой элемент в белом списке совпадает с любым текстом, поэтому
    # висящий "#" превращал фильтр в "кликать всё".
    driver = ScrolledAdsDriver(make_ad("Wireless Keyboard Sale"))
    controller = make_search_controller(query="wireless keyboard@amazon#", driver=driver)

    assert controller._get_ad_links() == []
    assert controller._stats.num_filtered_ads == 0


# --- Известные баги: тесты зафиксированы как xfail ---------------------------------


@pytest.mark.xfail(
    reason=(
        "Баг legacy-кода: range(min, max) в _get_wait_time не включает max, "
        "поэтому ad_page_max_wait фактически на секунду меньше заданного"
    ),
    strict=True,
)
def test_wait_time_should_be_able_to_reach_configured_maximum(
    make_search_controller, monkeypatch, behavior
):
    monkeypatch.setattr(behavior, "ad_page_min_wait", 10)
    monkeypatch.setattr(behavior, "ad_page_max_wait", 11)
    controller = make_search_controller()

    # Заставляем random.choice вернуть последний элемент переданного range.
    monkeypatch.setattr("random.choice", lambda population: list(population)[-1])

    assert controller._get_wait_time(True) == 11


# --- cookies: профильный набор против общего cookies.txt ----------------------

PROFILE_COOKIE = {"name": "sid", "value": "profile-secret", "sameSite": "lax"}


class ProfileCookieDriver(FakeDriver):
    """Драйвер для cookies-логики: удаление, добавление и выгрузка."""

    def __init__(self, browser_cookies=None):
        super().__init__()
        self.added = []
        self.browser_cookies = [dict(cookie) for cookie in browser_cookies or []]

    def add_cookie(self, cookie):
        self.added.append(dict(cookie))

    def get_cookies(self):
        return [dict(cookie) for cookie in self.browser_cookies]


class StopBeforeSearch(Exception):
    """Обрыв сразу после cookies: порядок действий search_for_ads."""


class LogRecorder:
    """Логгер-заглушка: пишет вызовы в список, в БД ничего не уходит."""

    def __init__(self):
        self.records = []

    def _record(self, level, category, message, **kwargs):
        self.records.append((level, category, message, kwargs.get("fields") or {}))

    def debug(self, category, message, **kwargs):
        self._record("DEBUG", category, message, **kwargs)

    def info(self, category, message, **kwargs):
        self._record("INFO", category, message, **kwargs)

    def warning(self, category, message, **kwargs):
        self._record("WARNING", category, message, **kwargs)

    def error(self, category, message, **kwargs):
        self._record("ERROR", category, message, **kwargs)

    @property
    def warnings(self):
        return [record for record in self.records if record[0] == "WARNING"]

    @property
    def text(self):
        return str(self.records)


@pytest.fixture
def record_log(monkeypatch):
    """Логгер-заглушка для обеих точек пишущего кода.

    В проде ``search_controller.log`` и ``engine.profile_apply.log`` — один и
    тот же ``get_logger()``-инстанс, поэтому здесь оба атрибута подменяются
    на один recorder: иначе запись про битый cookies-файл (её делает
    profile_apply) не попала бы в проверку.
    """

    recorder = LogRecorder()
    monkeypatch.setattr(search_controller, "log", recorder)
    # В профиле логгер ленивый (см. engine.profile_apply._log), поэтому
    # подменяется фабрика, а не атрибут.
    monkeypatch.setattr("engine.profile_apply.get_logger", lambda: recorder)
    return recorder


@pytest.fixture
def assign_profile(tmp_path, monkeypatch):
    """Профиль, назначенный процессу, и каталог для его cookies."""

    from engine.db import migrations
    from engine.profile_apply import PROFILE_DATA_DIR_ENV, PROFILE_ID_ENV
    from engine.profile_pool import ProfilePool

    db = tmp_path / "profiles.db"
    migrations.migrate(db)
    monkeypatch.setenv("ADCLICKER_DB", str(db))
    monkeypatch.setenv(PROFILE_DATA_DIR_ENV, str(tmp_path / "profile_data"))
    pool = ProfilePool(db)

    def _assign(cookies=None, **fields):
        pool.add_profiles([{"name": "acc", **fields}])
        row = pool.list_profiles()[-1]
        monkeypatch.setenv(PROFILE_ID_ENV, str(row["id"]))
        if cookies is not None:
            path = profile_cookies_path(row["id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cookies), encoding="utf-8")
        return row["id"]

    return _assign


class TestProfileCookies:
    """Правило применения: профильный набор всегда, без профиля — флаг.

    ``custom_cookies`` остаётся «применять cookies вообще» только для
    legacy-пути; с назначенным профилем его значение не спрашивается.
    """

    def test_profile_set_is_applied_even_when_the_flag_is_off(
        self, make_search_controller, assign_profile, behavior
    ):
        assert behavior.custom_cookies is False, "флаг в песочнице выключен"
        assign_profile(cookies=[PROFILE_COOKIE])
        driver = ProfileCookieDriver()
        controller = make_search_controller(driver=driver)

        controller._apply_cookies()

        assert driver.cookies_deleted == 1, "чужие cookies должны удаляться"
        assert driver.added == [
            {"name": "sid", "value": "profile-secret", "sameSite": "Lax"}
        ]

    def test_flag_without_a_profile_uses_the_global_cookies_txt(
        self, make_search_controller, monkeypatch, behavior, isolated_cwd
    ):
        # isolated_cwd кладёт в cwd четыре cookies из песочницы — ровно то,
        # что читает legacy utils.add_cookies.
        monkeypatch.setattr(behavior, "custom_cookies", True)
        driver = ProfileCookieDriver()
        controller = make_search_controller(driver=driver)

        controller._apply_cookies()

        assert driver.cookies_deleted == 1
        assert [cookie["name"] for cookie in driver.added] == [
            "strict_one",
            "lax_one",
            "none_secure",
            "none_insecure",
        ]

    def test_no_profile_and_no_flag_applies_nothing(self, make_search_controller):
        driver = ProfileCookieDriver()
        controller = make_search_controller(driver=driver)

        controller._apply_cookies()

        assert driver.cookies_deleted == 0
        assert driver.added == []

    def test_profile_file_wins_over_the_global_cookies_txt(
        self, make_search_controller, assign_profile, monkeypatch, behavior
    ):
        monkeypatch.setattr(behavior, "custom_cookies", True)
        assign_profile(cookies=[PROFILE_COOKIE])
        driver = ProfileCookieDriver()
        controller = make_search_controller(driver=driver)

        controller._apply_cookies()

        assert [cookie["name"] for cookie in driver.added] == ["sid"], (
            "общий cookies.txt не должен подмешиваться к профильному набору"
        )

    def test_missing_profile_file_starts_the_profile_clean(
        self, make_search_controller, assign_profile
    ):
        assign_profile()  # без файла: первый запуск профиля
        driver = ProfileCookieDriver()
        controller = make_search_controller(driver=driver)

        controller._apply_cookies()

        assert driver.cookies_deleted == 1
        assert driver.added == []

    def test_broken_profile_file_is_reported_and_does_not_stop_the_run(
        self, make_search_controller, assign_profile, record_log
    ):
        profile_id = assign_profile()
        path = profile_cookies_path(profile_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ это не json", encoding="utf-8")
        driver = ProfileCookieDriver()
        controller = make_search_controller(driver=driver)

        controller._apply_cookies()

        assert driver.cookies_deleted == 1, "прогон продолжается с пустым набором"
        assert driver.added == []
        assert record_log.warnings, "порча файла должна быть видна в логе"

    def test_search_for_ads_applies_cookies_before_any_page_work(
        self, make_search_controller, assign_profile, monkeypatch, behavior
    ):
        monkeypatch.setattr(behavior, "custom_cookies", True)
        assign_profile(cookies=[PROFILE_COOKIE])
        driver = ProfileCookieDriver()
        controller = make_search_controller(driver=driver)
        calls = []

        def stop():
            calls.append("cookies")
            raise StopBeforeSearch()

        # raising=False: в состоянии до фичи метода ещё нет, и именно это
        # и есть причина упасть — search_for_ads обязан звать его первым.
        monkeypatch.setattr(controller, "_apply_cookies", stop, raising=False)

        with pytest.raises(StopBeforeSearch):
            controller.search_for_ads(non_ad_domains=[])

        assert calls == ["cookies"], "cookies применяются до первой работы со страницей"

    def test_cookie_values_never_reach_the_log(
        self, make_search_controller, assign_profile, record_log
    ):
        assign_profile(cookies=[PROFILE_COOKIE])
        driver = ProfileCookieDriver()
        controller = make_search_controller(driver=driver)

        controller._apply_cookies()

        assert "profile-secret" not in record_log.text, (
            "значения cookies — сессионные креды, в логи не попадают"
        )

    def test_profile_cookies_are_applied_in_seleniumbase_mode_too(
        self, make_search_controller, assign_profile, monkeypatch, config
    ):
        """Хук cookies живёт в самом контроллере, а не в UC-драйвере."""
        monkeypatch.setattr(config.webdriver, "use_seleniumbase", True)
        assign_profile(cookies=[PROFILE_COOKIE])
        driver = ProfileCookieDriver()
        driver.uc_open_with_reconnect = lambda url, reconnect_time: driver.visited.append(url)
        controller = make_search_controller(driver=driver)

        controller._apply_cookies()

        assert driver.cookies_deleted == 1
        assert [cookie["name"] for cookie in driver.added] == ["sid"]


class TestProfileCookiesSave:
    """Выгрузка cookies браузера в файл профиля на выходе из сценария."""

    def test_end_search_saves_cookies_before_teardown(
        self, make_search_controller, assign_profile
    ):
        profile_id = assign_profile()
        driver = ProfileCookieDriver(
            browser_cookies=[{"name": "live", "value": "session-secret", "sameSite": "Lax"}]
        )
        controller = make_search_controller(driver=driver)

        controller.end_search()

        assert load_profile_cookies(profile_id) == [
            {"name": "live", "value": "session-secret", "sameSite": "Lax"}
        ]
        # Порядок важен: сохранение до удаления, teardown не тронут.
        assert driver.cookies_deleted == 1
        assert "quit" in driver.visited
        assert controller._driver is None

    def test_end_search_survives_a_failure_to_read_cookies(
        self, make_search_controller, assign_profile, record_log, monkeypatch
    ):
        profile_id = assign_profile()
        driver = ProfileCookieDriver()
        monkeypatch.setattr(driver, "get_cookies", lambda: (_ for _ in ()).throw(
            RuntimeError("драйвер уже умер")
        ))
        controller = make_search_controller(driver=driver)

        controller.end_search()

        assert driver.cookies_deleted == 1, "teardown обязан дойти до конца"
        assert "quit" in driver.visited
        assert load_profile_cookies(profile_id) == []
        assert record_log.warnings, "причина должна попасть в лог"
        assert record_log.warnings[0][1] == "browser"

    def test_unserializable_cookies_do_not_break_teardown(
        self, make_search_controller, assign_profile, record_log
    ):
        assign_profile()
        driver = ProfileCookieDriver(browser_cookies=[{"name": "x", "value": object()}])
        controller = make_search_controller(driver=driver)

        controller.end_search()

        assert driver.cookies_deleted == 1
        assert "quit" in driver.visited
        assert record_log.warnings, "отказ записи — это WARNING, а не падение"

    def test_without_a_profile_no_cookie_file_is_created(self, make_search_controller):
        driver = ProfileCookieDriver(browser_cookies=[{"name": "live", "value": "42"}])
        controller = make_search_controller(driver=driver)

        controller.end_search()

        assert "quit" in driver.visited
        assert not Path("profile_data").exists(), (
            "без профиля файл cookies профиля не появляется"
        )
