"""UC-режим не должен требовать seleniumbase (поймано живым e2e-прогоном).

Пакет — необязательная зависимость режима ``use_seleniumbase`` (план §9.1:
его нет ни в nix dev-shell, ни в sidecar-бандле; в тестах его заглушали).
Top-level ``import seleniumbase`` в ``webdriver.py`` ронял весь сценарий с
``ModuleNotFoundError`` при ``use_seleniumbase: false`` — воркеры умирали
до браузера, и только заглушка в ``tests/legacy/conftest.py`` маскировала
дефект в юнит-прогонах.

Здесь дефект ловится без заглушки: подпроцесс блокирует импорт seleniumbase
через ``sys.meta_path`` и импортирует ``webdriver`` — UC-ветка обязана
подняться, а при попытке реально зайти в SeleniumBase-ветку — получить
читаемую ошибку, а не ``ModuleNotFoundError`` из глубины модуля.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_BLOCK_IMPORT = textwrap.dedent(
    """
    import sys

    class BlockSeleniumBase:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "seleniumbase":
                raise ImportError("seleniumbase заблокирован тестом")
            return None

    sys.meta_path.insert(0, BlockSeleniumBase())
    import webdriver

    print("IMPORT_OK")
    """
)


def test_webdriver_imports_without_seleniumbase() -> None:
    """UC-режим поднимается, когда seleniumbase недоступен в принципе."""
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
        f"импорт webdriver упал без seleniumbase:\n{result.stderr}"
    )
    assert "IMPORT_OK" in result.stdout


def test_seleniumbase_mode_gives_readable_error_when_package_missing(
    monkeypatch,
) -> None:
    """Попытка зайти в режим без пакета — читаемая ошибка, не ModuleNotFoundError."""
    import importlib

    import webdriver

    monkeypatch.setitem(sys.modules, "seleniumbase", None)
    importlib.reload(webdriver)

    try:
        with pytest.raises(RuntimeError, match="seleniumbase"):
            webdriver.create_seleniumbase_driver("user:pass@host:80")
    finally:
        # Вернуть модуль на место для остальных тестов сессии.
        sys.modules.pop("seleniumbase", None)
        importlib.reload(webdriver)
