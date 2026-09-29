"""Фикстуры, общие для всех тестов.

Намеренно почти пусто: всё, что нужно legacy-коду (песочница, заглушка
seleniumbase, подмена Selenium), живёт в ``tests/legacy/conftest.py`` и не
должно просачиваться в тесты нового движка. Тесты движка обходятся
стандартными фикстурами pytest и своими фикстурами по каталогам.

Появление общей фикстуры означает, что она действительно нужна обоим слоям —
до этого момента держать её здесь незачем.

Есть, однако, настройка, которая общая по определению: тесты не должны
трогать ``adclicker.db`` разработчика. Модули, переведённые на
``engine.log.get_logger()``, открывают хранилище в ``ADCLICKER_DB`` при
импорте, а ``tests/legacy/conftest.py`` импортирует legacy-модули прямо в
своём ``pytest_configure`` — то есть раньше, чем доходит дело до фикстур.
Поэтому переменная выставляется на этапе импорта этого conftest'а: он лежит
выше ``tests/legacy/`` и загружается первым.
"""

import os
import shutil
import tempfile
from pathlib import Path

# Каталог тестовой БД; None — либо путь задан извне, либо настройка снята.
_test_db_dir: Path | None = None

if not os.environ.get("ADCLICKER_DB"):
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
