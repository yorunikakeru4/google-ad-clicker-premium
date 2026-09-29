"""Фикстуры, общие для всех тестов.

Намеренно почти пусто: всё, что нужно legacy-коду (песочница, заглушка
seleniumbase, подмена Selenium), живёт в ``tests/legacy/conftest.py`` и не
должно просачиваться в тесты нового движка. Тесты движка обходятся
стандартными фикстурами pytest и своими фикстурами по каталогам.

Появление общей фикстуры означает, что она действительно нужна обоим слоям —
до этого момента держать её здесь незачем.

Есть, однако, настройка, которая общая по определению: тесты не должны
трогать ``adclicker.db`` разработчика. Модули, переведённые на
``engine.log.get_logger()``, открывают хранилище в ``ADCLICKER_DB`` уже при
импорте, то есть во время сборки, — раньше, чем запускается первая фикстура.
Поэтому переменная выставляется в ``pytest_configure``: до импорта любого
тестового модуля и на всю сессию.
"""

import os
import shutil
import tempfile
from pathlib import Path

# Каталог тестовой БД; None — либо уже задано извне, либо настройка снята.
_test_db_dir: Path | None = None


def pytest_configure(config) -> None:
    """Увести запись логов в отдельный каталог на время тестовой сессии."""

    global _test_db_dir

    if os.environ.get("ADCLICKER_DB"):
        # Запуск с явным путём (например, отладка одной фичи) — не перетираем.
        return

    _test_db_dir = Path(tempfile.mkdtemp(prefix="adclicker-tests-"))
    os.environ["ADCLICKER_DB"] = str(_test_db_dir / "adclicker.db")


def pytest_unconfigure(config) -> None:
    """Убрать тестовую БД вместе с уже открытыми на неё соединениями."""

    global _test_db_dir

    if _test_db_dir is None:
        return

    shutil.rmtree(_test_db_dir, ignore_errors=True)
    os.environ.pop("ADCLICKER_DB", None)
    _test_db_dir = None
