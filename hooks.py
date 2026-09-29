from pathlib import Path

try:
    import undetected_chromedriver
except ImportError:
    import sys

    packages_path = Path.cwd() / "env" / "Lib" / "site-packages"
    sys.path.insert(0, f"{packages_path}")

    import undetected_chromedriver

from engine.log import get_logger


log = get_logger()


def before_search_hook(driver: undetected_chromedriver.Chrome) -> None:
    """Hook to be called before starting the search

    At this point, only webdriver is created and proxy is installed if it is used.
    No url is loaded yet. This can be used for loading other websites to create
    some history before starting the search or other custom setup on driver.

    :type driver: undetected_chromedriver.Chrome
    :param driver: Selenium Chrome webdriver instance
    """

    try:
        log.info("scheduler", "Executing before search hook...")

    except Exception as exp:
        log.error(
            "scheduler",
            "Hook failed",
            fields={"hook": "before_search", "error": str(exp)},
        )


def captcha_seen_hook(driver: undetected_chromedriver.Chrome) -> None:
    """Hook to be called in case of a captcha

    This is called at a point before after_query_sent_hook.

    :type driver: undetected_chromedriver.Chrome
    :param driver: Selenium Chrome webdriver instance
    """

    try:
        log.info("captcha", "Executing captcha seen hook...")

    except Exception as exp:
        log.error(
            "captcha",
            "Hook failed",
            fields={"hook": "captcha_seen", "error": str(exp)},
        )


def after_query_sent_hook(driver: undetected_chromedriver.Chrome, search_query: str) -> None:
    """Hook to be called after submitting the search query

    At this point, cookie dialogs are closed, captchas are solved, and search
    query is written to search box and submitted.

    :type driver: undetected_chromedriver.Chrome
    :param driver: Selenium Chrome webdriver instance
    :type search_query: str
    :param search_query: Search query
    """

    try:
        log.info("scheduler", "Executing after query sent hook...")

    except Exception as exp:
        log.error(
            "scheduler",
            "Hook failed",
            fields={"hook": "after_query_sent", "error": str(exp)},
        )


def results_ready_hook(driver: undetected_chromedriver.Chrome) -> None:
    """Hook to be called when search results are ready

    At this point, search query is executed and results are loaded. It is also
    before random actions on search results page and starting to link collection.

    :type driver: undetected_chromedriver.Chrome
    :param driver: Selenium Chrome webdriver instance
    """

    try:
        log.info("scheduler", "Executing search results ready hook...")

    except Exception as exp:
        log.error(
            "scheduler",
            "Hook failed",
            fields={"hook": "results_ready", "error": str(exp)},
        )


def after_search_hook(driver: undetected_chromedriver.Chrome) -> None:
    """Hook to be called after completing the search

    At this point, searching/collecting ad links, non-ad links, and
    shopping ad links if enabled is completed. Also, this is the point
    before starting click actions.

    :type driver: undetected_chromedriver.Chrome
    :param driver: Selenium Chrome webdriver instance
    """

    try:
        log.info("scheduler", "Executing after search hook...")

    except Exception as exp:
        log.error(
            "scheduler",
            "Hook failed",
            fields={"hook": "after_search", "error": str(exp)},
        )


def before_ad_click_hook(driver: undetected_chromedriver.Chrome) -> None:
    """Hook to be called before clicking to ad link

    At this point, ad links are found and ready to be clicked. It is also after
    clicking shopping ads if it is enabled and there are any found.

    :type driver: undetected_chromedriver.Chrome
    :param driver: Selenium Chrome webdriver instance
    """

    try:
        log.info("click", "Executing before ad click hook...")

    except Exception as exp:
        log.error(
            "click",
            "Hook failed",
            fields={"hook": "before_ad_click", "error": str(exp)},
        )


def after_ad_click_hook(driver: undetected_chromedriver.Chrome) -> None:
    """Hook to be called after clicking to ad link

    At this point, the ad link is opened in a new tab and browser is switched
    to this tab. It is before waiting and starting random actions on ad page.

    :type driver: undetected_chromedriver.Chrome
    :param driver: Selenium Chrome webdriver instance
    """

    try:
        log.info("click", "Executing after ad click hook...")

    except Exception as exp:
        log.error(
            "click",
            "Hook failed",
            fields={"hook": "after_ad_click", "error": str(exp)},
        )


def after_clicks_hook(driver: undetected_chromedriver.Chrome) -> None:
    """Hook to be called after clicking the found links

    At this point, clicking of the links is completed.

    :type driver: undetected_chromedriver.Chrome
    :param driver: Selenium Chrome webdriver instance
    """

    try:
        log.info("click", "Executing after clicks hook...")

    except Exception as exp:
        log.error(
            "click",
            "Hook failed",
            fields={"hook": "after_clicks", "error": str(exp)},
        )


def exception_hook(driver: undetected_chromedriver.Chrome) -> None:
    """Hook to be called in case of an exception

    At this point, a screenshot is saved and exception is logged to file.
    It can be used to investigate the exception before the browser is closed.

    :type driver: undetected_chromedriver.Chrome
    :param driver: Selenium Chrome webdriver instance
    """

    try:
        log.info("scheduler", "Executing exception hook...")

    except Exception as exp:
        log.error(
            "scheduler",
            "Hook failed",
            fields={"hook": "exception", "error": str(exp)},
        )


def before_browser_close_hook(driver: undetected_chromedriver.Chrome) -> None:
    """Hook to be called before closing the browser instance

    At this point, all actions are completed including the exception handling.
    Cache and cookies are deleted and browser is closed after this hook is called.

    :type driver: undetected_chromedriver.Chrome
    :param driver: Selenium Chrome webdriver instance
    """

    try:
        log.info("browser", "Executing before browser close hook...")

    except Exception as exp:
        log.error(
            "browser",
            "Hook failed",
            fields={"hook": "before_browser_close", "error": str(exp)},
        )


def after_browser_close_hook(driver: undetected_chromedriver.Chrome) -> None:
    """Hook to be called after closing the browser instance

    At this point, all actions are completed including the exception handling.

    :type driver: undetected_chromedriver.Chrome
    :param driver: Selenium Chrome webdriver instance
    """

    try:
        log.info("browser", "Executing after browser close hook...")

    except Exception as exp:
        log.error(
            "browser",
            "Hook failed",
            fields={"hook": "after_browser_close", "error": str(exp)},
        )
