import json
import multiprocessing
from dataclasses import dataclass
from typing import Optional

from engine.captcha_policy import DEFAULT_CAPTCHA_POLICY
from engine.captcha_threshold import (
    DEFAULT_CAPTCHA_THRESHOLD_ACTION,
    DEFAULT_CAPTCHA_THRESHOLD_PERCENT,
)
from engine.cleanup import DEFAULT_CLEANUP_INTERVAL_DAYS, DEFAULT_CLEANUP_TIME
from engine.log_rotation import (
    DEFAULT_DB_SIZE_LIMIT_MB,
    DEFAULT_LOG_FILE_LEVEL,
    DEFAULT_LOG_RETENTION_DAYS,
)
from engine.proxy_transport import DEFAULT_PROXY_TRANSPORT
from logger import apply_file_level, logger


@dataclass
class PathParams:
    query_file: str
    proxy_file: str
    user_agents: Optional[str] = "user_agents.txt"
    filtered_domains: Optional[str] = "domains.txt"


@dataclass
class WebdriverParams:
    proxy: str
    auth: Optional[bool] = False
    incognito: Optional[bool] = False
    country_domain: Optional[bool] = False
    language_from_proxy: Optional[bool] = False
    ss_on_exception: Optional[bool] = False
    window_size: Optional[str] = ""
    shift_windows: Optional[bool] = False
    use_seleniumbase: Optional[bool] = False
    # Транспорт прокси (cdp_auth | extension | direct). Дефолт берётся из
    # лёгкого engine.proxy_transport, а не дублируется строкой: словарь
    # значений и "что считается нормой" живут там же, где их проверяет
    # resolve_proxy_transport.
    proxy_transport: Optional[str] = DEFAULT_PROXY_TRANSPORT


@dataclass
class BehaviorParams:
    query: str
    ad_page_min_wait: Optional[int] = 10
    ad_page_max_wait: Optional[int] = 15
    nonad_page_min_wait: Optional[int] = 15
    nonad_page_max_wait: Optional[int] = 20
    max_scroll_limit: Optional[int] = 0
    check_shopping_ads: Optional[bool] = True
    excludes: Optional[str] = ""
    # Наш домен: добавляется в чёрный список клика автоматически
    # (utils.get_domains), поэтому держать его в domains.txt руками не нужно.
    own_domain: Optional[str] = ""
    random_mouse: Optional[bool] = False
    custom_cookies: Optional[bool] = False
    click_order: Optional[int] = 5
    browser_count: Optional[int] = 2
    multiprocess_style: Optional[int] = 1
    loop_wait_time: Optional[int] = 60
    wait_factor: Optional[float] = 1.0
    running_interval_start: Optional[str] = ""
    running_interval_end: Optional[str] = ""
    twocaptcha_apikey: Optional[str] = ""
    hooks_enabled: Optional[bool] = False
    telegram_enabled: Optional[bool] = False
    send_to_android: Optional[bool] = False
    request_boost: Optional[bool] = False
    # Политика CAPTCHA (stop | solve | both), дефолт — engine.captcha_policy.
    captcha_policy: Optional[str] = DEFAULT_CAPTCHA_POLICY
    # Порог CAPTCHA: проценты и действие (warn | pause | rotate). Дефолты
    # берутся из engine.captcha_threshold, а не дублируются строкой: там же
    # их читает политика и проверяет конфиг демона.
    captcha_threshold_percent: Optional[float] = DEFAULT_CAPTCHA_THRESHOLD_PERCENT
    captcha_threshold_action: Optional[str] = DEFAULT_CAPTCHA_THRESHOLD_ACTION
    # Хранение логов (план §5, фаза 9): сколько дней держать записи, какой
    # уровень писать в дневной файл и какой лимит размера БД в МБ держать
    # (0 — выключен). Дефолты — из engine.log_rotation, там же их проверяет
    # control plane: второго источника быть не должно.
    log_retention_days: Optional[int] = DEFAULT_LOG_RETENTION_DAYS
    log_file_level: Optional[str] = DEFAULT_LOG_FILE_LEVEL
    db_size_limit_mb: Optional[int] = DEFAULT_DB_SIZE_LIMIT_MB
    # Очистка профилей (план §5, фаза 10): время ежедневного прогона и период
    # в днях. Дефолты — из engine.cleanup, там же их проверяет control plane.
    cleanup_time: Optional[str] = DEFAULT_CLEANUP_TIME
    cleanup_interval_days: Optional[int] = DEFAULT_CLEANUP_INTERVAL_DAYS


class _ConfigSection(dict):
    """Раздел конфига, который вместо голого KeyError называет недостающий параметр"""

    def __init__(self, data: dict, section: str) -> None:
        super().__init__(data)
        self.section = section

    def __missing__(self, key: str) -> None:
        logger.error(f"Failed to read config file. Missing '{key}' parameter in {self.section}.")
        raise SystemExit()


def _get_config_section(config: dict, section: str) -> _ConfigSection:
    """Read a config section, reporting a readable error if it is not usable"""

    data = config.get(section)

    if data is None:
        logger.error(f"Failed to read config file. Missing '{section}' section.")
        raise SystemExit()

    if not isinstance(data, dict):
        logger.error(f"Failed to read config file. '{section}' must be a JSON object.")
        raise SystemExit()

    return _ConfigSection(data, f"'{section}' section")


class ConfigReader:
    """Config file reader"""

    def __init__(self) -> None:
        self.paths = None
        self.webdriver = None
        self.behavior = None

    def read_parameters(self) -> None:
        """Read parameters from the config.json file"""

        with open("config.json", encoding="utf-8") as config_file:
            try:
                config = json.loads(config_file.read())
            except Exception:
                logger.error("Failed to read config file. Check format and try again.")
                raise SystemExit()

        if not isinstance(config, dict):
            logger.error("Failed to read config file. Check format and try again.")
            raise SystemExit()

        for section in ("paths", "webdriver", "behavior"):
            config[section] = _get_config_section(config, section)

        self.paths = PathParams(
            query_file=config["paths"]["query_file"],
            proxy_file=config["paths"]["proxy_file"],
            user_agents=config["paths"]["user_agents"],
            filtered_domains=config["paths"]["filtered_domains"],
        )

        if self.paths.proxy_file and config["webdriver"]["proxy"]:
            logger.error("Either 'proxy_file' or 'proxy' parameter should be empty.")
            raise SystemExit()

        self.webdriver = WebdriverParams(
            proxy=config["webdriver"]["proxy"],
            auth=config["webdriver"]["auth"],
            incognito=config["webdriver"]["incognito"],
            country_domain=config["webdriver"]["country_domain"],
            language_from_proxy=config["webdriver"]["language_from_proxy"],
            ss_on_exception=config["webdriver"]["ss_on_exception"],
            window_size=config["webdriver"]["window_size"],
            shift_windows=config["webdriver"]["shift_windows"],
            use_seleniumbase=config["webdriver"]["use_seleniumbase"],
            # .get, а не []: config.json прошлой версии без ключа обязан
            # читаться, дефолт приходит из engine.proxy_transport.
            proxy_transport=config["webdriver"].get(
                "proxy_transport", DEFAULT_PROXY_TRANSPORT
            ),
        )

        if self.paths.query_file and config["behavior"]["query"]:
            logger.error("Either 'query_file' or 'query' parameter should be empty.")
            raise SystemExit()

        browser_count = config["behavior"]["browser_count"]

        self.behavior = BehaviorParams(
            query=config["behavior"]["query"],
            ad_page_min_wait=config["behavior"]["ad_page_min_wait"],
            ad_page_max_wait=config["behavior"]["ad_page_max_wait"],
            nonad_page_min_wait=config["behavior"]["nonad_page_min_wait"],
            nonad_page_max_wait=config["behavior"]["nonad_page_max_wait"],
            max_scroll_limit=config["behavior"]["max_scroll_limit"],
            check_shopping_ads=config["behavior"]["check_shopping_ads"],
            excludes=config["behavior"]["excludes"],
            # .get, а не []: config.json прошлой версии без ключа обязан
            # читаться (план §5, фаза 13), пустой строкой — «наш домен не задан».
            own_domain=config["behavior"].get("own_domain", ""),
            random_mouse=config["behavior"]["random_mouse"],
            custom_cookies=config["behavior"]["custom_cookies"],
            click_order=config["behavior"]["click_order"],
            browser_count=multiprocessing.cpu_count() if browser_count == 0 else browser_count,
            multiprocess_style=config["behavior"]["multiprocess_style"],
            loop_wait_time=config["behavior"]["loop_wait_time"],
            wait_factor=config["behavior"]["wait_factor"],
            running_interval_start=config["behavior"]["running_interval_start"],
            running_interval_end=config["behavior"]["running_interval_end"],
            twocaptcha_apikey=config["behavior"]["2captcha_apikey"],
            hooks_enabled=config["behavior"]["hooks_enabled"],
            telegram_enabled=config["behavior"]["telegram_enabled"],
            send_to_android=config["behavior"]["send_to_android"],
            request_boost=config["behavior"]["request_boost"],
            # .get, а не []: config.json прошлой версии без ключей обязан
            # читаться, дефолты приходят из engine.captcha_*.
            captcha_policy=config["behavior"].get(
                "captcha_policy", DEFAULT_CAPTCHA_POLICY
            ),
            captcha_threshold_percent=config["behavior"].get(
                "captcha_threshold_percent", DEFAULT_CAPTCHA_THRESHOLD_PERCENT
            ),
            captcha_threshold_action=config["behavior"].get(
                "captcha_threshold_action", DEFAULT_CAPTCHA_THRESHOLD_ACTION
            ),
            # .get, а не []: config.json прошлой версии без ключей хранения
            # логов обязан читаться, дефолты приходят из engine.log_rotation.
            log_retention_days=config["behavior"].get(
                "log_retention_days", DEFAULT_LOG_RETENTION_DAYS
            ),
            log_file_level=config["behavior"].get("log_file_level", DEFAULT_LOG_FILE_LEVEL),
            db_size_limit_mb=config["behavior"].get(
                "db_size_limit_mb", DEFAULT_DB_SIZE_LIMIT_MB
            ),
            # .get, а не []: config.json прошлой версии без ключей очистки
            # обязан читаться, дефолты приходят из engine.cleanup.
            cleanup_time=config["behavior"].get("cleanup_time", DEFAULT_CLEANUP_TIME),
            cleanup_interval_days=config["behavior"].get(
                "cleanup_interval_days", DEFAULT_CLEANUP_INTERVAL_DAYS
            ),
        )

        # Уровень файлового лога применяется при каждом чтении конфига:
        # поле behavior.log_file_level читается здесь же, поэтому воркер
        # подхватывает его и на старте, и при перечитывании настроек. Опечатка
        # в значении не должна ронять запуск — уровень остаётся прежним, а
        # причина уходит в лог.
        try:
            apply_file_level(self.behavior.log_file_level)
        except ValueError as exc:
            logger.error(
                f"Failed to apply 'log_file_level' from config file: {exc}. "
                "Keeping the current file level."
            )


config = ConfigReader()
config.read_parameters()
