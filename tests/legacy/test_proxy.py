"""Тесты proxy.py: чтение файла прокси и генерация расширения для авторизации.

proxy.py не разбирает строку прокси — это делает webdriver.create_webdriver
(см. test_webdriver.py). Здесь покрыты обе чистые функции модуля: чтение файла
и запись Chrome-расширения с кредентами.
"""

import json

import pytest
from selenium.webdriver import ChromeOptions

import proxy


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


def test_get_proxies_exits_when_file_is_missing(set_paths, tmp_path):
    set_paths(proxy_file=tmp_path / "nope.txt")

    with pytest.raises(SystemExit) as excinfo:
        proxy.get_proxies()

    assert "Couldn't find proxy file" in str(excinfo.value)


# --- install_plugin ---------------------------------------------------------------


def test_install_plugin_creates_extension_files(isolated_cwd):
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "user", "pass", "abcde")

    plugin_dir = isolated_cwd / "proxy_auth_plugin" / "abcde"
    assert (plugin_dir / "manifest.json").is_file()
    assert (plugin_dir / "background.js").is_file()


def test_install_plugin_writes_valid_manifest_v3(isolated_cwd):
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "user", "pass", "abcde")

    manifest = json.loads(
        (isolated_cwd / "proxy_auth_plugin" / "abcde" / "manifest.json").read_text("utf-8")
    )
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
    assert manifest["minimum_chrome_version"] == "108"


def test_install_plugin_substitutes_host_and_port_into_background_js(isolated_cwd):
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "user", "pass", "abcde")

    background = (isolated_cwd / "proxy_auth_plugin" / "abcde" / "background.js").read_text("utf-8")
    assert 'host: "10.0.0.1"' in background
    assert "port: 3128" in background
    assert 'scheme: "http"' in background


def test_install_plugin_substitutes_credentials_into_background_js(isolated_cwd):
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "user", "pass", "abcde")

    background = (isolated_cwd / "proxy_auth_plugin" / "abcde" / "background.js").read_text("utf-8")
    assert 'username: "user"' in background
    assert 'password: "pass"' in background


def test_install_plugin_adds_load_extension_argument(isolated_cwd):
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "user", "pass", "abcde")

    expected = f"--load-extension={(isolated_cwd / 'proxy_auth_plugin' / 'abcde').resolve()}"
    assert expected in options.arguments


def test_install_plugin_separates_extensions_by_folder_name(isolated_cwd):
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "user", "pass", "first")
    proxy.install_plugin(options, "10.0.0.2", 8080, "user2", "pass2", "second")

    root = isolated_cwd / "proxy_auth_plugin"
    assert (root / "first" / "background.js").is_file()
    assert (root / "second" / "background.js").is_file()
    first = (root / "first" / "background.js").read_text("utf-8")
    second = (root / "second" / "background.js").read_text("utf-8")
    assert 'host: "10.0.0.1"' in first
    assert 'host: "10.0.0.2"' in second


def test_install_plugin_does_not_leak_password_into_extension_path(isolated_cwd):
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "user", "secret-password", "abcde")

    argument = [arg for arg in options.arguments if arg.startswith("--load-extension=")][0]
    assert "secret-password" not in argument


@pytest.mark.xfail(
    reason=(
        "Баг legacy-кода: install_plugin кладёт логин и пароль открытым текстом "
        "в background.js на диске и никогда не удаляет файл - креды прокси "
        "остаются лежать в каталоге проекта после работы"
    ),
    strict=True,
)
def test_install_plugin_should_not_write_credentials_to_disk(isolated_cwd):
    options = ChromeOptions()

    proxy.install_plugin(options, "10.0.0.1", 3128, "user", "secret-password", "abcde")

    background = (isolated_cwd / "proxy_auth_plugin" / "abcde" / "background.js").read_text("utf-8")
    assert "secret-password" not in background
