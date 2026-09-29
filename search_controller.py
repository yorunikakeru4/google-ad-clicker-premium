import sys
import json
import random
import time
from datetime import datetime
from pathlib import Path
from time import sleep
from threading import Thread
from typing import Any, Optional, Union

import selenium
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    JavascriptException,
    TimeoutException,
    NoSuchElementException,
    ElementNotInteractableException,
    ElementClickInterceptedException,
    StaleElementReferenceException,
)

import hooks
from adb import adb_controller
from clicklogs_db import ClickLogsDB
from config_reader import config
from engine.captcha_policy import CAPTCHA_POLICIES, DEFAULT_CAPTCHA_POLICY
from engine.control_plane.state import StateStore
from engine.diagnostics import proxy_context
from engine.log import get_logger, resolve_db_path
from engine.profile_apply import (
    add_profile_cookies,
    current_profile,
    load_profile_cookies,
    save_profile_cookies,
    should_apply_cookies,
)
from stats import SearchStats
from utils import (
    Direction,
    add_cookies,
    solve_recaptcha,
    get_random_sleep,
    resolve_redirect,
    boost_requests,
)
from webdriver import execute_stealth_js_code


log = get_logger()


# --- CAPTCHA: константы политики solve -----------------------------------------

# Общий таймаут на решение ОДНОЙ капчи, сек. solve_recaptcha сам ждёт ответ
# сервиса (см. SOLVE_INITIAL_WAIT_S и опросы в utils), но сетевой код
# синхронный и без ограничения мог бы держать сценарий сколько угодно —
# вызов идёт в daemon-потоке с join ровно на столько. По истечении —
# неудача и stop-ветка, заброшенный поток процесс не блокирует.
CAPTCHA_SOLVE_TIMEOUT_S = 90

# Ретраи решения на одно событие: столько попыток solve_recaptcha даётся,
# прежде чем признать капчу нерешённой. Каждая попытка съедает единицу
# сессионного лимита ниже.
CAPTCHA_SOLVE_RETRIES = 2

# Лимит попыток 2captcha на процесс воркера за сессию. Счётчик живёт на
# классе, а не на инстансе: SearchController создаётся заново на каждый
# сценарий, а лимит защищает кошелёк оператора от бесконечных решений при
# сломанном детекте или мёртвом прокси. Исчерпан → stop-ветка с solved=false.
CAPTCHA_SOLVE_SESSION_LIMIT = 5

# Каталог скриншотов CAPTCHA — относительно текущего каталога (как и весь
# legacy-ввод/вывод: config.json, logs/, clicklogs.db), уже в .gitignore.
CAPTCHA_SCREENSHOT_DIR = Path("engine/screenshots")


LinkElement = selenium.webdriver.remote.webelement.WebElement
AdList = list[tuple[LinkElement, str, str]]
NonAdList = list[LinkElement]
AllLinks = list[Union[AdList, NonAdList]]


class SearchController:
    """Search controller for ad clicker

    :type driver: selenium.webdriver
    :param driver: Selenium Chrome webdriver instance
    :type query: str
    :param query: Search query
    :type country_code: str
    :param country_code: Country code for the proxy IP
    """

    URL = "https://www.google.com"

    SEARCH_INPUT = (By.NAME, "q")
    RESULTS_CONTAINER = (By.ID, "appbar")
    COOKIE_DIALOG_BUTTON = (By.TAG_NAME, "button")
    TOP_ADS_CONTAINER = (By.ID, "tads")
    BOTTOM_ADS_CONTAINER = (By.ID, "tadsb")
    AD_RESULTS = (By.CSS_SELECTOR, "div > a")
    AD_TITLE = (By.CSS_SELECTOR, "div[role='heading']")
    ALL_LINKS = (By.CSS_SELECTOR, "div a")
    CAPTCHA_DIALOG = (By.CSS_SELECTOR, "div[aria-label^='Captcha-Dialog']")
    CAPTCHA_IFRAME = (By.CSS_SELECTOR, "iframe[title='Captcha']")
    RECAPTCHA = (By.ID, "recaptcha")
    ESTIMATED_LOC_IMG = (
        By.CSS_SELECTOR,
        "img[src^='https://ssl.gstatic.com/oolong/preprompt/Estimated']",
    )
    LOC_CONTINUE_BUTTON = (By.TAG_NAME, "g-raised-button")
    NOT_NOW_BUTTON = (By.CSS_SELECTOR, "g-raised-button[data-ved]")

    # Попыток решить капчу за сессию процесса — см. CAPTCHA_SOLVE_SESSION_LIMIT.
    # На классе, а не на инстансе: контроллер живёт один сценарий, лимит —
    # весь воркер.
    _solve_attempts_used = 0

    def __init__(
        self, driver: selenium.webdriver, query: str, country_code: Optional[str] = None
    ) -> None:
        self._driver = driver
        self._search_query, self._filter_words = self._process_query(query)
        self._exclude_list = None
        self._random_mouse_enabled = config.behavior.random_mouse
        self._use_custom_cookies = config.behavior.custom_cookies
        # Строка профиля читается один раз на прогон: её видят и применение
        # cookies (search_for_ads), и выгрузка обратно (end_search).
        self._profile = current_profile()
        self._twocaptcha_apikey = config.behavior.twocaptcha_apikey
        self._max_scroll_limit = config.behavior.max_scroll_limit
        self._hooks_enabled = config.behavior.hooks_enabled

        self._ad_page_min_wait = config.behavior.ad_page_min_wait
        self._ad_page_max_wait = config.behavior.ad_page_max_wait
        self._nonad_page_min_wait = config.behavior.nonad_page_min_wait
        self._nonad_page_max_wait = config.behavior.nonad_page_max_wait

        self._android_device_id = None

        self._stats = SearchStats()

        if config.behavior.excludes:
            self._exclude_list = [item.strip() for item in config.behavior.excludes.split(",")]
            log.debug("click", "Words to be excluded", fields={"excludes": self._exclude_list})

        if country_code:
            self._set_start_url(country_code)

        self._clicklogs_db_client = ClickLogsDB()

        self._load()

    def _apply_cookies(self) -> None:
        """Применить cookies прогона: профильный набор или legacy cookies.txt.

        Правило (план.md, §5 «Фаза 6», «Персистентные cookie отдельно на
        профиль»), зафиксированное в :func:`engine.profile_apply.should_apply_cookies`:

        * **профиль назначен** → набор берётся из его файла и применяется
          **всегда**, независимо от ``behavior.custom_cookies``: cookies —
          часть привязки профиля, а флаг отвечает за legacy-путь. Чужие
          cookies при этом удаляются в любом случае, поэтому профиль без
          файла стартует чистым, а не с общего ``cookies.txt``;
        * **профиля нет** → прежнее поведение: ``custom_cookies`` и есть ответ
          «применять cookies вообще», источник — общий ``cookies.txt``.

        В лог уходят только имена cookies: значения — это сессионные креды
        профиля, а записи уходят в ``logs`` и в UI.
        """
        if not should_apply_cookies(self._profile, self._use_custom_cookies):
            return

        self._driver.delete_all_cookies()

        if self._profile is not None:
            add_profile_cookies(self._driver, load_profile_cookies(self._profile["id"]))
            source = "profile"
        else:
            add_cookies(self._driver)
            source = "cookies.txt"

        loaded = self._driver.get_cookies()
        log.debug(
            "browser",
            "Cookies applied",
            fields={
                "source": source,
                "count": len(loaded),
                "names": [cookie.get("name") for cookie in loaded],
            },
        )

    def search_for_ads(
        self, non_ad_domains: Optional[list[str]] = None
    ) -> tuple[AdList, NonAdList]:
        """Start search for the given query and return ads if any

        Also, get non-ad links including domains given.

        :type non_ad_domains: list
        :param non_ad_domains: List of domains to select for non-ad links
        :rtype: tuple
        :returns: Tuple of [(ad, ad_link, ad_title), non_ad_links]
        """

        self._apply_cookies()

        self._check_captcha()
        self._close_cookie_dialog()

        log.info("click", "Starting search for", fields={"query": self._search_query})
        sleep(get_random_sleep(1, 2) * config.behavior.wait_factor)

        try:
            search_input_box = self._driver.find_element(*self.SEARCH_INPUT)
            self._type_humanlike(search_input_box, self._search_query)

        except ElementNotInteractableException:
            self._check_captcha()
            self._close_cookie_dialog()

            try:
                log.debug("click", "Waiting for search box to be ready...")

                wait = WebDriverWait(self._driver, timeout=7)
                searchbox_ready = wait.until(EC.element_to_be_clickable(self.SEARCH_INPUT))

                if searchbox_ready:
                    log.debug("click", "Search box is ready...")

                    try:
                        search_input_box = self._driver.find_element(*self.SEARCH_INPUT)
                        self._type_humanlike(search_input_box, self._search_query)
                    except ElementNotInteractableException:
                        pass

            except TimeoutException:
                log.error("click", "Timed out waiting for search box!")
                self.end_search()

                return (None, None, None)

        self._check_captcha()

        # wait 2 to 3 seconds before checking if results were loaded
        sleep(get_random_sleep(2, 3) * config.behavior.wait_factor)

        if not self._driver.find_elements(*self.RESULTS_CONTAINER):
            self._close_cookie_dialog()

            search_input_box = self._driver.find_element(*self.SEARCH_INPUT)
            if not search_input_box.get_attribute("value"):
                log.debug("click", "Reentering search query", fields={"query": self._search_query})
                self._type_humanlike(search_input_box, self._search_query)

                # sleep after entering search keyword by randomly selected amount
                # between 2 to 3 seconds
                sleep(get_random_sleep(2, 3) * config.behavior.wait_factor)

        if self._hooks_enabled:
            hooks.after_query_sent_hook(self._driver, self._search_query)

        ad_links = []
        non_ad_links = []
        shopping_ad_links = []

        try:
            wait = WebDriverWait(self._driver, timeout=5)
            results_loaded = wait.until(EC.presence_of_element_located(self.RESULTS_CONTAINER))

            if results_loaded:
                if self._hooks_enabled:
                    hooks.results_ready_hook(self._driver)

                self._close_choose_location_popup()

                self._make_random_scrolls()
                self._make_random_mouse_movements()

                self._close_choose_location_popup()

                if config.behavior.check_shopping_ads:
                    shopping_ad_links = self._get_shopping_ad_links()

                ad_links = self._get_ad_links()
                non_ad_links = self._get_non_ad_links(ad_links, non_ad_domains)

        except TimeoutException:
            log.error("click", "Timed out waiting for results!")
            self.end_search()

        return (ad_links, non_ad_links, shopping_ad_links)

    def click_shopping_ads(self, shopping_ads: AdList) -> None:
        """Click shopping ads if there are any

        :type shopping_ads: AdList
        :param shopping_ads: List of (ad, ad_link, ad_title) tuples
        """

        # store the ID of the original window
        original_window_handle = self._driver.current_window_handle

        for ad in shopping_ads:
            try:
                ad_link_element = ad[0]
                ad_link = ad[1]
                ad_title = ad[2].replace("\n", " ")
                log.info("click", "Clicking to", fields={"title": ad_title, "url": ad_link})

                if self._hooks_enabled:
                    hooks.before_ad_click_hook(self._driver)

                if config.behavior.send_to_android and self._android_device_id:
                    self._handle_android_click(ad_link_element, ad_link, True, category="Shopping")
                else:
                    self._handle_browser_click(
                        ad_link_element, ad_link, True, original_window_handle, category="Shopping"
                    )

            except Exception:
                log.debug("click", "Failed to click ad element!", fields={"title": ad_title})

    def click_links(self, links: AllLinks) -> None:
        """Click links

        :type links: AllLinks
        :param links: List of [(ad, ad_link, ad_title), non_ad_links]
        """

        execute_stealth_js_code(self._driver)

        # store the ID of the original window
        original_window_handle = self._driver.current_window_handle

        for link in links:
            is_ad_element = isinstance(link, tuple)

            try:
                link_element, link_url, ad_title = self._extract_link_info(link, is_ad_element)

                if self._hooks_enabled and is_ad_element:
                    hooks.before_ad_click_hook(self._driver)

                log.info(
                    "click",
                    "Clicking to",
                    fields={"title": ad_title, "url": link_url, "is_ad": is_ad_element},
                )

                category = "Ad" if is_ad_element else "Non-ad"

                if config.behavior.send_to_android and self._android_device_id:
                    self._handle_android_click(link_element, link_url, is_ad_element, category)
                else:
                    self._handle_browser_click(
                        link_element, link_url, is_ad_element, original_window_handle, category
                    )

                # scroll the page to avoid elements remain outside of the view
                self._driver.execute_script("arguments[0].scrollIntoView(true);", link_element)

            except StaleElementReferenceException:
                log.debug(
                    "click",
                    "Ad element has changed. Skipping scroll into view...",
                    fields={"element": ad_title if is_ad_element else link_url},
                )

            except Exception:
                log.error(
                    "click",
                    "Failed to click on",
                    fields={"element": ad_title if is_ad_element else link_url},
                )

    def _extract_link_info(self, link: Any, is_ad_element: bool) -> tuple:
        """Extract link information

        :type link: tuple(ad, ad_link, ad_title) or LinkElement
        :param link: (ad, ad_link, ad_title) for ads LinkElement for non-ads
        :type is_ad_element: bool
        :param is_ad_element: Whether it is an ad or non-ad link
        :rtype: tuple
        :returns: (link_element, link_url, ad_title) tuple
        """

        if is_ad_element:
            link_element = link[0]
            link_url = link[1]
            ad_title = link[2]
        else:
            link_element = link
            link_url = link_element.get_attribute("href")
            ad_title = None

        return (link_element, link_url, ad_title)

    def _handle_android_click(
        self,
        link_element: selenium.webdriver.remote.webelement.WebElement,
        link_url: str,
        is_ad_element: bool,
        category: str = "Ad",
    ) -> None:
        """Handle opening link on Android device

        :type link_element: selenium.webdriver.remote.webelement.WebElement
        :param link_element: Link element
        :type link_url: str
        :param link_url: Canonical url for the clicked link
        :type is_ad_element: bool
        :param is_ad_element: Whether it is an ad or non-ad link
        :type category: str
        :param category: Specifies link category as Ad, Non-ad, or Shopping
        """

        url = link_url if category == "Shopping" else link_element.get_attribute("href")

        url = resolve_redirect(url)

        adb_controller.open_url(url, self._android_device_id)

        click_time = datetime.now().strftime("%H:%M:%S")

        # wait a little before starting random actions
        sleep(get_random_sleep(2, 3) * config.behavior.wait_factor)

        log.debug("click", "Current url on device", fields={"url": url})

        if self._hooks_enabled and category in ("Ad", "Shopping"):
            hooks.after_ad_click_hook(self._driver)

        self._start_random_scroll_thread()

        site_url = (
            "/".join(url.split("/", maxsplit=3)[:3])
            if category in ("Shopping", "Non-ad")
            else link_url
        )

        self._update_click_stats(site_url, click_time, category)

        if config.behavior.request_boost:
            boost_requests(url)

        wait_time = self._get_wait_time(is_ad_element) * config.behavior.wait_factor
        log.debug(
            "click",
            "Waiting on page",
            fields={"seconds": wait_time, "page": category.lower()},
        )
        sleep(wait_time)

        adb_controller.close_browser()
        sleep(get_random_sleep(0.5, 1) * config.behavior.wait_factor)

    def _handle_browser_click(
        self,
        link_element: selenium.webdriver.remote.webelement.WebElement,
        link_url: str,
        is_ad_element: bool,
        original_window_handle: str,
        category: str = "Ad",
    ) -> None:
        """Handle clicking in the browser

        :type link_element: selenium.webdriver.remote.webelement.WebElement
        :param link_element: Link element
        :type link_url: str
        :param link_url: Canonical url for the clicked link
        :type is_ad_element: bool
        :param is_ad_element: Whether it is an ad or non-ad link
        :type original_window_handle: str
        :param original_window_handle: Window handle for the search results tab
        :type category: str
        :param category: Specifies link category as Ad, Non-ad, or Shopping
        """

        self._open_link_in_new_tab(link_element)

        if len(self._driver.window_handles) != 2:
            log.debug("click", "Couldn't click! Scrolling element into view...")
            self._driver.execute_script("arguments[0].scrollIntoView(true);", link_element)
            self._open_link_in_new_tab(link_element)

        if len(self._driver.window_handles) != 2:
            log.debug("click", "Failed to open in a new tab!", fields={"url": link_url})
            return
        else:
            log.debug("click", "Opened link in a new tab. Switching to tab...")

        for window_handle in self._driver.window_handles:
            if window_handle != original_window_handle:
                self._driver.switch_to.window(window_handle)
                click_time = datetime.now().strftime("%H:%M:%S")

                sleep(get_random_sleep(3, 5) * config.behavior.wait_factor)
                log.debug("click", "Current url on new tab", fields={"url": self._driver.current_url})

                if self._hooks_enabled and category in ("Ad", "Shopping"):
                    hooks.after_ad_click_hook(self._driver)

                self._start_random_action_threads()

                url = (
                    "/".join(self._driver.current_url.split("/", maxsplit=3)[:3])
                    if category == "Shopping"
                    else (link_url if is_ad_element else self._driver.current_url)
                )

                self._update_click_stats(url, click_time, category)

                if config.behavior.request_boost:
                    boost_requests(self._driver.current_url)

                wait_time = self._get_wait_time(is_ad_element) * config.behavior.wait_factor
                log.debug(
            "click",
            "Waiting on page",
            fields={"seconds": wait_time, "page": category.lower()},
        )
                sleep(wait_time)

                self._driver.close()
                break

        # go back to the original window
        self._driver.switch_to.window(original_window_handle)
        sleep(get_random_sleep(1, 1.5) * config.behavior.wait_factor)

    def _open_link_in_new_tab(
        self, link_element: selenium.webdriver.remote.webelement.WebElement
    ) -> None:
        """Open the link in a new browser tab

        :type link_element: selenium.webdriver.remote.webelement.WebElement
        :param link_element: Link element
        """

        platform = sys.platform
        control_command_key = Keys.COMMAND if platform.endswith("darwin") else Keys.CONTROL

        try:
            actions = ActionChains(self._driver)
            actions.move_to_element(link_element)
            actions.key_down(control_command_key)
            actions.click()
            actions.key_up(control_command_key)
            actions.perform()

            sleep(get_random_sleep(0.5, 1) * config.behavior.wait_factor)

        except JavascriptException as exp:
            error_message = str(exp).split("\n")[0]

            if "has no size and location" in error_message:
                log.error(
                    "click",
                    "Failed to click element, skipping...",
                    fields={"element_html": link_element.get_attribute("outerHTML")},
                )

    def _get_wait_time(self, is_ad_element: bool) -> int:
        """Get wait time based on whether the link is an ad or non-ad

        :type is_ad_element: bool
        :param is_ad_element: Whether it is an ad or non-ad link
        :rtype: int
        :returns: Randomly selected number from the given range
        """

        if is_ad_element:
            return random.choice(range(self._ad_page_min_wait, self._ad_page_max_wait))
        else:
            return random.choice(range(self._nonad_page_min_wait, self._nonad_page_max_wait))

    def _update_click_stats(self, url: str, click_time: str, category: str) -> None:
        """Update click statistics

        :type url: str
        :param url: Clicked link url to save db
        :type click_time: str
        :param click_time: Click time in hh:mm:ss format
        :type category: str
        :param category: Specifies link category as Ad, Non-ad, or Shopping
        """

        if category == "Ad":
            self._stats.ads_clicked += 1
        elif category == "Non-ad":
            self._stats.non_ads_clicked += 1
        elif category == "Shopping":
            self._stats.shopping_ads_clicked += 1

        self._clicklogs_db_client.save_click(
            site_url=url, category=category, query=self._search_query, click_time=click_time
        )

    def _start_random_scroll_thread(self) -> None:
        """Start a thread for random swipes on Android device"""

        random_scroll_thread = Thread(target=self._make_random_swipes)
        random_scroll_thread.start()
        random_scroll_thread.join(
            timeout=float(max(self._ad_page_max_wait, self._nonad_page_max_wait))
        )

    def _start_random_action_threads(self) -> None:
        """Start threads for random actions on browser"""

        random_scroll_thread = Thread(target=self._make_random_scrolls)
        random_scroll_thread.start()
        random_mouse_thread = Thread(target=self._make_random_mouse_movements)
        random_mouse_thread.start()
        random_scroll_thread.join(
            timeout=float(max(self._ad_page_max_wait, self._nonad_page_max_wait))
        )
        random_mouse_thread.join(
            timeout=float(max(self._ad_page_max_wait, self._nonad_page_max_wait))
        )

    def end_search(self) -> None:
        """Close the browser.

        Delete cookies and cache before closing.
        """

        if self._driver:
            # Выгрузка профильных cookies — строго ДО удаления и закрытия:
            # после этого выгружать уже нечего. Best-effort внутри, поэтому
            # она не может ронять teardown.
            self._save_profile_cookies()

            try:
                self._delete_cache_and_cookies()
                self._driver.quit()

            except Exception as exp:
                log.debug("browser", "Failed to close the browser", fields={"error": str(exp)})

            self._driver = None

    def _save_profile_cookies(self) -> None:
        """Сохранить cookies браузера в файл профиля. Best-effort, не бросает.

        Персистентность cookies живёт здесь, а не в ad_clicker: эта точка
        есть у каждого режима (UC и SeleniumBase) и выполняется на успехе и
        на ошибке сценария — ровно тогда, когда браузер ещё содержит
        актуальный набор. Любая причина отказа (драйвер уже умер, файл не
        записывается) даёт ``WARNING`` в ``browser`` и не мешает закрытию:
        потерять один прогон cookies дешевле, чем не закрыть браузер.
        """
        if self._profile is None:
            return

        profile_id = self._profile["id"]

        try:
            cookies = self._driver.get_cookies()
        except Exception as exp:
            log.warning(
                "browser",
                "Profile cookies were not read from the browser",
                fields={
                    "profile_id": profile_id,
                    "error": str(exp),
                    "error_type": type(exp).__name__,
                },
            )
            return

        try:
            path = save_profile_cookies(profile_id, cookies)
        except Exception as exp:
            log.warning(
                "browser",
                "Profile cookies were not saved",
                fields={
                    "profile_id": profile_id,
                    "error": str(exp),
                    "error_type": type(exp).__name__,
                },
            )
        else:
            log.debug(
                "browser",
                "Profile cookies saved",
                fields={"profile_id": profile_id, "path": str(path), "count": len(cookies)},
            )

    def _load(self) -> None:
        """Load Google main page"""

        if config.webdriver.use_seleniumbase:
            self._driver.uc_open_with_reconnect(self.URL, reconnect_time=3)
        else:
            self._driver.get(self.URL)

    def _get_shopping_ad_links(self) -> AdList:
        """Extract shopping ad links to click if exists

        :rtype: AdList
        :returns: List of (ad, ad_link, ad_title) tuples
        """

        ads = []

        try:
            log.info("click", "Checking shopping ads...")

            # for mobile user-agents
            if self._driver.find_elements(By.CLASS_NAME, "pla-unit-container"):
                mobile_shopping_ads = self._driver.find_elements(
                    By.CLASS_NAME, "pla-unit-container"
                )
                for shopping_ad in mobile_shopping_ads[:5]:
                    ad = shopping_ad.find_element(By.TAG_NAME, "a")
                    shopping_ad_link = ad.get_attribute("href")
                    shopping_ad_title = shopping_ad.text.strip()
                    shopping_ad_target_link = shopping_ad_link

                    ad_fields = (
                        shopping_ad,
                        shopping_ad_link,
                        shopping_ad_title,
                        shopping_ad_target_link,
                    )
                    log.debug("click", "Shopping ad candidate", fields={"ad": ad_fields})

                    ads.append(ad_fields)

            else:
                commercial_unit_container = self._driver.find_element(By.CLASS_NAME, "cu-container")
                shopping_ads = commercial_unit_container.find_elements(By.CLASS_NAME, "pla-unit")

                for shopping_ad in shopping_ads[:5]:
                    ad = shopping_ad.find_element(By.TAG_NAME, "a")
                    shopping_ad_link = ad.get_attribute("href")

                    ad_data_element = shopping_ad.find_element(By.CSS_SELECTOR, "a:nth-child(2)")
                    shopping_ad_title = ad_data_element.get_attribute("aria-label")
                    shopping_ad_target_link = ad_data_element.get_attribute("href")

                    ad_fields = (
                        shopping_ad,
                        shopping_ad_link,
                        shopping_ad_title,
                        shopping_ad_target_link,
                    )
                    log.debug("click", "Shopping ad candidate", fields={"ad": ad_fields})

                    ads.append(ad_fields)

            self._stats.shopping_ads_found = len(ads)

            if not ads:
                return []

            # if there are filter words given, filter results accordingly
            filtered_ads = []

            if self._filter_words:
                for ad in ads:
                    ad_title = ad[2].replace("\n", " ")
                    ad_link = ad[3]

                    for word in self._filter_words:
                        if word in ad_link or word in ad_title.lower():
                            if ad not in filtered_ads:
                                log.debug("click", "Filtering", fields={"title": ad_title, "link": ad_link})
                                self._stats.num_filtered_shopping_ads += 1
                                filtered_ads.append(ad)
            else:
                filtered_ads = ads

            shopping_ad_links = []

            for ad in filtered_ads:
                ad_link = ad[1]
                ad_title = ad[2].replace("\n", " ")
                ad_target_link = ad[3]
                log.debug("click", "Ad title", fields={"title": ad_title, "link": ad_link})

                if self._exclude_list:
                    for exclude_item in self._exclude_list:
                        if (
                            exclude_item in ad_target_link
                            or exclude_item.lower() in ad_title.lower()
                        ):
                            log.debug("click", "Excluding", fields={"title": ad_title, "link": ad_target_link})
                            self._stats.num_excluded_shopping_ads += 1
                            break
                    else:
                        log.info("click", "======= Found a Shopping Ad =======")
                        shopping_ad_links.append((ad[0], ad_link, ad_title))
                else:
                    log.info("click", "======= Found a Shopping Ad =======")
                    shopping_ad_links.append((ad[0], ad_link, ad_title))

            return shopping_ad_links

        except NoSuchElementException:
            log.info("click", "No shopping ads are shown!")

        return ads

    def _get_ad_links(self) -> AdList:
        """Extract ad links to click

        :rtype: AdList
        :returns: List of (ad, ad_link, ad_title) tuples
        """

        log.info("click", "Getting ad links...")

        ads = []

        scroll_count = 0

        log.debug("click", "Max scroll limit", fields={"limit": self._max_scroll_limit})

        while not self._is_scroll_at_the_end():
            try:
                top_ads_containers = self._driver.find_elements(*self.TOP_ADS_CONTAINER)
                for ad_container in top_ads_containers:
                    ads.extend(ad_container.find_elements(*self.AD_RESULTS))

            except NoSuchElementException:
                log.debug("click", "Could not found top ads!")

            try:
                bottom_ads_containers = self._driver.find_elements(*self.BOTTOM_ADS_CONTAINER)
                for ad_container in bottom_ads_containers:
                    ads.extend(ad_container.find_elements(*self.AD_RESULTS))

            except NoSuchElementException:
                log.debug("click", "Could not found bottom ads!")

            if self._max_scroll_limit > 0:
                if scroll_count == self._max_scroll_limit:
                    log.debug("click", "Reached to max scroll limit! Ending scroll...")
                    break

            self._driver.find_element(By.TAG_NAME, "body").send_keys(Keys.PAGE_DOWN)
            sleep(get_random_sleep(2, 2.5) * config.behavior.wait_factor)

            scroll_count += 1

        if not ads:
            return []

        # clean non-ad links and duplicates
        cleaned_ads = []
        links = []

        for ad in ads:
            if ad.get_attribute("data-pcu"):
                ad_link = ad.get_attribute("href")

                if ad_link not in links:
                    links.append(ad_link)
                    cleaned_ads.append(ad)

        self._stats.ads_found = len(cleaned_ads)

        # if there are filter words given, filter results accordingly
        filtered_ads = []

        if self._filter_words:
            for ad in cleaned_ads:
                ad_title = ad.find_element(*self.AD_TITLE).text.lower()
                ad_link = ad.get_attribute("data-pcu")

                log.debug("click", "data-pcu ad_link", fields={"ad_link": ad_link})

                for word in self._filter_words:
                    if word in ad_link or word in ad_title:
                        if ad not in filtered_ads:
                            log.debug("click", "Filtering", fields={"title": ad_title, "link": ad_link})
                            self._stats.num_filtered_ads += 1
                            filtered_ads.append(ad)
        else:
            filtered_ads = cleaned_ads

        ad_links = []

        for ad in filtered_ads:
            ad_link = ad.get_attribute("href")
            ad_title = ad.find_element(*self.AD_TITLE).text
            log.debug("click", "Ad title", fields={"title": ad_title, "link": ad_link})

            if self._exclude_list:
                for exclude_item in self._exclude_list:
                    if (
                        exclude_item in ad.get_attribute("data-pcu")
                        or exclude_item.lower() in ad_title.lower()
                    ):
                        log.debug("click", "Excluding", fields={"title": ad_title, "link": ad_link})
                        self._stats.num_excluded_ads += 1
                        break
                else:
                    log.info("click", "======= Found an Ad =======")
                    ad_links.append((ad, ad_link, ad_title))
            else:
                log.info("click", "======= Found an Ad =======")
                ad_links.append((ad, ad_link, ad_title))

        return ad_links

    def _get_non_ad_links(
        self, ad_links: AdList, non_ad_domains: Optional[list[str]] = None
    ) -> NonAdList:
        """Extract non-ad link elements

        :type ad_links: AdList
        :param ad_links: List of ad links found to exclude
        :type non_ad_domains: list
        :param non_ad_domains: List of domains to select for non-ad links
        :rtype: NonAdList
        :returns: List of non-ad link elements
        """

        log.info("click", "Getting non-ad links...")

        # go to top of the page
        self._driver.find_element(By.TAG_NAME, "body").send_keys(Keys.HOME)

        all_links = self._driver.find_elements(*self.ALL_LINKS)

        log.debug("click", "len(all_links)", fields={"count": len(all_links)})

        non_ad_links = []

        for link in all_links:
            for ad in ad_links:
                if link == ad[0]:
                    # skip ad element
                    break
            else:
                link_url = link.get_attribute("href")
                if (
                    link_url
                    and (
                        link.get_attribute("role")
                        not in (
                            "link",
                            "button",
                            "menuitem",
                            "menuitemradio",
                        )
                    )
                    and link.get_attribute("jsname")
                    and link.get_attribute("data-ved")
                    and not link.get_attribute("data-rw")
                    and "/maps" not in link_url
                    and "/search?q" not in link_url
                    and "googleadservices" not in link_url
                    and "https://www.google" not in link_url
                    and (link_url and link_url.startswith("http"))
                    and len(link.find_elements(By.TAG_NAME, "svg")) == 0
                ):
                    if non_ad_domains:
                        log.debug("click", "Evaluating to add as non-ad link", fields={"url": link_url})

                        for domain in non_ad_domains:
                            if domain in link_url:
                                log.debug("click", "Adding to non-ad links", fields={"url": link_url})
                                non_ad_links.append(link)
                                break
                    else:
                        log.debug("click", "Adding to non-ad links", fields={"url": link_url})
                        non_ad_links.append(link)

        log.info("click", "Found non-ad links", fields={"count": len(non_ad_links)})

        # if there is no domain to filter, randomly select 3 links
        if not non_ad_domains and len(non_ad_links) > 3:
            log.info("click", "Randomly selecting 3 from non-ad links...")
            non_ad_links = random.sample(non_ad_links, k=3)

        return non_ad_links

    def _close_cookie_dialog(self) -> None:
        """If cookie dialog is opened, close it by accepting"""

        log.debug("browser", "Waiting for cookie dialog...")

        sleep(get_random_sleep(3, 3.5) * config.behavior.wait_factor)

        all_links = [
            element.get_attribute("href")
            for element in self._driver.find_elements(By.TAG_NAME, "a")
            if isinstance(element.get_attribute("href"), str)
        ]

        for link in all_links:
            if "policies.google.com" in link:
                buttons = self._driver.find_elements(*self.COOKIE_DIALOG_BUTTON)[6:-2]
                if len(buttons) < 6:
                    buttons = self._driver.find_elements(*self.COOKIE_DIALOG_BUTTON)

                for button in buttons:
                    try:
                        if (
                            button.get_attribute("role") != "link"
                            and button.get_attribute("style") != "display:none"
                        ):
                            log.debug(
                                "browser",
                                "Clicking button",
                                fields={"html": button.get_attribute("outerHTML")},
                            )
                            self._driver.execute_script(
                                "arguments[0].scrollIntoView(true);", button
                            )
                            sleep(get_random_sleep(0.5, 1) * config.behavior.wait_factor)
                            button.click()
                            sleep(get_random_sleep(1, 1.5) * config.behavior.wait_factor)

                            try:
                                search_input_box = self._driver.find_element(*self.SEARCH_INPUT)
                                if not search_input_box.get_attribute("value"):
                                    self._type_humanlike(search_input_box, self._search_query)
                                    break
                            except (
                                ElementNotInteractableException,
                                StaleElementReferenceException,
                            ):
                                pass

                    except (
                        ElementNotInteractableException,
                        ElementClickInterceptedException,
                        StaleElementReferenceException,
                    ):
                        pass

                sleep(get_random_sleep(1, 1.5) * config.behavior.wait_factor)
                break
        else:
            log.debug("browser", "No cookie dialog found! Continue with search...")

    def _is_scroll_at_the_end(self) -> bool:
        """Check if scroll is at the end

        :rtype: bool
        :returns: Whether the scrollbar was reached to end or not
        """

        page_height = self._driver.execute_script("return document.body.scrollHeight;")
        total_scrolled_height = self._driver.execute_script(
            "return window.pageYOffset + window.innerHeight;"
        )

        return page_height - 1 <= total_scrolled_height

    def _delete_cache_and_cookies(self) -> None:
        """Delete browser cache, storage, and cookies"""

        log.debug("cleanup", "Deleting browser cache and cookies...")

        try:
            self._driver.delete_all_cookies()

            self._driver.execute_cdp_cmd("Network.clearBrowserCache", {})
            self._driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
            self._driver.execute_script("window.localStorage.clear();")
            self._driver.execute_script("window.sessionStorage.clear();")

        except Exception as exp:
            if "not connected to DevTools" in str(exp):
                log.debug("cleanup", "Incognito mode is active. No need to delete cache. Skipping...")

    def _set_start_url(self, country_code: str) -> None:
        """Set start url according to country code of the proxy IP

        :type country_code: str
        :param country_code: Country code for the proxy IP
        """

        with open("domain_mapping.json", "r") as domains_file:
            domains = json.load(domains_file)

        country_domain = domains.get(country_code, "www.google.com")
        self.URL = f"https://{country_domain}"

        log.debug("browser", "Start url was set", fields={"url": self.URL})

    def _make_random_scrolls(self) -> None:
        """Make random scrolls on page"""

        log.debug("browser", "Making random scrolls...")

        directions = [Direction.DOWN]
        directions += random.choices(
            [Direction.UP] * 5 + [Direction.DOWN] * 5, k=random.choice(range(1, 5))
        )

        log.debug("browser", "Direction choices", fields={"directions": [d.value for d in directions]})

        for direction in directions:
            if direction == Direction.DOWN and not self._is_scroll_at_the_end():
                self._driver.find_element(By.TAG_NAME, "body").send_keys(Keys.PAGE_DOWN)
            elif direction == Direction.UP:
                self._driver.find_element(By.TAG_NAME, "body").send_keys(Keys.PAGE_UP)

            sleep(get_random_sleep(1, 3) * config.behavior.wait_factor)

        self._driver.find_element(By.TAG_NAME, "body").send_keys(Keys.HOME)

    def _make_random_swipes(self) -> None:
        """Make random swipes on page"""

        log.debug("browser", "Making random swipes...")

        directions = [Direction.DOWN, Direction.DOWN]
        directions += random.choices(
            [Direction.UP] * 5 + [Direction.DOWN] * 5, k=random.choice(range(1, 5))
        )

        log.debug("browser", "Direction choices", fields={"directions": [d.value for d in directions]})

        for direction in directions:
            if direction == Direction.DOWN:
                self._send_swipe(direction=Direction.DOWN)

            elif direction == Direction.UP:
                self._send_swipe(direction=Direction.UP)

            sleep(get_random_sleep(1, 2) * config.behavior.wait_factor)

        HOME_KEYCODE = 122
        adb_controller.send_keyevent(HOME_KEYCODE)  # go to top by sending Home key

    def _send_swipe(self, direction: Direction) -> None:
        """Send swipe action to mobile device

        :type direction: Direction
        :param direction: Direction to swipe
        """

        x_position = random.choice(range(100, 200))
        duration = random.choice(range(100, 500))

        if direction == Direction.DOWN:
            y_start_position = random.choice(range(1000, 1500))
            y_end_position = random.choice(range(500, 1000))

        elif direction == Direction.UP:
            y_start_position = random.choice(range(500, 1000))
            y_end_position = random.choice(range(1000, 1500))

        adb_controller.send_swipe(
            x1=x_position,
            y1=y_start_position,
            x2=x_position,
            y2=y_end_position,
            duration=duration,
        )

    def _make_random_mouse_movements(self) -> None:
        """Make random mouse movements"""

        if self._random_mouse_enabled:
            try:
                import pyautogui

                log.debug("browser", "Making random mouse movements...")

                screen_width, screen_height = pyautogui.size()
                pyautogui.moveTo(screen_width / 2 - 300, screen_height / 2 - 200)

                log.debug("browser", "Mouse position", fields={"position": pyautogui.position()})

                ease_methods = [
                    pyautogui.easeInQuad,
                    pyautogui.easeOutQuad,
                    pyautogui.easeInOutQuad,
                ]

                log.debug("browser", "Going LEFT and DOWN...")

                pyautogui.move(
                    -random.choice(range(200, 300)),
                    random.choice(range(250, 450)),
                    1,
                    random.choice(ease_methods),
                )

                log.debug("browser", "Mouse position", fields={"position": pyautogui.position()})

                for _ in range(1, random.choice(range(3, 7))):
                    direction = random.choice(list(Direction))
                    ease_method = random.choice(ease_methods)

                    log.debug("browser", "Going", fields={"direction": direction.value})

                    if direction == Direction.LEFT:
                        pyautogui.move(-(random.choice(range(100, 200))), 0, 0.5, ease_method)

                    elif direction == Direction.RIGHT:
                        pyautogui.move(random.choice(range(200, 400)), 0, 0.3, ease_method)

                    elif direction == Direction.UP:
                        pyautogui.move(0, -(random.choice(range(100, 200))), 1, ease_method)
                        pyautogui.scroll(random.choice(range(1, 7)))

                    elif direction == Direction.DOWN:
                        pyautogui.move(0, random.choice(range(150, 300)), 0.7, ease_method)
                        pyautogui.scroll(-random.choice(range(1, 7)))

                    else:
                        pyautogui.move(
                            random.choice(range(100, 200)),
                            random.choice(range(150, 250)),
                            1,
                            ease_method,
                        )

                    log.debug("browser", "Mouse position", fields={"position": pyautogui.position()})

            except pyautogui.FailSafeException:
                log.debug("browser", "The mouse cursor was moved to one of the screen corners!")

                pyautogui.FAILSAFE = False

                log.debug("browser", "Moving cursor to center...")
                pyautogui.moveTo(screen_width / 2, screen_height / 2)

    def _check_captcha(self) -> None:
        """Проверить страницу на CAPTCHA и применить ``behavior.captcha_policy``.

        Единственная точка детекта: все вызовы из ``search_for_ads`` (до
        поиска, в ветке ``ElementNotInteractable``, после набора запроса)
        зовут этот метод, а он обязан пройти через единственную точку события
        :meth:`_record_captcha_event` — строка в ``captcha_events``, счётчик
        ``runs``, лог с категорией ``captcha`` и telegram. Обход цепочки
        ловится структурным тестом.

        Политика (план §5, фаза 8):

        * **stop** — дефолт. Событие + скриншот + telegram и сценарий
          прерывается ``SystemExit``. Семантика ровно как у legacy-ветки
          «нет ключа»: ``ad_clicker.run_scenario`` в ``finally`` доводит
          teardown (закрывает браузер), ``engine.worker`` ловит
          ``SystemExit``, помечает прогон упавшим и ждёт следующего раунда
          по расписанию. «Ждать оператора» = остановленный сценарий и живой
          воркер, который ничего не делает, пока оператор не разберётся
          (сменит прокси, включит 2captcha, остановит пул). Отдельного
          «спящего» цикла здесь намеренно нет: он не получил бы SIGTERM
          от супервизора и завис бы вместе с процессом.
        * **solve** — авто-решение через 2captcha: таймаут, ретраи и
          сессионный лимит — константы ``CAPTCHA_SOLVE_*`` сверху файла; в
          событие уходят ``solved``/``solver``/``elapsed_ms``. Неудача,
          таймаут, отсутствие ключа или исчерпание лимита уводят в
          stop-ветку с ``solved=false``.
        * **both** — решает; любая неудача или исчерпание лимита — stop-ветка.

        **Изменение семантики (осознанное, по плану):** legacy при наличии
        ключа решал капчу сам и продолжал прогон, без ключа — останавливал.
        Новый дефолт ``captcha_policy=stop`` останавливает прогон и при
        наличии ключа: авто-решение включается только явным
        ``solve``/``both``.
        """
        sleep(get_random_sleep(2, 2.5) * config.behavior.wait_factor)

        try:
            captcha = self._driver.find_element(*self.RECAPTCHA)
        except NoSuchElementException:
            log.debug("captcha", "No captcha seen. Continue to search...")
            return

        if not captcha:
            log.debug("captcha", "No captcha seen. Continue to search...")
            return

        # --- обнаружено: дальше любая судьба идёт через одну точку события ---
        log.error("captcha", "Captcha was shown.")

        if self._hooks_enabled:
            hooks.captcha_seen_hook(self._driver)

        self._stats.captcha_seen = True

        policy = self._captcha_policy()
        browser_id = self._event_browser_id()
        page_url = self._current_url()
        sitekey = self._element_attribute(captcha, "data-sitekey")
        screenshot_path = self._captcha_screenshot(browser_id)

        # Словарь события: stop-ветки ниже отличаются только причиной,
        # solved/solver/elapsed_ms меняются при уходе в solve.
        event: dict[str, Any] = {
            "page_url": page_url,
            "sitekey": sitekey,
            "screenshot_path": screenshot_path,
            "solved": False,
            "solver": None,
            "elapsed_ms": None,
            "policy": policy,
        }

        if policy == "stop":
            self._record_captcha_event(**event)
            self._stop_for_captcha(f"captcha_policy={policy}")
            return

        # --- solve | both -----------------------------------------------------
        if not self._twocaptcha_apikey:
            log.error(
                "captcha",
                "2captcha API key is not configured (behavior.2captcha_apikey): cannot auto-solve",
                fields={"captcha_policy": policy},
            )
            self._record_captcha_event(**event)
            self._stop_for_captcha("no 2captcha api key")
            return

        if SearchController._solve_attempts_used >= CAPTCHA_SOLVE_SESSION_LIMIT:
            log.error(
                "captcha",
                "2captcha attempt limit reached for this worker session: cannot auto-solve",
                fields={
                    "used": SearchController._solve_attempts_used,
                    "limit": CAPTCHA_SOLVE_SESSION_LIMIT,
                },
            )
            self._record_captcha_event(**event)
            self._stop_for_captcha("2captcha session limit reached")
            return

        if page_url is None:
            log.error("captcha", "Current page URL is unavailable: cannot auto-solve CAPTCHA")
            self._record_captcha_event(**event)
            self._stop_for_captcha("page url unavailable")
            return

        data_s = self._element_attribute(captcha, "data-s")
        cookies = self._page_cookies()
        started_at = time.monotonic()
        response_code: Optional[str] = None
        last_error: Optional[str] = None
        attempt = 0

        while attempt < CAPTCHA_SOLVE_RETRIES and response_code is None:
            if SearchController._solve_attempts_used >= CAPTCHA_SOLVE_SESSION_LIMIT:
                # Лимит исчерпан предыдущими попытками этого же процесса.
                last_error = "2captcha session limit reached"
                break
            attempt += 1
            SearchController._solve_attempts_used += 1
            log.info(
                "captcha",
                "Trying to solve captcha...",
                fields={
                    "attempt": attempt,
                    "retries": CAPTCHA_SOLVE_RETRIES,
                    "session_used": SearchController._solve_attempts_used,
                },
            )
            response_code, last_error = self._solve_via_service(
                current_url=page_url,
                sitekey=sitekey,
                data_s=data_s,
                cookies=cookies,
            )
            if response_code is None:
                log.warning(
                    "captcha",
                    "CAPTCHA solve attempt failed",
                    fields={"attempt": attempt, "error": last_error},
                )

        elapsed_ms = int((time.monotonic() - started_at) * 1000)

        if response_code:
            log.info("captcha", "Captcha was solved.", fields={"elapsed_ms": elapsed_ms})
            self._stats.captcha_solved = True
            event.update(solved=True, solver="2captcha", elapsed_ms=elapsed_ms)
            self._record_captcha_event(**event)
            captcha_redirect_url = f"{page_url}&g-recaptcha-response={response_code}"
            self._driver.get(captcha_redirect_url)
            sleep(get_random_sleep(2, 2.5) * config.behavior.wait_factor)
            return

        # Неудача: сначала событие (solver="2captcha" — попытки реально были,
        # если дошли до ветки solve), затем stop-ветка.
        event.update(solver="2captcha" if attempt else None, elapsed_ms=elapsed_ms)
        self._record_captcha_event(**event)
        self._stop_for_captcha(last_error or "2captcha did not solve the captcha")

    def _captcha_policy(self) -> str:
        """Политика из конфига; неизвестное значение → дефолт ``stop`` + WARNING.

        ``config_reader`` не валидирует enum (в control plane это делает
        ``_ENUM_FIELDS``), поэтому старый или рукописный ``config.json`` с
        опечаткой не должен ни ронять сценарий, ни молча решать капчу:
        безопасный исход — остановка и явный WARNING, а не догадка.
        """
        policy = config.behavior.captcha_policy
        if policy not in CAPTCHA_POLICIES:
            log.warning(
                "captcha",
                "Unknown captcha_policy, falling back to stop",
                fields={"captcha_policy": policy, "resolved": DEFAULT_CAPTCHA_POLICY},
            )
            return DEFAULT_CAPTCHA_POLICY
        return policy

    def _event_browser_id(self) -> Optional[str]:
        """``browser_id`` события: из stats (``set_browser_id``), иначе биндинг логгера."""
        if self._stats.browser_id:
            return str(self._stats.browser_id)
        return log.browser_id

    def _current_url(self) -> Optional[str]:
        """URL страницы; мёртвый драйвер не роняет событие (None)."""
        try:
            return self._driver.current_url
        except Exception as exp:  # noqa: BLE001 - страница могла умереть вместе с драйвером
            log.warning(
                "captcha",
                "Page URL is unavailable for the CAPTCHA event",
                fields={"error": str(exp), "error_type": type(exp).__name__},
            )
            return None

    @staticmethod
    def _element_attribute(element: Any, name: str) -> Optional[str]:
        """Атрибут элемента без исключений: устаревший элемент не роняет событие."""
        try:
            return element.get_attribute(name)
        except Exception as exp:  # noqa: BLE001 - StaleElement и прочее на живой странице
            log.warning(
                "captcha",
                "CAPTCHA element attribute was not read",
                fields={"attribute": name, "error": str(exp), "error_type": type(exp).__name__},
            )
            return None

    def _page_cookies(self) -> Optional[str]:
        """Cookies страницы для запроса к 2captcha, в legacy-формате ``name:value;...``.

        Значения не логируются: это сессионные креды, и в ``logs`` они не
        должны попадать (dump cookies из legacy-ветки намеренно не
        переносится). Отказ чтения — None: сервис решает и без cookies.
        """
        try:
            return ";".join(
                f"{cookie['name']}:{cookie['value']}" for cookie in self._driver.get_cookies()
            )
        except Exception as exp:  # noqa: BLE001 - cookies нужны решению, не сценарию
            log.warning(
                "captcha",
                "Cookies were not read for the 2captcha request",
                fields={"error": str(exp), "error_type": type(exp).__name__},
            )
            return None

    def _captcha_screenshot(self, browser_id: Optional[str]) -> Optional[str]:
        """Снять страницу с CAPTCHA: ``engine/screenshots/<browser_id>_<epoch>.png``.

        Отдельный явный вызов драйвера (а не побочный эффект события), чтобы
        отказ снимка был виден отдельно и чтобы событие могло уйти с
        ``screenshot_path=NULL``. В имени файла — только id воркера и время:
        ни креды прокси, ни URL страницы туда не попадают (иначе утекут в
        списки файлов и отчёты).

        В БД уходит тот же абсолютный путь, что ушёл драйверу: каталог
        относится к текущему каталогу (как весь legacy-вывод), а читать его
        будет UI из другого процесса. Отказ (мёртвый драйвер, нет прав) →
        None + WARNING: скриншот не роняет ни событие, ни сценарий.
        """
        name = f"{browser_id or 'unknown'}_{int(time.time())}.png"
        path = Path.cwd() / CAPTCHA_SCREENSHOT_DIR / name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            saved = self._driver.get_screenshot_as_file(str(path))
        except Exception as exp:  # noqa: BLE001 - снимок не важнее события
            log.warning(
                "captcha",
                "CAPTCHA screenshot failed",
                fields={"path": str(path), "error": str(exp), "error_type": type(exp).__name__},
            )
            return None
        if not saved:
            log.warning(
                "captcha",
                "CAPTCHA screenshot failed",
                fields={"path": str(path), "error": "driver refused to save the screenshot"},
            )
            return None
        log.info("captcha", "CAPTCHA screenshot saved", fields={"path": str(path)})
        return str(path)

    def _captcha_context(
        self, browser_id: Optional[str]
    ) -> tuple[Optional[int], Optional[int], Optional[StateStore]]:
        """``(proxy_id, run_id, store)`` события — как у снимка сессии.

        Прокси берётся из строки воркера тем же
        ``engine.diagnostics.proxy_context``, что и диагностика; run —
        активная строка ``runs`` для этого ``browser_id``: счётчики пишет сам
        воркер, а его ``run_id`` процессу неизвестен. Ошибка чтения не роняет
        событие — контекст становится пустым, строка пишется в любом случае.
        """
        if not browser_id:
            return None, None, None
        try:
            store = StateStore(resolve_db_path())
            proxy_id, _country = proxy_context(store, browser_id)
            return proxy_id, store.active_run_id(browser_id), store
        except Exception as exp:  # noqa: BLE001 - контекст не важнее события
            log.warning(
                "captcha",
                "CAPTCHA event context is unavailable",
                fields={"error": str(exp), "error_type": type(exp).__name__},
            )
            return None, None, None

    def _record_captcha_event(
        self,
        *,
        page_url: Optional[str],
        sitekey: Optional[str],
        screenshot_path: Optional[str],
        solved: bool,
        solver: Optional[str],
        elapsed_ms: Optional[int],
        policy: str,
    ) -> None:
        """Единая точка события CAPTCHA: строка, счётчики run, лог и telegram.

        Все ветки детекта зовут только эту функцию. Порядок: строка в
        ``captcha_events`` (немедленно — оператор ждёт её вместе с
        уведомлением), счётчики активного запуска (``captcha_seen`` всегда,
        ``captcha_solved`` — если решилось), лог с категорией ``captcha``,
        затем telegram по флагу.

        Политика ошибок не бросает: запись события защищена политикой
        ``StoreWriter`` (``dropped``/``last_error``), контекст и счётчик —
        собственными WARNING'ами. Событие — это реакция на проблему, оно не
        должно превращаться в неё.
        """
        browser_id = self._event_browser_id()
        ts = time.time()
        proxy_id, run_id, store = self._captcha_context(browser_id)

        log.record_captcha_event(
            browser_id=browser_id,
            ts=ts,
            proxy_id=proxy_id,
            page_url=page_url,
            sitekey=sitekey,
            screenshot_path=screenshot_path,
            solved=solved,
            solver=solver,
            elapsed_ms=elapsed_ms,
        )

        if store is not None and run_id is not None:
            try:
                store.add_run_counts(run_id, captcha_seen=1, captcha_solved=int(solved))
            except Exception as exp:  # noqa: BLE001 - счётчик не важнее события
                log.warning(
                    "captcha",
                    "CAPTCHA run counter was not updated",
                    fields={
                        "run_id": run_id,
                        "error": str(exp),
                        "error_type": type(exp).__name__,
                    },
                )

        log.info(
            "captcha",
            "CAPTCHA event recorded",
            fields={
                "policy": policy,
                "solved": bool(solved),
                "solver": solver,
                "elapsed_ms": elapsed_ms,
                "screenshot_path": screenshot_path,
                "sitekey": sitekey,
                "page_url": page_url,
                "proxy_id": proxy_id,
                "run_id": run_id,
            },
        )

        self._notify_captcha_telegram(
            browser_id=browser_id,
            page_url=page_url,
            screenshot_path=screenshot_path,
            solved=bool(solved),
            policy=policy,
        )

    def _notify_captcha_telegram(
        self,
        *,
        browser_id: Optional[str],
        page_url: Optional[str],
        screenshot_path: Optional[str],
        solved: bool,
        policy: str,
    ) -> None:
        """Уведомление в Telegram на самое событие CAPTCHA. Никогда не бросает.

        Раньше telegram срабатывал только по результатам прогона
        (``notify_matching_ads``), а CAPTCHA — событие посреди сценария:
        уведомление уходит сразу после записи строки. Импорт ленивый и
        wrapped в try: пакет telegram не входит в nix-окружение, и его
        отсутствие или ошибка отправки не должны менять исход — событие уже
        записано, сценарий уже принял решение.
        """
        if not config.behavior.telegram_enabled:
            return
        try:
            from telegram_notifier import notify_captcha_event

            notify_captcha_event(
                browser_id=browser_id,
                page_url=page_url,
                screenshot_path=screenshot_path,
                solved=solved,
                policy=policy,
            )
        except Exception as exp:  # noqa: BLE001 - уведомление не решает сценарий
            log.warning(
                "captcha",
                "CAPTCHA telegram notification failed",
                fields={"error": str(exp), "error_type": type(exp).__name__},
            )

    def _solve_via_service(
        self,
        *,
        current_url: str,
        sitekey: Optional[str],
        data_s: Optional[str],
        cookies: Optional[str],
    ) -> tuple[Optional[str], Optional[str]]:
        """Один вызов 2captcha под таймаутом ``CAPTCHA_SOLVE_TIMEOUT_S``.

        Возвращает ``(response_code | None, причина | None)``.

        ``solve_recaptcha`` — синхронный сетевой код со своими паузами и
        ретраями (см. константы в ``utils``), поэтому вызов идёт в
        daemon-потоке с ``join(timeout)``: по таймауту сценарий уходит в
        stop-ветку, а заброшенный поток никого не держит и не блокирует
        выход процесса.

        ``SystemExit`` из ``solve_recaptcha`` (2captcha ответил фатально: не
        тот ключ, нет средств) перехватывается здесь же: это неудача
        решения, а не приказ завершать процесс — иначе событие не успело бы
        записаться.
        """
        result: dict[str, Any] = {}

        def _solve() -> None:
            try:
                result["code"] = solve_recaptcha(
                    apikey=self._twocaptcha_apikey,
                    sitekey=sitekey,
                    current_url=current_url,
                    data_s=data_s,
                    cookies=cookies,
                )
            except SystemExit as exp:
                result["error"] = str(exp) or f"2captcha solver exited (code {exp.code})"
            except Exception as exp:  # noqa: BLE001 - ошибка сервиса = неудача решения
                result["error"] = f"{type(exp).__name__}: {exp}"

        worker = Thread(target=_solve, name="captcha-solve", daemon=True)
        worker.start()
        worker.join(CAPTCHA_SOLVE_TIMEOUT_S)
        if worker.is_alive():
            log.warning(
                "captcha",
                "CAPTCHA solve timed out",
                fields={"timeout_s": CAPTCHA_SOLVE_TIMEOUT_S},
            )
            return None, f"timeout after {CAPTCHA_SOLVE_TIMEOUT_S}s"
        return result.get("code"), result.get("error")

    def _stop_for_captcha(self, reason: str) -> None:
        """Stop-ветка: прервать сценарий и ждать оператора.

        Семантика — legacy-ветка «нет ключа»: ``SystemExit`` выходит из
        ``search_for_ads`` в ``run_scenario``, whose ``finally`` закрывает
        браузер, а ``engine.worker`` ловит ``SystemExit``, помечает прогон
        упавшим и ждёт следующего раунда по расписанию. Скриншот, событие и
        уведомление к этому моменту уже сделаны — оператор получает полную
        картину и решает, что дальше.
        """
        log.info(
            "captcha",
            "Please try with a different proxy or enable 2captcha service.",
            fields={"reason": reason},
        )
        log.info("click", str(self.stats))
        raise SystemExit()

    def _close_choose_location_popup(self) -> None:
        """Close 'Choose location for search results' popup"""

        try:
            estimated_loc_img = self._driver.find_element(*self.ESTIMATED_LOC_IMG)
            log.debug(
            "browser",
            "Location dialog element",
            fields={"html": estimated_loc_img.get_attribute("outerHTML")},
        )

            log.debug("browser", "Closing location choose dialog...")
            estimated_loc_img.click()

            sleep(get_random_sleep(1, 1.5) * config.behavior.wait_factor)

            continue_button = self._driver.find_element(*self.LOC_CONTINUE_BUTTON)
            log.debug(
            "browser",
            "Location dialog continue button",
            fields={"html": continue_button.get_attribute("outerHTML")},
        )

            continue_button.click()

            sleep(get_random_sleep(0.1, 0.5) * config.behavior.wait_factor)

        except NoSuchElementException:

            sleep(get_random_sleep(1, 1.5) * config.behavior.wait_factor)

            try:
                log.debug("browser", "Checking alternative location dialog...")
                log.debug("browser", "Closing location choose dialog by selecting Not now...")

                not_now_button = self._driver.find_element(*self.NOT_NOW_BUTTON)
                log.debug(
                "browser",
                "Location dialog not-now button",
                fields={"html": not_now_button.get_attribute("outerHTML")},
            )

                not_now_button.click()

                sleep(get_random_sleep(0.2, 0.5) * config.behavior.wait_factor)

            except NoSuchElementException:
                log.debug("browser", "No location choose dialog seen. Continue to search...")

            except ElementNotInteractableException:
                log.debug("browser", "Location dialog button element is not interactable!")

        finally:
            # if no not now or continue button exists, send ESC to page to close the dialog
            self._driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)

    def _type_humanlike(
        self, element: selenium.webdriver.remote.webelement.WebElement, text: str
    ) -> None:
        """Type text slowly like a human

        :type element: selenium.webdriver.remote.webelement.WebElement
        :param element: Element to type into
        :type text: str
        :param text: Text to type
        """

        try:
            element.clear()

            for character in text:
                element.send_keys(character)
                sleep(get_random_sleep(0.05, 0.15) * config.behavior.wait_factor)

            element.send_keys(Keys.ENTER)

        except Exception as exp:
            log.debug("click", "Error while typing", fields={"error": str(exp)})

    def set_browser_id(self, browser_id: Optional[int] = None) -> None:
        """Set browser id in stats if multiple browsers are used

        :type browser_id: int
        :param browser_id: Browser id to separate instances in log for multiprocess runs
        """

        self._stats.browser_id = browser_id

    def assign_android_device(self, device_id: str) -> None:
        """Assign Android device to browser

        :type device_id: str
        :param device_id: Android device ID to assign
        """

        log.info(
        "browser",
        "Assigning device to browser",
        fields={"device_id": device_id, "browser_id": self._stats.browser_id},
    )

        self._android_device_id = device_id

    @staticmethod
    def _process_query(query: str) -> tuple[str, list[str]]:
        """Extract search query and filter words from the query input

        Query and filter words are splitted with "@" character. Multiple
        filter words can be used by separating with "#" character.

        e.g. wireless keyboard@amazon#ebay
             bluetooth headphones @ sony # amazon  #bose

        :type query: str
        :param query: Query string with optional filter words
        :rtype tuple
        :returns: Search query and list of filter words if any
        """

        search_query = query.split("@")[0].strip()

        filter_words = []

        if "@" in query:
            filter_words = [word.strip().lower() for word in query.split("@")[1].split("#")]

            # Empty words (dangling "@" or "#") would filter out every ad,
            # because an empty string is contained in any text
            filter_words = [word for word in filter_words if word]

        if filter_words:
            log.debug("click", "Filter words", fields={"filter_words": filter_words})

        return (search_query, filter_words)

    @property
    def stats(self) -> SearchStats:
        """Return search statistics data

        :rtype: SearchStats
        :returns: Search statistics data
        """

        return self._stats
