"""Тесты ad_clicker: CLI-контракт, выбор query/proxy и User-Agent прогона.

Сам ``run_scenario`` требует браузера, поэтому целиком не проверяется:
покрыты чистые функции выбора запроса/прокси/User-Agent и диспетчеризация
``main()``, которые и есть поведение, перенесённое в воркер. Прогона с
назначенным профилем касается только строка UA — она проверяется на
подменённой точке создания драйвера, до Chrome и сети.
"""

import contextlib
import io
import sys

import pytest

import ad_clicker
from ad_clicker import get_arg_parser, resolve_proxy, resolve_query
from utils import ProxyCountryError


def parse(argv):
    return get_arg_parser().parse_args(argv)


def test_parser_supports_documented_options():
    options = {action.dest for action in get_arg_parser()._actions}

    assert {
        "query",
        "proxy",
        "id",
        "enable_telegram",
        "report_clicks",
        "date",
        "excel",
        "check_stealth",
        "device_id",
    } <= options


def test_query_is_read_from_short_and_long_flags():
    assert parse(["-q", "usb hub"]).query == "usb hub"
    assert parse(["--query", "usb hub"]).query == "usb hub"


def test_proxy_is_read_from_short_and_long_flags():
    assert parse(["-p", "127.0.0.1:8080"]).proxy == "127.0.0.1:8080"
    assert parse(["--proxy", "user:pass@1.2.3.4:8080"]).proxy == "user:pass@1.2.3.4:8080"


def test_optional_values_default_to_none():
    args = parse([])

    assert args.query is None
    assert args.proxy is None
    assert args.id is None
    assert args.date is None
    assert args.device_id is None


def test_boolean_flags_default_to_false():
    args = parse([])

    assert args.enable_telegram is False
    assert args.report_clicks is False
    assert args.excel is False
    assert args.check_stealth is False


def test_boolean_flags_are_set_by_presence_only():
    assert parse(["--enable_telegram"]).enable_telegram is True
    assert parse(["--report_clicks"]).report_clicks is True
    assert parse(["--excel"]).excel is True
    assert parse(["--check_stealth"]).check_stealth is True


def test_browser_id_is_parsed_as_string():
    assert parse(["--id", "3"]).id == "3"


def test_device_id_is_parsed_from_short_and_long_flags():
    assert parse(["-d", "emulator-5554"]).device_id == "emulator-5554"
    assert parse(["--device_id", "emulator-5554"]).device_id == "emulator-5554"


def test_report_date_is_kept_verbatim():
    # Формат даты потом разбирается в datetime.strptime, парсер его не проверяет.
    assert parse(["--report_clicks", "--date", "28-09-2026"]).date == "28-09-2026"


def test_query_with_spaces_survives_parsing():
    assert parse(["-q", "wireless keyboard"]).query == "wireless keyboard"


def test_help_is_disabled_and_points_to_readme():
    assert "See README.md file" in get_arg_parser().format_usage()

    with pytest.raises(SystemExit):
        parse(["-h"])


def test_unknown_option_is_rejected():
    with pytest.raises(SystemExit):
        parse(["--definitely-not-an-option"])


class TestResolveQuery:
    """Выбор запроса для прогона: аргумент CLI против config.json."""

    def test_argument_wins_over_config(self, config):
        config.behavior.query = "from config"

        assert resolve_query(parse(["-q", "from cli"])) == "from cli"

    def test_config_is_used_when_no_argument(self, config):
        config.behavior.query = "from config"

        assert resolve_query(parse([])) == "from config"

    def test_empty_query_everywhere_is_an_error(self, config):
        config.behavior.query = ""

        with pytest.raises(SystemExit):
            resolve_query(parse([]))


class TestResolveProxy:
    """Выбор прокси для прогона: аргумент, файл со списком, конфиг, ничего."""

    def test_argument_wins_over_config(self, config):
        config.webdriver.proxy = "10.0.0.1:8080"

        assert resolve_proxy(parse(["-p", "1.2.3.4:80"])) == "1.2.3.4:80"

    def test_proxy_file_is_read_and_respected(self, config, set_paths, sandbox_dir):
        set_paths(proxy_file=str(sandbox_dir / "proxies.txt"))
        config.webdriver.proxy = "10.0.0.1:8080"

        # Файл главнее одиночного прокси: так же это решает и воркер.
        assert resolve_proxy(parse([])) in {"127.0.0.1:8080", "user:pass@10.0.0.1:3128"}

    def test_single_proxy_from_config_when_no_file(self, config, set_paths):
        set_paths(proxy_file="")
        config.webdriver.proxy = "10.0.0.1:8080"

        assert resolve_proxy(parse([])) == "10.0.0.1:8080"

    def test_nothing_configured_yields_none(self, config, set_paths):
        set_paths(proxy_file="")
        config.webdriver.proxy = ""

        assert resolve_proxy(parse([])) is None


class TestResolveProxyEnv:
    """Env-контракт супервизора: ``ADCLICKER_PROXY`` главнее legacy-источников.

    Супервизор закрепляет прокси за воркером и передаёт его через окружение;
    legacy-путь (файл со списком, ``random.choice``) остаётся для одиночного
    запуска и при пустой переменной.
    """

    def test_env_proxy_wins_over_the_proxy_file(
        self, config, set_paths, sandbox_dir, monkeypatch
    ):
        set_paths(proxy_file=str(sandbox_dir / "proxies.txt"))
        monkeypatch.setenv("ADCLICKER_PROXY", "user:pass@10.0.0.1:9999")

        assert resolve_proxy(parse([])) == "user:pass@10.0.0.1:9999"

    def test_env_proxy_wins_over_the_cli_argument(self, monkeypatch):
        monkeypatch.setenv("ADCLICKER_PROXY", "user:pass@10.0.0.1:9999")

        assert resolve_proxy(parse(["-p", "1.2.3.4:80"])) == "user:pass@10.0.0.1:9999"

    def test_env_proxy_is_trimmed(self, monkeypatch):
        monkeypatch.setenv("ADCLICKER_PROXY", "  user:pass@10.0.0.1:9999  ")

        assert resolve_proxy(parse([])) == "user:pass@10.0.0.1:9999"

    def test_empty_env_keeps_the_legacy_choice(
        self, config, set_paths, sandbox_dir, monkeypatch
    ):
        set_paths(proxy_file=str(sandbox_dir / "proxies.txt"))
        monkeypatch.setenv("ADCLICKER_PROXY", "")

        assert resolve_proxy(parse([])) in {"127.0.0.1:8080", "user:pass@10.0.0.1:3128"}


class TestMainDispatch:
    """main() разбирает argv и передаёт управление в run_scenario."""

    def test_main_hands_over_to_run_scenario(self, monkeypatch, config, set_paths):
        monkeypatch.setattr(sys, "argv", ["ad_clicker.py"])
        set_paths(proxy_file="")
        config.webdriver.proxy = ""
        config.behavior.query = "usb hub"
        captured = {}

        def fake_run_scenario(**kwargs):
            captured.update(kwargs)
            return True

        monkeypatch.setattr(ad_clicker, "run_scenario", fake_run_scenario)

        ad_clicker.main()

        assert captured["query"] == "usb hub"
        assert captured["proxy"] is None
        assert captured["browser_id"] is None
        assert captured["device_id"] is None
        assert captured["check_stealth"] is False

    def test_report_mode_does_not_run_a_scenario(self, monkeypatch, config):
        monkeypatch.setattr(sys, "argv", ["ad_clicker.py", "--report_clicks"])
        calls = []
        monkeypatch.setattr(ad_clicker, "run_scenario", lambda **kwargs: calls.append(kwargs))

        with contextlib.redirect_stdout(io.StringIO()):
            ad_clicker.main()

        assert calls == [], "отчёт не должен запускать сценарий"


# --- User-Agent прогона: профиль против случайного ---------------------------


class StopBeforeBrowser(Exception):
    """create_webdriver перехвачен: прогон обрывается до запуска Chrome."""


@pytest.fixture
def capture_user_agent(monkeypatch):
    """create_webdriver вместо браузера: запомнить UA и оборвать прогон."""

    captured = {}

    def fake_create_webdriver(proxy, user_agent, plugin_folder_name):
        captured["user_agent"] = user_agent
        raise StopBeforeBrowser()

    monkeypatch.setattr(ad_clicker, "create_webdriver", fake_create_webdriver)
    return captured


@pytest.fixture
def assign_profile(tmp_path, monkeypatch):
    """Профиль в своей БД, назначенный процессу через env супервизора."""

    from engine.db import migrations
    from engine.profile_apply import PROFILE_ID_ENV
    from engine.profile_pool import ProfilePool

    db = tmp_path / "profiles.db"
    migrations.migrate(db)
    monkeypatch.setenv("ADCLICKER_DB", str(db))
    pool = ProfilePool(db)

    def _assign(**fields):
        pool.add_profiles([{"name": "acc", **fields}])
        row = pool.list_profiles()[-1]
        monkeypatch.setenv(PROFILE_ID_ENV, str(row["id"]))
        return row["id"]

    return _assign


class TestProfileUserAgent:
    """UA прогона: подмена только явная — строка профиля.

    Без профиля в create_webdriver уходит None, и Chrome отдаёт честную
    строку: прежний подбор ставил в --user-agent устаревшие и мобильные
    UA («Chrome/136 при браузере 153»), которые сервер читает вместе с
    Sec-CH-UA и считает браузером подделкой.
    """

    def test_profile_user_agent_reaches_the_browser(
        self, assign_profile, capture_user_agent
    ):
        assign_profile(user_agent="UA/Profile")

        with pytest.raises(StopBeforeBrowser):
            ad_clicker.run_scenario(query="usb hub")

        assert capture_user_agent["user_agent"] == "UA/Profile"

    def test_without_a_profile_no_user_agent_is_passed(self, capture_user_agent):
        with pytest.raises(StopBeforeBrowser):
            ad_clicker.run_scenario(query="usb hub")

        assert capture_user_agent["user_agent"] is None

    def test_empty_profile_user_agent_means_no_override(
        self, assign_profile, capture_user_agent
    ):
        assign_profile(user_agent="   ")

        with pytest.raises(StopBeforeBrowser):
            ad_clicker.run_scenario(query="usb hub")

        assert capture_user_agent["user_agent"] is None

    def test_profile_without_a_user_agent_means_no_override(
        self, assign_profile, capture_user_agent
    ):
        assign_profile(locale="de-DE")

        with pytest.raises(StopBeforeBrowser):
            ad_clicker.run_scenario(query="usb hub")

        assert capture_user_agent["user_agent"] is None

    def test_mobile_profile_user_agent_is_dropped(self, assign_profile, capture_user_agent):
        assign_profile(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 13; SM-S901B) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36"
            )
        )

        with pytest.raises(StopBeforeBrowser):
            ad_clicker.run_scenario(query="usb hub")

        assert capture_user_agent["user_agent"] is None, (
            "мобильный UA под десктопным окном отдаёт мобильную вёрстку "
            "под десктопными селекторами — только честная строка Chrome"
        )


class TestProxyHealthCheck:
    """Отбраковка нерабочего exit IP до запуска Chrome (отчёт по CAPTCHA, §4.5).

    ~30% строк в пуле капчат даже чистый браузер — раунд на них заведомо
    проигран. probe=True (капча) и probe=None (сеть молчит либо ответ
    4xx/5xx) → раунд не начинается (False), сигнал degraded уходит
    супервизору на ротацию; поднимается браузер только при probe=False —
    ответ доехал и капчи нет.
    """

    @staticmethod
    def _run(monkeypatch, probe_result):
        probes = []

        def fake_probe(proxy):
            probes.append(proxy)
            return probe_result

        monkeypatch.setattr(ad_clicker, "probe_proxy_captcha", fake_probe)
        started = []

        def fake_create(*args, **kwargs):
            started.append(args)
            raise StopBeforeBrowser()

        monkeypatch.setattr(ad_clicker, "create_webdriver", fake_create)

        # Не-False → run_scenario возвращает False ДО create_webdriver;
        # иначе фейковый драйвер бросает StopBeforeBrowser, как в остальных
        # тестах этого файла (исключения подготовки не гасятся).
        if probe_result is not False:
            completed = ad_clicker.run_scenario(query="usb hub", proxy="u:p@proxy.host:80")
        else:
            with pytest.raises(StopBeforeBrowser):
                ad_clicker.run_scenario(query="usb hub", proxy="u:p@proxy.host:80")
            completed = "started"
        return completed, probes, started

    def test_captcha_flagged_proxy_skips_round_before_browser(self, monkeypatch):
        completed, probes, started = self._run(monkeypatch, True)

        assert completed is False, "отбракованный IP — раунд не начат, не успех"
        assert probes == ["u:p@proxy.host:80"]
        assert started == [], "Chrome не должен запускаться на мёртвом IP"

    def test_unknown_probe_result_skips_the_round(self, monkeypatch):
        # None = «проверить не удалось»: таймаут, обрыв, отказ прокси на
        # CONNECT, ответ 4xx/5xx. Браузер на таком прокси лишь сжёг бы время
        # и трафик вхолостую, поэтому раунд не начинается.
        completed, probes, started = self._run(monkeypatch, None)

        assert probes == ["u:p@proxy.host:80"]
        assert completed is False, "неудачная проверка — раунд пропущен"
        assert started == [], "Chrome не должен запускаться без подтверждения"

    def test_probe_failure_is_reported_as_degraded(self, monkeypatch):
        # Причина уходит супервизору (workers.last_error и ротация) и не
        # должна выглядеть как капча: это разные события для оператора.
        records = []

        class _Log:
            def warning(self, *args, **kwargs):
                records.append(("warning", args, kwargs))

            def mark_degraded(self, reason):
                records.append(("degraded", reason))

            def __getattr__(self, name):
                return lambda *args, **kwargs: None

        monkeypatch.setattr(ad_clicker, "log", _Log())
        monkeypatch.setattr(ad_clicker, "probe_proxy_captcha", lambda proxy: None)
        monkeypatch.setattr(
            ad_clicker,
            "create_webdriver",
            lambda *a, **k: pytest.fail("браузер не должен стартовать"),
        )

        assert ad_clicker.run_scenario(query="usb hub", proxy="u:p@proxy.host:80") is False
        assert ("degraded", "proxy pre-check failed") in records

    def test_non_german_exit_skips_the_round_and_marks_degraded(self, monkeypatch):
        """Гейт до Chrome: раунд не начинается, причина уходит супервизору.

        Проба сети уже прошла (прокси жив), но exit-IP не Германия — раунд
        заведомо проигран, поэтому он пропускается тем же путём, что и
        отбраковка пробой: warning в лог и mark_degraded на ротацию.
        """
        records = []

        class _Log:
            def warning(self, *args, **kwargs):
                records.append(("warning", args, kwargs))

            def mark_degraded(self, reason):
                records.append(("degraded", reason))

            def __getattr__(self, name):
                return lambda *args, **kwargs: None

        monkeypatch.setattr(ad_clicker, "log", _Log())
        monkeypatch.setattr(ad_clicker, "probe_proxy_captcha", lambda proxy: False)
        monkeypatch.setattr(
            ad_clicker,
            "create_webdriver",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                ProxyCountryError("exit-IP не Германия: TR", "proxy.host:8080")
            ),
        )

        assert ad_clicker.run_scenario(query="usb hub", proxy="u:p@proxy.host:80") is False

        assert ("degraded", "exit-IP не Германия: TR (proxy.host:8080)") in records
        warnings = [entry for entry in records if entry[0] == "warning"]
        assert warnings, "отказ гейта обязан быть виден в логе"
        fields = warnings[0][2]["fields"]
        assert fields["reason"] == "exit-IP не Германия: TR"
        assert fields["proxy"] == "proxy.host:8080", "креды не должны попасть в лог"
        assert "u:p@" not in str(records)

    def test_clean_probe_starts_the_round(self, monkeypatch):
        completed, probes, started = self._run(monkeypatch, False)

        assert probes == ["u:p@proxy.host:80"]
        assert len(started) == 1

    def test_no_proxy_means_no_probe(self, monkeypatch):
        probes = []
        monkeypatch.setattr(
            ad_clicker, "probe_proxy_captcha", lambda p: probes.append(p)
        )
        monkeypatch.setattr(
            ad_clicker,
            "create_webdriver",
            lambda *a, **k: (_ for _ in ()).throw(StopBeforeBrowser()),
        )

        with pytest.raises(StopBeforeBrowser):
            ad_clicker.run_scenario(query="usb hub")

        assert probes == [], "без прокси health-check не вызывается"


class TestDiagnosticsCheckpoint:
    """Снимок диагностики в сценарии: две точки входа и защита от сбоев.

    Сбор обязан проходить при живом браузере (автосбор за сессию и kv-сигнал
    из UI) и ни при каких условиях не ронять прогон — иначе диагностика
    стоила бы сценария.
    """

    @staticmethod
    def _record(monkeypatch, error=None):
        calls = []
        warnings = []

        def fake_checkpoint(driver, **kwargs):
            calls.append((driver, kwargs))
            if error is not None:
                raise error

        monkeypatch.setattr(ad_clicker, "session_checkpoint", fake_checkpoint)
        monkeypatch.setattr(
            ad_clicker.log,
            "warning",
            lambda *args, **kwargs: warnings.append((args, kwargs)),
        )
        return calls, warnings

    def test_forwards_driver_browser_id_store_and_logger(self, monkeypatch):
        calls, _ = self._record(monkeypatch)
        driver = object()

        ad_clicker.diagnostics_checkpoint(driver, "br-1")

        assert len(calls) == 1
        got_driver, kwargs = calls[0]
        assert got_driver is driver
        assert kwargs["browser_id"] == "br-1"
        assert kwargs["store"] is not None
        assert kwargs["logger"] is ad_clicker.log

    def test_without_a_browser_id_nowhere_to_write(self, monkeypatch):
        calls, warnings = self._record(monkeypatch)

        ad_clicker.diagnostics_checkpoint(object(), None)

        assert calls == [], "CLI-прогон без --id не должен трогать БД"
        assert warnings == []

    def test_a_failing_checkpoint_never_escapes(self, monkeypatch):
        calls, warnings = self._record(monkeypatch, RuntimeError("checkpoint exploded"))

        ad_clicker.diagnostics_checkpoint(object(), "br-1")

        assert len(calls) == 1, "попытка была"
        assert warnings, "сбой обязан остаться в логе"
        assert "checkpoint exploded" in str(warnings)


class TestScenarioCheckpoints:
    """Места вызова в run_scenario: сразу после создания браузера и в finally."""

    @pytest.fixture
    def fake_driver(self):
        class Driver:
            def quit(self):
                pass

        return Driver()

    @pytest.fixture
    def checkpoints(self, monkeypatch, fake_driver):
        calls = []
        monkeypatch.setattr(
            ad_clicker,
            "diagnostics_checkpoint",
            lambda driver, browser_id: calls.append((driver, browser_id)),
        )
        # Поиск падает сразу: дальше по сценарию идти некуда, а обе точки
        # сбора (после create_webdriver и в finally) уже должны были пройти.
        def boom(*args, **kwargs):
            raise RuntimeError("no search")

        monkeypatch.setattr(ad_clicker, "SearchController", boom)
        return calls

    def test_checkpoint_runs_after_the_browser_opens_and_before_it_closes(
        self, fake_driver, checkpoints, monkeypatch
    ):
        monkeypatch.setattr(
            ad_clicker, "create_webdriver", lambda *args, **kwargs: (fake_driver, None)
        )

        completed = ad_clicker.run_scenario(query="usb hub")

        assert completed is False, "упавший поиск — прогон с ошибкой, а не падение процесса"
        assert checkpoints == [(fake_driver, None), (fake_driver, None)], (
            "снимок должен быть и при открытии браузера, и перед его закрытием"
        )
