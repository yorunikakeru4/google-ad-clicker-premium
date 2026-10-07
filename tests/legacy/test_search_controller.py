"""Тесты search_controller.py: разбор запроса, паузы, статистика, отбор ссылок.

SearchController создаётся с фальшивым драйвером (никакого Chrome и сети), но
внутри он по-прежнему работает с настоящими config, ClickLogsDB и разбором
ссылок - покрывается именно логика, а не заглушки.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest
from selenium.common.exceptions import (
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

import search_controller
from clicklogs_db import ClickLogsDB
from conftest import FakeDriver, FakeElement
from engine.profile_apply import load_profile_cookies, profile_cookies_path
from search_controller import (
    CAPTCHA_PROXY_ROTATION_REASON,
    SearchController,
    SearchRoundError,
)
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


def test_get_non_ad_links_skips_blocked_domains(make_search_controller):
    # Список — чёрный: ссылки на домен из списка не кликаются вовсе.
    blocked = make_link("http://www.edelind.de/goldkette")
    other = make_link("http://shop.example/product")
    controller = make_search_controller(driver=FakeDriver(links=[blocked, other]))

    assert controller._get_non_ad_links([], ["edelind.de"]) == [other]


def test_get_non_ad_links_keeps_links_when_only_other_domains_blocked(make_search_controller):
    first = make_link("http://www.booking.com/hotel/1")
    second = make_link("http://www.booking.com/hotel/2")
    controller = make_search_controller(driver=FakeDriver(links=[first, second]))

    assert controller._get_non_ad_links([], ["edelind.de"]) == [first, second]


def test_get_non_ad_links_caps_result_to_three_links_with_blocked_domains(make_search_controller):
    links = [make_link(f"http://shop{index}.example/product") for index in range(6)]
    controller = make_search_controller(driver=FakeDriver(links=links))

    result = controller._get_non_ad_links([], ["edelind.de"])

    assert len(result) == 3
    assert all(any(item is original for original in links) for item in result)


def test_get_non_ad_links_skips_link_blocked_by_two_entries(make_search_controller):
    link = make_link("http://www.booking.com/hotel")
    controller = make_search_controller(driver=FakeDriver(links=[link]))

    assert controller._get_non_ad_links([], ["booking.com", "www.booking.com"]) == []


def test_get_non_ad_links_skips_exclude_word_in_href(
    make_search_controller, monkeypatch, behavior
):
    monkeypatch.setattr(behavior, "excludes", "edelind")
    blocked = make_link("http://www.edelind.de/goldkette")
    other = make_link("http://shop.example/product")
    controller = make_search_controller(driver=FakeDriver(links=[blocked, other]))
    # Слово из behavior.excludes действует и без файлового списка доменов.
    controller._blocked_domains = []

    assert controller._get_non_ad_links([]) == [other]


def test_get_non_ad_links_skips_exclude_word_in_link_text(
    make_search_controller, monkeypatch, behavior
):
    monkeypatch.setattr(behavior, "excludes", "goldkette")
    blocked = FakeElement(
        attributes={
            "href": "http://shop.example/a",
            "role": "presentation",
            "jsname": "UWckNb",
            "data-ved": "2eVUEQ",
        },
        text="Edelind Goldkette",
    )
    other = FakeElement(
        attributes={
            "href": "http://shop.example/b",
            "role": "presentation",
            "jsname": "UWckNb",
            "data-ved": "2eVUEQ",
        },
        text="Wireless Keyboard",
    )
    controller = make_search_controller(driver=FakeDriver(links=[blocked, other]))

    assert controller._get_non_ad_links([]) == [other]


def test_get_non_ad_links_uses_blocked_domains_stored_by_search_for_ads(
    make_search_controller,
):
    blocked = make_link("http://www.edelind.de/goldkette")
    other = make_link("http://shop.example/product")
    controller = make_search_controller(driver=FakeDriver(links=[blocked, other]))
    controller._blocked_domains = ["edelind.de"]

    assert controller._get_non_ad_links([]) == [other]


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


# --- _get_ad_links: чёрный список доменов и слов ---------------------------------


def test_ad_links_dropped_when_href_is_blocked_domain(make_search_controller):
    driver = ScrolledAdsDriver(
        make_ad("Wireless Keyboard Sale", link="https://www.edelind.de/goldkette")
    )
    controller = make_search_controller(query="wireless keyboard", driver=driver)
    controller._blocked_domains = ["edelind.de"]

    assert controller._get_ad_links() == []
    assert controller._stats.num_excluded_ads == 1


def test_ad_links_kept_when_href_is_other_domain(make_search_controller):
    driver = ScrolledAdsDriver(make_ad("Wireless Keyboard Sale"))
    controller = make_search_controller(query="wireless keyboard", driver=driver)
    controller._blocked_domains = ["edelind.de"]

    assert len(controller._get_ad_links()) == 1
    assert controller._stats.num_excluded_ads == 0


def test_ad_links_dropped_when_exclude_word_in_title(
    make_search_controller, monkeypatch, behavior
):
    monkeypatch.setattr(behavior, "excludes", "edelind")
    driver = ScrolledAdsDriver(make_ad("Edelind Goldkette Sale"))
    controller = make_search_controller(query="wireless keyboard", driver=driver)

    assert controller._get_ad_links() == []
    assert controller._stats.num_excluded_ads == 1


def test_ad_links_dropped_when_exclude_word_in_href(
    make_search_controller, monkeypatch, behavior
):
    monkeypatch.setattr(behavior, "excludes", "goldkette")
    driver = ScrolledAdsDriver(
        make_ad("Wireless Keyboard Sale", link="https://shop.example/goldkette")
    )
    controller = make_search_controller(query="wireless keyboard", driver=driver)

    assert controller._get_ad_links() == []
    assert controller._stats.num_excluded_ads == 1


# --- _get_shopping_ad_links: чёрный список ---------------------------------------


class ShoppingUnit:
    """Мобильный блок shopping-рекламы: контейнер со ссылкой и заголовком."""

    def __init__(self, href, title):
        self.text = title
        self._anchor = FakeElement(attributes={"href": href}, text=title)

    def find_element(self, by, value=None):
        return self._anchor


class ShoppingAdsDriver(FakeDriver):
    """Драйвер, отдающий готовые shopping-блоки по любому селектору."""

    def __init__(self, units):
        super().__init__()
        self.units = [ShoppingUnit(*unit) for unit in units]

    def find_elements(self, by, value=None):
        return list(self.units)


def test_shopping_ads_dropped_when_target_is_blocked_domain(make_search_controller):
    driver = ShoppingAdsDriver(
        [
            ("https://www.edelind.de/goldkette", "Goldkette 75cm"),
            ("https://shop.example/keyboard", "Wireless Keyboard"),
        ]
    )
    controller = make_search_controller(query="wireless keyboard", driver=driver)
    controller._blocked_domains = ["edelind.de"]

    result = controller._get_shopping_ad_links()

    assert [entry[1] for entry in result] == ["https://shop.example/keyboard"]
    assert controller._stats.num_excluded_shopping_ads == 1


def test_shopping_ads_dropped_when_exclude_word_in_title(
    make_search_controller, monkeypatch, behavior
):
    monkeypatch.setattr(behavior, "excludes", "goldkette")
    driver = ShoppingAdsDriver(
        [
            ("https://shop.example/a", "Edelind Goldkette"),
            ("https://shop.example/b", "Wireless Keyboard"),
        ]
    )
    controller = make_search_controller(query="wireless keyboard", driver=driver)

    result = controller._get_shopping_ad_links()

    assert [entry[2] for entry in result] == ["Wireless Keyboard"]
    assert controller._stats.num_excluded_shopping_ads == 1


def test_search_for_ads_stores_blocked_domains(make_search_controller, monkeypatch):
    controller = make_search_controller()

    def stop(*args, **kwargs):
        raise StopBeforeSearch()

    monkeypatch.setattr(controller, "_apply_cookies", stop, raising=False)

    with pytest.raises(StopBeforeSearch):
        controller.search_for_ads(blocked_domains=["edelind.de"])

    assert controller._blocked_domains == ["edelind.de"]


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
            controller.search_for_ads(blocked_domains=[])

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


# --- cookie-баннер: кликается только кнопка согласия ------------------------------


class ConsentButton:
    """Кнопка баннера: текст и запись кликов в общий список."""

    def __init__(self, label, clicks):
        self.text = label
        self._clicks = clicks

    def get_attribute(self, name):
        return None

    def click(self):
        self._clicks.append(self.text)


class ConsentDriver(FakeDriver):
    """Признак баннера (policies-ссылка) + набор кнопок в порядке DOM."""

    def __init__(self, buttons=(), consent_link=True):
        super().__init__()
        self.clicks = []
        self._buttons = [ConsentButton(label, self.clicks) for label in buttons]
        self._consent_link = consent_link

    def find_elements(self, by, value=None):
        if by == By.TAG_NAME and value == "a":
            if self._consent_link:
                return [FakeElement(attributes={"href": "https://policies.google.com/terms"})]
            return []
        if by == By.TAG_NAME and value == "button":
            return list(self._buttons)
        return []


class TestCloseCookieDialog:
    """Один клик по согласию; прежний спам «все кнопки подряд» удалён."""

    def test_only_the_accept_button_is_clicked(self, make_search_controller):
        driver = ConsentDriver(
            buttons=["Manage your data", "Alle akzeptieren", "Reject all"]
        )
        controller = make_search_controller(driver=driver)

        controller._close_cookie_dialog()

        assert driver.clicks == ["Alle akzeptieren"]

    def test_unknown_button_texts_are_left_alone(self, make_search_controller):
        driver = ConsentDriver(buttons=["Manage your data", "More options"])
        controller = make_search_controller(driver=driver)

        controller._close_cookie_dialog()

        assert driver.clicks == [], "без известного текста согласия — ноль кликов"

    def test_without_the_policies_link_nothing_is_touched(self, make_search_controller):
        driver = ConsentDriver(buttons=["Alle akzeptieren"], consent_link=False)
        controller = make_search_controller(driver=driver)

        controller._close_cookie_dialog()

        assert driver.clicks == [], "нет признака баннера — кнопки не ищутся"


# --- _type_humanlike: лестница вместо молчаливого глотания -------------------------


class BrokenTypingElement(FakeElement):
    """Элемент, на который send_keys никогда не сработает."""

    def send_keys(self, keys):
        raise ElementNotInteractableException("поле перекрыто")


class HiddenElement(FakeElement):
    """Элемент есть, но не виден — кликабельным не станет."""

    def is_displayed(self):
        return False


class DeadScriptDriver(FakeDriver):
    """execute_script всегда падает: и JS-фолбэк обязан отказать."""

    def execute_script(self, script, *args):
        super().execute_script(script, *args)
        raise WebDriverException("renderer dead")


class TestTypeHumanlike:
    """Набор → JS-фолбэк → явный отказ; тишина в лог.debug больше не вариант."""

    def test_types_the_query_char_by_char_and_sends_enter(self, make_search_controller):
        controller = make_search_controller()
        element = FakeElement()

        controller._type_humanlike(element, "usb hub")

        assert element.recorded_keys == list("usb hub") + [Keys.ENTER]
        assert not any("dispatchEvent" in s for s in controller._driver.scripts), (
            "без сбоев JS-фолбэк не запускается"
        )

    def test_send_keys_failure_falls_back_to_js(self, make_search_controller):
        controller = make_search_controller()

        controller._type_humanlike(BrokenTypingElement(), "usb hub")

        assert any("dispatchEvent" in s for s in controller._driver.scripts), (
            "после сбоя send_keys запрос уходит через JS-фолбэк"
        )

    def test_failed_js_fallback_raises_search_round_error(self, make_search_controller):
        driver = DeadScriptDriver()
        controller = make_search_controller(driver=driver)

        with pytest.raises(SearchRoundError):
            controller._type_humanlike(BrokenTypingElement(), "usb hub")

    def test_never_clickable_box_raises_search_round_error(
        self, make_search_controller, monkeypatch
    ):
        monkeypatch.setattr(search_controller, "SEARCH_BOX_WAIT_TIMEOUT_S", 0.01)
        controller = make_search_controller()

        with pytest.raises(SearchRoundError):
            controller._type_humanlike(HiddenElement(), "usb hub")


# --- search_for_ads: явный отказ вместо тихого «успеха» ------------------------------


class FlakyBoxDriver(FakeDriver):
    """Поле поиска: find #1 и #3 падают ENI, find #2 отдаёт готовый элемент."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def find_element(self, by, value=None):
        if by == By.NAME and value == "q":
            self.calls += 1
            if self.calls in (1, 3):
                raise ElementNotInteractableException("поле ещё не готово")
            return FakeElement()
        if by == By.ID and value == "recaptcha":
            raise NoSuchElementException("капчи нет")
        return FakeElement()


class NeverReadyBoxDriver(FakeDriver):
    """Первый find падает ENI, дальше поле есть, но скрыто — так и не готово."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def find_element(self, by, value=None):
        if by == By.NAME and value == "q":
            self.calls += 1
            if self.calls == 1:
                raise ElementNotInteractableException("поле ещё не готово")
            return HiddenElement()
        if by == By.ID and value == "recaptcha":
            raise NoSuchElementException("капчи нет")
        return FakeElement()


class NoResultsDriver(FakeDriver):
    """Набор запроса проходит, но #appbar не появляется никогда."""

    def find_element(self, by, value=None):
        if by == By.ID and value == "appbar":
            raise NoSuchElementException("результатов нет")
        if by == By.ID and value == "recaptcha":
            raise NoSuchElementException("капчи нет")
        return FakeElement()


class TestSearchRoundFailures:
    """Отказы поиска — SearchRoundError (run_scenario → completed=False).

    Раньше эти ветки возвращали пустой результат, и воркер записывал раунд
    как успех «No ads found» с нулём кликов (живой прогон на маке).
    """

    def test_still_not_interactable_after_recovery_raises(self, make_search_controller):
        driver = FlakyBoxDriver()
        controller = make_search_controller(driver=driver)

        with pytest.raises(SearchRoundError):
            controller.search_for_ads(blocked_domains=[])

    def test_search_box_never_ready_raises(self, make_search_controller, monkeypatch):
        monkeypatch.setattr(search_controller, "SEARCH_BOX_WAIT_TIMEOUT_S", 0.01)
        driver = NeverReadyBoxDriver()
        controller = make_search_controller(driver=driver)

        with pytest.raises(SearchRoundError):
            controller.search_for_ads(blocked_domains=[])

    def test_results_timeout_raises_instead_of_returning_empty(
        self, make_search_controller, monkeypatch
    ):
        monkeypatch.setattr(search_controller, "RESULTS_WAIT_TIMEOUT_S", 0.05)
        driver = NoResultsDriver()
        controller = make_search_controller(driver=driver)

        with pytest.raises(SearchRoundError):
            controller.search_for_ads(blocked_domains=[])


# --- CAPTCHA: сигнал ротации прокси ---------------------------------------------


class CaptchaVisibleDriver(FakeDriver):
    """Драйвер с видимой reCAPTCHA: любая ветка детекта уходит в stop."""

    def find_element(self, by, value=None):
        if by == By.ID and value == "recaptcha":
            return FakeElement({"data-sitekey": "sitekey-1", "data-s": "data-s-1"})
        return super().find_element(by, value)


class NoCaptchaDriver(FakeDriver):
    """Драйвер без капчи: ``#recaptcha`` не находится никогда."""

    def find_element(self, by, value=None):
        if by == By.ID and value == "recaptcha":
            raise NoSuchElementException("капчи нет")
        return super().find_element(by, value)


class CaptchaLogRecorder(LogRecorder):
    """Логгер captcha-ветки: пишет событие и сигнал ротации, в БД ничего не уходит."""

    # ``_event_browser_id`` читает его у логгера: без биндинга контекст события
    # (прокси/run в БД) пропускается, а тестам важен только сигнал.
    browser_id = None

    def __init__(self):
        super().__init__()
        self.captcha_events = []
        self.degraded = []
        self.timeline = []

    def record_captcha_event(self, *args, **kwargs):
        self.captcha_events.append(kwargs)
        self.timeline.append("event")

    def mark_degraded(self, reason, browser_id=None):
        self.degraded.append((reason, browser_id))
        self.timeline.append("degraded")


@pytest.fixture
def captcha_log(monkeypatch):
    """Логгер-заглушка captcha-ветки: без БД, с фиксацией сигнала ротации."""

    recorder = CaptchaLogRecorder()
    monkeypatch.setattr(search_controller, "log", recorder)
    return recorder


class TestCaptchaProxyRotation:
    """Сигнал ротации прокси из captcha-ветки (без БД и настоящего браузера).

    Дефект: капча детектилась, событие писалось, а прокси не менялся —
    следующий заход шёл с того же IP. Сигнал — ``log.mark_degraded``, его
    читает супервизор (``_handle_degraded``) и подменяет прокси.
    """

    def test_no_captcha_leaves_the_proxy_alone(self, make_search_controller, captcha_log):
        """Капчи нет — ни события, ни сигнала: ротировать нечего."""

        controller = make_search_controller(driver=NoCaptchaDriver())

        controller._check_captcha()

        assert captcha_log.timeline == []
        assert captcha_log.captcha_events == []
        assert captcha_log.degraded == []

    def test_detected_captcha_signals_rotation_after_the_event(
        self, make_search_controller, captcha_log
    ):
        """Детект при дефолтной политике stop: событие, затем сигнал, затем выход."""

        controller = make_search_controller(driver=CaptchaVisibleDriver())

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert captcha_log.degraded == [(CAPTCHA_PROXY_ROTATION_REASON, None)]
        assert captcha_log.timeline == [
            "event",
            "degraded",
        ], "событие обязано быть записано раньше сигнала: супервизор гасит процесс"

    def test_stop_for_captcha_is_the_funnel_for_the_signal(
        self, make_search_controller, captcha_log
    ):
        """Stop-ветка шлёт ровно один сигнал и только потом роняет процесс."""

        controller = make_search_controller(driver=FakeDriver())

        with pytest.raises(SystemExit):
            controller._stop_for_captcha("captcha_policy=stop")

        assert captcha_log.degraded == [(CAPTCHA_PROXY_ROTATION_REASON, None)]
        assert captcha_log.timeline == ["degraded"]



# --- Прод-дефект 1: реклама не собирается → раунд закрывается без кликов -----------


class NonScrollableAdsDriver(FakeDriver):
    """Драйвер с рекламой, у которого страница уже «в конце»: скроллить некуда.

    Так выглядит короткая выдача: ``_is_scroll_at_the_end`` истинна с самого
    первого вызова, и цикл прокрутки ``_get_ad_links`` не выполняется ни разу.
    """

    def __init__(self, ad):
        super().__init__()
        self.ad = ad

    def find_elements(self, by, value=None):
        return [AdContainer([self.ad])]

    def execute_script(self, script, *args):
        # И scrollHeight, и pageYOffset+innerHeight равны → «в конце».
        return 1000


def test_ad_links_collected_when_page_is_not_scrollable(make_search_controller):
    # Верхняя реклама (``#tads``) видна без прокрутки, но старый цикл собирал
    # её только внутри while-условия: на не-прокручиваемой странице он не
    # запускался вовсе, ads оставался пустым и раунд закрывал браузер.
    driver = NonScrollableAdsDriver(make_ad("Wireless Keyboard Sale"))
    controller = make_search_controller(query="wireless keyboard", driver=driver)

    assert controller._get_ad_links() == [
        (driver.ad, "https://ads.example.com/kb", "Wireless Keyboard Sale")
    ]


class _ScrollBody(FakeElement):
    """Тело страницы: PAGE_DOWN помечает драйвер прокрученным."""

    def __init__(self, driver):
        super().__init__()
        self._driver = driver

    def send_keys(self, keys):
        super().send_keys(keys)
        if keys == Keys.PAGE_DOWN:
            self._driver.scrolled = True


class LazyBottomAdsDriver(FakeDriver):
    """Нижняя реклама появляется в DOM только после прокрутки вниз.

    Позиция после последнего PAGE_DOWN — ровно та, на которой Google дорисовывает
    ``#tadsb``: старый цикл проверял конец страницы ДО сбора и никогда не
    обследовал финальную позицию.
    """

    def __init__(self, top_ad, bottom_ad):
        super().__init__()
        self.top_ad = top_ad
        self.bottom_ad = bottom_ad
        self.scrolled = False
        self.body = _ScrollBody(self)

    def find_element(self, by, value=None):
        if value == "body":
            return self.body
        return super().find_element(by, value)

    def find_elements(self, by, value=None):
        ads = [self.top_ad] + ([self.bottom_ad] if self.scrolled else [])
        return [AdContainer(ads)]

    def execute_script(self, script, *args):
        if "scrollHeight" in script:
            return 1000
        return 1000 if self.scrolled else 0


def test_ad_links_collected_after_the_final_scroll(make_search_controller):
    top = make_ad("Top Ad", link="https://ads.example.com/top")
    bottom = make_ad("Bottom Ad", link="https://ads.example.com/bottom")
    driver = LazyBottomAdsDriver(top, bottom)
    controller = make_search_controller(query="wireless keyboard", driver=driver)

    assert controller._get_ad_links() == [
        (top, "https://ads.example.com/top", "Top Ad"),
        (bottom, "https://ads.example.com/bottom", "Bottom Ad"),
    ]


# --- Прод-дефект 2: цель из чёрного списка попадает в клик -------------------------


def test_is_blocked_target_matches_destination_inside_redirect(make_search_controller):
    # href клика — перенаправление Google: по хосту ссылки чёрный список не
    # срабатывает, а клик всё равно ведёт на запрещённый домен.
    controller = make_search_controller()
    controller._blocked_domains = ["edelind.de"]
    redirect = (
        "https://www.google.com/aclk?sa=L&ai=xyz"
        "&adurl=https%3A%2F%2Fwww.edelind.de%2Fgoldkette"
    )

    assert controller._is_blocked_target(redirect) is True
    assert (
        controller._is_blocked_target(
            "https://www.google.com/aclk?adurl=https%3A%2F%2Fshop.example%2Fx"
        )
        is False
    )


def test_get_non_ad_links_skips_google_redirect_to_blocked_domain(make_search_controller):
    # Нестандартная схема/хост Google: фильтр «https://www.google» такие ссылки
    # пропускает, поэтому запрет обязан читать цель из параметра ``url=``.
    link = make_link(
        "http://google.de/url?sa=t&source=web"
        "&url=https%3A%2F%2Fwww.edelind.de%2Fgoldkette"
    )
    controller = make_search_controller(driver=FakeDriver(links=[link]))

    assert controller._get_non_ad_links([], ["edelind.de"]) == []


def test_refresh_blocked_domains_adds_entries_written_during_the_round(
    make_search_controller, monkeypatch
):
    # domains.txt мог обновиться (через UI), пока шёл поиск: перед кликами
    # файл читается заново, а список за раунд только расширяется.
    controller = make_search_controller()
    controller._blocked_domains = ["edelind.de"]
    monkeypatch.setattr(search_controller, "get_domains", lambda: ["shop.example"])

    controller._refresh_blocked_domains()

    assert controller._blocked_domains == ["edelind.de", "shop.example"]


def test_refresh_blocked_domains_keeps_round_list_when_read_fails(
    make_search_controller, monkeypatch
):
    controller = make_search_controller()
    controller._blocked_domains = ["edelind.de"]

    def no_file():
        raise SystemExit("Couldn't find domains file: domains.txt")

    monkeypatch.setattr(search_controller, "get_domains", no_file)

    controller._refresh_blocked_domains()

    assert controller._blocked_domains == ["edelind.de"], (
        "сбой чтения не должен разблокировать уже известный список"
    )


class DesktopUnit:
    """Десктопный ``pla-unit``: якорь с href и якорь с aria-label."""

    def __init__(self, href, target, title, broken=False):
        self._anchor = FakeElement(attributes={"href": href})
        self._data = FakeElement(attributes={"href": target, "aria-label": title})
        self.broken = broken

    def find_element(self, by, value=None):
        if value == "a:nth-child(2)":
            if self.broken:
                raise NoSuchElementException("якорь с aria-label отсутствует")
            return self._data
        return self._anchor


class DesktopShoppingDriver(FakeDriver):
    """Вёрстка без мобильного контейнера: путь ``cu-container``/``pla-unit``."""

    def __init__(self, units):
        super().__init__()
        self.units = units

    def find_elements(self, by, value=None):
        if value == "pla-unit-container":
            return []
        if value == "pla-unit":
            return list(self.units)
        return []

    def find_element(self, by, value=None):
        if value == "cu-container":
            return AdContainer(self.units)
        return super().find_element(by, value)


def test_shopping_ads_filtered_when_collection_fails_halfway(make_search_controller):
    # Сбор прерван NoSuchElement на втором блоке: раньше ветка отдавала ads
    # как есть — минуя и фильтры, и чёрный список, — и кликер уходил на цель
    # из списка запрещённых доменов.
    driver = DesktopShoppingDriver(
        [
            DesktopUnit(
                href="https://www.edelind.de/goldkette",
                target="https://www.edelind.de/goldkette",
                title="Goldkette 75cm",
            ),
            DesktopUnit(
                href="https://shop.example/keyboard",
                target="https://shop.example/keyboard",
                title="Wireless Keyboard",
                broken=True,
            ),
        ]
    )
    controller = make_search_controller(query="wireless keyboard", driver=driver)
    controller._blocked_domains = ["edelind.de"]

    assert controller._get_shopping_ad_links() == []
    assert controller._stats.num_excluded_shopping_ads == 1


class ClickDriver(FakeDriver):
    """Драйвер для клик-фазы: окно результатов и вызов stealth через CDP."""

    current_window_handle = "search-window"

    def execute_cdp_cmd(self, command, payload=None):
        return None


@pytest.fixture
def record_clicks(monkeypatch):
    """Перехват клика на точке входа ``_handle_browser_click``."""

    def _install(controller):
        clicked = []
        monkeypatch.setattr(
            controller,
            "_handle_browser_click",
            lambda element, url, is_ad, handle, category="Ad": clicked.append(
                (url, category, is_ad)
            ),
            raising=False,
        )
        return clicked

    return _install


def test_click_links_rechecks_blacklist_right_before_click(
    make_search_controller, monkeypatch, record_clicks
):
    # Ссылка собрана до того, как в domains.txt появился запрещённый домен:
    # список перечитывается и проверяется непосредственно перед кликом.
    link = make_link("http://www.edelind.de/goldkette")
    controller = make_search_controller(driver=ClickDriver())
    controller._blocked_domains = []
    monkeypatch.setattr(search_controller, "get_domains", lambda: ["edelind.de"])
    clicked = record_clicks(controller)

    controller.click_links([link])

    assert clicked == [], "клика по домену из чёрного списка быть не должно"


def test_click_links_clicks_target_that_is_not_blocked(
    make_search_controller, record_clicks
):
    link = make_link("http://shop.example/product")
    controller = make_search_controller(driver=ClickDriver())
    controller._blocked_domains = ["edelind.de"]
    clicked = record_clicks(controller)

    controller.click_links([link])

    assert [(url, category) for url, category, _ in clicked] == [
        ("http://shop.example/product", "Non-ad")
    ]


def test_click_links_skips_ad_whose_real_target_is_blocked(
    make_search_controller, monkeypatch, record_clicks
):
    href = "https://www.google.com/aclk?adurl=https%3A%2F%2Fwww.edelind.de%2Fx"
    ad = FakeElement(
        attributes={"href": href, "data-pcu": "https://www.edelind.de/x"},
        text="Ad title",
    )
    controller = make_search_controller(driver=ClickDriver())
    monkeypatch.setattr(search_controller, "get_domains", lambda: ["edelind.de"])
    clicked = record_clicks(controller)

    controller.click_links([(ad, href, "Ad title")])

    assert clicked == []
    assert controller._stats.num_excluded_ads == 1


def test_click_links_survives_a_stale_element(make_search_controller, record_clicks):
    # Протухший элемент раньше ронял сам click_links (UnboundLocalError в
    # обработчике) — прогон обрывался, и оставшиеся ссылки не кликались.
    class StaleLink(FakeElement):
        def get_attribute(self, name):
            if name == "href":
                raise StaleElementReferenceException("element is stale")
            return super().get_attribute(name)

    stale = StaleLink(attributes={"href": "http://gone.example"})
    good = make_link("http://shop.example/product")
    controller = make_search_controller(driver=ClickDriver())
    clicked = record_clicks(controller)

    controller.click_links([stale, good])

    assert [url for url, _, _ in clicked] == ["http://shop.example/product"], (
        "протухший элемент не должен обрывать оставшиеся клики"
    )


def test_click_shopping_ads_rechecks_blacklist_right_before_click(
    make_search_controller, monkeypatch, record_clicks
):
    target = "http://www.edelind.de/goldkette"
    ad = FakeElement(attributes={"href": target})
    controller = make_search_controller(driver=ClickDriver())
    controller._blocked_domains = []
    monkeypatch.setattr(search_controller, "get_domains", lambda: ["edelind.de"])
    clicked = record_clicks(controller)

    controller.click_shopping_ads([(ad, target, "Goldkette 75cm")])

    assert clicked == []
    assert controller._stats.num_excluded_shopping_ads == 1, (
        "пропуск на этапе клика должен попадать в счётчик shopping-исключений"
    )


# --- Прод-дыра: цель скрыта в редиректе Google, домен виден только в тексте ----


def test_is_blocked_target_matches_domain_only_in_title(make_search_controller):
    # Прод-кейс: href = google.com/aclk без расшифруемой цели, а в aria-label
    # объявления написано «... edelind.de Rabattcode...». Хост href — google,
    # destination-параметров нет, но клик уходит на домен из чёрного списка.
    controller = make_search_controller()
    controller._blocked_domains = ["edelind.de"]
    aclk = "https://www.google.com/aclk?sa=L&ai=DChsSEwi1xJfvrqe&co=1"

    assert controller._is_blocked_target(
        aclk,
        title="Kette Gold 750 Damen 50 cm | EDELIND edelind.de Rabattcode Kostenlos",
    )
    assert not controller._is_blocked_target(
        aclk, title="Wireless Keyboard Sale shop.example"
    )


def test_is_blocked_target_matches_domain_in_text(make_search_controller):
    controller = make_search_controller()
    controller._blocked_domains = ["edelind.de"]
    goto = "https://www.google.com/goto?url=CAESbwHrOzAVVU3fis6DpR9W"

    assert controller._is_blocked_target(
        goto, text="Edle Goldketten - Gold 750 edelind.de"
    )


def test_is_blocked_target_ignores_lookalikes_and_numbers_in_title(
    make_search_controller,
):
    # «notedelind.de» — чужой хост с той же подстрокой: граница домена
    # обязана сохраниться, а числа вида «1.039» и версии «154.0.8037.92»
    # доменами не являются и блокировать не должны.
    controller = make_search_controller()
    controller._blocked_domains = ["edelind.de"]
    aclk = "https://www.google.com/aclk?sa=L&ai=xyz"

    assert not controller._is_blocked_target(
        aclk, title="Notedelind.de Wettbewerber Kette 1.039 € Chrome/154.0.8037.92"
    )


def test_shopping_ads_dropped_when_domain_only_in_title(make_search_controller):
    # Прод-кейс: href shopping-якоря — aclk-редирект Google (цель
    # непрозрачна), data-pcu у shopping отсутствует; спасает только
    # упоминание домена в aria-label.
    driver = ShoppingAdsDriver(
        [
            (
                "https://www.google.com/aclk?sa=L&ai=DChsSEwi1xJfvrqe&co=1",
                "Kette Gold 750 | EDELIND edelind.de Rabattcode",
            ),
            ("https://www.google.com/aclk?sa=L&ai=other&co=1", "Wireless Keyboard"),
        ]
    )
    controller = make_search_controller(query="wireless keyboard", driver=driver)
    controller._blocked_domains = ["edelind.de"]

    result = controller._get_shopping_ad_links()

    assert [entry[2] for entry in result] == ["Wireless Keyboard"]
    assert controller._stats.num_excluded_shopping_ads == 1


def test_click_shopping_ads_skips_when_domain_only_in_title(
    make_search_controller, monkeypatch, record_clicks
):
    # Последний рубеж: ссылка собрана до обновления списка, домен виден
    # только в заголовке — клика не должно быть.
    ad = FakeElement(
        attributes={"href": "https://www.google.com/aclk?sa=L&ai=xyz"},
        text="Kette Gold 750",
    )
    controller = make_search_controller(driver=ClickDriver())
    controller._blocked_domains = []
    monkeypatch.setattr(search_controller, "get_domains", lambda: ["edelind.de"])
    clicked = record_clicks(controller)

    controller.click_shopping_ads(
        [(ad, "https://www.google.com/aclk?sa=L&ai=xyz", "edelind.de Rabattcode")]
    )

    assert clicked == []
    assert controller._stats.num_excluded_shopping_ads == 1


def test_click_links_skips_ad_whose_text_mentions_blocked_domain(
    make_search_controller, monkeypatch, record_clicks
):
    # goto-ссылка: href непрозрачен, заголовок пуст, а видимый текст
    # объявления содержит домен — клика не должно быть.
    href = "https://www.google.com/goto?url=CAESbwHrOzAVVU3f"
    ad = FakeElement(
        attributes={"href": href}, text="Edle Goldketten edelind.de Rabattcode"
    )
    controller = make_search_controller(driver=ClickDriver())
    controller._blocked_domains = ["edelind.de"]
    clicked = record_clicks(controller)

    controller.click_links([(ad, href, "")])

    assert clicked == []
    assert controller._stats.num_excluded_ads == 1
