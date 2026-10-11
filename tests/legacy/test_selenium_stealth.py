"""Флаг use_selenium_stealth: ленивый импорт и набор патчей (план §17).

Пакет selenium-stealth — необязательная зависимость (нет ни в nix-среде, ни
в sidecar-бандле; ставится pip'ом в venv поверх dev-shell). UC-режим с
выключенным флагом не должен его требовать, а включённый флаг без пакета —
падать читаемой ошибкой, а не ``ModuleNotFoundError`` из глубины модуля
(тот же контракт, что у seleniumbase, см. test_lazy_seleniumbase.py).

Набор применяемых патчей зафиксирован тестом: два патча пакета
(``user_agent_override``, ``navigator_languages``) сознательно не
применяются — см. докстринг ``webdriver.execute_selenium_stealth``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

import webdriver

REPO_ROOT = Path(__file__).resolve().parents[2]

_BLOCK_IMPORT = textwrap.dedent(
    """
    import sys

    class BlockSeleniumStealth:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "selenium_stealth":
                raise ImportError("selenium_stealth заблокирован тестом")
            return None

    sys.meta_path.insert(0, BlockSeleniumStealth())
    import webdriver

    print("IMPORT_OK")
    """
)

# Патчи, которые обязаны применяться, — ровно и в этом порядке.
EXPECTED_PATCHES = (
    "with_utils",
    "chrome_app",
    "chrome_runtime",
    "iframe_content_window",
    "media_codecs",
    "navigator_permissions",
    "navigator_plugins",
    "navigator_vendor",
    "navigator_webdriver",
    "webgl_vendor_override",
    "window_outerdimensions",
    "hairline_fix",
)

# Пакетные патчи, применяемые сознательно НЕ бывают (см. докстринг).
FORBIDDEN_PATCHES = ("user_agent_override", "navigator_languages")


@pytest.fixture
def selenium_stealth_stub(monkeypatch):
    """Модуль selenium_stealth в sys.modules: только записывает вызовы."""

    calls: list[tuple[str, tuple, dict]] = []

    module = ModuleType("selenium_stealth")

    def _record(name):
        def _fn(driver, *args, **kwargs):
            calls.append((name, (driver, *args), kwargs))

        return _fn

    for name in EXPECTED_PATCHES + FORBIDDEN_PATCHES:
        setattr(module, name, _record(name))

    monkeypatch.setitem(sys.modules, "selenium_stealth", module)
    return calls


def test_webdriver_imports_without_selenium_stealth() -> None:
    """Модуль поднимается, когда selenium_stealth недоступен в принципе."""

    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(REPO_ROOT))

    result = subprocess.run(
        [sys.executable, "-c", _BLOCK_IMPORT],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=120,
    )

    assert result.returncode == 0, (
        f"импорт webdriver упал без selenium_stealth:\n{result.stderr}"
    )
    assert "IMPORT_OK" in result.stdout


def test_stealth_flag_without_package_gives_readable_error(monkeypatch) -> None:
    """Включённый флаг без пакета — RuntimeError с читаемым текстом."""

    monkeypatch.setitem(sys.modules, "selenium_stealth", None)

    with pytest.raises(RuntimeError, match="selenium-stealth"):
        webdriver.execute_selenium_stealth(object())


def test_patch_set_is_the_documented_one(selenium_stealth_stub) -> None:
    """Применяются ровно задокументированные патчи, в заданном порядке."""

    driver = object()
    webdriver.execute_selenium_stealth(driver)

    applied = [name for name, _args, _kwargs in selenium_stealth_stub]

    assert applied == list(EXPECTED_PATCHES)
    assert all(args[0] is driver for _name, args, _kwargs in selenium_stealth_stub)


def test_webgl_vendor_is_pinned_to_the_documented_pair(selenium_stealth_stub) -> None:
    """webgl_vendor_override получает фиксированную пару (Intel), не kwargs."""

    webdriver.execute_selenium_stealth(object())

    _name, args, kwargs = next(
        call for call in selenium_stealth_stub if call[0] == "webgl_vendor_override"
    )

    assert args[1:] == ("Intel Inc.", "Intel Iris OpenGL Engine")
    assert kwargs == {}


def test_navigator_vendor_gets_the_required_google_string(selenium_stealth_stub) -> None:
    """navigator_vendor требует строку вендора — передаётся "Google Inc."."""

    webdriver.execute_selenium_stealth(object())

    _name, args, kwargs = next(
        call for call in selenium_stealth_stub if call[0] == "navigator_vendor"
    )

    assert args[1:] == ("Google Inc.",)
    assert kwargs == {}
