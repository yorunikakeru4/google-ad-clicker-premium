"""Тесты proxy.py: чтение файла прокси и генерация расширения для авторизации.

proxy.py не разбирает строку прокси — это делает webdriver.create_webdriver
(см. test_webdriver.py). Здесь покрыты обе чистые функции модуля: чтение файла
и запись Chrome-расширения, а также то, что креды прокси не попадают на диск.
"""

import http.client
import json
import re
import tempfile
import time
import urllib.request

import pytest
from selenium.webdriver import ChromeOptions

import proxy


CREDENTIALS_ENDPOINT_PATTERN = re.compile(r"http://127\.0\.0\.1:(\d+)/credentials")


@pytest.fixture(autouse=True)
def closed_credentials_services():
    """Закрывать endpoint'ы кредов, поднятые install_plugin, вместе с тестом.

    install_plugin открывает loopback-сервис на каждый вызов, поэтому без этой
    фикстуры сокеты жили бы до конца сессии pytest.
    """

    yield
    proxy._close_credentials_services()


def credentials_port(background_js: str) -> int:
    """Достать порт endpoint'а, с которого background.js забирает креды."""

    match = CREDENTIALS_ENDPOINT_PATTERN.search(background_js)

    assert match, f"В background.js нет endpoint'а с кредами:\n{background_js}"

    return int(match.group(1))


def read_credentials(port: int) -> dict:
    """Прочитать креды так же, как это делает service worker расширения."""

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/credentials", timeout=2) as response:
        return json.loads(response.read())


def read_extension(isolated_cwd, plugin_folder_name: str, filename: str) -> str:
    """Прочитать файл сгенерированного расширения."""

    return (isolated_cwd / "proxy_auth_plugin" / plugin_folder_name / filename).read_text("utf-8")


# --- get_proxies ------------------------------------------------------------------


def test_get_proxies_returns_cleaned_lines(set_paths, tmp_path):
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text("127.0.0.1:8080\n user:pass@10.0.0.1:3128 \n", encoding="utf-8")
    set_paths(proxy_file=proxy_file)

    assert proxy.get_proxies() == ["127.0.0.1:8080", "user:pass@10.0.0.1:3128"]


def test_get_proxies_removes_wrapping_quotes(set_paths, tmp_path):
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text("'127.0.0.1:8080'\n\"user:pass@10.0.0.1:3128\"\n", encoding="utf-8")
    set_paths(proxy_file=proxy_file)

    assert proxy.get_proxies() == ["127.0.0.1:8080", "user:pass@10.0.0.1:3128"]


def test_get_proxies_strips_whitespace_only_from_line_edges(set_paths, tmp_path):
    # Внутренние пробелы сохраняются: это часть формата "логин:пароль@хост:порт".
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text("\tuser name:pa ss@10.0.0.1:3128\t\n", encoding="utf-8")
    set_paths(proxy_file=proxy_file)

    assert proxy.get_proxies() == ["user name:pa ss@10.0.0.1:3128"]


def test_get_proxies_returns_empty_list_for_empty_file(set_paths, tmp_path):
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text("", encoding="utf-8")
    set_paths(proxy_file=proxy_file)

    assert proxy.get_proxies() == []


def test_get_proxies_skips_blank_lines(set_paths, tmp_path):
    # Тот же класс бага, что и в queries.txt: пустая строка стала бы пустым прокси.
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text("127.0.0.1:8080\n\n  \n''\nuser:pass@10.0.0.1:3128\n", encoding="utf-8")
    set_paths(proxy_file=proxy_file)

    assert proxy.get_proxies() == ["127.0.0.1:8080", "user:pass@10.0.0.1:3128"]


def test_get_proxies_exits_when_file_is_missing(set_paths, tmp_path):
    set_paths(proxy_file=tmp_path / "nope.txt")

    with pytest.raises(SystemExit) as excinfo:
        proxy.get_proxies()

    assert "Couldn't find proxy file" in str(excinfo.value)


# --- install_plugin ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def plugin_dirs_under_isolated_cwd(isolated_cwd, monkeypatch):
    """Класть расширения в isolated_cwd вместо системного tempdir.

    По умолчанию install_plugin пишет в системный tempdir, чтобы не мусорить
    в cwd. Старые тесты читают файлы через isolated_cwd, поэтому шов
    _plugins_base_dir перенаправляется одной фикстурой, а не правкой 13
    вызовов. Настоящее поведение шва проверяют новые тесты ниже — они
    вызывают его напрямую, без этой подмены.
    """

    monkeypatch.setattr(
        proxy,
        "_plugins_base_dir",
        lambda plugins_dir=None: isolated_cwd / "proxy_auth_plugin",
    )


def test_install_plugin_creates_extension_files(isolated_cwd):
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "user", "pass", "abcde")

    plugin_dir = isolated_cwd / "proxy_auth_plugin" / "abcde"
    assert (plugin_dir / "manifest.json").is_file()
    assert (plugin_dir / "background.js").is_file()


def test_install_plugin_writes_valid_manifest_v3(isolated_cwd):
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "user", "pass", "abcde")

    manifest = json.loads(read_extension(isolated_cwd, "abcde", "manifest.json"))
    assert manifest["manifest_version"] == 3
    assert manifest["permissions"] == [
        "proxy",
        "tabs",
        "unlimitedStorage",
        "storage",
        "webRequest",
        "webRequestAuthProvider",
    ]
    assert manifest["host_permissions"] == ["<all_urls>"]
    # 120 — минимальная версия Chrome с asyncBlocking у onAuthRequired.
    assert manifest["minimum_chrome_version"] == "120"


def test_install_plugin_substitutes_host_and_port_into_background_js(isolated_cwd):
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "user", "pass", "abcde")

    background = read_extension(isolated_cwd, "abcde", "background.js")
    # PAC вместо fixed_servers: прокси только на домены Google.
    assert "FindProxyForURL" in background
    assert "PROXY 10.0.0.1:3128" in background
    assert 'mode: "pac_script"' in background


def test_install_plugin_pac_keeps_credentials_out_of_the_script(isolated_cwd):
    # В PAC адрес обязан быть без кредов: логин/пароль в аргументах запуска или в
    # скрипте расширения читаются из списка процессов и из самого профиля.
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "proxy-login-42", "secret-password", "abcde")

    background = read_extension(isolated_cwd, "abcde", "background.js")
    pac_data = json.loads(re.search(r"data: (\".*?\")\n", background).group(1))
    assert "proxy-login-42" not in pac_data
    assert "secret-password" not in pac_data
    assert "PROXY 10.0.0.1:3128" in pac_data


def test_install_plugin_answers_auth_challenges_from_session_storage(isolated_cwd):
    # Креды приходят в браузер по одноразовому endpoint'у и остаются в
    # chrome.storage.session, а обработчик авторизации берёт их оттуда.
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "user", "pass", "abcde")

    background = read_extension(isolated_cwd, "abcde", "background.js")
    assert "['asyncBlocking']" in background
    assert "chrome.storage.session.set" in background
    assert "chrome.storage.session.get" in background


def test_install_plugin_keeps_credentials_out_of_extension_files(isolated_cwd):
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "proxy-login-42", "secret-password", "abcde")

    background = read_extension(isolated_cwd, "abcde", "background.js")
    manifest = read_extension(isolated_cwd, "abcde", "manifest.json")
    assert "proxy-login-42" not in background
    assert "secret-password" not in background
    assert "proxy-login-42" not in manifest
    assert "secret-password" not in manifest


def test_install_plugin_adds_load_extension_argument(isolated_cwd):
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "user", "pass", "abcde")

    expected = f"--load-extension={(isolated_cwd / 'proxy_auth_plugin' / 'abcde').resolve()}"
    assert expected in options.arguments


def test_install_plugin_separates_extensions_by_folder_name(isolated_cwd):
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 8080, "user2", "pass2", "first")
    proxy.install_plugin(options, "10.0.0.2", 8080, "user3", "pass3", "second")

    first = read_extension(isolated_cwd, "first", "background.js")
    second = read_extension(isolated_cwd, "second", "background.js")
    assert "PROXY 10.0.0.1:8080" in first
    assert "PROXY 10.0.0.2:8080" in second


def test_install_plugin_does_not_leak_password_into_extension_path(isolated_cwd):
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "user", "secret-password", "abcde")

    argument = [arg for arg in options.arguments if arg.startswith("--load-extension=")][0]
    assert "secret-password" not in argument


# --- Выдача кредов расширению -----------------------------------------------------


def test_install_plugin_serves_credentials_from_memory(isolated_cwd):
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "user", "secret-password", "abcde")

    background = read_extension(isolated_cwd, "abcde", "background.js")
    assert read_credentials(credentials_port(background)) == {
        "username": "user",
        "password": "secret-password",
    }


def test_install_plugin_serves_each_extension_its_own_credentials(isolated_cwd):
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "first-login", "first-password", "first")
    proxy.install_plugin(options, "10.0.0.2", 3128, "second-login", "second-password", "second")

    first_port = credentials_port(read_extension(isolated_cwd, "first", "background.js"))
    second_port = credentials_port(read_extension(isolated_cwd, "second", "background.js"))
    assert first_port != second_port
    assert read_credentials(first_port)["password"] == "first-password"
    assert read_credentials(second_port)["password"] == "second-password"


def test_install_plugin_credentials_endpoint_answers_only_once(isolated_cwd):
    # Креды забираются один раз и живут в памяти браузера, поэтому после первого
    # ответа endpoint закрывается и второй раз их не отдаёт.
    options = ChromeOptions()
    proxy.install_plugin(options, "10.0.0.1", 3128, "user", "secret-password", "abcde")

    port = credentials_port(read_extension(isolated_cwd, "abcde", "background.js"))
    assert read_credentials(port)["password"] == "secret-password"

    deadline = time.monotonic() + 5

    while time.monotonic() < deadline:
        try:
            read_credentials(port)
        except (OSError, http.client.HTTPException):
            break
        time.sleep(0.05)
    else:
        pytest.fail("Endpoint продолжал отдавать креды после первого запроса")


def test_close_credentials_services_stops_endpoints_nobody_read(isolated_cwd):
    # Расширение могло не стартовать: незабранные креды не должны остаться
    # слушающими сокет до конца процесса.
    options = ChromeOptions()
    proxy.install_plugin(options, "10.0.0.1", 3128, "user", "secret-password", "abcde")

    port = credentials_port(read_extension(isolated_cwd, "abcde", "background.js"))

    proxy._close_credentials_services()

    with pytest.raises(OSError):
        read_credentials(port)


# --- расположение расширений: tempdir вместо cwd -----------------------------------
#
# Настоящий шов _plugins_base_dir захвачен на уровне модуля, до того как
# фикстура plugin_dirs_under_isolated_cwd его подменит: monkeypatch действует
# только внутри тестов, поэтому здесь лежит оригинал.


_real_plugins_base_dir = proxy._plugins_base_dir


def test_default_plugins_dir_is_system_tempdir_not_cwd(tmp_path, monkeypatch):
    # Расширения больше не мусорят в cwd: по умолчанию это системный tempdir.
    monkeypatch.chdir(tmp_path)

    created = _real_plugins_base_dir()
    try:
        assert tempfile.gettempdir() in [str(p) for p in created.parents]
        assert tmp_path not in created.parents
        assert created.is_dir()
    finally:
        proxy._cleanup_auto_plugin_dirs()


def test_explicit_plugins_dir_is_honoured(tmp_path):
    # Явно переданный каталог используется как есть — для тестов и диагностики.
    wanted = tmp_path / "custom-plugins"

    assert _real_plugins_base_dir(wanted) == wanted


def test_cleanup_removes_only_auto_created_dirs(tmp_path):
    # Чистка при выходе не должна трогать каталог, переданный явно.
    auto = _real_plugins_base_dir()
    manual = tmp_path / "manual"
    manual.mkdir()
    _real_plugins_base_dir(manual)

    proxy._cleanup_auto_plugin_dirs()

    assert not auto.exists(), "автосозданный tempdir должен быть удалён"
    assert manual.is_dir(), "явно переданный каталог трогать нельзя"


# --- PAC: разделение трафика (прокси только на Google) ------------------------
#
# Скрипт проверяется как текст, а не исполнением: Chromium выполняет PAC через
# V8, и подставлять node в юнит-тест нельзя — его нет в сборочном окружении.
# Исполняемую проверку (PAC реально рулит маршрутом в живом Chromium) даёт
# e2e, а здесь контракт: какие домены в списке, куда уходит локаль и что в
# скрипте не оказалось кредов.


@pytest.fixture
def pac():
    return proxy.build_pac_script("10.0.0.1:3128")


def test_pac_is_a_proxy_autoconfig_function(pac):
    assert pac.startswith("function FindProxyForURL(url, host) {")


def test_pac_sends_google_hosts_to_the_proxy(pac):
    for domain in proxy.GOOGLE_PROXY_HOSTS:
        assert f"host.endsWith('.{domain}') || host === '{domain}'" in pac, (
            f"{domain} должен уходить через прокси и как сам домен, и как поддомен"
        )


def test_pac_keeps_www_google_in_the_proxy(pac):
    # Поддоменная проверка обязательна для каждого домена, а не только для
    # первого: поисковая выдача приходит с www.google.<cc>.
    assert "host.endsWith('.google.com')" in pac


def test_pac_covers_national_google_domains(pac):
    # Прокси выдаётся для любой страны, а не только под .com: без ccTLD-правила
    # не-американский прокси отправил бы поиск в DIRECT, и клики ушли бы с
    # реального адреса машины.
    assert proxy.GOOGLE_CCTLD_MATCHER in pac
    assert "[a-z]{2,3}" in pac


def test_pac_matches_cc_tld_domains_anchored(pac):
    # Якорь по всей строке: вариант «хост начинается с google.» пропускал бы в
    # прокси чужие google.evil.net и google.com.evil.net.
    assert "^" in proxy.GOOGLE_CCTLD_MATCHER
    assert "$" in proxy.GOOGLE_CCTLD_MATCHER
    assert "startsWith" not in proxy.GOOGLE_CCTLD_MATCHER


def test_pac_lowercases_host_before_comparing(pac):
    # Регистр хоста не от набора пользователем запроса, а приходит из URL.
    assert "host = host.toLowerCase();" in pac


def test_pac_routes_localhost_direct_before_the_proxy(pac):
    # Локаль проверяется раньше прокси: сам PAC и credentials отдаются через
    # loopback, и уход этого запроса в прокси означал бы круговую зависимость.
    assert "host === 'localhost'" in pac
    assert pac.index("'localhost'") < pac.index("PROXY 10.0.0.1:3128")


def test_pac_falls_back_to_direct(pac):
    assert pac.rstrip().endswith("}")


def test_pac_has_no_credentials():
    # build_pac_script принимает адрес без кредов, а и расширение, и эндпоинт
    # отдают ровно этот текст: пароля в браузере оказаться не может.
    script = proxy.build_pac_script("10.0.0.1:3128")
    assert "@" not in script


# --- PAC endpoint для --proxy-pac-url ------------------------------------------


@pytest.fixture(autouse=True)
def closed_pac_services():
    """Закрывать PAC-endpoint'ы, поднятые open_pac_service, вместе с тестом."""

    yield
    proxy._close_pac_services()


def test_open_pac_service_returns_http_url_on_loopback():
    # Именно http, а не file://: Chromium молча игнорирует file:// в
    # --proxy-pac-url и уходит в DIRECT (проверено на живом 154), то есть
    # трафик Google пошёл бы с реального адреса машины вместо прокси.
    url = proxy.open_pac_service("10.0.0.1:3128")

    assert url.startswith("http://127.0.0.1:")
    assert url.endswith("/proxy.pac")


def test_open_pac_service_serves_the_script():
    url = proxy.open_pac_service("10.0.0.1:3128")

    with urllib.request.urlopen(url, timeout=5) as response:
        served = response.read().decode("utf-8")

    assert "FindProxyForURL" in served
    assert "PROXY 10.0.0.1:3128" in served


def test_open_pac_service_serves_repeatedly():
    # В отличие от endpoint'а с кредами PAC перечитывается на каждом новом
    # хосте, поэтому закрываться после первого запроса он не должен.
    url = proxy.open_pac_service("10.0.0.1:3128")

    for _ in range(3):
        with urllib.request.urlopen(url, timeout=5) as response:
            assert b"FindProxyForURL" in response.read()


def test_open_pac_service_answers_with_the_proxy_autoconfig_type():
    url = proxy.open_pac_service("10.0.0.1:3128")

    with urllib.request.urlopen(url, timeout=5) as response:
        content_type = response.headers.get("Content-Type")

    assert content_type == "application/x-ns-proxy-autoconfig"


def test_close_pac_services_stops_the_endpoint():
    url = proxy.open_pac_service("10.0.0.1:3128")

    proxy._close_pac_services()

    with pytest.raises(OSError):
        urllib.request.urlopen(url, timeout=2)


def test_close_pac_service_stops_only_its_own_endpoint():
    # Воркер держит по одному PAC на живой браузер: закрытие одного не должно
    # рвать скрипт у соседа — иначе тот молча ушёл бы в DIRECT.
    closing = proxy.open_pac_service("10.0.0.1:3128")
    living = proxy.open_pac_service("10.0.0.2:3128")

    proxy.close_pac_service(closing)

    with pytest.raises(OSError):
        urllib.request.urlopen(closing, timeout=2)
    with urllib.request.urlopen(living, timeout=5) as response:
        assert b"PROXY 10.0.0.2:3128" in response.read()


def test_close_pac_service_ignores_an_unknown_url():
    # quit() драйвера не должен падать: URL мог быть закрыт уже этим же quit.
    proxy.open_pac_service("10.0.0.1:3128")

    proxy.close_pac_service("http://127.0.0.1:1/proxy.pac")


def test_two_services_get_two_ports():
    first = proxy.open_pac_service("10.0.0.1:3128")
    second = proxy.open_pac_service("10.0.0.1:3128")

    assert first != second
