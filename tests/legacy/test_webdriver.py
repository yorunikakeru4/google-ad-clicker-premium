"""Тесты webdriver: валидация строки прокси и выбор транспорта авторизации.

Проверяется ветка, которая срабатывает ДО создания Chrome (неверный формат
прокси в режиме авторизации), и то, какой набор аргументов уходит в драйвер
для каждого значения ``webdriver.proxy_transport``. Браузер, сеть и
geolocation-lookup не задействованы: драйвер создаётся на заглушках, поэтому
тест падает раньше, чем код до них дойдёт, а отсутствие побочных файлов это
подтверждает.
"""

import sys
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


# --- Транспорты прокси: cdp_auth | extension | direct --------------------------

PROXY = "user:pass@10.0.0.1:3128"
PROXY_HOST_PORT = "10.0.0.1:3128"
PLAIN_PROXY = "10.0.0.1:8080"


class _LogRecorder:
    """Логгер-заглушка: пишет вызовы в список, в БД ничего не уходит."""

    def __init__(self) -> None:
        self.records: list[tuple[str, tuple, dict]] = []
        self.degraded: list[tuple[str, str | None]] = []

    def _record(self, level: str, *args, **kwargs) -> None:
        self.records.append((level, args, kwargs))

    def debug(self, *args, **kwargs) -> None:
        self._record("DEBUG", *args, **kwargs)

    def info(self, *args, **kwargs) -> None:
        self._record("INFO", *args, **kwargs)

    def warning(self, *args, **kwargs) -> None:
        self._record("WARNING", *args, **kwargs)

    def error(self, *args, **kwargs) -> None:
        self._record("ERROR", *args, **kwargs)

    def mark_degraded(self, reason, browser_id=None) -> None:
        self.degraded.append((reason, browser_id))

    @property
    def text(self) -> str:
        return str(self.records)

    @property
    def warnings(self) -> list[tuple[str, tuple, dict]]:
        return [record for record in self.records if record[0] == "WARNING"]


class _FakeManager:
    """Менеджер ProxyAuthManager: считает остановки."""

    def __init__(self) -> None:
        self.stopped = 0

    def stop(self) -> None:
        self.stopped += 1


class _FakeChrome:
    """CustomChrome без запуска браузера: аргументы, CDP-вызовы и quit."""

    def __init__(self, **kwargs) -> None:
        self.init_kwargs = kwargs
        self.options = kwargs["options"]
        self.quit_calls = 0
        self.cdp_calls: list[tuple[tuple, dict]] = []

    def set_window_size(self, width, height) -> None:
        pass

    def maximize_window(self) -> None:
        pass

    def execute_cdp_cmd(self, *args, **kwargs) -> None:
        self.cdp_calls.append((args, kwargs))

    def quit(self) -> None:
        self.quit_calls += 1


@pytest.fixture
def record_log(monkeypatch):
    """Логгер webdriver: вызовы записываются, в БД ничего не пишется."""

    recorder = _LogRecorder()
    monkeypatch.setattr(webdriver, "log", recorder)
    return recorder


@pytest.fixture
def fake_chrome(monkeypatch):
    """Точка создания драйвера вместо настоящего Chrome."""

    created: list[_FakeChrome] = []

    def factory(**kwargs):
        driver = _FakeChrome(**kwargs)
        created.append(driver)
        return driver

    monkeypatch.setattr(webdriver, "CustomChrome", factory)
    return created


@pytest.fixture
def proxy_auth_stub(monkeypatch):
    """create_proxy_auth: записывает вызовы и отдаёт менеджер-заглушку."""

    class Stub:
        def __init__(self) -> None:
            self.calls: list[dict] = []
            self.managers: list[_FakeManager] = []
            self.error: Exception | None = None

        def __call__(self, username, password, **kwargs):
            if self.error is not None:
                raise self.error
            self.calls.append({"username": username, "password": password, **kwargs})
            manager = _FakeManager()
            self.managers.append(manager)
            return manager

    stub = Stub()
    monkeypatch.setattr(webdriver, "create_proxy_auth", stub)
    return stub


@pytest.fixture
def install_plugin_stub(monkeypatch):
    """install_plugin: записывает вызовы, файлы и loopback-сервис не поднимаются."""

    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        webdriver, "install_plugin", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    return calls


@pytest.fixture
def no_geolocation(monkeypatch):
    """Без реального запроса геолокации: только готовые значения."""

    monkeypatch.setattr(
        webdriver, "get_location", lambda client, proxy: (None, None, "US", "Europe/Berlin")
    )


@pytest.fixture
def geolocated_proxy(monkeypatch):
    """Прокси с координатами: гео-локация и гео-часовой пояс без сети."""

    monkeypatch.setattr(
        webdriver,
        "get_location",
        lambda client, proxy: (52.52, 13.405, "DE", "Europe/Berlin"),
    )


@pytest.fixture
def assign_profile(tmp_path, monkeypatch):
    """Профиль в отдельной БД, назначенный процессу через env супервизора."""

    from engine.db import migrations
    from engine.profile_apply import PROFILE_ID_ENV
    from engine.profile_pool import ProfilePool

    db = tmp_path / "profiles.db"
    migrations.migrate(db)
    monkeypatch.setenv("ADCLICKER_DB", str(db))
    pool = ProfilePool(db)

    def _assign(**fields):
        pool.add_profiles([{"name": f"acc-{len(pool.list_profiles()) + 1}", **fields}])
        row = pool.list_profiles()[-1]
        monkeypatch.setenv(PROFILE_ID_ENV, str(row["id"]))
        return row["id"]

    return _assign


def _cdp_payloads(driver, command: str) -> list[dict]:
    """Полезные нагрузки всех вызовов данной CDP-команды.

    Драйвер вызывает ``execute_cdp_cmd(command, payload)`` позиционно, поэтому
    payload — второй элемент args, а не kwargs.
    """

    return [args[1] for args, _kwargs in driver.cdp_calls if args and args[0] == command]


@pytest.fixture
def transport(config, monkeypatch):
    """Задать ``webdriver.proxy_transport`` (и заодно ``auth``) на время теста."""

    def _set(value: str, *, auth: bool | None = None) -> None:
        monkeypatch.setattr(config.webdriver, "proxy_transport", value)
        if auth is not None:
            monkeypatch.setattr(config.webdriver, "auth", auth)

    return _set


def _proxy_args(driver) -> list[str]:
    return [a for a in driver.options.arguments if a.startswith("--proxy-server=")]


class TestProxyTransportOptions:
    """Какие аргументы уходят в Chrome и что поднимается для каждого транспорта."""

    def test_cdp_auth_passes_only_host_and_port_installing_no_plugin(
        self,
        isolated_tempdir,
        fake_chrome,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
    ):
        transport("cdp_auth")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        assert _proxy_args(driver) == [f"--proxy-server={PROXY_HOST_PORT}"]
        assert PROXY not in " ".join(driver.options.arguments)
        assert install_plugin_stub == []
        # Креды не должны утекать ни в аргументы Chrome, ни в логи.
        assert PROXY not in record_log.text

    def test_cdp_auth_starts_proxy_auth_manager_after_driver_creation(
        self,
        isolated_tempdir,
        fake_chrome,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
    ):
        transport("cdp_auth")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        assert len(proxy_auth_stub.calls) == 1
        call = proxy_auth_stub.calls[0]
        assert call["username"] == "user"
        assert call["password"] == "pass"
        assert "uc_profiles" in str(call["user_data_dir"])
        # UC держит фиксированный --remote-debugging-port (файла
        # DevToolsActivePort нет) — адрес обязан доехать до менеджера,
        # иначе авторизация не поднимается ни на одной сессии (e2e).
        assert call["debugger_address"] == driver.options.debugger_address
        assert callable(call["on_proxy_dead"])
        assert callable(call["client_factory"])
        # Менеджер привязан к драйверу и гасится в его quit().
        assert getattr(driver, "_proxy_auth_manager") is proxy_auth_stub.managers[0]
        driver.quit()
        assert proxy_auth_stub.managers[0].stopped == 1
        assert driver.quit_calls == 1

    def test_cdp_auth_without_credentials_needs_no_manager(
        self,
        isolated_tempdir,
        fake_chrome,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
    ):
        transport("cdp_auth", auth=False)

        driver, _ = webdriver.create_webdriver(PLAIN_PROXY, "Mozilla/5.0", "abcde")

        assert _proxy_args(driver) == [f"--proxy-server={PLAIN_PROXY}"]
        assert proxy_auth_stub.calls == []
        assert install_plugin_stub == []

    def test_driver_executable_path_is_prepared_even_without_pool(
        self,
        isolated_tempdir,
        fake_chrome,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
        monkeypatch,
    ):
        """Путь к драйверу готовится всегда, а не только когда пул больше одного.

        Раньше одиночный запуск получал ``None``, UC качал последний релиз
        (154) под Chromium 153 — ``session not created`` (поймано e2e).
        """
        prepared: list[str] = []

        def fake_prepare(path):
            prepared.append(str(path))
            return path

        monkeypatch.setattr(webdriver, "prepare_driver", fake_prepare)
        transport("direct")

        driver, _ = webdriver.create_webdriver(PLAIN_PROXY, "Mozilla/5.0", "abcde")

        assert prepared, "_get_driver_exe_path должен готовить драйвер"
        assert driver.init_kwargs["driver_executable_path"] == prepared[0]

    def test_extension_installs_plugin_and_adds_no_proxy_flag(
        self,
        isolated_tempdir,
        fake_chrome,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
    ):
        transport("extension")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        assert install_plugin_stub, "расширение не установлено"
        args, _ = install_plugin_stub[0]
        assert args[1:] == ("10.0.0.1", 3128, "user", "pass", "abcde")
        assert _proxy_args(driver) == []
        assert proxy_auth_stub.calls == []
        assert PROXY not in " ".join(driver.options.arguments)

    def test_extension_without_auth_keeps_the_plain_proxy_flag(
        self,
        isolated_tempdir,
        fake_chrome,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
    ):
        transport("extension", auth=False)

        driver, _ = webdriver.create_webdriver(PLAIN_PROXY, "Mozilla/5.0", "abcde")

        assert _proxy_args(driver) == [f"--proxy-server={PLAIN_PROXY}"]
        assert install_plugin_stub == []
        assert proxy_auth_stub.calls == []

    def test_extension_delivers_credentials_whenever_they_are_present(
        self,
        isolated_tempdir,
        fake_chrome,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
    ):
        """Транспорт решает по наличию кредов, а не по флагу ``auth``.

        ``auth=false`` со строкой ``user:pass@host:port`` — противоречивый
        конфиг: раньше креды утекали в ``--proxy-server`` (Chrome их всё
        равно игнорирует), теперь транспорт делает то, ради чего выбран.
        """
        transport("extension", auth=False)

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        assert install_plugin_stub, "креды есть — расширение должно быть установлено"
        assert _proxy_args(driver) == []
        assert PROXY not in " ".join(driver.options.arguments)

    def test_direct_has_neither_plugin_nor_manager(
        self,
        isolated_tempdir,
        fake_chrome,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
    ):
        transport("direct")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        assert _proxy_args(driver) == [f"--proxy-server={PROXY_HOST_PORT}"]
        assert install_plugin_stub == []
        assert proxy_auth_stub.calls == []
        assert PROXY not in " ".join(driver.options.arguments)

    def test_direct_accepts_a_whitelist_proxy_without_credentials(
        self,
        isolated_tempdir,
        fake_chrome,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
    ):
        # auth в песочнице включён, но direct его не читает: whitelist-IP —
        # нормальный режим для этого транспорта.
        transport("direct")

        driver, _ = webdriver.create_webdriver(PLAIN_PROXY, "Mozilla/5.0", "abcde")

        assert _proxy_args(driver) == [f"--proxy-server={PLAIN_PROXY}"]

    def test_without_proxy_no_proxy_flag_and_no_manager(
        self,
        isolated_tempdir,
        fake_chrome,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
    ):
        transport("cdp_auth")

        driver, _ = webdriver.create_webdriver("", "Mozilla/5.0", "abcde")

        assert _proxy_args(driver) == []
        assert install_plugin_stub == []
        assert proxy_auth_stub.calls == []

    def test_unknown_transport_is_rejected_before_chrome_starts(
        self, isolated_tempdir, no_browser, transport
    ):
        transport("socks")

        with pytest.raises(ValueError, match="proxy_transport"):
            webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")


class TestProxyDegradation:
    """Сигнал деградации: WARNING с host:port без кредов + workers.status."""

    def test_proxy_dead_callback_reports_degradation(
        self,
        isolated_tempdir,
        fake_chrome,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
    ):
        transport("cdp_auth")
        webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        proxy_auth_stub.calls[0]["on_proxy_dead"]()

        assert record_log.degraded == [("proxy rejected credentials", None)]
        warning = record_log.warnings[0]
        assert warning[1][:2] == ("proxy", "proxy transport degraded")
        fields = warning[2]["fields"]
        assert fields["reason"] == "proxy rejected credentials"
        assert fields["proxy"] == PROXY_HOST_PORT
        assert PROXY not in record_log.text

    def test_cdp_connection_loss_is_reported_through_client_factory(
        self,
        isolated_tempdir,
        fake_chrome,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
    ):
        transport("cdp_auth")
        webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        client = proxy_auth_stub.calls[0]["client_factory"]("ws://127.0.0.1:1/devtools/browser/x")
        # Обрыв установленного соединения; сам сигнал проверен в
        # tests/engine/test_cdp.py::TestConnectionLostCallback.
        client._on_connection_lost()

        assert record_log.degraded == [("cdp connection lost", None)]
        fields = record_log.warnings[0][2]["fields"]
        assert fields["proxy"] == PROXY_HOST_PORT
        assert PROXY not in record_log.text

    def test_manager_start_failure_is_reported_and_does_not_raise(
        self,
        isolated_tempdir,
        fake_chrome,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
    ):
        transport("cdp_auth")
        proxy_auth_stub.error = RuntimeError("CDP connect timed out after 10s")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        assert driver is not None
        assert not getattr(driver, "_proxy_auth_manager", None)
        assert record_log.degraded == [("cdp auth start failed: RuntimeError", None)]
        fields = record_log.warnings[0][2]["fields"]
        assert fields["proxy"] == PROXY_HOST_PORT
        assert PROXY not in record_log.text

    def test_quit_survives_a_failing_manager_stop(self, record_log):
        """quit() не должен роняться: остановка менеджера — best effort."""

        driver = object.__new__(webdriver.CustomChrome)
        driver.browser_pid = 999999999
        driver.service = None
        driver.reactor = None
        driver.keep_user_data_dir = True

        class ExplodingManager:
            def stop(self):
                raise RuntimeError("ws already closed")

        driver._proxy_auth_manager = ExplodingManager()

        driver.quit()

        stopped_logs = [
            record for record in record_log.records if "Proxy auth stop failed" in str(record)
        ]
        assert stopped_logs, "проглоченная остановка должна попасть в лог"

    def test_quit_stops_the_manager_only_once(self, record_log):
        driver = object.__new__(webdriver.CustomChrome)
        driver.browser_pid = 999999999
        driver.service = None
        driver.reactor = None
        driver.keep_user_data_dir = True
        manager = _FakeManager()
        driver._proxy_auth_manager = manager

        driver.quit()
        driver.quit()

        assert manager.stopped == 1


# --- SeleniumBase: тот же транспорт, порт DevTools общий -----------------------


class _FakeSeleniumBaseDriver:
    """Драйвер SeleniumBase без браузера: аргументы get_driver, CDP и quit."""

    def __init__(self, kwargs: dict) -> None:
        self.init_kwargs = kwargs
        self.quit_calls = 0
        self.cdp_calls: list[tuple[tuple, dict]] = []

    def set_window_size(self, width, height) -> None:
        pass

    def maximize_window(self) -> None:
        pass

    def execute_cdp_cmd(self, *args, **kwargs) -> None:
        self.cdp_calls.append((args, kwargs))

    def quit(self) -> None:
        self.quit_calls += 1


@pytest.fixture
def seleniumbase_mode(config, monkeypatch):
    monkeypatch.setattr(config.webdriver, "use_seleniumbase", True)


@pytest.fixture
def seleniumbase_stub(monkeypatch):
    """seleniumbase.get_driver: записывает kwargs, браузер не поднимается."""

    created: list[_FakeSeleniumBaseDriver] = []

    def fake_get_driver(**kwargs):
        driver = _FakeSeleniumBaseDriver(kwargs)
        created.append(driver)
        return driver

    # seleniumbase импортируется лениво внутри create_seleniumbase_driver,
    # атрибута у webdriver больше нет — патчим модуль в sys.modules
    # (заглушку из conftest), ровно туда, куда придёт локальный import.
    monkeypatch.setattr(sys.modules["seleniumbase"], "get_driver", fake_get_driver)
    return created


class TestSeleniumBaseTransport:
    """Ветка use_seleniumbase: те же транспорты, тот же порт DevTools."""

    def test_cdp_auth_strips_credentials_and_starts_manager(
        self,
        isolated_tempdir,
        seleniumbase_mode,
        seleniumbase_stub,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
    ):
        transport("cdp_auth")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        assert driver.init_kwargs["proxy_string"] == PROXY_HOST_PORT
        assert PROXY not in str(driver.init_kwargs)
        assert install_plugin_stub == []
        assert len(proxy_auth_stub.calls) == 1
        call = proxy_auth_stub.calls[0]
        assert (call["username"], call["password"]) == ("user", "pass")
        # Порт DevTools берётся из того же user_data_dir, что и у драйвера.
        assert str(call["user_data_dir"]) == driver.init_kwargs["user_data_dir"]
        assert PROXY not in record_log.text

    def test_cdp_auth_manager_is_stopped_when_the_driver_quits(
        self,
        isolated_tempdir,
        seleniumbase_mode,
        seleniumbase_stub,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
    ):
        transport("cdp_auth")
        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        driver.quit()

        assert proxy_auth_stub.managers[0].stopped == 1
        assert driver.quit_calls == 1

    def test_direct_passes_proxy_without_credentials_and_no_manager(
        self,
        isolated_tempdir,
        seleniumbase_mode,
        seleniumbase_stub,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
    ):
        transport("direct")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        assert driver.init_kwargs["proxy_string"] == PROXY_HOST_PORT
        assert proxy_auth_stub.calls == []
        assert install_plugin_stub == []

    def test_extension_keeps_the_current_behaviour(
        self,
        isolated_tempdir,
        seleniumbase_mode,
        seleniumbase_stub,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
    ):
        transport("extension")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        # Ровно то, что было до появления транспортов: строка целиком,
        # без расширения и без CDP-менеджера.
        assert driver.init_kwargs["proxy_string"] == PROXY
        assert proxy_auth_stub.calls == []
        assert install_plugin_stub == []

    def test_without_proxy_proxy_string_is_empty(
        self,
        isolated_tempdir,
        seleniumbase_mode,
        seleniumbase_stub,
        proxy_auth_stub,
        install_plugin_stub,
        no_geolocation,
        record_log,
        transport,
    ):
        transport("cdp_auth")

        driver, _ = webdriver.create_webdriver("", "Mozilla/5.0", "abcde")

        assert driver.init_kwargs["proxy_string"] is None
        assert proxy_auth_stub.calls == []
        assert install_plugin_stub == []


# --- Настройки профиля: локаль и часовой пояс против гео-вычислений ----------


class TestProfileSettings:
    """Профильные locale/timezone главнее гео-вычислений (UC-режим).

    Пустое поле профиля равносильно отсутствующему: прежнее поведение
    сохраняется строка в строку, и часть тестов ниже — это именно паритет,
    зафиксированный на случай, если приоритет «случайно» станет
    безусловным.
    """

    def test_profile_locale_replaces_the_geo_one(
        self,
        isolated_tempdir,
        fake_chrome,
        geolocated_proxy,
        assign_profile,
        config,
        monkeypatch,
        transport,
    ):
        transport("direct")
        monkeypatch.setattr(config.webdriver, "language_from_proxy", True)
        assign_profile(locale="de-DE")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        prefs = driver.options.experimental_options["prefs"]
        assert prefs["intl.accept_languages"] == "de-DE"
        assert "--lang=de" in driver.options.arguments

    def test_without_a_profile_the_geo_locale_is_used_unchanged(
        self,
        isolated_tempdir,
        fake_chrome,
        geolocated_proxy,
        config,
        monkeypatch,
        transport,
    ):
        transport("direct")
        monkeypatch.setattr(config.webdriver, "language_from_proxy", True)

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        # Квирк legacy зафиксирован намеренно: get_locale_language возвращает
        # список, и в prefs/--lang он уходит через str() целиком.
        prefs = driver.options.experimental_options["prefs"]
        assert prefs["intl.accept_languages"] == str(["de-DE", "en-US"])
        assert "--lang=['de-DE', 'en-US']" in driver.options.arguments

    def test_empty_profile_locale_keeps_the_geo_one(
        self,
        isolated_tempdir,
        fake_chrome,
        geolocated_proxy,
        assign_profile,
        config,
        monkeypatch,
        transport,
    ):
        transport("direct")
        monkeypatch.setattr(config.webdriver, "language_from_proxy", True)
        assign_profile(locale="   ")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        prefs = driver.options.experimental_options["prefs"]
        assert prefs["intl.accept_languages"] == str(["de-DE", "en-US"])

    def test_profile_locale_is_applied_without_the_geo_flag(
        self,
        isolated_tempdir,
        fake_chrome,
        geolocated_proxy,
        assign_profile,
        config,
        monkeypatch,
        transport,
    ):
        transport("direct")
        assign_profile(locale="fr-FR")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        prefs = driver.options.experimental_options["prefs"]
        assert prefs["intl.accept_languages"] == "fr-FR"
        assert "--lang=fr" in driver.options.arguments

    def test_disabled_geo_flag_without_profile_leaves_locale_alone(
        self, isolated_tempdir, fake_chrome, geolocated_proxy, transport
    ):
        transport("direct")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        prefs = driver.options.experimental_options.get("prefs", {})
        assert "intl.accept_languages" not in prefs

    def test_profile_locale_is_applied_without_a_proxy(
        self, isolated_tempdir, fake_chrome, assign_profile, transport
    ):
        transport("direct")
        assign_profile(locale="it-IT")

        driver, _ = webdriver.create_webdriver("", "Mozilla/5.0", "abcde")

        prefs = driver.options.experimental_options["prefs"]
        assert prefs["intl.accept_languages"] == "it-IT"

    def test_profile_timezone_replaces_the_geo_one(
        self,
        isolated_tempdir,
        fake_chrome,
        geolocated_proxy,
        assign_profile,
        transport,
    ):
        transport("direct")
        assign_profile(timezone="Europe/Paris")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        assert _cdp_payloads(driver, "Emulation.setTimezoneOverride") == [
            {"timezoneId": "Europe/Paris"}
        ]
        assert driver._custom_timezone == "Europe/Paris"
        # Геолокация при этом продолжает применяться — меняется только пояс.
        assert _cdp_payloads(driver, "Emulation.setGeolocationOverride")

    def test_without_a_profile_the_geo_timezone_is_used_unchanged(
        self, isolated_tempdir, fake_chrome, geolocated_proxy, transport
    ):
        transport("direct")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        assert _cdp_payloads(driver, "Emulation.setTimezoneOverride") == [
            {"timezoneId": "Europe/Berlin"}
        ]
        assert driver._custom_timezone == "Europe/Berlin"

    def test_profile_timezone_is_applied_without_geo_coordinates(
        self, isolated_tempdir, fake_chrome, no_geolocation, assign_profile, transport
    ):
        transport("direct")
        assign_profile(timezone="Europe/Paris")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        assert _cdp_payloads(driver, "Emulation.setTimezoneOverride") == [
            {"timezoneId": "Europe/Paris"}
        ]
        assert _cdp_payloads(driver, "Emulation.setGeolocationOverride") == []

    def test_without_coordinates_and_without_profile_no_timezone_is_set(
        self, isolated_tempdir, fake_chrome, no_geolocation, transport
    ):
        transport("direct")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        # Паритет: legacy не ставил пояс, пока координат нет, даже если
        # get_location вернул часовой пояс отдельно от широты/долготы.
        assert _cdp_payloads(driver, "Emulation.setTimezoneOverride") == []
        assert not hasattr(driver, "_custom_timezone")

    def test_profile_timezone_is_applied_without_a_proxy(
        self, isolated_tempdir, fake_chrome, assign_profile, transport
    ):
        transport("direct")
        assign_profile(timezone="Europe/Paris")

        driver, _ = webdriver.create_webdriver("", "Mozilla/5.0", "abcde")

        assert _cdp_payloads(driver, "Emulation.setTimezoneOverride") == [
            {"timezoneId": "Europe/Paris"}
        ]

    def test_user_agent_reaches_chrome_as_an_argument(
        self, isolated_tempdir, fake_chrome, no_geolocation, transport
    ):
        transport("direct")

        driver, _ = webdriver.create_webdriver(PROXY, "Profile-UA", "abcde")

        assert "--user-agent=Profile-UA" in driver.options.arguments


class TestProfileSettingsSeleniumBase:
    """Те же настройки в режиме use_seleniumbase: хук не привязан к UC."""

    def test_profile_locale_replaces_the_geo_one(
        self,
        isolated_tempdir,
        seleniumbase_mode,
        seleniumbase_stub,
        geolocated_proxy,
        assign_profile,
        config,
        monkeypatch,
        transport,
    ):
        transport("direct")
        monkeypatch.setattr(config.webdriver, "language_from_proxy", True)
        assign_profile(locale="de-DE")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        assert driver.init_kwargs["locale_code"] == "de-DE"

    def test_without_a_profile_the_geo_locale_is_used_unchanged(
        self,
        isolated_tempdir,
        seleniumbase_mode,
        seleniumbase_stub,
        geolocated_proxy,
        config,
        monkeypatch,
        transport,
    ):
        transport("direct")
        monkeypatch.setattr(config.webdriver, "language_from_proxy", True)

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        assert driver.init_kwargs["locale_code"] == str(["de-DE", "en-US"])

    def test_without_the_geo_flag_and_without_profile_locale_code_is_none(
        self,
        isolated_tempdir,
        seleniumbase_mode,
        seleniumbase_stub,
        geolocated_proxy,
        transport,
    ):
        transport("direct")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        assert driver.init_kwargs["locale_code"] is None

    def test_profile_locale_is_applied_without_a_proxy(
        self, isolated_tempdir, seleniumbase_mode, seleniumbase_stub, assign_profile, transport
    ):
        transport("direct")
        assign_profile(locale="it-IT")

        driver, _ = webdriver.create_webdriver("", "Mozilla/5.0", "abcde")

        assert driver.init_kwargs["locale_code"] == "it-IT"

    def test_user_agent_reaches_the_seleniumbase_driver(
        self, isolated_tempdir, seleniumbase_mode, seleniumbase_stub, no_geolocation, transport
    ):
        transport("direct")

        driver, _ = webdriver.create_webdriver(PROXY, "Profile-UA", "abcde")

        assert driver.init_kwargs["user_agent"] == "Profile-UA"

    def test_profile_timezone_replaces_the_geo_one(
        self,
        isolated_tempdir,
        seleniumbase_mode,
        seleniumbase_stub,
        geolocated_proxy,
        assign_profile,
        transport,
    ):
        transport("direct")
        assign_profile(timezone="Europe/Paris")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        assert _cdp_payloads(driver, "Emulation.setTimezoneOverride") == [
            {"timezoneId": "Europe/Paris"}
        ]
        assert driver._custom_timezone == "Europe/Paris"

    def test_without_a_profile_the_geo_timezone_is_used_unchanged(
        self,
        isolated_tempdir,
        seleniumbase_mode,
        seleniumbase_stub,
        geolocated_proxy,
        transport,
    ):
        transport("direct")

        driver, _ = webdriver.create_webdriver(PROXY, "Mozilla/5.0", "abcde")

        assert _cdp_payloads(driver, "Emulation.setTimezoneOverride") == [
            {"timezoneId": "Europe/Berlin"}
        ]
        assert driver._custom_timezone == "Europe/Berlin"
