import json
import platform
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from enum import Enum
from itertools import cycle
from pathlib import Path
from time import sleep
from typing import Optional

try:
    import requests
    import openpyxl
    import undetected_chromedriver
    from openpyxl.styles import Alignment, Font

except ImportError:
    packages_path = Path.cwd() / "env" / "Lib" / "site-packages"
    sys.path.insert(0, f"{packages_path}")

    import requests
    import openpyxl
    import undetected_chromedriver
    from openpyxl.styles import Alignment, Font

from config_reader import config
from engine.log import get_logger
from geolocation_db import GeolocationDB
from proxy import get_proxies


log = get_logger()


class Direction(Enum):
    UP = "UP"
    DOWN = "DOWN"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    BOTH = "BOTH"


def get_random_user_agent_string() -> str:
    """Get random user agent

    :rtype: str
    :returns: User agent string
    """

    all_user_agents = _get_user_agents(config.paths.user_agents)

    current_os = platform.system()
    filtered_user_agents = []

    if current_os == "Windows":
        filtered_user_agents = [ua for ua in all_user_agents if "Windows" in ua]

    elif current_os == "Darwin":
        # Только десктопный Mac. iPhone/iPad-UA в десктопном Chrome даёт
        # пару «navigator.platform = MacIntel + мобильная строка» — это
        # ловит уже suspicion_flags в диагностике («Платформа 'MacIntel'
        # не соответствует ОС ios») и это же читает сервер Google по
        # заголовкам (Sec-CH-UA-Platform: macOS против UA iOS).
        filtered_user_agents = [ua for ua in all_user_agents if "Macintosh" in ua]

    elif current_os == "Linux":
        # Как и на macOS: Android-UA на десктопном Chrome — то же несоответствие.
        filtered_user_agents = [
            ua for ua in all_user_agents if "Linux" in ua and "Android" not in ua
        ]

    else:
        # fallback to all agents if no matching OS found
        filtered_user_agents = all_user_agents

    user_agent_string = random.choice(filtered_user_agents)

    log.debug("browser", "user_agent", fields={"user_agent": user_agent_string})

    return user_agent_string


# Мобильная строка: Android/iOS-токены, которые никогда не встречаются в
# десктопном Chrome. Нужен для проверки UA профиля — мобильная строка под
# десктопным окном (1920x1080) отдаёт Google мобильную вёрстку, а селекторы
# результата (tads/appbar/q) написаны под десктоп.
_MOBILE_USER_AGENT = re.compile(r"Android|iPhone|iPad|iPod|CriOS|\bMobile\b")


def is_mobile_user_agent(user_agent: str) -> bool:
    """Мобильный ли User-Agent (Android/iOS).

    :type user_agent: str
    :param user_agent: User agent string
    :rtype: bool
    :returns: True для мобильной строки, False для десктопной
    """

    return bool(_MOBILE_USER_AGENT.search(user_agent))


def _get_user_agents(user_agent_file: Path) -> list[str]:
    """Get user agents from file

    :type user_agent_file: Path
    :param user_agent_file: File containing user agents
    :rtype: list
    :returns: List of user agents
    """

    filepath = Path(user_agent_file)

    if not filepath.exists():
        raise SystemExit(f"Couldn't find user agents file: {filepath}")

    with open(filepath, encoding="utf-8") as useragentfile:
        user_agents = [
            user_agent.strip().replace("'", "").replace('"', "")
            for user_agent in useragentfile.read().splitlines()
        ]

    # blank lines would become empty user agents
    return [user_agent for user_agent in user_agents if user_agent]


def get_location(geolocation_db_client: GeolocationDB, proxy: str) -> tuple[float, float, str, str]:
    """Get latitude, longitude, country code, and timezone of ip address

    :type geolocation_db_client: GeolocationDB
    :param geolocation_db_client: GeolocationDB instance
    :type proxy: str
    :param proxy: Proxy to get geolocation
    :rtype: tuple
    :returns: (latitude, longitude, country_code, timezone) tuple for the given proxy IP
    """

    proxies_header = {"http": f"http://{proxy}", "https": f"http://{proxy}"}

    ip_address = ""

    if config.webdriver.auth:
        for repeat in range(2):
            try:
                response = requests.get("https://api.ipify.org", proxies=proxies_header, timeout=5)
                ip_address = response.text

                if not ip_address:
                    raise Exception("Failed with https://api.ipify.org")

                break

            except Exception as exp:
                log.debug(
                    "proxy",
                    "IP lookup attempt failed",
                    fields={"source": "api.ipify.org", "error": str(exp)},
                )

                try:
                    log.debug("proxy", "Trying with ipv4.webshare.io...")
                    response = requests.get(
                        "https://ipv4.webshare.io/", proxies=proxies_header, timeout=5
                    )
                    ip_address = response.text

                    if not ip_address:
                        raise Exception("Failed with https://ipv4.webshare.io")

                    break

                except Exception as exp:
                    log.debug(
                        "proxy",
                        "IP lookup attempt failed",
                        fields={"source": "ipv4.webshare.io", "error": str(exp)},
                    )

                    try:
                        log.debug("proxy", "Trying with ipconfig.io...")
                        response = requests.get(
                            "https://ipconfig.io/json", proxies=proxies_header, timeout=5
                        )
                        ip_address = response.json().get("ip")

                        if not ip_address:
                            raise Exception("Failed with https://ipconfig.io/json")

                        break

                    except Exception as exp:
                        log.debug(
                            "proxy",
                            "IP lookup attempt failed",
                            fields={"source": "ipconfig.io", "error": str(exp)},
                        )

                        if repeat == 1:
                            break

                        request_retry_timeout = 60 * config.behavior.wait_factor
                        log.info(
                            "proxy",
                            "Request will be resend after",
                            fields={"retry_after_s": request_retry_timeout},
                        )

                        sleep(request_retry_timeout)

            sleep(get_random_sleep(0.5, 1) * config.behavior.wait_factor)
    else:
        ip_address = proxy.split(":")[0]

    if not ip_address:
        log.info("proxy", "Couldn't verify IP address!", fields={"proxy": proxy})
        log.debug("proxy", "Geolocation won't be set")
        return (None, None, None, None)

    log.info("proxy", "Connecting with IP", fields={"ip_address": ip_address})

    db_result = geolocation_db_client.query_geolocation(ip_address)

    latitude = None
    longitude = None
    country_code = None
    timezone = None

    if db_result:
        latitude, longitude, country_code = db_result
        log.debug(
            "proxy",
            "Cached latitude and longitude",
            fields={"ip_address": ip_address, "latitude": latitude, "longitude": longitude},
        )
        log.debug(
            "proxy",
            "Cached country code",
            fields={"ip_address": ip_address, "country_code": country_code},
        )

        if not country_code:
            try:
                response = requests.get(f"https://ipapi.co/{ip_address}/json/", timeout=5)
                country_code = response.json().get("country_code")
                timezone = response.json().get("timezone")
                log.debug(
                    "proxy",
                    "Country code",
                    fields={"ip_address": ip_address, "country_code": country_code},
                )

            except Exception:
                try:
                    response = requests.get(
                        "https://ifconfig.co/json", proxies=proxies_header, timeout=5
                    )
                    country_code = response.json().get("country_iso")
                    timezone = response.json().get("time_zone")
                except Exception:
                    log.debug(
                        "proxy",
                        "Couldn't find country code!",
                        fields={"ip_address": ip_address},
                    )

        return (float(latitude), float(longitude), country_code, timezone)

    else:
        retry_count = 0
        max_retry_count = 5
        sleep_seconds = 5 * config.behavior.wait_factor

        while retry_count < max_retry_count:
            try:
                response = requests.get(f"https://ipapi.co/{ip_address}/json/", timeout=5)
                latitude, longitude, country_code, timezone = (
                    response.json().get("latitude"),
                    response.json().get("longitude"),
                    response.json().get("country_code"),
                    response.json().get("timezone"),
                )

                if not (latitude and longitude and country_code):
                    raise Exception("Failed with https://ipapi.co")

                break
            except Exception as exp:
                log.debug(
                    "proxy",
                    "Geolocation lookup attempt failed",
                    fields={"source": "ipapi.co", "error": str(exp)},
                )
                log.debug("proxy", "Continue with ifconfig.co")

                try:
                    response = requests.get(
                        "https://ifconfig.co/json", proxies=proxies_header, timeout=5
                    )
                    latitude, longitude, country_code, timezone = (
                        response.json().get("latitude"),
                        response.json().get("longitude"),
                        response.json().get("country_iso"),
                        response.json().get("time_zone"),
                    )

                    if not (latitude and longitude and country_code):
                        raise Exception("Failed with https://ifconfig.co/json")

                    break
                except Exception as exp:
                    log.debug(
                        "proxy",
                        "Geolocation lookup attempt failed",
                        fields={"source": "ifconfig.co", "error": str(exp)},
                    )
                    log.debug("proxy", "Continue with ipconfig.io")

                    try:
                        response = requests.get(
                            "https://ipconfig.io/json", proxies=proxies_header, timeout=5
                        )
                        latitude, longitude, country_code, timezone = (
                            response.json().get("latitude"),
                            response.json().get("longitude"),
                            response.json().get("country_iso"),
                            response.json().get("time_zone"),
                        )

                        if not (latitude and longitude and country_code):
                            raise Exception("Failed with https://ipconfig.io/json")

                        break
                    except Exception as exp:
                        log.debug(
                            "proxy",
                            "Geolocation lookup attempt failed",
                            fields={"source": "ipconfig.io", "error": str(exp)},
                        )
                        log.error(
                            "proxy",
                            "Couldn't find latitude and longitude! Retrying after",
                            fields={"ip_address": ip_address, "retry_after_s": sleep_seconds},
                        )

                        retry_count += 1
                        sleep(sleep_seconds)
                        sleep_seconds *= 2

            sleep(0.5 * config.behavior.wait_factor)

        if latitude and longitude and country_code:
            log.debug(
                "proxy",
                "Latitude and longitude",
                fields={"ip_address": ip_address, "latitude": latitude, "longitude": longitude},
            )
            log.debug(
                "proxy",
                "Country code",
                fields={"ip_address": ip_address, "country_code": country_code},
            )

            geolocation_db_client.save_geolocation(ip_address, latitude, longitude, country_code)

            return (latitude, longitude, country_code, timezone)
        else:
            log.error(
                "proxy",
                "Couldn't find latitude, longitude, and country_code!",
                fields={"ip_address": ip_address},
            )
            return (None, None, None, None)


class ProxyCountryError(RuntimeError):
    """Exit-IP прокси не подтверждён как Германия — раунд не начинается.

    Требование продукта: трафик идёт только с немецкого exit-IP. Любое
    другое значение (TR, FR, US, …) означает либо неподходящий прокси,
    либо смену exit-узла живой сессией — в обоих случаях раунд заведомо
    проигран, а Google видит регион, на который заточено поведение
    браузера. Исключение поднимается до старта Chrome
    (см. ``webdriver.create_webdriver``), поэтому браузер не тратит время
    и трафик на проигранный круг.

    Креды в текст не попадают: сообщение собирается из кода страны и уже
    замаскированного адреса прокси.
    """

    def __init__(self, reason: str, proxy_address: str) -> None:
        super().__init__(f"{reason} ({proxy_address})")
        self.reason = reason
        self.proxy_address = proxy_address


def require_german_exit(country_code: Optional[str], proxy: str) -> str:
    """Подтверждает, что exit-IP прокси — Германия; иначе ``ProxyCountryError``.

    :type country_code: Optional[str]
    :param country_code: страна из :func:`get_location`
    :type proxy: str
    :param proxy: строка прокси, возможно с кредами
    :rtype: str
    :returns: код страны (``"DE"``)

    **Не определена — тоже отказ.** Непроверенный exit не гарантирует
    Германию, а требование — ровно никакой возможности выйти не из
    Германии. У :func:`get_location` три источника и локальный кэш, поэтому
    пустой ответ на уже прошедшую пробу сеть означает отказ всех сервисов.

    Страна сравнивается без учёта регистра: код приходит от разных
    сервисов, и нормализуют его по-разному.
    """
    code = (country_code or "").strip().upper()
    if code == "DE":
        return code
    reason = "exit-IP не Германия: " + (code or "страна не определена")
    raise ProxyCountryError(reason, proxy.split("@")[-1])


def get_queries() -> list[str]:
    """Get queries from file

    :rtype: list
    :returns: List of queries
    """

    filepath = Path(config.paths.query_file)

    if not filepath.exists():
        raise SystemExit(f"Couldn't find queries file: {filepath}")

    with open(filepath, encoding="utf-8") as queryfile:
        queries = [
            query.strip().replace("'", "").replace('"', "")
            for query in queryfile.read().splitlines()
        ]

    # blank lines would become empty search queries
    return [query for query in queries if query]


def get_domains() -> list[str]:
    """Get domains from file

    :rtype: list
    :returns: List of domains
    """

    filepath = Path(config.paths.filtered_domains)

    if not filepath.exists():
        raise SystemExit(f"Couldn't find domains file: {filepath}")

    with open(filepath, encoding="utf-8") as domainsfile:
        domains = [
            domain.strip().replace("'", "").replace('"', "")
            for domain in domainsfile.read().splitlines()
        ]

    log.debug("click", "Domains", fields={"domains": domains})

    # blank lines would match any domain
    return [domain for domain in domains if domain]


def add_cookies(driver: undetected_chromedriver.Chrome) -> None:
    """Add cookies from cookies.txt file

    :type driver: undetected_chromedriver.Chrome
    :param driver: Selenium Chrome webdriver instance
    """

    filepath = Path.cwd() / "cookies.txt"

    if not filepath.exists():
        raise SystemExit("Missing cookies.txt file!")

    log.info("browser", "Adding cookies from", fields={"path": str(filepath)})

    with open(filepath, encoding="utf-8") as cookie_file:
        try:
            cookies = json.loads(cookie_file.read())
        except Exception:
            log.error("browser", "Failed to read cookies file. Check format and try again.")
            raise SystemExit()

    for cookie in cookies:
        if cookie["sameSite"] == "strict":
            cookie["sameSite"] = "Strict"
        elif cookie["sameSite"] == "lax":
            cookie["sameSite"] = "Lax"
        else:
            cookie["sameSite"] = "None" if cookie["secure"] else "Lax"

        driver.add_cookie(cookie)


# --- таймауты и ретраи 2captcha ------------------------------------------------

# Ретраи протокола: in.php отвечает «не сейчас» на ERROR_NO_SLOT_AVAILABLE,
# res.php — на CAPCHA_NOT_READY, и обе петли крутятся столько запросов
# подряд, прежде чем признать неудачу. Внешний бюджет на решение одной
# капчи — CAPTCHA_SOLVE_TIMEOUT_S в search_controller: эти числа границы
# сетевого протокола, а не сценария.
SOLVE_MAX_RETRIES = 20

# Пауза перед первым опросом res.php, сек: сервис распределяет задачу по
# своим воркерам, и без этой паузы первые опросы гарантированно вернут
# CAPCHA_NOT_READY, сжигая ретраи вхолостую.
SOLVE_INITIAL_WAIT_S = 15


def solve_recaptcha(
    apikey: str,
    sitekey: str,
    current_url: str,
    data_s: str,
    cookies: Optional[str] = None,
) -> Optional[str]:
    """Solve the recaptcha using the 2captcha service

    :type apikey: str
    :param apikey: API key for the 2captcha service
    :type sitekey: str
    :param sitekey: data-sitekey attribute value of the recaptcha element
    :type current_url: str
    :param current_url: Url that is showing the captcha
    :type data_s: str
    :param data_s: data-s attribute of the captcha element
    :type cookies: str
    :param cookies: Cookies to send 2captcha service
    :rtype: str
    :returns: Response code obtained from the service or None
    """

    log.info("captcha", "Trying to solve captcha...")

    api_url = "http://2captcha.com/in.php"
    params = {
        "key": apikey,
        "method": "userrecaptcha",
        "googlekey": sitekey,
        "pageurl": current_url,
        "data-s": data_s,
    }

    if cookies:
        params["cookies"] = cookies

    request_retry_count = 0

    while request_retry_count < SOLVE_MAX_RETRIES:
        response = requests.get(api_url, params=params)

        log.debug("captcha", "Response", fields={"response": response.text})

        error_to_exit, error_to_continue, error_to_break = _check_error(response.text)

        if error_to_exit:
            raise SystemExit()

        elif error_to_break:
            request_id = response.text.split("|")[1]
            log.debug("captcha", "request_id", fields={"request_id": request_id})
            break

        elif error_to_continue:
            request_retry_count += 1
            continue

    sleep(SOLVE_INITIAL_WAIT_S * config.behavior.wait_factor)

    # check if the CAPTCHA has been solved
    response_api_url = "http://2captcha.com/res.php"
    params = {"key": apikey, "action": "get", "id": request_id}

    response_retry_count = 0
    captcha_response = None

    while response_retry_count < SOLVE_MAX_RETRIES:
        response = requests.get(response_api_url, params=params)

        log.debug("captcha", "Response", fields={"response": response.text})

        error_to_exit, error_to_continue, error_to_break = _check_error(
            response.text, request_type="res_php"
        )

        if error_to_exit:
            raise SystemExit()

        elif error_to_continue:
            response_retry_count += 1
            continue

        elif error_to_break:
            if "CAPCHA_NOT_READY" not in response.text:
                captcha_response = response.text.split("|")[1]
                return captcha_response

    if not captcha_response:
        log.error("captcha", "Failed to solve captcha!")

    return captcha_response


def take_screenshot(driver: undetected_chromedriver.Chrome) -> None:
    """Save screenshot during exception

    :type driver: undetected_chromedriver.Chrome
    :param driver: Selenium Chrome webdriver instance
    """

    now = datetime.now().strftime("%d-%m-%Y_%H_%M_%S")
    filename = f"exception_ss_{now}.png"

    if driver:
        driver.save_screenshot(filename)
        sleep(get_random_sleep(1, 1.5) * config.behavior.wait_factor)
        log.info("browser", "Saved screenshot during exception as", fields={"filename": filename})


def generate_click_report(click_results: list[tuple[str, str, str]], report_date: str) -> None:
    """Update results file with new rows

    :type click_results: list
    :param click_results: List of (site_url, clicks, category, click_time, query) tuples
    :type report_date: str
    :param report_date: Date to query clicks
    """

    click_report_file = Path(f"click_report_{report_date}.xlsx")

    workbook = openpyxl.Workbook()
    sheet = workbook.active

    sheet.row_dimensions[1].height = 20

    # add header
    sheet["A1"] = "URL"
    sheet["B1"] = "Query"
    sheet["C1"] = "Clicks"
    sheet["D1"] = "Time"
    sheet["E1"] = "Category"

    bold_font = Font(bold=True)
    center_align = Alignment(horizontal="center", vertical="center")

    for cell in ("A1", "B1", "C1", "D1", "E1"):
        sheet[cell].font = bold_font
        sheet[cell].alignment = center_align

    # adjust column widths
    sheet.column_dimensions["A"].width = 80
    sheet.column_dimensions["B"].width = 25
    sheet.column_dimensions["C"].width = 15
    sheet.column_dimensions["D"].width = 20
    sheet.column_dimensions["E"].width = 15

    for result in click_results:
        url, click_count, category, click_time, query = result
        sheet.append((url, query, click_count, f"{report_date} {click_time}", category))

    for column_letter in ("B", "C", "D", "E"):
        sheet.column_dimensions[column_letter].alignment = center_align

    workbook.save(click_report_file)

    log.info("click", "Results were written to", fields={"path": str(click_report_file)})


def get_random_sleep(start: float, end: float) -> float:
    """Generate a random number from the given range

    :type start: float
    :param start: Start value
    :type end: float
    :param end: End value
    :rtype: float
    :returns: Randomly selected number rounded to 2 decimals
    """

    return round(random.uniform(start, end), 2)


def _check_error(response_text: str, request_type: str = "in_php") -> tuple[bool, bool, bool]:
    """Check errors returned from requests to in.php or res.php endpoints

    :type response_text: str
    :param response_text: Response returned from the request
    :request_type: str
    :param request_type: Request type to differentiate error groups
    :rtype: tuple
    :returns: Flags for exit, continue, and break
    """

    log.debug("captcha", "Checking error code...")

    error_to_exit, error_to_continue, error_to_break = False, False, False
    error_wait = 5 * config.behavior.wait_factor

    if request_type == "in_php":
        if "ERROR_WRONG_USER_KEY" in response_text or "ERROR_KEY_DOES_NOT_EXIST" in response_text:
            log.error("captcha", "Invalid API key. Please check your 2captcha API key.")
            error_to_exit = True

        elif "ERROR_ZERO_BALANCE" in response_text:
            log.error("captcha", "You don't have funds on your account. Please load your account.")
            error_to_exit = True

        elif "ERROR_NO_SLOT_AVAILABLE" in response_text:
            log.error(
                "captcha",
                "The queue of your captchas that are not distributed to workers is too long.",
            )
            log.info(
                "captcha",
                "Waiting before sending new request...",
                fields={"wait_s": error_wait},
            )
            sleep(error_wait)

            error_to_continue = True

        elif "IP_BANNED" in response_text:
            log.error(
                "captcha",
                "Your IP address is banned due to many frequent attempts to access the server",
            )
            error_to_exit = True

        elif "ERROR_GOOGLEKEY" in response_text:
            log.error("captcha", "Blank or malformed sitekey.")
            error_to_exit = True

        else:
            log.debug("captcha", "2captcha response", fields={"response": response_text})
            error_to_break = True

    elif request_type == "res_php":
        if "ERROR_WRONG_USER_KEY" in response_text or "ERROR_KEY_DOES_NOT_EXIST" in response_text:
            log.error("captcha", "Invalid API key. Please check your 2captcha API key.")
            error_to_exit = True

        elif "ERROR_CAPTCHA_UNSOLVABLE" in response_text:
            log.error("captcha", "Unable to solve the captcha.")
            error_to_exit = True

        elif "CAPCHA_NOT_READY" in response_text:
            log.info(
                "captcha",
                "Waiting before checking response again...",
                fields={"wait_s": error_wait},
            )
            sleep(error_wait)

            error_to_continue = True

        else:
            log.debug("captcha", "2captcha response", fields={"response": response_text})
            error_to_break = True

    else:
        log.error("captcha", "Wrong request type", fields={"request_type": request_type})

    return (error_to_exit, error_to_continue, error_to_break)


def get_locale_language(country_code: Optional[str]) -> Optional[str]:
    """Get locale language for the given country code

    :type country_code: Optional[str]
    :param country_code: Country code for proxy IP
    :rtype: Optional[str]
    :returns: Primary locale for the given country code (напр. ``"de-DE"``)
        или ``None``, если страна неизвестна/не определена геолокацией

    ``None`` вместо прежнего ``"en"``: одиночный Accept-Language ``en`` —
    редкий для десктопа паттерн, который на проводе читается как аномалия
    (отчёт по CAPTCHA, §4.1). Без локали браузер шлёт свой честный
    дефолт (``en-US,en;q=0.9``); защита от ``country_code=None`` есть и в
    ``webdriver`` (обе ветки), здесь — для неизвестных стран.
    """

    log.debug("browser", "Getting locale language...", fields={"country_code": country_code})

    # Файл ищется сначала в cwd (legacy-песочница и юзерский override), иначе
    # — рядом с модулем-владельцем: в упакованном запуске cwd — каталог данных
    # без этого файла, а сам он лежит внутри бинарника sidecar (_MEIPASS,
    # bundle.spec кладёт его в корень). Путь модуля общий с
    # engine.diagnostics — одна точка правды; импорт ленивый, чтобы
    # legacy-модуль не тянул engine при своём собственном импорте.
    from engine.diagnostics import COUNTRY_LOCALES_FILE

    locales_path = Path.cwd() / "country_to_locale.json"
    if not locales_path.is_file():
        locales_path = COUNTRY_LOCALES_FILE

    with open(locales_path, "r", encoding="utf-8") as locales_file:
        locales = json.load(locales_file)

    # Список → первая локаль: str(list) уходил в prefs и в --lang как
    # "['de-DE']" (и в --lang=['de-DE'] при срезе [:2]), а поле Accept-Language
    # в снимке диагностики выглядело как массив. Нет записи (неизвестная
    # страна, гео не определено) → None, локаль не ставится вовсе.
    locales_list = locales.get(country_code) if country_code else None

    log.debug(
        "browser",
        "Locale language code",
        fields={"country_code": country_code, "language": locales_list[0] if locales_list else None},
    )

    return locales_list[0] if locales_list else None


# Честный UA для probe-запроса: python-requests сам по себе уже аномалия,
# а нам нужно отличить «IP в бане» от «клиент странный» — шлём как Chrome.
_PROBE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

_PROBE_URL = "https://www.google.com/search?q=proxy+health+check"


def probe_proxy_captcha(proxy: str, timeout: float = 10.0) -> Optional[bool]:
    """Прямой GET /search через прокси: отбраковка нерабочего exit IP.

    Дешёвый health-check перед раундом (отчёт по CAPTCHA, §4.5): мёртый
    IP (капча даже чистому браузеру) виден на неавторизованном запросе —
    ``/sorry/`` в URL или ``unusual traffic`` в теле, — и раунд на нём
    заведомо проигран.

    :type proxy: str
    :param proxy: Прокси в формате ``[user:pass@]host:port``
    :type timeout: float
    :param timeout: Таймаут запроса, сек
    :rtype: Optional[bool]
    :returns: ``True`` — IP в бане (капча), ``False`` — чисто,
        ``None`` — проверить не удалось (сеть молчит либо ответ 4xx/5xx)

    ``False`` — единственный исход, при котором проверка что-то подтвердила:
    ответ доехал и капчи нет. Всё остальное (таймаут, обрыв, отказ прокси
    на CONNECT, ответ прокси или Google 4xx/5xx) — ``None``, и вызывающий
    обязан НЕ поднимать браузер: раньше ошибки считались «чисто»
    (``flagged=False``), и раунд стартовал с заведомо нерабочего прокси —
    Chrome поднимался, тратил время и трафик и всё равно умирал. Маркер
    капчи главнее кода ответа: ``/sorry/`` на ответе 429 — это капча,
    а не ошибка транспорта. Креды в логи не попадают — вызывающий маскирует
    сам.
    """

    proxies = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
    try:
        response = requests.get(
            _PROBE_URL,
            proxies=proxies,
            timeout=timeout,
            headers={"User-Agent": _PROBE_UA},
        )
    except requests.RequestException as exp:
        log.debug(
            "proxy",
            "proxy captcha probe did not complete",
            fields={"error_type": type(exp).__name__},
        )
        return None

    flagged = "/sorry/" in response.url or "unusual traffic" in response.text.lower()
    if flagged:
        log.debug(
            "proxy",
            "proxy captcha probe",
            fields={
                "proxy": proxy.split("@")[-1],
                "status_code": response.status_code,
                "captcha": True,
            },
        )
        return True

    # Ошибка транспорта, упакованная в HTTP-ответ: прокси отдал 407/502 на
    # CONNECT или 403/429 на сам запрос, Google — 429/503. Тело такого ответа
    # не содержит маркера капчи, поэтому без этой проверки он читался бы как
    # «чисто» и раунд стартовал бы с мёртвым прокси.
    if response.status_code >= 400:
        log.debug(
            "proxy",
            "proxy captcha probe got an error response",
            fields={
                "proxy": proxy.split("@")[-1],
                "status_code": response.status_code,
            },
        )
        return None

    log.debug(
        "proxy",
        "proxy captcha probe",
        fields={
            "proxy": proxy.split("@")[-1],
            "status_code": response.status_code,
            "captcha": False,
        },
    )
    return False


def resolve_redirect(url: str) -> str:
    """Resolve any redirects and return the final destination URL

    :type url: str
    :param url: Input url to resolve
    :rtype: str
    :returns: Final destination URL
    """

    try:
        response = requests.get(url, allow_redirects=True)
        return response.url

    except requests.RequestException as exp:
        log.error(
            "click",
            "Error resolving URL redirection",
            fields={"error": str(exp)},
        )
        return url


def _make_boost_request(url: str, proxy: str, user_agent: str) -> None:
    """Make a single GET request for the given url through a random proxy and user agent

    :type url: str
    :param url: Input URL to send request to
    :type proxy: str
    :param proxy: Proxy to use for the request
    :type user_agent: str
    :param user_agent: User agent to use for the request
    """

    headers = {"User-Agent": user_agent}
    proxy_config = {"http": f"http://{proxy}", "https": f"http://{proxy}"}

    try:
        response = requests.get(url, headers=headers, proxies=proxy_config, timeout=5)
        log.debug(
            "proxy",
            "Boosted",
            fields={
                "url": url,
                "proxy": proxy.split("@")[1] if "@" in proxy else proxy,
                "user_agent": headers["User-Agent"],
                "status_code": response.status_code,
            },
        )

    except Exception as exp:
        log.debug(
            "proxy",
            "Boost request failed",
            fields={"url": url, "proxy": proxy, "error": str(exp)},
        )


def boost_requests(url: str) -> None:
    """Send multiple requests to the given URL

    :type url: str
    :param url: Input URL to send requests to
    """

    log.debug("proxy", "Sending 10 requests to", fields={"url": url})

    proxies = get_proxies()
    user_agents = _get_user_agents(config.paths.user_agents)

    random.shuffle(proxies)
    random.shuffle(user_agents)

    proxy = cycle(proxies)
    user_agent = cycle(user_agents)

    with ThreadPoolExecutor(max_workers=10) as executor:
        for _ in range(10):
            executor.submit(_make_boost_request, url, next(proxy), next(user_agent))
