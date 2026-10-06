"""CRUD файловых списков: запросы (``queries.txt``) и домены (``domains.txt``).

Файлы — source of truth этих списков, как ``config.json`` — source of truth
поведения (план §1): движок читает их напрямую (``utils.get_queries``,
``utils.get_domains``), поэтому любая правка через UI обязана дойти до файла,
а не жить только в памяти демона. Отсюда и форма модуля — построчное чтение
и атомарная запись (временный файл рядом + ``rename``): воркер, читающий
список в этот же момент, получает либо старую, либо новую версию целиком.

Запись всегда всего файла: списки малы (запросы и домены — десятки строк),
а частичная запись при нехватке места оставила бы рваный файл, который
legacy-чтение не отличит от корректного.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

from engine.domains import normalize_domain


class WordlistError(ValueError):
    """Список изменить не удалось; ``code``/``status`` — контракт HTTP-ответа.

    Отдельный тип, а не ``InvalidRequestError`` из API: ошибка приходит от
    файловой системы (нет каталога, нет прав), и сообщение с причиной
    полезно показать оператору как есть.
    """

    def __init__(self, message: str, *, code: str = "wordlist_error", status: int = 500):
        super().__init__(message)
        self.code = code
        self.status = status


def clean_query(line: str) -> str:
    """Строка запроса в том виде, в каком её читает ``utils.get_queries``.

    Кавычки вырезаются полностью — ровно как в legacy-чтении, иначе список в
    UI расходился бы с тем, что реально уходит в поиск.
    """
    return line.replace("'", "").replace('"', "").strip()


def clean_domain(line: str) -> str:
    """Домен в виде хоста — как его читает ``utils.get_domains``."""
    return normalize_domain(line)


def read_items(path: Path, clean: Callable[[str], str] | None = None) -> list[str]:
    """Строки списка; отсутствующий файл — пустой список (чистая установка).

    :param path: файл списка
    :param clean: нормализация строки; ``None`` — просто обрезка пробелов
    """

    if not path.is_file():
        return []

    items: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        item = clean(line) if clean is not None else line.strip()
        if item:
            items.append(item)
    return items


def write_items(path: Path, items: Iterable[str]) -> None:
    """Атомарная запись списка: временный файл рядом и ``rename``.

    Родительский каталог создаётся — списки в ``paths.*`` могут указывать в
    подкаталог, а чистая установка не обязана иметь его заранее.
    """

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".wordlist-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for item in items:
                    handle.write(f"{item}\n")
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise WordlistError(f"не удалось записать {path}: {exc}") from exc


def add_items(
    path: Path,
    lines: Iterable[str],
    *,
    clean: Callable[[str], str],
) -> dict[str, object]:
    """Добавить строки в список: дедупликация регистронезависимая, порядок сохраняется.

    Пустая после нормализации строка и уже существующий элемент — это
    ``skipped`` с причиной в ``problems``, а не ошибка: оператор ждёт отчёта,
    а не отмены всего запроса из-за одной дублирующейся строки.

    :returns: ``{"added", "skipped", "problems"}``
    """

    existing = read_items(path, clean)
    known = {item.casefold() for item in existing}
    added = 0
    skipped = 0
    problems: list[str] = []

    for raw in lines:
        item = clean(raw)
        if not item:
            skipped += 1
            problems.append("пустая строка не добавляется")
            continue
        if item.casefold() in known:
            skipped += 1
            problems.append(f"уже есть: {item}")
            continue
        existing.append(item)
        known.add(item.casefold())
        added += 1

    if added:
        write_items(path, existing)

    return {"added": added, "skipped": skipped, "problems": problems}


def delete_items(
    path: Path,
    targets: Iterable[str],
    *,
    clean: Callable[[str], str],
    delete_all: bool = False,
) -> dict[str, object]:
    """Удалить строки из списка: по значениям или весь файл.

    Отсутствующий файл — не ошибка, а отчёт: чистая установка, удалять нечего.
    Ненайденные значения идут в ``problems`` (best-effort, как у прокси:
    одна пропавшая строка не должна ронять удаление остальных).

    :returns: ``{"deleted", "skipped", "problems"}``
    """

    if not path.is_file():
        return {"deleted": 0, "skipped": 0, "problems": [f"файл не найден: {path}"]}

    # list, а не итератор: значения нормализуются дважды (набор и отчёт).
    targets = [clean(raw) for raw in targets]
    existing = read_items(path, clean)

    if delete_all:
        deleted = len(existing)
        if deleted:
            write_items(path, [])
        return {"deleted": deleted, "skipped": 0, "problems": []}

    wanted = {item.casefold() for item in targets if item}
    kept: list[str] = []
    found: set[str] = set()
    for item in existing:
        if item.casefold() in wanted:
            found.add(item.casefold())
        else:
            kept.append(item)

    problems: list[str] = []
    for target in targets:
        if target and target.casefold() not in found:
            problems.append(f"не найдено: {target}")

    deleted = len(existing) - len(kept)
    if deleted:
        write_items(path, kept)

    return {"deleted": deleted, "skipped": len(problems), "problems": problems}
