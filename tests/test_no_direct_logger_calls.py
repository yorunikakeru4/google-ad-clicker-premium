"""Линт-контракт миграции логирования (план.md, фаза 3).

Прямые вызовы ``logger.debug/info/warning/error(...)`` в коде — это legacy-лог
из ``logger.py``, который до фазы 9 остаётся единственным дублёром в файле и
в консоли. Каждая такая запись должна уходить через ``engine.log.get_logger()``
(StructuredLogger) с категорией и полями, чтобы попасть в таблицу ``logs``.

Поэтому проверка инвертирована: во всём репозитории прямые вызовы разрешены
только в белом списке — в самом legacy-модуле ``logger.py``, в модулях, которые
ещё не мигрируют (``config_reader.py``, ``gui.py`` — свои фичи), и в тестах,
которые проверяют или подменяют логгер.

Тест читает файлы по тексту, а не по AST: он должен падать с понятным
сообщением (файл:строка) на первом нарушении, а не молча пропустить новый
прямой вызов, добавленный в легаси-коде.
"""

from __future__ import annotations

import re
from pathlib import Path

# Корень репозитория: tests/test_...py -> на уровень выше.
REPO_ROOT = Path(__file__).resolve().parents[1]

# Прямой вызов метода уровня у переменной с именем logger. Взгляд назад не
# даёт совпасть с ``legacy_logger.debug(`` и похожими именами переменных.
DIRECT_CALL = re.compile(r"(?<!\w)logger\.(?:debug|info|warning|error)\s*\(")

# Файлы, которым разрешено звать legacy-логгер напрямую.
WHITELIST_FILES = frozenset({"logger.py", "config_reader.py", "gui.py"})

# Каталоги, которым разрешено (тесты ловят и подменяют логгер).
WHITELIST_DIRS = frozenset({"tests"})

# Файлы, которые фаза 3 обязана перевести на структурированные вызовы.
# Они намеренно НЕ в белом списке: попытка «отбелить» мигрируемый файл
# проваливает тест ниже, а не делает линт тише.
MIGRATION_TARGETS = (
    "adb.py",
    "ad_clicker.py",
    "clicklogs_db.py",
    "engine/baseline.py",
    "engine/cdp.py",
    "engine/proxy_auth.py",
    "geolocation_db.py",
    "hooks.py",
    "proxy.py",
    "run_ad_clicker.py",
    "run_in_loop.py",
    "search_controller.py",
    "telegram_notifier.py",
    "utils.py",
    "webdriver.py",
)

# Сторонний и сгенерированный код: не наша зона ответственности.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".worktrees",
        ".venv",
        ".pytest_cache",
        "__pycache__",
        "build",
        "dist",
        "docs",
        "env",
        "node_modules",
        "ui",
        "venv",
    }
)


def _python_files() -> list[Path]:
    found = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        found.append(relative)
    return found


def _is_whitelisted(relative: Path) -> bool:
    return relative.name in WHITELIST_FILES or relative.parts[0] in WHITELIST_DIRS


def _violations() -> list[str]:
    hits = []
    for relative in _python_files():
        if _is_whitelisted(relative):
            continue
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if DIRECT_CALL.search(line):
                hits.append(f"{relative}:{lineno}: {line.strip()}")
    return hits


def test_no_direct_legacy_logger_calls_outside_whitelist() -> None:
    hits = _violations()

    assert not hits, (
        "Прямые вызовы logger.debug/info/warning/error остались в коде. "
        "Перенесите запись на engine.log.get_logger() с категорией и полями; "
        "белый список — logger.py, config_reader.py, gui.py и каталог tests/.\n"
        "Первое нарушение:\n  " + "\n  ".join(hits)
    )


def test_migration_targets_exist_and_are_not_whitelisted() -> None:
    """Мигрируемые файлы нельзя спрятать от линта.

    Если файл из MIGRATION_TARGETS удалён/переименован, проверка выше могла
    бы молча перестать на него смотреть. Здесь это ловится явно: состав
    миграции и состав линта не должны расходиться.
    """

    missing = [name for name in MIGRATION_TARGETS if not (REPO_ROOT / name).is_file()]
    assert not missing, f"Файлы из MIGRATION_TARGETS не найдены: {', '.join(missing)}"

    escaped = [
        name
        for name in MIGRATION_TARGETS
        if _is_whitelisted(Path(name))
    ]
    assert not escaped, (
        "Мигрируемый файл попал в белый список линта: " + ", ".join(escaped)
    )
