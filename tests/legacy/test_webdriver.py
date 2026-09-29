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


def _bare_chrome():
    """Экземпляр CustomChrome без запуска браузера: только поля, которые трогает quit()."""

    driver = object.__new__(webdriver.CustomChrome)
    driver.browser_pid = 999999999
    driver.service = None
    driver.reactor = None
    driver.keep_user_data_dir = True
    return driver


def test_quit_does_not_raise_when_browser_kill_fails_unexpectedly(monkeypatch):
    """quit() обязан завершаться, даже если os.kill упал нештатно."""

    driver = _bare_chrome()

    def boom(*args, **kwargs):
        raise ValueError("unexpected kill failure")

    monkeypatch.setattr(webdriver.os, "kill", boom)
    monkeypatch.setattr(webdriver.log, "debug", lambda *args, **kwargs: None)

    driver.quit()


def test_quit_logs_swallowed_kill_error_instead_of_silencing_it(monkeypatch):
    """Проглоченная при завершении ошибка должна попадать в debug-лог, а не в /dev/null."""

    driver = _bare_chrome()
    records = []

    def boom(*args, **kwargs):
        raise ValueError("unexpected kill failure")

    monkeypatch.setattr(webdriver.os, "kill", boom)
    monkeypatch.setattr(webdriver.log, "debug", lambda *args, **kwargs: records.append((args, kwargs)))

    driver.quit()

    assert records, "проглоченная ошибка нигде не залогирована"
    assert any("unexpected kill failure" in str(args) for args, _ in records)
    assert any(call[1].get("exc_info") for call in records)


class TestMultiProcsMarker:
    """Признак многопроцессного запуска.

    Раньше его нёс файл ``.MULTI_BROWSERS_IN_USE``, создававшийся
    ``run_ad_clicker.py``. Точка входа удалена, файл больше не создаётся, и
    оставленная на диске копия от прошлой версии не должна была бы включать
    многопроцессный путь одиночному ``ad_clicker.py`` — поэтому источником
    признака осталось только окружение супервизора.
    """

    def test_marker_is_off_by_default(self, monkeypatch):
        monkeypatch.delenv(webdriver.MULTI_BROWSERS_ENV, raising=False)

        assert webdriver.is_multi_procs_enabled() is False

    def test_env_from_supervisor_enables_marker(self, monkeypatch):
        monkeypatch.setenv(webdriver.MULTI_BROWSERS_ENV, "1")

        assert webdriver.is_multi_procs_enabled() is True

    def test_env_other_than_one_keeps_marker_off(self, monkeypatch):
        monkeypatch.setenv(webdriver.MULTI_BROWSERS_ENV, "0")

        assert webdriver.is_multi_procs_enabled() is False

    def test_leftover_marker_file_is_ignored(self, monkeypatch, tmp_path):
        """Мусор прошлой версии не должен менять поведение одиночного запуска."""
        monkeypatch.delenv(webdriver.MULTI_BROWSERS_ENV, raising=False)
        (tmp_path / ".MULTI_BROWSERS_IN_USE").touch()

        assert webdriver.is_multi_procs_enabled() is False
