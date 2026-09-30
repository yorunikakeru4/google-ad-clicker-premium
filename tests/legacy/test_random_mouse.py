"""Деградация ``random_mouse`` при недоступном pyautogui (план.md, фаза 11).

Риск из плана §6: ``pyautogui`` на macOS требует Accessibility, а на headless-
Linux не стартует без ``DISPLAY`` — случайное движение мыши не имеет права быть
обязательным («не делать ``random_mouse`` обязательным, деградация с
предупреждением»). Контракт, зафиксированный здесь:

* ни одна недоступность (нет пакета, нет дисплея/разрешения, FailSafe, прочие
  ошибки pyautogui) не выходит наружу из ``_make_random_mouse_movements``;
* прочие ошибки дают ровно ОДИН структурированный WARNING (категория
  ``browser``, поле ``reason``, без traceback) и выключают
  ``_random_mouse_enabled`` — импорт на остаток сессии не повторяется, сцена не
  падает и не спамит;
* FailSafe (курсор в углу) сохраняет легаси-поведение: лог, ``FAILSAFE=False``,
  возврат в центр — и не падает NameError, даже когда ошибка случилась до
  вычисления размеров экрана;
* при рабочем pyautogui движения вызываются как раньше, флаг не гасится;
* поток из ``_start_random_action_threads`` завершается без трейсбека в stderr.

pyautogui подменяется модулем в ``sys.modules`` либо падающим импортом — тесты
не трогают дисплей и сеть.
"""

import builtins
import json
import logging
import sqlite3
import sys
from types import ModuleType

import pytest

import search_controller
from conftest import FakeDriver
from engine.log import resolve_db_path

DISABLED_MESSAGE = "Random mouse movements disabled"
CORNER_MESSAGE = "The mouse cursor was moved to one of the screen corners!"


class FailSafeError(Exception):
    """Аналог pyautogui.FailSafeException для заглушки."""


class PyAutoGUIError(Exception):
    """Аналог pyautogui.PyAutoGUIException: отказ доступа к дисплею/клавиатуре."""


# --- чтение структурированного лога ------------------------------------------


def _max_log_id() -> int:
    """id последней записи в ``logs``: граница «до» для подсчёта новых строк."""

    with sqlite3.connect(resolve_db_path()) as conn:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM logs").fetchone()[0]


def _rows_since(mark: int, message: str) -> list[sqlite3.Row]:
    """Записи ``logs`` с данным message, записанные после ``mark``."""

    with sqlite3.connect(resolve_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT level, category, message, fields FROM logs "
            "WHERE id > ? AND message = ? ORDER BY id",
            (mark, message),
        ).fetchall()


# --- заглушка pyautogui -------------------------------------------------------


class StubPyAutoGUI(ModuleType):
    """Модуль-заглушка pyautogui: записывает вызовы, ничего не двигая.

    Очереди ``*_errors`` отдают по одному исключению на вызов, дальше вызовы
    проходят — так воспроизводится FailSafe ровно на первом движении (как в
    проде, когда курсор стартует в углу) и «отказ доступа» после успешного
    чтения размеров экрана.
    """

    def __init__(self, width: int = 1920, height: int = 1200) -> None:
        super().__init__("pyautogui")
        self.FAILSAFE = True
        self.easeInQuad = "easeInQuad"
        self.easeOutQuad = "easeOutQuad"
        self.easeInOutQuad = "easeInOutQuad"
        self.width = width
        self.height = height
        self.FailSafeException = FailSafeError
        self.calls: list[tuple] = []
        self.size_errors: list[Exception] = []
        self.move_to_errors: list[Exception] = []
        self.move_errors: list[Exception] = []

    def size(self):
        self.calls.append(("size", ()))
        if self.size_errors:
            raise self.size_errors.pop(0)
        return (self.width, self.height)

    def position(self):
        self.calls.append(("position", ()))
        return (0, 0)

    def moveTo(self, x, y, *args, **kwargs):  # noqa: N802 - имя API pyautogui
        self.calls.append(("moveTo", (x, y)))
        if self.move_to_errors:
            raise self.move_to_errors.pop(0)

    def move(self, *args, **kwargs):
        self.calls.append(("move", args))
        if self.move_errors:
            raise self.move_errors.pop(0)

    def scroll(self, *args, **kwargs):
        self.calls.append(("scroll", args))


@pytest.fixture
def stub_pyautogui(monkeypatch):
    """Здоровый pyautogui в sys.modules: вызовы записываются, дисплей не нужен."""

    stub = StubPyAutoGUI()
    monkeypatch.setitem(sys.modules, "pyautogui", stub)
    return stub


@pytest.fixture
def broken_pyautogui_import(monkeypatch):
    """Импорт pyautogui падает (нет пакета / нет дисплея).

    Считает попытки: деградация обязана не повторять импорт на каждой сцене.
    """

    state = {"calls": 0, "error": ImportError("No module named 'pyautogui'")}
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pyautogui":
            state["calls"] += 1
            raise state["error"]
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    return state


@pytest.fixture
def make_random_mouse_controller(make_search_controller, behavior, monkeypatch):
    """Фабрика контроллера с включённым ``random_mouse``.

    В песочнице флаг выключен, а таймауты потоков большие (10–20 с) — для
    теста потоков они ужимаются, чтобы ``join`` не тянул секунды.
    """

    def _make(driver=None, ad_page_max_wait=None, nonad_page_max_wait=None):
        monkeypatch.setattr(behavior, "random_mouse", True)
        if ad_page_max_wait is not None:
            monkeypatch.setattr(behavior, "ad_page_max_wait", ad_page_max_wait)
        if nonad_page_max_wait is not None:
            monkeypatch.setattr(behavior, "nonad_page_max_wait", nonad_page_max_wait)
        return make_search_controller(driver=driver)

    return _make


# --- недоступность: импорт падает ---------------------------------------------


@pytest.mark.parametrize(
    ("error", "reason_prefix"),
    [
        (ImportError("No module named 'pyautogui'"), "ImportError:"),
        (KeyError("DISPLAY"), "KeyError:"),
    ],
    ids=["missing-package", "no-display"],
)
def test_import_failure_logs_one_warning_and_disables_random_mouse(
    make_random_mouse_controller, broken_pyautogui_import, error, reason_prefix
):
    """Нет пакета или дисплея — одна запись WARNING и флаг выключен."""

    broken_pyautogui_import["error"] = error
    mark = _max_log_id()
    controller = make_random_mouse_controller()

    controller._make_random_mouse_movements()  # наружу исключение не выходит

    search_controller.log.flush()

    assert controller._random_mouse_enabled is False
    rows = _rows_since(mark, DISABLED_MESSAGE)
    assert [(row["level"], row["category"]) for row in rows] == [("WARNING", "browser")]
    reason = json.loads(rows[0]["fields"])["reason"]
    assert reason.startswith(reason_prefix)
    assert "Traceback (most recent call last)" not in reason


def test_random_mouse_is_not_retried_after_degradation(
    make_random_mouse_controller, broken_pyautogui_import
):
    """После первой ошибки импорт не повторяется и WARNING не спамит."""

    mark = _max_log_id()
    controller = make_random_mouse_controller()

    controller._make_random_mouse_movements()
    controller._make_random_mouse_movements()
    search_controller.log.flush()

    assert broken_pyautogui_import["calls"] == 1
    assert len(_rows_since(mark, DISABLED_MESSAGE)) == 1


def test_disabled_flag_short_circuits_without_touching_pyautogui(
    make_random_mouse_controller, stub_pyautogui
):
    """Выключенный флаг не доходит до pyautogui вообще."""

    controller = make_random_mouse_controller()
    controller._random_mouse_enabled = False

    controller._make_random_mouse_movements()

    assert stub_pyautogui.calls == []


# --- прочие ошибки pyautogui --------------------------------------------------


def test_pyautogui_runtime_error_disables_random_mouse_with_single_warning(
    make_random_mouse_controller, stub_pyautogui, caplog
):
    """Отказ доступа (Accessibility) — один WARNING, повторных движений нет."""

    stub_pyautogui.move_to_errors.append(PyAutoGUIError("PyAutoGUI failed to secure a lock"))
    mark = _max_log_id()
    controller = make_random_mouse_controller()

    with caplog.at_level(logging.WARNING, logger="logger"):
        controller._make_random_mouse_movements()
        controller._make_random_mouse_movements()
    search_controller.log.flush()

    assert controller._random_mouse_enabled is False
    assert len([call for call in stub_pyautogui.calls if call[0] == "moveTo"]) == 1

    rows = _rows_since(mark, DISABLED_MESSAGE)
    assert [(row["level"], row["category"]) for row in rows] == [("WARNING", "browser")]
    assert json.loads(rows[0]["fields"])["reason"].startswith("PyAutoGUIError:")

    mirrored = [
        record
        for record in caplog.records
        if record.name == "logger" and record.getMessage().startswith(DISABLED_MESSAGE)
    ]
    assert len(mirrored) == 1
    assert mirrored[0].levelno == logging.WARNING
    assert mirrored[0].category == "browser"
    assert mirrored[0].exc_info is None


def test_working_pyautogui_still_moves_the_mouse(
    make_random_mouse_controller, stub_pyautogui
):
    """Рабочий pyautogui: движения вызываются, флаг не гасится."""

    mark = _max_log_id()
    controller = make_random_mouse_controller()

    controller._make_random_mouse_movements()
    search_controller.log.flush()

    assert controller._random_mouse_enabled is True
    assert ("moveTo", (1920 / 2 - 300, 1200 / 2 - 200)) in stub_pyautogui.calls
    assert any(call[0] == "move" for call in stub_pyautogui.calls)
    assert _rows_since(mark, DISABLED_MESSAGE) == []


# --- FailSafe: курсор в углу --------------------------------------------------


def test_failsafe_logs_resets_flag_and_returns_cursor_to_center(
    make_random_mouse_controller, stub_pyautogui
):
    """Легаси-поведение FailSafe: лог, FAILSAFE=False, возврат в центр."""

    stub_pyautogui.move_to_errors.append(FailSafeError("fail-safe triggered from moveTo"))
    mark = _max_log_id()
    controller = make_random_mouse_controller()

    controller._make_random_mouse_movements()
    search_controller.log.flush()

    assert controller._random_mouse_enabled is True
    assert stub_pyautogui.FAILSAFE is False
    assert ("moveTo", (1920 / 2, 1200 / 2)) in stub_pyautogui.calls
    assert _rows_since(mark, CORNER_MESSAGE)
    assert _rows_since(mark, DISABLED_MESSAGE) == []


def test_failsafe_before_screen_size_does_not_raise_name_error(
    make_random_mouse_controller, stub_pyautogui
):
    """FailSafe до вычисления размеров экрана: возврат в центр не падает NameError."""

    stub_pyautogui.size_errors.append(FailSafeError("fail-safe triggered from size"))
    mark = _max_log_id()
    controller = make_random_mouse_controller()

    controller._make_random_mouse_movements()
    search_controller.log.flush()

    assert controller._random_mouse_enabled is True
    assert stub_pyautogui.FAILSAFE is False
    assert ("moveTo", (1920 / 2, 1200 / 2)) in stub_pyautogui.calls
    assert _rows_since(mark, DISABLED_MESSAGE) == []


# --- поток из _start_random_action_threads -------------------------------------


class ScrollFriendlyDriver(FakeDriver):
    """FakeDriver, отдающий числа в execute_script.

    ``_is_scroll_at_the_end`` вычитает значения из ``execute_script``: у
    наследника из conftest там ``None``, и скролл-поток упал бы TypeError —
    трейсбек в stderr имеет отношения к скроллу, а не к pyautogui.
    """

    def execute_script(self, script, *args):
        self.scripts.append(script)
        return 1000 if "scrollHeight" in script else 0


def test_random_action_thread_finishes_cleanly_with_broken_pyautogui(
    make_random_mouse_controller, broken_pyautogui_import, capsys
):
    """Поток с битым pyautogui завершается без трейсбека в stderr."""

    controller = make_random_mouse_controller(
        driver=ScrollFriendlyDriver(), ad_page_max_wait=1, nonad_page_max_wait=1
    )
    mark = _max_log_id()

    controller._start_random_action_threads()
    search_controller.log.flush()

    stderr = capsys.readouterr().err
    assert "Traceback (most recent call last)" not in stderr
    assert controller._random_mouse_enabled is False
    assert broken_pyautogui_import["calls"] == 1
    assert [(row["level"], row["category"]) for row in _rows_since(mark, DISABLED_MESSAGE)] == [
        ("WARNING", "browser")
    ]
