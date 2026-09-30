from __future__ import annotations

import os
import random
import shutil
import sys
import tempfile
from pathlib import Path
from time import sleep
from typing import TYPE_CHECKING, Optional, Union

import pyautogui
import requests
import undetected_chromedriver

if TYPE_CHECKING:
    # Только для аннотаций: рантайм-импорт живёт в create_seleniumbase_driver,
    # UC-режим не должен требовать необязательный пакет на import модуля.
    import seleniumbase

from config_reader import config
from engine.cdp import CdpClient
from engine.log import get_logger
from engine.profile_apply import current_profile, resolve_locale, resolve_timezone
from engine.proxy_auth import (
    PROXY_TRANSPORT_CDP_AUTH,
    PROXY_TRANSPORT_DIRECT,
    PROXY_TRANSPORT_EXTENSION,
    ProxyAuthManager,
    create_proxy_auth,
    mask_secret,
    parse_proxy_credentials,
    resolve_proxy_transport,
)
from geolocation_db import GeolocationDB
from proxy import install_plugin
from utils import get_location, get_locale_language, get_random_sleep


log = get_logger()


IS_POSIX = sys.platform.startswith(("cygwin", "linux"))


class CustomChrome(undetected_chromedriver.Chrome):
    """Modified Chrome implementation"""

    def quit(self):

        # Остановка CDP-авторизации — первой: сокет гасится до того, как
        # умрёт браузер, иначе приёмный поток успеет наловить ошибок чтения.
        # Ошибка остановки не должна ронять выход (см. stop_proxy_auth).
        stop_proxy_auth(self)

        try:
            os.kill(self.browser_pid, 15)
            if IS_POSIX:
                os.waitpid(self.browser_pid, 0)
            else:
                sleep(0.05 * config.behavior.wait_factor)
        except (AttributeError, ChildProcessError, RuntimeError, OSError):
            pass
        except TimeoutError as e:
            log.debug("browser", str(e), fields={"error_type": type(e).__name__}, exc_info=e)
        except Exception as e:
            # quit() не должен ронять завершение, но молча глотать ошибку
            # нельзя: без записи в лог падение браузера при выходе
            # недиагностируемо.
            log.debug("browser", str(e), fields={"error_type": type(e).__name__}, exc_info=e)

        if hasattr(self, "service") and getattr(self.service, "process", None):
                self.service.stop()

        try:
            if self.reactor:
                self.reactor.event.set()
        except Exception as e:
            log.debug("browser", str(e), fields={"error_type": type(e).__name__}, exc_info=e)

        if (
            hasattr(self, "keep_user_data_dir")
            and hasattr(self, "user_data_dir")
            and not self.keep_user_data_dir
        ):
            for _ in range(5):
                try:
                    shutil.rmtree(self.user_data_dir, ignore_errors=False)
                except FileNotFoundError:
                    pass
                except (RuntimeError, OSError, PermissionError) as e:
                    log.debug(
                        "browser",
                        "When removing the temp profile, retrying...",
                        fields={"error_type": e.__class__.__name__, "error": str(e)},
                    )
                else:
                    break

                sleep(0.1 * config.behavior.wait_factor)

        # dereference patcher, so patcher can start cleaning up as well.
        # this must come last, otherwise it will throw 'in use' errors
        self.patcher = None

    def __del__(self):
        try:
            self.service.process.kill()
        except Exception:  # noqa
            pass

        try:
            self.quit()
        except OSError:
            pass

    @classmethod
    def _ensure_close(cls, self):
        # needs to be a classmethod so finalize can find the reference
        if (
            hasattr(self, "service")
            and hasattr(self.service, "process")
            and hasattr(self.service.process, "kill")
        ):
            self.service.process.kill()

            if IS_POSIX:
                try:
                    # prevent zombie processes
                    os.waitpid(self.service.process.pid, 0)
                except ChildProcessError:
                    pass
                except Exception:
                    pass
            else:
                sleep(0.05 * config.behavior.wait_factor)


# Признак многопроцессного запуска.
#
# Раньше его нёс файл ``.MULTI_BROWSERS_IN_USE``, который создавал
# ``run_ad_clicker.py``. Эта точка входа удалена, файл больше не создаёт
# никто, а читать его — значит реагировать на мусор прошлой версии и
# включать многопроцессный путь одиночному запуску. Источник признака —
# окружение, которое расставляет супервизор демона при пуле больше одного.
MULTI_BROWSERS_ENV = "ADCLICKER_MULTI_BROWSERS"


def is_multi_procs_enabled() -> bool:
    """Участвует ли этот запуск в пуле из нескольких браузеров.

    Нужно, чтобы N процессов не качали и не патчили один chromedriver
    одновременно. Супервизор выставляет переменную только когда в пуле
    больше одного воркера, поэтому одиночный ``ad_clicker.py`` её не видит.
    """
    return os.environ.get(MULTI_BROWSERS_ENV) == "1"


# --- Транспорты прокси (план.md §2.1) -----------------------------------------
#
# Транспорт выбирает, КАК креды доходят до Chrome:
#   cdp_auth   — --proxy-server=host:port + ProxyAuthManager по CDP (дефолт);
#   extension  — install_plugin (MV3-расширение), флаг --proxy-server не нужен;
#   direct     — только --proxy-server=host:port, без кредов (whitelist-IP).
# Значения объявлены в engine.proxy_transport и переэкспортируются
# engine.proxy_auth — второго словаря значений негде появиться.

# Атрибут драйвера, в котором живёт поднятый ProxyAuthManager.
PROXY_AUTH_ATTR = "_proxy_auth_manager"


def _proxy_host_port(proxy: str) -> str:
    """Адрес прокси без кредов — ровно то, что уходит в ``--proxy-server``.

    Логин/пароль не должны быть видны в списке процессов Chrome: их доставляет
    транспорт (расширение или CDP), а не аргументы запуска.
    """
    return proxy.rsplit("@", 1)[-1] if "@" in proxy else proxy


def _mask_proxy(proxy: str) -> str:
    """Прокси для лога: креды маскируются по образцу engine.proxy_auth."""
    if "@" not in proxy:
        return proxy
    credentials, _, host_port = proxy.rpartition("@")
    username, _, password = credentials.partition(":")
    return f"{mask_secret(username)}:{mask_secret(password)}@{host_port}"


def _proxy_credentials(proxy: str, transport: str) -> tuple[str, str] | None:
    """``(логин, пароль)`` из строки прокси или None, если кредов нет.

    ``direct`` креды не читает вовсе: это whitelist-IP, и адрес без логина для
    него — норма, даже при ``auth=true``. Остальные транспорты при ``auth=true``
    наследуют legacy-проверку со старым текстом ошибки: его ищут скрипты и
    тесты, менять его нельзя. ``auth=false`` со строкой, в которой креды всё же
    есть, разбирается по-мягкому: некорректная строка не роняет запуск, а в
    ``--proxy-server`` креды всё равно не попадут.
    """
    if transport == PROXY_TRANSPORT_DIRECT:
        return None
    if config.webdriver.auth:
        if "@" not in proxy or proxy.count(":") != 2:
            raise ValueError(
                "Invalid proxy format! Should be in 'username:password@host:port' format"
            )
        username, password = proxy.split("@")[0].split(":")
        return username, password
    if "@" not in proxy:
        return None
    try:
        return parse_proxy_credentials(proxy)
    except ValueError:
        # Строка с "@" не разбирается — считаем, что кредов нет. Сама строка
        # в лог не идёт: там может быть пароль.
        log.debug("proxy", "Proxy credentials are malformed, ignoring them")
        return None


def _report_degraded(reason: str, proxy_host_port: str) -> None:
    """Сигнал деградации транспорта: WARNING в лог и ``workers.status``.

    В полях только ``reason`` и ``host:port`` без кредов: и лог, и
    ``last_error`` обязаны оставаться чистыми (план.md §2.1). Реакция
    супервизора (ротация прокси, видимость в ``/state``) — другая ветка;
    здесь важно, что сигнал доставлен.
    """
    log.warning(
        "proxy",
        "proxy transport degraded",
        fields={"reason": reason, "proxy": proxy_host_port},
    )
    log.mark_degraded(reason)


def stop_proxy_auth(driver) -> None:
    """Остановить CDP-авторизацию драйвера. Никогда не бросает исключений.

    Вызывается и из ``CustomChrome.quit``, и из обёртки quit у SeleniumBase:
    двойной вызов безопасен — менеджер снимается с драйвера до остановки.
    """
    manager = getattr(driver, PROXY_AUTH_ATTR, None)
    if manager is None:
        return
    try:
        setattr(driver, PROXY_AUTH_ATTR, None)
        manager.stop()
    except Exception as exc:
        # quit() не должен роняться из-за менеджера, но и молчать нельзя:
        # без записи в лог причина была бы недиагностируема.
        log.debug(
            "proxy",
            "Proxy auth stop failed",
            fields={"error_type": type(exc).__name__, "error": str(exc)},
        )


def attach_proxy_auth(driver, manager: ProxyAuthManager) -> None:
    """Привязать менеджер к драйверу и остановить его в ``quit()``.

    ``CustomChrome.quit`` зовёт :func:`stop_proxy_auth` сам, но драйвер
    SeleniumBase — чужий класс, поэтому ``quit`` заворачивается всегда: один
    путь остановки для обоих браузеров.
    """
    setattr(driver, PROXY_AUTH_ATTR, manager)
    original_quit = driver.quit

    def quit_with_proxy_auth() -> None:
        stop_proxy_auth(driver)
        original_quit()

    driver.quit = quit_with_proxy_auth


def _start_proxy_auth(
    credentials: tuple[str, str],
    user_data_dir,
    proxy_host_port: str,
    debugger_address: str | None = None,
) -> ProxyAuthManager | None:
    """Поднять CDP-авторизацию после создания драйвера. None — не вышло.

    Порт DevTools менеджер находит сам: файл ``DevToolsActivePort`` в
    ``user_data_dir``, а при его отсутствии — ``debugger_address`` (UC
    держит фиксированный ``--remote-debugging-port`` и файла не пишет;
    без этой ветки авторизация не поднималась ни разу — e2e). Отказ не
    роняет создание драйвера: браузер жив и доедет до первой 407, а
    супервизору нужен сигнал, а не падение воркера — причина уходит в
    WARNING и в ``workers.status='degraded'``. Колбэк ``on_proxy_dead``
    и обрыв CDP держат ту же дорогу сигнала, только с другой причиной.
    """
    username, password = credentials
    try:
        return create_proxy_auth(
            username,
            password,
            user_data_dir=user_data_dir,
            debugger_address=debugger_address,
            on_proxy_dead=lambda: _report_degraded(
                "proxy rejected credentials", proxy_host_port
            ),
            client_factory=lambda url: CdpClient(
                url,
                on_connection_lost=lambda: _report_degraded(
                    "cdp connection lost", proxy_host_port
                ),
            ),
        )
    except Exception as exc:
        _report_degraded(f"cdp auth start failed: {type(exc).__name__}", proxy_host_port)
        return None


def _debugger_address_of(driver) -> str | None:
    """DevTools-адрес драйвера, если платформа его знает.

    UC всегда пишет ``options.debugger_address`` (фиксированный
    ``--remote-debugging-port``); у SeleniumBase и заглушек атрибута может
    не быть вовсе — тогда возвращается None и менеджер ищет порт по
    старой цепочке (файл в профиле).
    """
    options = getattr(driver, "options", None)
    return getattr(options, "debugger_address", None)


def _apply_locale(chrome_options, lang: Optional[object]) -> None:
    """Поставить локаль в опции Chrome. None — нечего применять.

    Форма строк сохранена до буквы: legacy складывает список локалей из
    ``get_locale_language`` через ``str()`` в prefs и обрезает его в ``--lang``
    как есть, а профильная локаль приходит строкой. Менять это значило бы
    менять поведение для старых конфигов (см. тесты паритета).
    """

    if lang is None:
        return
    chrome_options.add_experimental_option("prefs", {"intl.accept_languages": str(lang)})
    chrome_options.add_argument(f"--lang={lang[:2]}")


def _override_timezone(driver, timezone: object, fields: Optional[dict] = None) -> None:
    """Поставить часовой пояс через CDP и запомнить его на драйвере.

    ``_custom_timezone`` читает ``execute_stealth_js_code``, поэтому атрибут
    выставляется здесь же, а не в местах вызова. Список полей — контекст
    записи в лог (прокси или профиль), сам часовой пояс добавляет всегда.
    """

    driver._custom_timezone = timezone
    driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": timezone})
    log.debug("browser", "Timezone of", fields={"timezone": timezone, **(fields or {})})


def create_webdriver(
    proxy: str, user_agent: Optional[str] = None, plugin_folder_name: Optional[str] = None
) -> tuple[undetected_chromedriver.Chrome, Optional[str]]:
    """Create Selenium Chrome webdriver instance

    :type proxy: str
    :param proxy: Proxy to use in ip:port or user:pass@host:port format
    :type user_agent: str
    :param user_agent: User agent string
    :type plugin_folder_name: str
    :param plugin_folder_name: Plugin folder name for proxy
    :rtype: tuple
    :returns: (undetected_chromedriver.Chrome, country_code) pair
    """

    if config.webdriver.use_seleniumbase:
        log.debug("browser", "Using SeleniumBase...")
        return create_seleniumbase_driver(proxy, user_agent)

    # Транспорт читается до любых побочных эффектов: неверное значение — это
    # ошибка конфигурации, и упасть она должна раньше, чем созданы каталоги
    # профиля и запущен Chrome.
    transport = resolve_proxy_transport(config.webdriver.proxy_transport)

    # Настройки профиля читаются до опций Chrome: локаль обязана попасть в
    # add_experimental_option до создания драйвера, а часовой пояс — в CDP
    # сразу после. UA приходит аргументом, его разрешает ad_clicker.
    profile = current_profile()
    profile_timezone = resolve_timezone(profile, None)

    geolocation_db_client = GeolocationDB()

    chrome_options = undetected_chromedriver.ChromeOptions()
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--no-service-autorun")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-infobars")
    chrome_options.add_argument("--disable-popup-blocking")
    chrome_options.add_argument("--disable-notifications")
    chrome_options.add_argument("--disable-translate")
    chrome_options.add_argument("--deny-permission-prompts")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--disable-application-cache")
    chrome_options.add_argument("--disable-breakpad")
    chrome_options.add_argument("--disable-renderer-backgrounding")
    chrome_options.add_argument("--disable-browser-side-navigation")
    chrome_options.add_argument("--disable-save-password-bubble")
    chrome_options.add_argument("--disable-single-click-autofill")
    chrome_options.add_argument("--disable-prompt-on-repost")
    chrome_options.add_argument("--disable-backgrounding-occluded-windows")
    chrome_options.add_argument("--disable-hang-monitor")
    chrome_options.add_argument("--dns-prefetch-disable")
    chrome_options.add_argument("--allow-running-insecure-content")
    chrome_options.add_argument("--disable-search-engine-choice-screen")
    chrome_options.add_argument(f"--user-agent={user_agent}")

    if IS_POSIX:
        chrome_options.add_argument("--disable-setuid-sandbox")

    disabled_features = [
        "OptimizationGuideModelDownloading",
        "OptimizationHintsFetching",
        "OptimizationTargetPrediction",
        "OptimizationHints",
        "Translate",
        "DownloadBubble",
        "DownloadBubbleV2",
        "PrivacySandboxSettings4",
        "UserAgentClientHint",
        "DisableLoadExtensionCommandLineSwitch",
    ]
    chrome_options.add_argument(f"--disable-features={','.join(disabled_features)}")

    # disable WebRTC IP tracking
    webrtc_preferences = {
        "webrtc.ip_handling_policy": "disable_non_proxied_udp",
        "webrtc.multiple_routes_enabled": False,
        "webrtc.nonproxied_udp_enabled": False,
    }
    chrome_options.add_experimental_option("prefs", webrtc_preferences)

    if config.webdriver.incognito:
        chrome_options.add_argument("--incognito")

    base_dir = Path(tempfile.gettempdir()) / "uc_profiles"
    base_dir.mkdir(exist_ok=True)
    profile_dir = base_dir / f"profile_{random.randint(1000, 9999)}"

    chrome_options.add_argument(f"--user-data-dir={profile_dir}")
    chrome_options.add_argument("--profile-directory=Default")

    country_code = None
    # Гео до приоритета профиля: их читает диагностика (engine.diagnostics),
    # чтобы сверить часовой пояс и страну страницы с прокси, а не с тем, что
    # оператор захотел видеть в профиле. Оба — best effort: без прокси и без
    # get_location здесь остаётся None, и правила сравнения их пропускают.
    geo_country: Optional[str] = None
    geo_timezone: Optional[str] = None

    multi_procs_enabled = is_multi_procs_enabled()
    driver_exe_path = None

    if multi_procs_enabled:
        driver_exe_path = _get_driver_exe_path()

    if proxy:
        credentials = _proxy_credentials(proxy, transport)
        host_port = _proxy_host_port(proxy)
        masked_proxy = _mask_proxy(proxy)

        log.info("proxy", "Using proxy", fields={"proxy": masked_proxy})
        log.debug("proxy", "Using proxy", fields={"proxy": masked_proxy})

        if transport == PROXY_TRANSPORT_EXTENSION and credentials is not None:
            # extension: ровно прежнее поведение — креды уходят в расширение,
            # поэтому отдельный --proxy-server Chrome не нужен.
            username, password = credentials
            host, port = host_port.split(":")

            install_plugin(chrome_options, host, int(port), username, password, plugin_folder_name)
            sleep(2 * config.behavior.wait_factor)
        else:
            chrome_options.add_argument(f"--proxy-server={host_port}")

        # get location of the proxy IP
        lat, long, country_code, timezone = get_location(geolocation_db_client, proxy)
        # До resolve_timezone: дальше пояс может заменить профильный, а
        # гео-значение для диагностики должно остаться именно гео.
        geo_country, geo_timezone = country_code, timezone

        # Профильная локаль главнее гео-вычисления; само гео вызывается
        # только когда включён language_from_proxy, как и раньше.
        geo_locale = (
            get_locale_language(country_code) if config.webdriver.language_from_proxy else None
        )
        _apply_locale(chrome_options, resolve_locale(profile, geo_locale))
        timezone = resolve_timezone(profile, timezone)

        driver = CustomChrome(
            driver_executable_path=(
                driver_exe_path if multi_procs_enabled and Path(driver_exe_path).exists() else None
            ),
            options=chrome_options,
            user_multi_procs=multi_procs_enabled,
            use_subprocess=False,
        )

        if transport == PROXY_TRANSPORT_CDP_AUTH and credentials is not None:
            # Старт после создания драйвера: DevTools-порт доступен только
            # когда Chrome поднялся. UC пишет его в options.debugger_address
            # (файла DevToolsActivePort при фиксированном порту нет) —
            # отдаём менеджеру как фолбэк. Остановка — в quit() драйвера.
            manager = _start_proxy_auth(
                credentials,
                profile_dir,
                host_port,
                _debugger_address_of(driver),
            )
            if manager is not None:
                attach_proxy_auth(driver, manager)

        accuracy = 95

        # set geolocation and timezone of the browser according to IP address
        if lat and long:
            driver.execute_cdp_cmd(
                "Emulation.setGeolocationOverride",
                {"latitude": lat, "longitude": long, "accuracy": accuracy},
            )

            if not timezone:
                response = requests.get(f"http://timezonefinder.michelfe.it/api/0_{long}_{lat}")

                if response.status_code == 200:
                    timezone = response.json()["tz_name"]
                # Ветка возможна только когда не гео, не профиль не дали
                # пояса, поэтому найденный здесь — тоже гео-вычисление.
                geo_timezone = timezone

            _override_timezone(driver, timezone, fields={"proxy": host_port})

        elif profile_timezone:
            # Координат нет — legacy часовой пояс не ставил вовсе, даже если
            # geolocation вернул пояс отдельно от широты/долготы. Профильный
            # применяется сам по себе: он к гео не привязан.
            _override_timezone(driver, profile_timezone)

        # Для диагностики: страна и пояс прокси, какими их дал get_location,
        # — по ним правило «timezone ↔ гео» отличает рассинхрон от профиля.
        driver._geo_country = geo_country
        driver._geo_timezone = geo_timezone

    else:
        # Без прокси legacy локаль и пояс не ставил ни в каком виде; с профилем
        # применяются только его значения (гео здесь неоткуда взять).
        _apply_locale(chrome_options, resolve_locale(profile, None))
        driver = CustomChrome(
            driver_executable_path=(
                driver_exe_path if multi_procs_enabled and Path(driver_exe_path).exists() else None
            ),
            options=chrome_options,
            user_multi_procs=multi_procs_enabled,
            use_subprocess=False,
        )
        if profile_timezone:
            _override_timezone(driver, profile_timezone)

    if config.webdriver.window_size:
        width, height = config.webdriver.window_size.split(",")
        log.debug("browser", "Setting window size", fields={"width": width, "height": height})
        driver.set_window_size(width, height)
    else:
        log.debug("browser", "Maximizing window...")
        driver.maximize_window()

    if config.webdriver.shift_windows:
        width, height = (
            config.webdriver.window_size.split(",")
            if config.webdriver.window_size
            else (None, None)
        )
        _shift_window_position(driver, width, height)

    return (driver, country_code) if config.webdriver.country_domain else (driver, None)


def create_seleniumbase_driver(
    proxy: str, user_agent: Optional[str] = None
) -> tuple[seleniumbase.Driver, Optional[str]]:
    """Create SeleniumBase Chrome webdriver instance

    :type proxy: str
    :param proxy: Proxy to use in ip:port or user:pass@host:port format
    :type user_agent: str
    :param user_agent: User agent string
    :rtype: tuple
    :returns: (Driver, country_code) pair
    """

    # Лениво и только здесь: UC-режим (use_seleniumbase=false) не должен
    # требовать необязательный пакет — иначе сценарий падает до браузера
    # (поймано живым e2e-прогоном: sidecar и nix не содержат seleniumbase).
    try:
        import seleniumbase
    except ImportError as exc:
        raise RuntimeError(
            "режим use_seleniumbase требует пакет seleniumbase: "
            "установите его (pip install seleniumbase) или отключите "
            "webdriver.use_seleniumbase"
        ) from exc

    geolocation_db_client = GeolocationDB()

    # Как и в UC-ветке: ошибка конфигурации раньше любых побочных эффектов.
    transport = resolve_proxy_transport(config.webdriver.proxy_transport)

    # Настройки профиля — до get_driver: locale_code уходит в него аргументом,
    # а часовой пояс ставится через CDP сразу после создания драйвера.
    profile = current_profile()
    profile_timezone = resolve_timezone(profile, None)

    country_code = None
    # Гео до приоритета профиля — их читает диагностика (см. UC-ветку).
    geo_country: Optional[str] = None
    geo_timezone: Optional[str] = None
    credentials: tuple[str, str] | None = None
    host_port = ""
    lang = None

    if proxy:
        credentials = _proxy_credentials(proxy, transport)
        host_port = _proxy_host_port(proxy)
        masked_proxy = _mask_proxy(proxy)

        log.info("proxy", "Using proxy", fields={"proxy": masked_proxy})
        log.debug("proxy", "Using proxy", fields={"proxy": masked_proxy})

        # get location of the proxy IP
        lat, long, country_code, timezone = get_location(geolocation_db_client, proxy)
        # Как в UC-ветке: гео-пояс фиксируется до приоритета профиля — его
        # читает диагностика.
        geo_country, geo_timezone = country_code, timezone

        if config.webdriver.language_from_proxy:
            lang = get_locale_language(country_code)
        timezone = resolve_timezone(profile, timezone)

    base_dir = Path(tempfile.gettempdir()) / "sb_profiles"
    base_dir.mkdir(exist_ok=True)
    profile_dir = base_dir / f"profile_{random.randint(1000,9999)}"

    # Креды в proxy_string не идут ни при каком транспорте, кроме extension:
    # там строка остаётся целиком, как и до появления транспортов, а
    # cdp_auth/direct отдают Chrome только адрес — логин/пароль в списке
    # процессов недопустимы.
    proxy_string = None
    if proxy:
        if transport == PROXY_TRANSPORT_EXTENSION and credentials is not None:
            proxy_string = proxy
        else:
            proxy_string = host_port

    # Профильная локаль главнее гео-вычисленной; без профиля и без
    # language_from_proxy в аргумент уходит None, как и раньше.
    lang = resolve_locale(profile, lang)

    driver = seleniumbase.get_driver(
        browser_name="chrome",
        undetectable=True,
        headless2=False,
        do_not_track=True,
        user_agent=user_agent,
        proxy_string=proxy_string,
        multi_proxy=config.behavior.browser_count > 1,
        incognito=config.webdriver.incognito,
        locale_code=str(lang) if lang is not None else None,
        user_data_dir=str(profile_dir),
    )

    if proxy and transport == PROXY_TRANSPORT_CDP_AUTH and credentials is not None:
        # Тот же механизм, что и в UC-ветке: DevTools-порт берётся из
        # user_data_dir (файл) или из options.debugger_address (фолбэк,
        # обязателен для UC с фиксированным портом).
        manager = _start_proxy_auth(
            credentials,
            profile_dir,
            host_port,
            _debugger_address_of(driver),
        )
        if manager is not None:
            attach_proxy_auth(driver, manager)

    # set geolocation and timezone if available
    if proxy and lat and long:
        accuracy = 95
        driver.execute_cdp_cmd(
            "Emulation.setGeolocationOverride",
            {"latitude": lat, "longitude": long, "accuracy": accuracy},
        )

        if not timezone:
            response = requests.get(f"http://timezonefinder.michelfe.it/api/0_{long}_{lat}")
            if response.status_code == 200:
                timezone = response.json()["tz_name"]
            # Ни гео, ни профиль пояса не дали — значит, он гео-вычисленный.
            geo_timezone = timezone

        _override_timezone(driver, timezone, fields={"proxy": host_port})

    elif profile_timezone:
        # Без координат legacy пояс не ставил вовсе; профильный применяется
        # сам по себе — и в прокси-ветке, и без прокси.
        _override_timezone(driver, profile_timezone)

    if proxy:
        # Для диагностики: гео-страна и гео-пояс прокси, как их дал get_location.
        driver._geo_country = geo_country
        driver._geo_timezone = geo_timezone

    # handle window size and position
    if config.webdriver.window_size:
        width, height = config.webdriver.window_size.split(",")
        log.debug("browser", "Setting window size", fields={"width": width, "height": height})
        driver.set_window_size(int(width), int(height))
    else:
        log.debug("browser", "Maximizing window...")
        driver.maximize_window()

    if config.webdriver.shift_windows:
        width, height = (
            config.webdriver.window_size.split(",")
            if config.webdriver.window_size
            else (None, None)
        )
        _shift_window_position(driver, width, height)

    return (driver, country_code) if config.webdriver.country_domain else (driver, None)


def _shift_window_position(
    driver: Union[undetected_chromedriver.Chrome, seleniumbase.Driver],
    width: int = None,
    height: int = None,
) -> None:
    """Shift the browser window position randomly

    :type driver: Union[undetected_chromedriver.Chrome, seleniumbase.Driver]
    :param driver: WebDriver instance
    :type width: int
    :param width: Predefined window width
    :type height: int
    :param height: Predefined window height
    """

    # get screen size
    screen_width, screen_height = pyautogui.size()

    window_position = driver.get_window_position()
    x, y = window_position["x"], window_position["y"]

    random_x_offset = random.choice(range(150, 300))
    random_y_offset = random.choice(range(75, 150))

    if width and height:
        new_width = int(width) - random_x_offset
        new_height = int(height) - random_y_offset
    else:
        new_width = int(screen_width * 2 / 3) - random_x_offset
        new_height = int(screen_height * 2 / 3) - random_y_offset

    # set the window size and position
    driver.set_window_size(new_width, new_height)

    new_x = min(x + random_x_offset, screen_width - new_width)
    new_y = min(y + random_y_offset, screen_height - new_height)

    log.debug("browser", "Setting window position", fields={"x": new_x, "y": new_y})

    driver.set_window_position(new_x, new_y)
    sleep(get_random_sleep(0.1, 0.5) * config.behavior.wait_factor)


def _get_driver_exe_path() -> str:
    """Get the path for the chromedriver executable to avoid downloading and patching each time

    :rtype: str
    :returns: Absoulute path of the chromedriver executable
    """

    platform = sys.platform
    prefix = "undetected"
    exe_name = "chromedriver%s"

    if platform.endswith("win32"):
        exe_name %= ".exe"
    if platform.endswith(("linux", "linux2")):
        exe_name %= ""
    if platform.endswith("darwin"):
        exe_name %= ""

    if platform.endswith("win32"):
        dirpath = "~/appdata/roaming/undetected_chromedriver"
    elif "LAMBDA_TASK_ROOT" in os.environ:
        dirpath = "/tmp/undetected_chromedriver"
    elif platform.startswith(("linux", "linux2")):
        dirpath = "~/.local/share/undetected_chromedriver"
    elif platform.endswith("darwin"):
        dirpath = "~/Library/Application Support/undetected_chromedriver"
    else:
        dirpath = "~/.undetected_chromedriver"

    driver_exe_folder = os.path.abspath(os.path.expanduser(dirpath))
    driver_exe_path = os.path.join(driver_exe_folder, "_".join([prefix, exe_name]))

    return driver_exe_path


def execute_stealth_js_code(driver: Union[undetected_chromedriver.Chrome, seleniumbase.Driver]):
    """Execute the stealth JS code to prevent detection

    Signature changes can be tested by loading the following addresses
    - https://browserleaks.com/canvas
    - https://browserleaks.com/webrtc
    - https://browserleaks.com/webgl

    For bot check
    - https://pixelscan.net/bot-check
    - https://www.browserscan.net/
    - https://bot.sannysoft.com/

    :type driver: Union[undetected_chromedriver.Chrome, seleniumbase.Driver]
    :param driver: WebDriver instance
    """

    # timezone spoofing and normalization
    timezone = getattr(driver, "_custom_timezone", None)

    if timezone:
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": timezone})

        timezone_js = f"""
        (() => {{
            const tz = "{timezone}";
            const getOffset = (tzName) => {{
                try {{
                    const now = new Date();
                    const local = new Date(now.toLocaleString("en-US", {{ timeZone: tzName }}));
                    const utc = new Date(now.toLocaleString("en-US", {{ timeZone: "UTC" }}));
                    return (utc - local) / 60000; // minutes
                }} catch (e) {{
                    return 0;
                }}
            }};
            const offset = getOffset(tz);
            const sign = offset <= 0 ? "+" : "-";
            const absOffset = Math.abs(offset);
            const hours = String(Math.floor(absOffset / 60)).padStart(2, "0");
            const minutes = String(Math.abs(offset) % 60).padStart(2, "0");
            const gmtString = `GMT${{sign}}${{hours}}${{minutes}}`;

            // Patch Intl
            const origIntl = Intl.DateTimeFormat.prototype.resolvedOptions;
            Intl.DateTimeFormat.prototype.resolvedOptions = function() {{
                const opts = origIntl.call(this);
                opts.timeZone = tz;
                return opts;
            }};

            // Patch Date
            const origOffset = Date.prototype.getTimezoneOffset;
            Date.prototype.getTimezoneOffset = function() {{ return offset; }};

            const origToString = Date.prototype.toString;
            Date.prototype.toString = function() {{
                const str = origToString.call(this);
                return str.replace(/GMT[+-]\\d{{4}}.*$/, `${{gmtString}} (${{tz}})`);
            }};

            const origLocale = Date.prototype.toLocaleString;
            Date.prototype.toLocaleString = function(...args) {{
                const opts = args[1] || {{}};
                if (!opts.timeZone) opts.timeZone = tz;
                return origLocale.call(this, args[0] || undefined, opts);
            }};

            // Hide modifications
            const fakeNative = (n) => `function ${{n}}() {{ [native code] }}`;
            [
                Date.prototype.getTimezoneOffset,
                Date.prototype.toString,
                Date.prototype.toLocaleString,
                Intl.DateTimeFormat.prototype.resolvedOptions
            ].forEach(fn => {{
                if (fn && fn.name)
                    Object.defineProperty(fn, "toString", {{ value: () => fakeNative(fn.name) }});
            }});

            // Proxy Intl.DateTimeFormat constructor for consistency
            Object.defineProperty(Intl, "DateTimeFormat", {{
                value: new Proxy(Intl.DateTimeFormat, {{
                    construct(target, args) {{
                        if (args[1] && args[1].timeZone)
                            args[1].timeZone = tz;
                        return new target(...args);
                    }}
                }})
            }});
        }})();
        """
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": timezone_js})

    # DevTools detection prevention
    devtools_evasion_js = """
    (function() {
        const realInnerWidth = window.innerWidth;
        const realInnerHeight = window.innerHeight;

        // The key: outerHeight should be VERY CLOSE to innerHeight (browser closed)
        // Not random, but consistent small difference
        try {
            Object.defineProperty(window, 'outerHeight', {
                get: function() {
                    return realInnerHeight + 39;
                },
                configurable: true
            });

            Object.defineProperty(window, 'outerWidth', {
                get: function() {
                    return realInnerWidth + 12;
                },
                configurable: true
            });
        } catch(e) {}

        // 2. Override innerWidth/innerHeight to be stable
        try {
            Object.defineProperty(window, 'innerWidth', {
                get: function() {
                    return realInnerWidth;
                },
                configurable: true
            });

            Object.defineProperty(window, 'innerHeight', {
                get: function() {
                    return realInnerHeight;
                },
                configurable: true
            });
        } catch(e) {}

        // 3. Override screen properties
        try {
            Object.defineProperty(screen, 'availWidth', {
                get: () => screen.width,
                configurable: true
            });
            Object.defineProperty(screen, 'availHeight', {
                get: () => screen.height,
                configurable: true
            });
        } catch(e) {}

        // 4. Remove debugger detection
        Object.defineProperty(window, 'devtools', {
            get: () => undefined,
            set: () => {},
            configurable: false
        });

        // 5. Override console methods
        const noop = () => {};
        ['log', 'debug', 'info', 'warn', 'error'].forEach(m => {
            console[m] = noop;
        });

        // 6. Block Function toString inspection
        const OriginalToString = Function.prototype.toString;
        Function.prototype.toString = function() {
            if (this === Function.prototype.toString) {
                return 'function toString() { [native code] }';
            }
            return 'function() { [native code] }';
        };

        // 7. Prevent Error.stack inspection
        const OriginalError = Error;
        window.Error = function(...args) {
            const error = new OriginalError(...args);
            if (error.stack) {
                error.stack = error.stack.split('\\n').slice(0, 2).join('\\n');
            }
            return error;
        };

        // 8. Block debugger statement
        window.eval = new Proxy(window.eval, {
            apply(target, thisArg, args) {
                if (args[0] && args[0].includes('debugger')) {
                    return undefined;
                }
                return Reflect.apply(target, thisArg, args);
            }
        });

    })();
    """
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": devtools_evasion_js})

    # navigator.plugins evasion
    plugins_js = """
    (function() {
        // Save original PluginArray and MimeTypeArray constructors
        const OriginalPluginArray = PluginArray;
        const OriginalMimeTypeArray = MimeTypeArray;

        // Create MimeType objects
        const createMimeType = (type, suffixes, description, plugin) => {
            const mimeType = {
                type: type,
                suffixes: suffixes,
                description: description,
                enabledPlugin: plugin
            };
            return mimeType;
        };

        // Create Plugin objects
        const createPlugin = (name, description, filename, mimeTypes) => {
            const plugin = {
                name: name,
                description: description,
                filename: filename,
                length: mimeTypes.length
            };

            mimeTypes.forEach((mimeType, index) => {
                plugin[index] = createMimeType(
                    mimeType.type,
                    mimeType.suffixes,
                    mimeType.description,
                    plugin
                );
            });

            plugin.item = function(index) {
                return this[index] || null;
            };

            plugin.namedItem = function(name) {
                for (let i = 0; i < this.length; i++) {
                    if (this[i].type === name) return this[i];
                }
                return null;
            };

            return plugin;
        };

        // Create plugin data
        const pluginsData = [
            createPlugin('PDF Viewer', 'Portable Document Format', 'internal-pdf-viewer', [
                { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }
            ]),
            createPlugin('Chrome PDF Viewer', 'Portable Document Format', 'internal-pdf-viewer', [
                { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }
            ]),
            createPlugin('Chromium PDF Viewer', 'Portable Document Format', 'internal-pdf-viewer', [
                { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }
            ]),
            createPlugin('Microsoft Edge PDF Viewer', 'Portable Document Format', 'internal-pdf-viewer', [
                { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }
            ]),
            createPlugin('WebKit built-in PDF', 'Portable Document Format', 'internal-pdf-viewer', [
                { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }
            ])
        ];

        // Create a real PluginArray instance by extending it
        class FakePluginArray extends OriginalPluginArray {
            constructor(plugins) {
                super();
                plugins.forEach((plugin, index) => {
                    this[index] = plugin;
                    this[plugin.name] = plugin;
                });

                // Override length
                Object.defineProperty(this, 'length', {
                    get: () => plugins.length,
                    enumerable: false
                });
            }

            item(index) {
                return this[index] || null;
            }

            namedItem(name) {
                return this[name] || null;
            }

            refresh() {}
        }

        // Create the plugin array instance
        const pluginArray = new FakePluginArray(pluginsData);

        // Make methods look native
        ['item', 'namedItem', 'refresh'].forEach(method => {
            Object.defineProperty(pluginArray[method], 'toString', {
                value: () => `function ${method}() { [native code] }`,
                writable: false,
                configurable: false
            });
        });

        // Create MimeTypeArray
        class FakeMimeTypeArray extends OriginalMimeTypeArray {
            constructor(plugins) {
                super();
                let mimeIndex = 0;

                plugins.forEach(plugin => {
                    for (let i = 0; i < plugin.length; i++) {
                        this[mimeIndex] = plugin[i];
                        this[plugin[i].type] = plugin[i];
                        mimeIndex++;
                    }
                });

                Object.defineProperty(this, 'length', {
                    get: () => mimeIndex,
                    enumerable: false
                });
            }

            item(index) {
                return this[index] || null;
            }

            namedItem(name) {
                return this[name] || null;
            }
        }

        const mimeTypesArray = new FakeMimeTypeArray(pluginsData);

        // Make methods look native
        ['item', 'namedItem'].forEach(method => {
            Object.defineProperty(mimeTypesArray[method], 'toString', {
                value: () => `function ${method}() { [native code] }`,
                writable: false,
                configurable: false
            });
        });

        // Override navigator.plugins and navigator.mimeTypes
        Object.defineProperty(navigator, 'plugins', {
            get: () => pluginArray,
            enumerable: true,
            configurable: true
        });

        Object.defineProperty(navigator, 'mimeTypes', {
            get: () => mimeTypesArray,
            enumerable: true,
            configurable: true
        });

    })();
    """
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": plugins_js})

    # iframe.contentWindow evasion
    iframe_js = """
    try {
        const defaultGetter = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow').get;
        Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
            get: function() {
                const win = defaultGetter.call(this);
                if (!win) return win;

                try {
                    const proxy = new Proxy(win, {
                        get: (target, prop) => {
                            if (prop === 'self' || prop === 'window' || prop === 'parent' || prop === 'top') {
                                return proxy;
                            }
                            return Reflect.get(target, prop);
                        },
                        has: (target, prop) => {
                            if (prop === 'webdriver') return false;
                            return Reflect.has(target, prop);
                        }
                    });
                    return proxy;
                } catch(e) {
                    return win;
                }
            },
            configurable: true,
            enumerable: true
        });
    } catch(e) {}
    """
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": iframe_js})

    # media codecs evasion
    media_codecs_js = """
    const originalCanPlayType = HTMLMediaElement.prototype.canPlayType;
    HTMLMediaElement.prototype.canPlayType = function(type) {
        if (type === 'video/mp4; codecs="avc1.42E01E"') return 'probably';
        if (type === 'audio/mpeg') return 'probably';
        if (type === 'audio/mp4; codecs="mp4a.40.2"') return 'probably';
        return originalCanPlayType.apply(this, arguments);
    };

    Object.defineProperty(HTMLMediaElement.prototype.canPlayType, 'toString', {
        value: () => 'function canPlayType() { [native code] }'
    });
    """
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": media_codecs_js})

    # canvas fingerprint randomization
    canvas_js = """
    // Generate consistent but random noise seed
    const noiseSeed = Math.random() * 10;

    const noisify = (canvas, context) => {
        const shift = {
            r: Math.floor(noiseSeed * 2) - 1,
            g: Math.floor(noiseSeed * 2) - 1,
            b: Math.floor(noiseSeed * 2) - 1,
            a: Math.floor(noiseSeed * 2) - 1
        };

        const width = canvas.width;
        const height = canvas.height;

        if (width > 0 && height > 0) {
            try {
                const imageData = context.getImageData(0, 0, width, height);
                for (let i = 0; i < imageData.data.length; i += 4) {
                    imageData.data[i + 0] = imageData.data[i + 0] + shift.r;
                    imageData.data[i + 1] = imageData.data[i + 1] + shift.g;
                    imageData.data[i + 2] = imageData.data[i + 2] + shift.b;
                    imageData.data[i + 3] = imageData.data[i + 3] + shift.a;
                }
                context.putImageData(imageData, 0, 0);
            } catch(e) {}
        }
    };

    const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
    const originalToBlob = HTMLCanvasElement.prototype.toBlob;
    const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;

    HTMLCanvasElement.prototype.toDataURL = function(...args) {
        const context = this.getContext('2d');
        if (context) noisify(this, context);
        return originalToDataURL.apply(this, args);
    };

    HTMLCanvasElement.prototype.toBlob = function(...args) {
        const context = this.getContext('2d');
        if (context) noisify(this, context);
        return originalToBlob.apply(this, args);
    };

    // Protect toString
    Object.defineProperty(HTMLCanvasElement.prototype.toDataURL, 'toString', {
        value: () => 'function toDataURL() { [native code] }'
    });
    Object.defineProperty(HTMLCanvasElement.prototype.toBlob, 'toString', {
        value: () => 'function toBlob() { [native code] }'
    });
    """
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": canvas_js})

    # WebGL vendor/renderer randomization (enhanced)
    webgl_js = """
    // Hardware-based vendor/renderer pairs only (avoid SwiftShader/Google to prevent detection)
    const webglData = [
        { vendor: 'Intel Inc.', renderer: 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'NVIDIA Corporation', renderer: 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'AMD', renderer: 'ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'Intel Inc.', renderer: 'ANGLE (Intel, Intel(R) Iris(TM) Graphics 6100 Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'NVIDIA Corporation', renderer: 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)' }
    ];

    const selected = webglData[Math.floor(Math.random() * webglData.length)];
    const vendor = selected.vendor;
    const renderer = selected.renderer;

    // Override getParameter for WebGLRenderingContext
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
        // Unmasked vendor/renderer
        if (parameter === 37445) return vendor;
        if (parameter === 37446) return renderer;
        // Standard vendor/renderer
        if (parameter === 33901) return vendor;
        if (parameter === 33902) return renderer;
        // Version info
        if (parameter === 7938) return 'WebGL 1.0 (OpenGL ES 2.0 Chromium)';
        if (parameter === 35724) return 'WebGL GLSL ES 1.00 (OpenGL ES GLSL ES 1.0 Chromium)';
        // Max texture size
        if (parameter === 3379) return 16384 + Math.floor(Math.random() * 1024);
        // Other parameters
        if (parameter === 34076) return 16;
        if (parameter === 34930) return 16;
        if (parameter === 36349) return 32;
        return getParameter.apply(this, arguments);
    };

    // Same for WebGL2RenderingContext
    if (window.WebGL2RenderingContext) {
        const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
        WebGL2RenderingContext.prototype.getParameter = function(parameter) {
            if (parameter === 37445) return vendor;
            if (parameter === 37446) return renderer;
            if (parameter === 33901) return vendor;
            if (parameter === 33902) return renderer;
            if (parameter === 7938) return 'WebGL 2.0 (OpenGL ES 3.0 Chromium)';
            if (parameter === 35724) return 'WebGL GLSL ES 3.00 (OpenGL ES GLSL ES 3.0 Chromium)';
            if (parameter === 3379) return 16384 + Math.floor(Math.random() * 1024);
            if (parameter === 34076) return 16;
            if (parameter === 34930) return 16;
            if (parameter === 36349) return 32;
            return getParameter2.apply(this, arguments);
        };

        Object.defineProperty(WebGL2RenderingContext.prototype.getParameter, 'toString', {
            value: () => 'function getParameter() { [native code] }'
        });
    }

    // Make it look native
    Object.defineProperty(WebGLRenderingContext.prototype.getParameter, 'toString', {
        value: () => 'function getParameter() { [native code] }'
    });
    """
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": webgl_js})

    # WebRTC blocking
    webrtc_js = """
    (function() {
        // 1. Completely disable RTCPeerConnection
        window.RTCPeerConnection = undefined;
        window.webkitRTCPeerConnection = undefined;
        window.mozRTCPeerConnection = undefined;

        // 2. Disable RTCDataChannel
        window.RTCDataChannel = undefined;

        // 3. Block getUserMedia
        if (navigator.mediaDevices) {
            navigator.mediaDevices.getUserMedia = () => Promise.reject(new Error('Permission denied'));
            navigator.mediaDevices.getDisplayMedia = () => Promise.reject(new Error('Permission denied'));
            navigator.mediaDevices.enumerateDevices = () => Promise.resolve([]);
        }

        // 4. Block legacy getUserMedia
        if (navigator.getUserMedia) {
            navigator.getUserMedia = (c, s, e) => e(new Error('Permission denied'));
        }

        // 5. Block webkitGetUserMedia
        if (navigator.webkitGetUserMedia) {
            navigator.webkitGetUserMedia = (c, s, e) => e(new Error('Permission denied'));
        }

        // 6. Disable getStats
        if (window.RTCPeerConnection) {
            window.RTCPeerConnection.prototype.getStats = undefined;
        }

        // 7. Disable createDataChannel
        if (window.RTCPeerConnection) {
            window.RTCPeerConnection.prototype.createDataChannel = undefined;
        }
    })();
    """
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": webrtc_js})

    log.debug("browser", "Applied advanced stealth JavaScript techniques")
