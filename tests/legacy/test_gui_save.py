"""Тесты пути сохранения конфигурации в gui.py.

Сохранение обязано сначала валидировать и только потом трогать config.json:
невалидный конфиг — это список проблем в диалоге, а не изменённый файл,
SystemExit или traceback. customtkinter в nix-окружении нет (план.md §9.1),
поэтому перед импортом gui ставится заглушка модуля: gui импортируется целиком,
но никакие виджеты в тестах не создаются — save_button_callback работает со
словарями, а показ ошибки идёт через tkinter.messagebox и подменяется здесь.
"""

import importlib
import json
import sys
import types
from types import SimpleNamespace

import pytest

from engine.control_plane.config import default_config


def _install_customtkinter_stub() -> None:
    """Заглушка модуля: gui наследует от CTk-классов уже на этапе импорта."""

    stub = types.ModuleType("customtkinter")

    class _Widget:
        def __init__(self, *args, **kwargs) -> None:
            pass

    stub.CTk = _Widget
    stub.CTkFrame = _Widget
    stub.CTkLabel = _Widget
    stub.CTkTextbox = _Widget
    stub.CTkButton = _Widget
    stub.CTkCheckBox = _Widget
    stub.BooleanVar = object
    stub.set_appearance_mode = lambda *args, **kwargs: None
    stub.set_default_color_theme = lambda *args, **kwargs: None
    sys.modules["customtkinter"] = stub


try:
    import customtkinter  # noqa: F401
except ModuleNotFoundError:
    _install_customtkinter_stub()

gui = importlib.import_module("gui")

# Файлы, на которые ссылаются значения путей по умолчанию.
DEFAULT_PATH_FILES = ("user_agents.txt", "domains.txt")


class _Form:
    """Значения формы: три getter'а, которые вызывает save_button_callback."""

    def __init__(self, paths, webdriver, behavior) -> None:
        self._values = {"paths": paths, "webdriver": webdriver, "behavior": behavior}

    def get_paths(self):
        return dict(self._values["paths"])

    def get_webdriver_config(self):
        return dict(self._values["webdriver"])

    def get_behavior_config(self):
        return dict(self._values["behavior"])


def _gui_input(data):
    """Self-двойник для save_button_callback: три фрейма отдают один конфиг."""

    form = _Form(data["paths"], data["webdriver"], data["behavior"])
    return SimpleNamespace(paths_frame=form, webdriver_frame=form, behavior_frame=form)


@pytest.fixture
def workdir(isolated_cwd):
    """Каталог теста с файлами, на которые ссылаются пути по умолчанию."""

    for name in DEFAULT_PATH_FILES:
        (isolated_cwd / name).write_text("placeholder\n", encoding="utf-8")
    return isolated_cwd


@pytest.fixture
def shown_errors(monkeypatch):
    """Перехват модального окна ошибки: в тестах список сообщений, не диалог."""

    messages = []

    def _record(title, message, **kwargs):
        messages.append(message)

    monkeypatch.setattr(gui.messagebox, "showerror", _record)
    return messages


@pytest.fixture
def fresh_reader(monkeypatch):
    """Новый ConfigReader вместо глобального синглтона: видно, был ли перечитан."""

    reader = importlib.import_module("config_reader").ConfigReader()
    monkeypatch.setattr(gui, "config", reader)
    return reader


def test_invalid_config_keeps_file_untouched_and_shows_problem_list(
    workdir, shown_errors, fresh_reader
):
    config_path = workdir / "config.json"
    original = json.dumps({"sentinel": True}, indent=4)
    config_path.write_text(original, encoding="utf-8")

    data = default_config()
    data["behavior"]["ad_page_min_wait"] = 30
    data["behavior"]["ad_page_max_wait"] = 10
    data["behavior"]["browser_count"] = 99
    data["paths"]["user_agents"] = "missing-agents.txt"

    gui.ConfigGUI.save_button_callback(_gui_input(data))

    assert config_path.read_text(encoding="utf-8") == original
    assert len(shown_errors) == 1
    message = shown_errors[0]
    assert "behavior.ad_page_min_wait: " in message
    assert "behavior.ad_page_max_wait: " in message
    assert "behavior.browser_count: " in message
    assert "paths.user_agents: файл не найден: missing-agents.txt" in message
    # невалидный конфиг не перечитывается: глобальный ридер остаётся нетронутым
    assert fresh_reader.paths is None


def test_missing_file_blocks_save_when_everything_else_is_valid(
    workdir, shown_errors, fresh_reader
):
    data = default_config()
    data["paths"]["query_file"] = "no-such-queries.txt"

    gui.ConfigGUI.save_button_callback(_gui_input(data))

    assert not (workdir / "config.json").exists()
    assert shown_errors == ["paths.query_file: файл не найден: no-such-queries.txt"]
    assert fresh_reader.paths is None


def test_valid_config_is_written_and_reread(workdir, shown_errors, fresh_reader):
    (workdir / "queries.txt").write_text("usb hub\n", encoding="utf-8")
    data = default_config()
    data["paths"]["query_file"] = "queries.txt"
    data["behavior"]["browser_count"] = 3

    gui.ConfigGUI.save_button_callback(_gui_input(data))

    assert shown_errors == []
    assert json.loads((workdir / "config.json").read_text(encoding="utf-8")) == data
    assert fresh_reader.paths.query_file == "queries.txt"
    assert fresh_reader.behavior.browser_count == 3


def test_valid_config_without_query_file_is_written(workdir, shown_errors, fresh_reader):
    data = default_config()
    data["behavior"]["browser_count"] = 1

    gui.ConfigGUI.save_button_callback(_gui_input(data))

    assert shown_errors == []
    assert json.loads((workdir / "config.json").read_text(encoding="utf-8")) == data
    assert fresh_reader.behavior.browser_count == 1
