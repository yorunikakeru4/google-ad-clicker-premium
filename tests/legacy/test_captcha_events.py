"""Тесты события CAPTCHA (план.md, §5 «Фаза 8»): единая точка, скриншот,
telegram и политика ``behavior.captcha_policy``.

Контракт, зафиксированный здесь:

- все ветки детекта в ``search_for_ads`` (до поиска, в ветке
  ``ElementNotInteractable``, после набора запроса) сходятся в одну функцию
  события: одна строка в ``captcha_events`` на обнаружение, плюс счётчик
  ``runs.captcha_seen`` и лог с категорией ``captcha``;
- скриншот — отдельный явный вызов драйвера в ``engine/screenshots/`` с
  именем ``<browser_id>_<epoch>.png``; отказ скриншота не роняет ни событие,
  ни сценарий (``screenshot_path`` остаётся NULL);
- telegram-уведомление срабатывает на само событие и только при
  ``behavior.telegram_enabled``: выключено или ошибка отправки не меняют
  исход;
- политики различимы: ``stop`` (дефолт) прерывает сценарий ``SystemExit`` и
  ждёт оператора, ``solve`` решает через 2captcha и продолжает, ``both``
  решает и при неудаче уходит в stop-ветку; лимит попыток на воркер и
  отсутствие ключа не взрывают сценарий иначе чем stop-веткой;
- ротация прокси: любая ветка, уводящая прогон в stop, после события шлёт
  сигнал ``log.mark_degraded`` (причина без кредов) — супервизор подменяет
  прокси, и следующий заход идёт с другого IP. Решённая капча сигнал не
  шлёт: ротация — рестарт процесса и убила бы продолжаемый прогон.

Драйвер здесь фальшивый (без Chrome и сети), ``solve_recaptcha`` подменяется:
проверяется логика пайплайна, а не реальный 2captcha.
"""

import re
import sqlite3
import sys
import time
import types
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest
from selenium.common.exceptions import (
    ElementNotInteractableException,
    NoSuchElementException,
    WebDriverException,
)
from selenium.webdriver.common.by import By

import search_controller
from conftest import FakeDriver, FakeElement
from engine.control_plane.state import StateStore
from engine.db import migrations
from engine.log import resolve_db_path
from search_controller import (
    CAPTCHA_PROXY_ROTATION_REASON,
    CAPTCHA_SOLVE_RETRIES,
    CAPTCHA_SOLVE_SESSION_LIMIT,
    SearchController,
)


SITEKEY = "6L-captcha-sitekey"
DATA_S = "data-s-value"
PAGE_URL = "https://www.google.com/search?q=wireless+keyboard"


class SearchBoxElement(FakeElement):
    """Поисковое поле: есть clear, который у голого FakeElement отсутствует."""

    def clear(self):
        return None


class CaptchaDriver(FakeDriver):
    """Драйвер для детекта CAPTCHA: ``#recaptcha`` появляется по сценарию.

    ``captcha_after`` — сколько первых проверок проходят без капчи (0 —
    видна сразу). ``search_box`` — поведение поискового поля: ``ready``
    отдаёт элемент, ``not_interactable`` бросает ровно то, что ловит
    ``search_for_ads`` на своей второй ветке детекта.
    """

    def __init__(
        self,
        *,
        captcha_after=0,
        search_box="ready",
        screenshot="ok",
        page_url=PAGE_URL,
    ):
        super().__init__()
        self.captcha_after = captcha_after
        self.search_box_mode = search_box
        self.screenshot_mode = screenshot
        self.current_url = page_url
        self.recaptcha_checks = 0
        self.search_boxes = []
        self.screenshot_paths = []
        self.browser_cookies = [{"name": "sid", "value": "cookie-value"}]

    def find_element(self, by, value=None):
        if value == "recaptcha":
            self.recaptcha_checks += 1
            if self.recaptcha_checks > self.captcha_after:
                return FakeElement({"data-sitekey": SITEKEY, "data-s": DATA_S})
            raise NoSuchElementException("recaptcha не показан")

        if by == By.NAME and value == "q":
            if self.search_box_mode == "not_interactable":
                raise ElementNotInteractableException("поисковое поле ещё не готово")
            box = SearchBoxElement()
            self.search_boxes.append(box)
            return box

        raise NoSuchElementException(f"нет элемента {by}={value}")

    def get_screenshot_as_file(self, path):
        if self.screenshot_mode == "raises":
            raise WebDriverException("браузер уже умер")
        if self.screenshot_mode == "false":
            return False
        Path(path).write_bytes(b"\x89PNG fake screenshot")
        self.screenshot_paths.append(path)
        return True

    def get_cookies(self):
        return list(self.browser_cookies)


# --- окружение: прокси, воркер и активный run в общей тестовой БД -------------


@pytest.fixture
def captcha_env(monkeypatch):
    """Сессия воркера в тестовой БД: прокси, строка worker'а и активный run.

    БД — общая на сессию (``ADCLICKER_DB`` из ``tests/conftest.py``), поэтому
    ``browser_id`` уникален на тест: события и счётчики читаются только по нему.
    Лимит решений за сессию сбрасывается здесь же — он живёт на классе и
    переживал бы тесты.
    """

    db = Path(resolve_db_path())
    migrations.migrate(db)
    store = StateStore(db)

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT id FROM proxies WHERE host = ? AND port = ?", ("10.0.0.1", 8080)
        ).fetchone()
        if row is not None:
            proxy_id = row[0]
        else:
            cursor = conn.execute(
                "INSERT INTO proxies (host, port) VALUES ('10.0.0.1', 8080)"
            )
            proxy_id = cursor.lastrowid
            conn.commit()

    browser_id = f"br-{uuid.uuid4().hex[:12]}"
    worker_id = store.register_worker(browser_id, pid=None)
    assert proxy_id is not None, "строка прокси создана или найдена выше"
    store.assign_proxy(browser_id, proxy_id)
    run_id = store.start_run(worker_id)

    monkeypatch.setattr(SearchController, "_solve_attempts_used", 0)

    # Как в проде (ad_clicker.run_scenario): общий логгер процесса
    # перебиндован на воркер, поэтому строки logs/captcha_events фильтруются
    # по browser_id теста. Биндинг возвращается в teardown — он глобальный.
    logger = search_controller.log
    previous_binding = logger.browser_id
    logger.bind(browser_id)
    try:
        yield SimpleNamespace(
            db=db, store=store, browser_id=browser_id, proxy_id=proxy_id, run_id=run_id
        )
    finally:
        logger.bind(previous_binding)


def make_controller(env, driver):
    controller = SearchController(driver, "wireless keyboard")
    controller.set_browser_id(env.browser_id)
    return controller


def _events(db, browser_id):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM captcha_events WHERE browser_id = ? ORDER BY id",
            (browser_id,),
        ).fetchall()


def _run(db, run_id):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT captcha_seen, captcha_solved FROM runs WHERE id = ?", (run_id,)
        ).fetchone()


def _log_messages(db, browser_id, category="captcha"):
    search_controller.log.flush()
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT message FROM logs WHERE browser_id = ? AND category = ? ORDER BY id",
            (browser_id, category),
        ).fetchall()
    return [row[0] for row in rows]


def _solve_spy(monkeypatch, result: Optional[str] = "RESP-CODE"):
    """Подменить ``solve_recaptcha``: записывает вызовы, отдаёт ``result``."""

    calls = []

    def fake_solve(**kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(search_controller, "solve_recaptcha", fake_solve)
    return calls


def _worker(db, browser_id):
    """Строка ``workers`` воркера: статус и причина, которые читает супервизор."""

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT status, last_error FROM workers WHERE browser_id = ?",
            (browser_id,),
        ).fetchone()


class RotationSpy:
    """Логгер-шпион сигнала ротации поверх настоящего ``search_controller.log``.

    Все вызовы прокидываются в реальный логгер — событие, счётчики и статус
    ``workers`` в тестах остаются настоящими, — а порядок пары
    ``record_captcha_event`` / ``mark_degraded`` запоминается: сигнал обязан
    идти после строки в ``captcha_events``, иначе супервизор мог бы погасить
    процесс раньше, чем событие доедет до БД.
    """

    def __init__(self, real):
        self._real = real
        self.timeline: list[tuple[str, object]] = []

    @property
    def degraded(self) -> list[object]:
        """Причины, с которыми ушёл сигнал ротации."""

        return [payload for kind, payload in self.timeline if kind == "degraded"]

    @property
    def captcha_events(self) -> list[object]:
        """``solved`` каждого события, прошедшего через логгер."""

        return [payload for kind, payload in self.timeline if kind == "event"]

    def record_captcha_event(self, *args, **kwargs):
        self.timeline.append(("event", kwargs.get("solved")))
        return self._real.record_captcha_event(*args, **kwargs)

    def mark_degraded(self, reason, browser_id=None):
        self.timeline.append(("degraded", reason))
        return self._real.mark_degraded(reason, browser_id=browser_id)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def rotation(captcha_env, monkeypatch):
    """Шпион сигнала ротации поверх логгера процесса (зависимость от ``captcha_env``)."""

    spy = RotationSpy(search_controller.log)
    monkeypatch.setattr(search_controller, "log", spy)
    return spy


# --- единая точка: каждая ветка детекта даёт одно событие -----------------------


class TestDetectionBranches:
    """Три точки вызова ``_check_captcha`` в ``search_for_ads``."""

    def test_branch_before_search_records_one_event(self, captcha_env):
        driver = CaptchaDriver(captcha_after=0)
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller.search_for_ads(blocked_domains=[])

        rows = _events(captcha_env.db, captcha_env.browser_id)
        assert len(rows) == 1, "детект на входе даёт ровно одно событие"
        assert driver.recaptcha_checks == 1
        assert driver.search_boxes == [], "поисковое поле ещё не искалось"
        assert controller._stats.captcha_seen is True

        row = rows[0]
        assert row["browser_id"] == captcha_env.browser_id
        assert row["proxy_id"] == captcha_env.proxy_id
        assert row["page_url"] == PAGE_URL
        assert row["sitekey"] == SITEKEY
        assert row["solved"] == 0
        assert row["solver"] is None
        assert row["elapsed_ms"] is None
        assert row["screenshot_path"] is not None
        assert row["ts"] == pytest.approx(time.time(), abs=30)

        assert _run(captcha_env.db, captcha_env.run_id)["captcha_seen"] == 1

    def test_branch_on_uninteractable_search_box_records_one_event(self, captcha_env):
        driver = CaptchaDriver(captcha_after=1, search_box="not_interactable")
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller.search_for_ads(blocked_domains=[])

        rows = _events(captcha_env.db, captcha_env.browser_id)
        assert len(rows) == 1
        assert driver.recaptcha_checks == 2, (
            "первая проверка без капчи, вторая — в ветке ElementNotInteractable"
        )
        assert driver.search_boxes == [], "поле не вернулось — капча до него"
        assert _run(captcha_env.db, captcha_env.run_id)["captcha_seen"] == 1

    def test_branch_after_typing_query_records_one_event(self, captcha_env):
        driver = CaptchaDriver(captcha_after=1, search_box="ready")
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller.search_for_ads(blocked_domains=[])

        rows = _events(captcha_env.db, captcha_env.browser_id)
        assert len(rows) == 1
        assert driver.recaptcha_checks == 2, (
            "первая проверка без капчи, вторая — после набора запроса"
        )
        assert len(driver.search_boxes) == 1, "запрос набран до детекта"
        assert "wireless" in "".join(driver.search_boxes[0].recorded_keys)
        assert _run(captcha_env.db, captcha_env.run_id)["captcha_seen"] == 1

    def test_event_is_recorded_in_exactly_one_place(self):
        """Структурный контракт: запись строки — только в единой точке.

        Ветки детекта могут звать её столько, сколько нужно, но обход
        минуя ``_record_captcha_event`` (свой INSERT, свой writer) означал бы
        второе место правды для одной таблицы.
        """

        source = Path(search_controller.__file__).read_text(encoding="utf-8")

        assert source.count("log.record_captcha_event(") == 1
        assert source.count("def _record_captcha_event(") == 1


# --- скриншот ------------------------------------------------------------------


class TestScreenshot:
    def test_screenshot_is_saved_with_browser_id_and_epoch_in_the_name(self, captcha_env):
        driver = CaptchaDriver()
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        path = Path(_events(captcha_env.db, captcha_env.browser_id)[0]["screenshot_path"])
        assert path.parent == Path.cwd() / "engine" / "screenshots", (
            "каталог engine/screenshots (лежит в .gitignore), путь в БД — как записан"
        )
        assert re.fullmatch(
            rf"{re.escape(captcha_env.browser_id)}_\d+\.png", path.name
        ), "имя — только browser_id и epoch, без кредов и URL"
        assert path.is_file(), "скриншот реально записан"
        assert [str(path)] == driver.screenshot_paths

    @pytest.mark.parametrize("failure", ["raises", "false"])
    def test_refused_screenshot_does_not_break_the_event(self, captcha_env, failure):
        driver = CaptchaDriver(screenshot=failure)
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        rows = _events(captcha_env.db, captcha_env.browser_id)
        assert len(rows) == 1, "событие обязано пережить отказ скриншота"
        assert rows[0]["screenshot_path"] is None
        screenshots = Path.cwd() / "engine" / "screenshots"
        assert not screenshots.exists() or not list(screenshots.glob("*.png"))

        messages = _log_messages(captcha_env.db, captcha_env.browser_id)
        assert any("creenshot" in message for message in messages), (
            "отказ скриншота должен быть виден в логе"
        )


# --- telegram ------------------------------------------------------------------


class TestTelegram:
    @pytest.fixture
    def notifier(self, monkeypatch):
        """Заглушка ``telegram_notifier``: пакета telegram в тестах нет."""

        calls = []
        module = types.ModuleType("telegram_notifier")

        def notify_captcha_event(**kwargs):
            calls.append(kwargs)

        setattr(module, "notify_captcha_event", notify_captcha_event)
        monkeypatch.setitem(sys.modules, "telegram_notifier", module)
        return calls

    def test_disabled_flag_sends_nothing_but_event_is_recorded(
        self, captcha_env, notifier, behavior
    ):
        assert behavior.telegram_enabled is False
        driver = CaptchaDriver()
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert notifier == []
        assert len(_events(captcha_env.db, captcha_env.browser_id)) == 1

    def test_enabled_flag_notifies_on_the_event_itself(
        self, captcha_env, notifier, behavior, monkeypatch
    ):
        monkeypatch.setattr(behavior, "telegram_enabled", True)
        driver = CaptchaDriver()
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert len(notifier) == 1, "одно уведомление на одно событие"
        payload = notifier[0]
        assert payload["browser_id"] == captcha_env.browser_id
        assert payload["page_url"] == PAGE_URL
        assert payload["solved"] is False
        assert payload["screenshot_path"] is not None

    def test_notifier_failure_does_not_break_the_scenario(
        self, captcha_env, notifier, behavior, monkeypatch
    ):
        monkeypatch.setattr(behavior, "telegram_enabled", True)

        def broken(**kwargs):
            raise RuntimeError("telegram недоступен")

        monkeypatch.setattr(notifier_module(), "notify_captcha_event", broken, raising=False)
        driver = CaptchaDriver()
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert len(_events(captcha_env.db, captcha_env.browser_id)) == 1

    def test_missing_notifier_module_is_not_fatal(self, captcha_env, behavior, monkeypatch):
        """Ошибка импорта (нет пакета telegram) не роняет событие."""

        monkeypatch.setattr(behavior, "telegram_enabled", True)
        monkeypatch.setitem(sys.modules, "telegram_notifier", None)
        driver = CaptchaDriver()
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert len(_events(captcha_env.db, captcha_env.browser_id)) == 1


def notifier_module():
    return sys.modules["telegram_notifier"]


# --- политики ------------------------------------------------------------------


class TestStopPolicy:
    """``stop`` (дефолт): событие, скриншот, telegram и ожидание оператора."""

    def test_default_policy_is_stop_and_scenario_waits(self, captcha_env, monkeypatch):
        solve_calls = _solve_spy(monkeypatch)
        driver = CaptchaDriver()
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert solve_calls == [], "stop не решает капчу, даже если ключ есть"
        row = _events(captcha_env.db, captcha_env.browser_id)[0]
        assert row["solved"] == 0
        assert row["solver"] is None
        assert row["elapsed_ms"] is None
        assert row["screenshot_path"] is not None, "стоп-ветка идёт со скриншотом"

    def test_stop_waits_even_with_api_key_configured(
        self, captcha_env, behavior, monkeypatch
    ):
        """Изменение семантики: раньше с ключом legacy решал сам."""

        monkeypatch.setattr(behavior, "twocaptcha_apikey", "key-42")
        monkeypatch.setattr(behavior, "captcha_policy", "stop")
        solve_calls = _solve_spy(monkeypatch)
        driver = CaptchaDriver()
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert solve_calls == []

    def test_unknown_policy_falls_back_to_stop_with_a_warning(
        self, captcha_env, behavior, monkeypatch
    ):
        monkeypatch.setattr(behavior, "captcha_policy", "hold-please")
        monkeypatch.setattr(behavior, "twocaptcha_apikey", "key-42")
        solve_calls = _solve_spy(monkeypatch)
        driver = CaptchaDriver()
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert solve_calls == []
        messages = _log_messages(captcha_env.db, captcha_env.browser_id)
        assert any("captcha_policy" in message for message in messages)


class TestSolvePolicy:
    """``solve``: авто-решение через 2captcha, событие с метриками."""

    @pytest.fixture
    def solve_env(self, captcha_env, behavior, monkeypatch):
        monkeypatch.setattr(behavior, "twocaptcha_apikey", "key-42")
        monkeypatch.setattr(behavior, "captcha_policy", "solve")
        return captcha_env

    def test_successful_solve_records_metrics_and_continues(
        self, solve_env, monkeypatch
    ):
        solve_calls = _solve_spy(monkeypatch, result="RESP-CODE")
        driver = CaptchaDriver()
        controller = make_controller(solve_env, driver)

        controller._check_captcha()

        assert solve_calls and solve_calls[0]["apikey"] == "key-42"
        assert solve_calls[0]["sitekey"] == SITEKEY
        assert solve_calls[0]["current_url"] == PAGE_URL
        assert solve_calls[0]["data_s"] == DATA_S
        assert solve_calls[0]["cookies"] == "sid:cookie-value"

        row = _events(solve_env.db, solve_env.browser_id)[0]
        assert row["solved"] == 1
        assert row["solver"] == "2captcha"
        assert isinstance(row["elapsed_ms"], int)
        assert row["elapsed_ms"] >= 0
        assert row["screenshot_path"] is not None

        assert any("g-recaptcha-response=RESP-CODE" in url for url in driver.visited)
        assert controller._stats.captcha_solved is True
        assert SearchController._solve_attempts_used == 1

        run = _run(solve_env.db, solve_env.run_id)
        assert run["captcha_seen"] == 1
        assert run["captcha_solved"] == 1

    def test_failed_solve_stops_scenario_with_the_event(self, solve_env, monkeypatch):
        solve_calls = _solve_spy(monkeypatch, result=None)
        driver = CaptchaDriver()
        controller = make_controller(solve_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert len(solve_calls) == CAPTCHA_SOLVE_RETRIES, "ретраи решают по константе"
        row = _events(solve_env.db, solve_env.browser_id)[0]
        assert row["solved"] == 0
        assert row["solver"] == "2captcha"
        assert isinstance(row["elapsed_ms"], int)
        assert _run(solve_env.db, solve_env.run_id)["captcha_seen"] == 1

    def test_solve_timeout_stops_scenario_and_keeps_the_event(
        self, solve_env, monkeypatch
    ):
        import time as _time

        def hanging_solve(**kwargs):
            _time.sleep(1.0)
            return "TOO-LATE"

        monkeypatch.setattr(search_controller, "solve_recaptcha", hanging_solve)
        monkeypatch.setattr(search_controller, "CAPTCHA_SOLVE_TIMEOUT_S", 0.05)
        driver = CaptchaDriver()
        controller = make_controller(solve_env, driver)

        started = _time.monotonic()
        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert _time.monotonic() - started < 0.9, "таймаут не ждёт зависший сервис"
        row = _events(solve_env.db, solve_env.browser_id)[0]
        assert row["solved"] == 0
        assert row["solver"] == "2captcha"
        messages = _log_messages(solve_env.db, solve_env.browser_id)
        assert any("timed out" in message for message in messages)

    def test_missing_apikey_stops_with_a_readable_log(
        self, captcha_env, behavior, monkeypatch
    ):
        monkeypatch.setattr(behavior, "twocaptcha_apikey", "")
        monkeypatch.setattr(behavior, "captcha_policy", "solve")
        solve_calls = _solve_spy(monkeypatch)
        driver = CaptchaDriver()
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert solve_calls == [], "без ключа решать нечем"
        row = _events(captcha_env.db, captcha_env.browser_id)[0]
        assert row["solved"] == 0
        assert row["solver"] is None
        messages = _log_messages(captcha_env.db, captcha_env.browser_id)
        assert any("2captcha" in message for message in messages), (
            "причина остановки должна называть ключ"
        )

    def test_session_limit_stops_without_calling_the_solver(
        self, solve_env, monkeypatch
    ):
        monkeypatch.setattr(
            SearchController, "_solve_attempts_used", CAPTCHA_SOLVE_SESSION_LIMIT
        )
        solve_calls = _solve_spy(monkeypatch)
        driver = CaptchaDriver()
        controller = make_controller(solve_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert solve_calls == [], "лимит на воркер за сессию исчерпан"
        row = _events(solve_env.db, solve_env.browser_id)[0]
        assert row["solved"] == 0
        assert row["solver"] is None
        messages = _log_messages(solve_env.db, solve_env.browser_id)
        assert any("limit" in message for message in messages)


class TestBothPolicy:
    """``both``: решает, неудача или исчерпание лимита → stop-ветка."""

    @pytest.fixture
    def both_env(self, captcha_env, behavior, monkeypatch):
        monkeypatch.setattr(behavior, "twocaptcha_apikey", "key-42")
        monkeypatch.setattr(behavior, "captcha_policy", "both")
        return captcha_env

    def test_successful_solve_continues(self, both_env, monkeypatch):
        _solve_spy(monkeypatch, result="RESP-BOTH")
        driver = CaptchaDriver()
        controller = make_controller(both_env, driver)

        controller._check_captcha()

        row = _events(both_env.db, both_env.browser_id)[0]
        assert row["solved"] == 1

    def test_failed_solve_falls_back_to_stop(self, both_env, monkeypatch):
        _solve_spy(monkeypatch, result=None)
        driver = CaptchaDriver()
        controller = make_controller(both_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        row = _events(both_env.db, both_env.browser_id)[0]
        assert row["solved"] == 0
        assert row["solver"] == "2captcha"

    def test_exhausted_limit_falls_back_to_stop(self, both_env, monkeypatch):
        monkeypatch.setattr(
            SearchController, "_solve_attempts_used", CAPTCHA_SOLVE_SESSION_LIMIT
        )
        solve_calls = _solve_spy(monkeypatch, result="NEVER")
        driver = CaptchaDriver()
        controller = make_controller(both_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert solve_calls == []
        row = _events(both_env.db, both_env.browser_id)[0]
        assert row["solved"] == 0
        assert row["solver"] is None


# --- ротация прокси: сигнал на каждую остановку из-за капчи -----------------------


class TestProxyRotationSignal:
    """Сигнал ротации прокси на каждой ветке, уводящей прогон в stop.

    Прод-дефект: событие писалось в ``captcha_events``, а сигнала на смену
    прокси не было — воркер работал с тем же IP, и следующий заход снова
    ловил капчу. Сигнал — готовый механизм ``log.mark_degraded`` (им же
    пользуются ``ad_clicker`` при отбраковке пробой и ``webdriver`` при
    ошибке прокси): статус ``degraded`` читает супервизор и подменяет прокси
    на резервный.
    """

    def test_stop_policy_signals_rotation_after_the_event(self, captcha_env, rotation):
        """Политика stop: событие в БД, затем сигнал — и только потом выход."""

        driver = CaptchaDriver()
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert rotation.captcha_events == [False]
        assert rotation.degraded == [CAPTCHA_PROXY_ROTATION_REASON], (
            "stop-ветка обязана просить сменить прокси, иначе следующий заход "
            "уйдёт с того же IP"
        )
        assert rotation.timeline == [
            ("event", False),
            ("degraded", CAPTCHA_PROXY_ROTATION_REASON),
        ], "событие обязано попасть в БД раньше сигнала: супервизор гасит процесс"
        assert len(_events(captcha_env.db, captcha_env.browser_id)) == 1

    def test_signal_reaches_the_store_as_degraded_status(self, captcha_env, rotation):
        """Сигнал — не только зов логгера: строка воркера реально меняется."""

        driver = CaptchaDriver()
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        worker = _worker(captcha_env.db, captcha_env.browser_id)
        assert worker["status"] == "degraded", "супервизор читает именно этот статус"
        assert worker["last_error"] == CAPTCHA_PROXY_ROTATION_REASON

    def test_no_captcha_sends_no_signal(self, captcha_env, rotation):
        """Капчи нет — прокси здоров, ротировать нечего."""

        driver = CaptchaDriver(captcha_after=10_000)
        controller = make_controller(captcha_env, driver)

        controller._check_captcha()

        assert rotation.timeline == []
        assert _events(captcha_env.db, captcha_env.browser_id) == []

    def test_rotation_reason_carries_no_credentials(self):
        """Причина уходит в ``workers.last_error`` и видна в UI — без кредов."""

        reason = CAPTCHA_PROXY_ROTATION_REASON
        assert reason == search_controller.CAPTCHA_PROXY_ROTATION_REASON
        assert "captcha" in reason
        assert "@" not in reason, "user:pass@host в UI и логах быть не должно"
        assert "://" not in reason

    def test_missing_api_key_signals_rotation(self, captcha_env, behavior, rotation, monkeypatch):
        """solve без ключа: событие + stop-ветка → сигнал ротации."""

        monkeypatch.setattr(behavior, "captcha_policy", "solve")
        monkeypatch.setattr(behavior, "twocaptcha_apikey", "")
        solve_calls = _solve_spy(monkeypatch)
        driver = CaptchaDriver()
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert solve_calls == []
        assert rotation.degraded == [CAPTCHA_PROXY_ROTATION_REASON]
        assert rotation.timeline == [
            ("event", False),
            ("degraded", CAPTCHA_PROXY_ROTATION_REASON),
        ]

    def test_exhausted_session_limit_signals_rotation(
        self, captcha_env, behavior, rotation, monkeypatch
    ):
        """Лимит 2captcha за сессию исчерпан до решения: stop → сигнал."""

        monkeypatch.setattr(behavior, "captcha_policy", "solve")
        monkeypatch.setattr(behavior, "twocaptcha_apikey", "key-42")
        monkeypatch.setattr(
            SearchController, "_solve_attempts_used", CAPTCHA_SOLVE_SESSION_LIMIT
        )
        _solve_spy(monkeypatch)
        driver = CaptchaDriver()
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert rotation.degraded == [CAPTCHA_PROXY_ROTATION_REASON]

    def test_limit_hit_inside_the_retry_loop_signals_rotation(
        self, captcha_env, behavior, rotation, monkeypatch
    ):
        """«Не решилось» внутри ретраев: лимит кончился на второй попытке."""

        monkeypatch.setattr(behavior, "captcha_policy", "solve")
        monkeypatch.setattr(behavior, "twocaptcha_apikey", "key-42")
        monkeypatch.setattr(
            SearchController,
            "_solve_attempts_used",
            CAPTCHA_SOLVE_SESSION_LIMIT - 1,
        )
        solve_calls = _solve_spy(monkeypatch, result=None)
        driver = CaptchaDriver()
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert len(solve_calls) == 1, "первая попытка тратит лимит, вторая — стоп"
        row = _events(captcha_env.db, captcha_env.browser_id)[0]
        assert row["solved"] == 0
        assert rotation.degraded == [CAPTCHA_PROXY_ROTATION_REASON]

    def test_missing_page_url_signals_rotation(
        self, captcha_env, behavior, rotation, monkeypatch
    ):
        """Нет URL страницы — решать неоткуда: stop → сигнал."""

        monkeypatch.setattr(behavior, "captcha_policy", "solve")
        monkeypatch.setattr(behavior, "twocaptcha_apikey", "key-42")
        solve_calls = _solve_spy(monkeypatch)
        driver = CaptchaDriver(page_url=None)
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert solve_calls == []
        assert rotation.degraded == [CAPTCHA_PROXY_ROTATION_REASON]

    def test_failed_solve_signals_rotation(self, captcha_env, behavior, rotation, monkeypatch):
        """solve: сервис не решил → stop-ветка → сигнал ротации."""

        monkeypatch.setattr(behavior, "captcha_policy", "solve")
        monkeypatch.setattr(behavior, "twocaptcha_apikey", "key-42")
        _solve_spy(monkeypatch, result=None)
        driver = CaptchaDriver()
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert rotation.captcha_events == [False]
        assert rotation.degraded == [CAPTCHA_PROXY_ROTATION_REASON]
        assert rotation.timeline == [
            ("event", False),
            ("degraded", CAPTCHA_PROXY_ROTATION_REASON),
        ]

    def test_failed_solve_under_both_policy_signals_rotation(
        self, captcha_env, behavior, rotation, monkeypatch
    ):
        """both: неудача решения уходит в stop-ветку, сигнал не теряется."""

        monkeypatch.setattr(behavior, "captcha_policy", "both")
        monkeypatch.setattr(behavior, "twocaptcha_apikey", "key-42")
        _solve_spy(monkeypatch, result=None)
        driver = CaptchaDriver()
        controller = make_controller(captcha_env, driver)

        with pytest.raises(SystemExit):
            controller._check_captcha()

        assert rotation.degraded == [CAPTCHA_PROXY_ROTATION_REASON]

    @pytest.mark.parametrize("policy", ["solve", "both"])
    def test_successful_solve_does_not_signal_rotation(
        self, captcha_env, behavior, rotation, monkeypatch, policy
    ):
        """Решённая капча: сигнал НЕ шлётся — иначе погиб бы сам прогон.

        Ротация = рестарт процесса супервизором (SIGTERM → выдержка →
        SIGKILL): сигнал посреди продолжаемого прогона убил бы его на
        середине и обесценил бы политику solve. Смена прокси после
        решённой капчи откладывается до конца прогона — точка завершения
        (``engine.worker`` / ``ad_clicker.end_search``) вне зоны этой ветки.
        """

        monkeypatch.setattr(behavior, "captcha_policy", policy)
        monkeypatch.setattr(behavior, "twocaptcha_apikey", "key-42")
        _solve_spy(monkeypatch, result="RESP-CODE")
        driver = CaptchaDriver()
        controller = make_controller(captcha_env, driver)

        controller._check_captcha()

        assert rotation.degraded == [], "решённая капча не должна ронять прогон"
        assert rotation.captcha_events == [True]
        assert _events(captcha_env.db, captcha_env.browser_id)[0]["solved"] == 1
        assert _worker(captcha_env.db, captcha_env.browser_id)["status"] != "degraded"
