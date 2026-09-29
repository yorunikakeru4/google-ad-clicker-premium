import random
import shutil
import string
import traceback
from argparse import ArgumentParser
from datetime import datetime
from itertools import chain, filterfalse, zip_longest
from pathlib import Path

import hooks
from clicklogs_db import ClickLogsDB
from config_reader import config
from engine.control_plane.state import StateStore
from engine.diagnostics import session_checkpoint
from engine.log import get_logger, resolve_db_path
from engine.profile_apply import current_profile, resolve_user_agent
from engine.worker import proxy_from_environ
from logger import update_log_formats
from proxy import get_proxies
from search_controller import SearchController
from utils import (
    get_random_user_agent_string,
    get_domains,
    take_screenshot,
    generate_click_report,
)
from webdriver import create_webdriver


if config.behavior.telegram_enabled:
    from telegram_notifier import notify_matching_ads, start_bot


log = get_logger()


__author__ = "Coşkun Deniz <coskun.denize@gmail.com>"


def get_arg_parser() -> ArgumentParser:
    """Get argument parser

    :rtype: ArgumentParser
    :returns: ArgumentParser object
    """

    arg_parser = ArgumentParser(add_help=False, usage="See README.md file")
    arg_parser.add_argument("-q", "--query", help="Search query")
    arg_parser.add_argument(
        "-p",
        "--proxy",
        help="""Use the given proxy in "ip:port" or "username:password@host:port" format""",
    )
    arg_parser.add_argument("--id", help="Browser id for multiprocess run")
    arg_parser.add_argument(
        "--enable_telegram", action="store_true", help="Enable telegram notifications"
    )
    arg_parser.add_argument(
        "--report_clicks", action="store_true", help="Get click report for the given date"
    )
    arg_parser.add_argument("--date", help="Give a specific report date in DD-MM-YYYY format")
    arg_parser.add_argument("--excel", action="store_true", help="Write results to an Excel file")
    arg_parser.add_argument(
        "--check_stealth", action="store_true", help="Check stealth for undetection"
    )
    arg_parser.add_argument("-d", "--device_id", help="Android device ID for assigning to browser")

    return arg_parser


def resolve_query(args) -> str:
    """Один запрос для прогона: из аргумента, иначе из конфига.

    Выделено из main(), потому что воркер передаёт запрос сам — он делит
    очередь по браузерам, а CLI по-прежнему берёт его из config.json.
    """
    if args.query:
        return args.query

    if not config.behavior.query:
        log.error("scheduler", "Fill the query parameter!")
        raise SystemExit()

    return config.behavior.query


def resolve_proxy(args) -> str | None:
    """Один прокси для прогона: env супервизора, аргумент, файл, конфиг, ничего.

    ``ADCLICKER_PROXY`` главнее всех legacy-источников: супервизор закрепляет
    прокси за воркером и передаёт его через окружение. Пустая переменная —
    прежнее поведение, вплоть до ``random.choice`` по файлу.
    """
    assigned = proxy_from_environ()
    if assigned is not None:
        return assigned
    if args.proxy:
        return args.proxy
    if config.paths.proxy_file:
        proxies = get_proxies()
        log.debug("proxy", "Proxies", fields={"proxies": proxies})
        return random.choice(proxies)
    if config.webdriver.proxy:
        return config.webdriver.proxy
    return None


def diagnostics_checkpoint(driver, browser_id: str | None) -> None:
    """Снимок диагностики при живом браузере. Никогда не бросает.

    Два вызова на прогон: сразу после ``create_webdriver`` (автосбор за
    сессию + kv-сигнал, пришедший между сценариями) и в ``finally`` перед
    закрытием браузера (сигнал, пришедший во время сценария). Между
    итерациями цикла браузера нет — там сигнал только наблюдается
    (``engine.worker``), поэтому эти две точки и есть места, где сбор
    реально возможен.

    Без ``browser_id`` писать некуда (CLI-прогон без ``--id``) — пропускается.
    Любая ошибка гасится здесь с WARNING: диагностика не должна ронять ни
    воркер, ни сценарий.
    """
    if not browser_id:
        return
    try:
        session_checkpoint(
            driver,
            browser_id=browser_id,
            store=StateStore(resolve_db_path()),
            logger=log,
        )
    except Exception as exp:  # noqa: BLE001 - сценарий важнее снимка
        log.warning(
            "browser",
            "diagnostics checkpoint failed",
            fields={"error": str(exp), "error_type": type(exp).__name__},
        )


def run_scenario(
    *,
    query: str,
    proxy: str | None = None,
    browser_id: str | None = None,
    device_id: str | None = None,
    check_stealth: bool = False,
) -> bool:
    """Один прогон сценария: драйвер, поиск, клики, teardown.

    Тело бывшего ``main()`` после разбора аргументов. Оставлено здесь, а
    не перенесено в ``engine/``: движок переиспользует сценарий как есть
    (план, раздел 3), а значит и жить он должен в том же модуле, что и раньше.

    Возвращает ``False``, если прогон не доехал до конца. Сигнал обязателен:
    legacy глотает исключения сам, чтобы один упавший прогон не ронял весь
    процесс, но без результата вызывающий не отличил бы завершившийся
    сценарий от упавшего и записал бы в статистику успех.

    Исключения из подготовки (драйвер, файлы) не гасятся намеренно:
    CLI показывает трейсбек, а цикл воркера ловит их и продолжает работу.
    """
    if browser_id:
        update_log_formats(browser_id)
        # Общий логгер процесса: все записи модулей ниже получают --id.
        log.bind(browser_id)

    domains = get_domains()

    # UA назначённого профиля главнее случайного; без профиля (и с пустым
    # полем) фолбэк ровно тот же, что и раньше — get_random_user_agent_string.
    profile = current_profile()
    user_agent = resolve_user_agent(profile, get_random_user_agent_string())
    if profile is not None:
        log.debug("browser", "Profile applied", fields={"profile_id": profile["id"]})

    plugin_folder_name = "".join(random.choices(string.ascii_lowercase, k=5))

    driver, country_code = create_webdriver(proxy, user_agent, plugin_folder_name)

    # Старт сессии: автосбор один раз за жизнь процесса + запрос из UI,
    # пришедший, пока браузера не было. До поиска и до cookies — снимок
    # отражает именно то, что увидит сайт на входе.
    diagnostics_checkpoint(driver, browser_id)

    if check_stealth:
        from time import sleep
        from webdriver import execute_stealth_js_code

        execute_stealth_js_code(driver)

        driver.get("https://bot.sannysoft.com/")
        sleep(5)
        driver.get("https://browserleaks.com/canvas")
        sleep(10)
        driver.get("https://www.browserscan.net/")
        sleep(15)
        driver.get("https://pixelscan.net/bot-check")
        sleep(30)

        driver.quit()

        raise SystemExit()

    if config.behavior.hooks_enabled:
        hooks.before_search_hook(driver)

    search_controller = None
    completed = True

    try:
        search_controller = SearchController(driver, query, country_code)

        if browser_id:
            search_controller.set_browser_id(browser_id)

        if device_id:
            search_controller.assign_android_device(device_id)

        ads, non_ad_links, shopping_ads = search_controller.search_for_ads(non_ad_domains=domains)

        if config.behavior.hooks_enabled:
            hooks.after_search_hook(driver)

        if not (ads or shopping_ads):
            log.info("click", "No ads found in the search results!")

            if config.behavior.telegram_enabled:
                notify_matching_ads(query, links=None, stats=search_controller.stats)
        else:
            log.debug(
                "click",
                "Selected click order",
                fields={"click_order": config.behavior.click_order},
            )

            if config.behavior.click_order == 1:
                all_links = non_ad_links + ads

            elif config.behavior.click_order == 2:
                all_links = ads + non_ad_links

            elif config.behavior.click_order == 3:
                if non_ad_links:
                    all_links = [non_ad_links[0]] + [ads[0]] + non_ad_links[1:] + ads[1:]
                else:
                    log.debug("click", "Couldn't found non-ads! Continue with ads only.")
                    all_links = ads

            elif config.behavior.click_order == 4:
                all_links = list(
                    filterfalse(
                        lambda x: not x, chain.from_iterable(zip_longest(non_ad_links, ads))
                    )
                )

            else:
                all_links = ads + non_ad_links
                random.shuffle(all_links)

            log.info(
                "click",
                "Found ads",
                fields={"count": len(ads) + len(shopping_ads)},
            )

            search_controller.click_shopping_ads(shopping_ads)
            search_controller.click_links(all_links)

            if config.behavior.hooks_enabled:
                hooks.after_clicks_hook(driver)

            if config.behavior.telegram_enabled:
                notify_matching_ads(query, links=ads + shopping_ads, stats=search_controller.stats)

            log.info("click", str(search_controller.stats))

    except Exception as exp:
        completed = False
        log.error("scheduler", "Exception occurred. See the details in the log file.")

        if config.webdriver.ss_on_exception:
            take_screenshot(driver)

        message = str(exp).split("\n")[0]
        log.debug("scheduler", "Exception", fields={"error": message})
        details = traceback.format_tb(exp.__traceback__)
        log.debug(
            "scheduler",
            "Exception details:",
            fields={"traceback": "".join(details)},
        )

        (
            log.debug("scheduler", "Exception cause", fields={"cause": str(exp.__cause__)})
            if exp.__cause__
            else None
        )

        if config.behavior.hooks_enabled:
            hooks.exception_hook(driver)

    finally:
        # Последний чекпоинт за прогон: браузер ещё жив, а запрос из UI мог
        # прийти в любой момент сценария — сигнал снимается здесь, не дожидаясь
        # следующего прогона.
        diagnostics_checkpoint(driver, browser_id)

        if search_controller:
            if config.behavior.hooks_enabled:
                hooks.before_browser_close_hook(driver)

            search_controller.end_search()

            if config.behavior.hooks_enabled:
                hooks.after_browser_close_hook(driver)

        if proxy and config.webdriver.auth:
            plugin_folder = Path.cwd() / "proxy_auth_plugin" / plugin_folder_name
            log.debug("cleanup", "Removing folder...", fields={"folder": str(plugin_folder)})
            shutil.rmtree(plugin_folder, ignore_errors=True)

    return completed

def main():
    """Entry point for the tool"""

    arg_parser = get_arg_parser()
    args = arg_parser.parse_args()

    if args.report_clicks:
        report_date = datetime.now().strftime("%d-%m-%Y") if not args.date else args.date

        clicklogs_db_client = ClickLogsDB()
        click_results = clicklogs_db_client.query_clicks(click_date=report_date)

        border = (
            "+" + "-" * 70 + "+" + "-" * 27 + "+" + "-" * 9 + "+" + "-" * 12 + "+" + "-" * 12 + "+"
        )

        if click_results:
            print(border)
            print(
                f"| {'URL':68s} | {'Query':25s} | {'Clicks':7s} | {'Time':10s} | {'Category':10s} |"
            )
            print(border)

            for result in click_results:
                url, clicks, category, click_time, search_query = result

                if len(url) > 68:
                    url = url[:65] + "..."

                print(
                    f"| {url:68s} | {search_query:25s} | {str(clicks):7s} | {click_time:10s} | {category:10s} |"
                )

                print(border)

            # write results to Excel with name click_report_dd-mm-yyyy.xlsx
            if args.excel:
                generate_click_report(click_results, report_date)

        else:
            log.info(
                "click",
                "No click result was found for",
                fields={"report_date": report_date},
            )

        return

    if args.enable_telegram:
        if config.behavior.telegram_enabled:
            start_bot()
            return
        else:
            log.info(
                "scheduler",
                "Please set the telegram_enabled option to true in config and try again.",
            )
            return

    run_scenario(
        query=resolve_query(args),
        proxy=resolve_proxy(args),
        browser_id=args.id,
        device_id=args.device_id,
        check_stealth=args.check_stealth,
    )


if __name__ == "__main__":
    main()
