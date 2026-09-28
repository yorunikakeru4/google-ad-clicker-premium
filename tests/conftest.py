"""Общие фикстуры для characterization-тестов legacy-модулей.

Legacy-код читает относительные пути (``config.json``, ``country_to_locale.json``,
``cookies.txt``) и пишет относительные файлы (``logs/``, ``clicklogs.db``) прямо
в текущем рабочем каталоге, причём часть этого происходит на этапе импорта
модуля. Поэтому изоляция построена на трёх уровнях:

1. Все legacy-модули импортируются из временной «песочницы» с синтетическим
   ``config.json`` (хук ``pytest_configure``). Ни один тест не видит настоящий
   ``config.json`` из корня репозитория и не пишет в корень ``logs/``.
2. Сразу после импорта ``cwd`` возвращается в корень репозитория, чтобы pytest
   корректно разрешил ``testpaths`` из ``pyproject.toml``.
3. Каждый тест выполняется в собственном ``tmp_path`` (автофикстура
   ``isolated_cwd``). БД SQLite, отчёты xlsx и прочие артефакты остаются внутри
   ``tmp_path`` и удаляются вместе с ним.

В песочнице ``wait_factor`` намеренно задан маленьким (0.02): код умножает на
него почти все ``sleep()``, и тесты не должны ждать реальные паузы.
"""

import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent

# pytest добавляет в sys.path относительный "." из pyproject.toml, который
# разрешается от текущего каталога, а тесты меняют cwd на время выполнения.
# Добавляем абсолютный путь, чтобы legacy-модули импортировались всегда.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# --- Заглушка отсутствующего в nix-окружении seleniumbase ---------------------------
# Это не тестовый двойник, а подмена окружения: flake.nix намеренно не поставляет
# seleniumbase (он есть только в необязательном extra), а webdriver.py импортирует
# его безусловно на верхнем уровне, из-за чего webdriver/search_controller/ad_clicker
# не импортируются вообще. Модуль-заглушка позволяет импортировать код; при любом
# реальном обращении к seleniumbase (create_seleniumbase_driver -> get_driver) он
# падает с RuntimeError, поэтому пропущенный кусок поведения не может молча
# превратиться в «зелёный» тест.
if "seleniumbase" not in sys.modules:
    _seleniumbase_stub = ModuleType("seleniumbase")
    _seleniumbase_stub.Driver = object  # используется только в аннотациях
    _seleniumbase_stub.__version__ = "0.0.0-test-stub"

    def _get_driver(*args, **kwargs):
        raise RuntimeError("seleniumbase недоступен в тестовом окружении")

    _seleniumbase_stub.get_driver = _get_driver
    sys.modules["seleniumbase"] = _seleniumbase_stub


SANDBOX_DIR = Path(tempfile.mkdtemp(prefix="adclicker-legacy-tests-"))


def _sandbox_config() -> dict:
    """Синтетический config.json для песочницы: все пути абсолютные и временные."""

    return {
        "paths": {
            "query_file": str(SANDBOX_DIR / "queries.txt"),
            "proxy_file": str(SANDBOX_DIR / "proxies.txt"),
            "user_agents": str(SANDBOX_DIR / "user_agents.txt"),
            "filtered_domains": str(SANDBOX_DIR / "domains.txt"),
        },
        "webdriver": {
            "proxy": "",
            "auth": True,
            "incognito": False,
            "country_domain": False,
            "language_from_proxy": False,
            "ss_on_exception": False,
            "window_size": "",
            "shift_windows": False,
            "use_seleniumbase": False,
        },
        "behavior": {
            "query": "",
            "ad_page_min_wait": 10,
            "ad_page_max_wait": 15,
            "nonad_page_min_wait": 15,
            "nonad_page_max_wait": 20,
            "max_scroll_limit": 0,
            "check_shopping_ads": True,
            "excludes": "",
            "random_mouse": False,
            "custom_cookies": False,
            "click_order": 5,
            "browser_count": 2,
            "multiprocess_style": 1,
            "loop_wait_time": 60,
            "wait_factor": 0.02,
            "running_interval_start": "00:00",
            "running_interval_end": "00:00",
            "2captcha_apikey": "",
            "hooks_enabled": False,
            "telegram_enabled": False,
            "send_to_android": False,
            "request_boost": False,
        },
    }


SANDBOX_QUERIES = "wireless keyboard\nbluetooth headphones\n"
SANDBOX_PROXIES = "127.0.0.1:8080\nuser:pass@10.0.0.1:3128\n"
SANDBOX_USER_AGENTS = "\n".join(
    [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/119.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) Chrome/118.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Linux; Android 13) Chrome/117.0.0.0 Safari/537.36",
    ]
)
SANDBOX_DOMAINS = "www.booking.com\nwww.sixt.com\n"
# Управляемый файл локалей: первая кодовая страна определяет --lang и заголовок.
SANDBOX_LOCALES = {
    "US": ["en-US", "en"],
    "DE": ["de-DE", "en-US"],
    "AF": ["ps-AF", "uz-AF"],
}
# Управляемый маппинг кодовых стран на google-домены (search_controller._set_start_url).
SANDBOX_DOMAIN_MAPPING = {
    "DE": "www.google.de",
    "US": "www.google.com",
    "AF": "www.google.com.af",
}
# Cookies покрывают все ветви нормализации sameSite в utils.add_cookies.
SANDBOX_COOKIES = [
    {"name": "strict_one", "sameSite": "strict", "secure": True},
    {"name": "lax_one", "sameSite": "lax", "secure": False},
    {"name": "none_secure", "sameSite": "no_restriction", "secure": True},
    {"name": "none_insecure", "sameSite": "whatever", "secure": False},
]


def _populate_sandbox() -> None:
    """Наполнить песочницу файлами данных, нужными legacy-модулям при импорте."""

    (SANDBOX_DIR / "config.json").write_text(
        json.dumps(_sandbox_config(), indent=4), encoding="utf-8"
    )
    (SANDBOX_DIR / "queries.txt").write_text(SANDBOX_QUERIES, encoding="utf-8")
    (SANDBOX_DIR / "proxies.txt").write_text(SANDBOX_PROXIES, encoding="utf-8")
    (SANDBOX_DIR / "user_agents.txt").write_text(SANDBOX_USER_AGENTS, encoding="utf-8")
    (SANDBOX_DIR / "domains.txt").write_text(SANDBOX_DOMAINS, encoding="utf-8")
    (SANDBOX_DIR / "country_to_locale.json").write_text(
        json.dumps(SANDBOX_LOCALES), encoding="utf-8"
    )
    (SANDBOX_DIR / "domain_mapping.json").write_text(
        json.dumps(SANDBOX_DOMAIN_MAPPING), encoding="utf-8"
    )
    (SANDBOX_DIR / "cookies.txt").write_text(json.dumps(SANDBOX_COOKIES), encoding="utf-8")


_populate_sandbox()

# Список legacy-модулей, которые читают или пишут файлы в cwd на этапе импорта.
# Список явный: если тест начнёт импортировать другой legacy-модуль, его нужно
# вписать сюда, иначе модуль загрузится с настоящим config.json из корня.
LEGACY_MODULES = (
    "ad_clicker",
    "clicklogs_db",
    "config_reader",
    "geolocation_db",
    "logger",
    "proxy",
    "run_in_loop",
    "search_controller",
    "stats",
    "utils",
    "webdriver",
)


def pytest_configure(config) -> None:
    """Импортировать legacy-модули из песочницы и вернуть cwd в корень репозитория.

    Импорт сделан в хуке, а не на верхнем уровне conftest: на верхнем уровне
    pytest ещё не разрешил ``testpaths``, а они относительные и считаются от
    текущего каталога, поэтому chdir в песочницу приводил бы к падению поиска
    тестов. К моменту ``pytest_configure`` ``tests/conftest.py`` уже загружен
    (pytest берёт initial conftests из testpaths), но ни один тест ещё не
    импортирован.
    """

    os.chdir(SANDBOX_DIR)
    try:
        for name in LEGACY_MODULES:
            __import__(name)
    finally:
        os.chdir(REPO_ROOT)


@pytest.fixture(scope="session", autouse=True)
def _remove_sandbox() -> None:
    """Удалить песочницу после всей сессии."""

    yield
    os.chdir(REPO_ROOT)
    shutil.rmtree(SANDBOX_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    """Выполнять каждый тест в отдельном tmp_path с файлами данных.

    Пути из config.json абсолютные, поэтому перенос cwd не ломает чтение
    queries/proxies/user_agents/domains, а вот БД и отчёты, которые legacy-код
    создаёт по относительному пути, гарантированно остаются внутри tmp_path.
    """

    (tmp_path / "country_to_locale.json").write_text(
        json.dumps(SANDBOX_LOCALES), encoding="utf-8"
    )
    (tmp_path / "domain_mapping.json").write_text(
        json.dumps(SANDBOX_DOMAIN_MAPPING), encoding="utf-8"
    )
    (tmp_path / "cookies.txt").write_text(json.dumps(SANDBOX_COOKIES), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def sandbox_dir() -> Path:
    """Каталог песочницы, созданный на этапе сбора тестов."""

    return SANDBOX_DIR


@pytest.fixture
def config():
    """Глобальный синглтон конфигурации, который импортируют legacy-модули."""

    return sys.modules["config_reader"].config


@pytest.fixture
def set_paths(monkeypatch):
    """Точечно подменить значения секции paths глобального конфига.

    Использует monkeypatch, поэтому изменения откатываются после теста и тесты
    не зависят от порядка выполнения.
    """

    paths = sys.modules["config_reader"].config.paths

    def _set(**values):
        for name, value in values.items():
            monkeypatch.setattr(paths, name, value)

    return _set


@pytest.fixture
def behavior(config):
    """Секция behavior глобального конфига (значения патчатся и откатываются)."""

    return config.behavior


@pytest.fixture
def base_config():
    """Копия валидного config.json, которую тест может править под себя."""

    return _sandbox_config()


@pytest.fixture
def make_config(tmp_path, monkeypatch):
    """Фабрика ConfigReader, читающего конфиг из tmp_path.

    Возвращает новый экземпляр ``ConfigReader`` (не глобальный синглтон), чтобы
    тесты не зависели от порядка выполнения.
    """

    def _make(cfg=None, raw=None):
        monkeypatch.chdir(tmp_path)
        payload = raw if raw is not None else json.dumps(cfg if cfg is not None else _sandbox_config())
        (tmp_path / "config.json").write_text(payload, encoding="utf-8")

        reader = sys.modules["config_reader"].ConfigReader()
        reader.read_parameters()
        return reader

    return _make


# --- Минимальные тестовые двойники Selenium ---------------------------------------


@dataclass
class FakeElement:
    """Замена WebElement: отдаёт заранее заданные атрибуты и текст.

    Двойник не подменяет логику: он только возвращает данные, которые
    webdriver.py/search_controller.py читали бы у настоящего DOM-элемента.
    """

    attributes: dict = field(default_factory=dict)
    text: str = ""
    svg_count: int = 0
    recorded_keys: list = field(default_factory=list)

    def get_attribute(self, name):
        return self.attributes.get(name)

    def find_element(self, by, value=None):
        # Заголовок объявления: у реального элемента это один вложенный элемент.
        return SimpleNamespace(text=self.text)

    def find_elements(self, by, value=None):
        return [SimpleNamespace() for _ in range(self.svg_count)]

    def send_keys(self, keys):
        self.recorded_keys.append(keys)

    def click(self):
        return None


class FakeDriver:
    """Замена webdriver: фиксирует переходы, ничего не открывая."""

    def __init__(self, links=None):
        self.visited = []
        self.scripts = []
        self.cookies_deleted = 0
        self._links = list(links or [])

    def get(self, url):
        self.visited.append(url)

    def find_elements(self, by, value=None):
        return list(self._links)

    def find_element(self, by, value=None):
        return FakeElement()

    def execute_script(self, script, *args):
        self.scripts.append(script)

    def delete_all_cookies(self):
        self.cookies_deleted += 1

    def quit(self):
        self.visited.append("quit")


class RecordingCookieDriver:
    """Замена webdriver для add_cookies: только собирает переданные cookies."""

    def __init__(self):
        self.cookies = []

    def add_cookie(self, cookie):
        self.cookies.append(cookie)


@pytest.fixture
def recording_cookie_driver():
    """Драйвер, накапливающий cookies вместо браузера."""

    return RecordingCookieDriver()


@pytest.fixture
def make_search_controller():
    """Фабрика SearchController с фальшивым драйвером (без браузера и сети)."""

    controller_class = sys.modules["search_controller"].SearchController

    def _make(query="wireless keyboard", country_code=None, driver=None):
        return controller_class(driver or FakeDriver(), query, country_code)

    return _make


@pytest.fixture
def make_db():
    """Фабрика экземпляров ClickLogsDB: своя БД в tmp_path на каждый тест."""

    def _make():
        from clicklogs_db import ClickLogsDB

        return ClickLogsDB()

    return _make


@pytest.fixture
def make_geolocation_db():
    """Фабрика экземпляров GeolocationDB: своя БД в tmp_path на каждый тест."""

    def _make():
        from geolocation_db import GeolocationDB

        return GeolocationDB()

    return _make


@pytest.fixture
def restore_logging():
    """Откатить глобальные изменения обработчиков/форматтеров логгера."""

    log = sys.modules["logger"].logger
    handlers = list(log.handlers)
    states = [(handler, handler.formatter, list(handler.filters)) for handler in handlers]

    yield

    log.handlers[:] = handlers
    for handler, formatter, filters in states:
        handler.formatter = formatter
        handler.filters[:] = filters
