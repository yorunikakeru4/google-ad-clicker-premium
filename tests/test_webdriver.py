"""Тесты webdriver.create_webdriver: валидация формата строки прокси.

Проверяется только ветка, которая срабатывает ДО создания Chrome: неверный
формат прокси в режиме авторизации. Браузер, сеть и geolocation-lookup в этих
тестах не задействованы - тест падает раньше, чем код до них дойдёт, а
отсутствие побочных файлов это подтверждает.
"""

import tempfile

import pytest

import webdriver


@pytest.fixture
def isolated_tempdir(tmp_path, monkeypatch):
    """Увести каталог профилей Chrome из /tmp в tmp_path."""

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


@pytest.fixture
def no_browser(monkeypatch):
    """Страховка: если проверка формата прокса исчезнет, тест упадёт, а не запустит Chrome.

    Подменяется только точка создания драйвера; на проверяемое поведение
    (ValueError до старта браузера) это не влияет.
    """

    def explode(*args, **kwargs):
        raise AssertionError("Chrome не должен запускаться в этом тесте")

    monkeypatch.setattr(webdriver, "CustomChrome", explode)


@pytest.mark.parametrize(
    "invalid_proxy",
    [
        "127.0.0.1:8080",  # нет логина/пароля, хотя webdriver.auth = true
        "user:pass@10.0.0.1",  # нет порта
        "user:pass@10.0.0.1:3128:extra",  # лишнее поле
        "10.0.0.1:3128:user:pass@extra:80",  # три двоеточия
    ],
)
def test_create_webdriver_rejects_malformed_authenticated_proxy(
    isolated_tempdir, no_browser, invalid_proxy
):
    with pytest.raises(ValueError) as excinfo:
        webdriver.create_webdriver(invalid_proxy, "Mozilla/5.0", "abcde")

    assert "Invalid proxy format!" in str(excinfo.value)
    assert "username:password@host:port" in str(excinfo.value)


def test_create_webdriver_rejects_proxy_without_credentials_before_any_side_effect(
    isolated_tempdir,
    no_browser,
):
    with pytest.raises(ValueError):
        webdriver.create_webdriver("127.0.0.1:8080", "Mozilla/5.0", "abcde")

    # Ни расширение, ни флаг многопроцессности не должны появиться до проверки.
    assert not (isolated_tempdir / "proxy_auth_plugin").exists()
    assert not (isolated_tempdir / ".MULTI_BROWSERS_IN_USE").exists()


def test_create_webdriver_validation_message_mentions_supported_format(
    isolated_tempdir, no_browser
):
    with pytest.raises(ValueError) as excinfo:
        webdriver.create_webdriver("nonsense", "Mozilla/5.0", "abcde")

    assert str(excinfo.value) == (
        "Invalid proxy format! Should be in 'username:password@host:port' format"
    )
