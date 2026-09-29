"""Настройки профиля для сценария: UA, локаль, часовой пояс, cookies.

Профиль (план.md, §5 «Фаза 6») — это привязка аккаунта: прокси, User-Agent,
локаль, timezone и свой набор cookies. Прокси супервизор отдаёт воркеру
отдельной переменной ``ADCLICKER_PROXY``, а всё остальное читается здесь:
модуль находит строку ``profiles``, назначенную этому процессу через
``ADCLICKER_PROFILE_ID``, и превращает её поля в значения, которые
legacy-код подставляет вместо своих вычислений.

Три правила, зафиксированные в этом модуле:

1. **Приоритет профиля, но только при непустом поле.**
   :func:`resolve_user_agent`, :func:`resolve_locale` и
   :func:`resolve_timezone` возвращают значение профиля, если оно задано и не
   пустое, иначе — фолбэк ровно в том виде, в каком его передали (случайный
   UA, локаль из ``country_to_locale``, часовой пояс из геолокации или
   timezonefinder). Пустое поле равносильно отсутствующему: профиль, в котором
   заполнен только UA, не отменяет ни локаль, ни часовой пояс.
2. **Cookies — отдельно на профиль.** Набор живёт в
   ``<каталог>/<id>.cookies.json`` (каталог: ``ADCLICKER_PROFILE_DATA_DIR``,
   иначе ``./profile_data/``) и применяется, когда профиль назначен,
   независимо от ``behavior.custom_cookies`` — этот флаг остаётся решением
   «применять cookies вообще» для legacy-пути без профиля (см.
   :func:`should_apply_cookies`).
3. **Сценарий не падает из-за профиля.** Битый cookies-файл, отсутствующая
   строка, недоступная БД — это пустой набор или ``None`` с записью
   ``WARNING`` в лог, а не исключение посреди прогона. Запись cookies идёт
   атомарно (временный файл + ``os.replace``), поэтому читатель никогда не
   увидит наполовину записанный JSON.

Модуль сознательно не импортирует selenium и сеть: только sqlite, файлы и
логгер. Драйвер ему нужен как объект с методом ``add_cookie``, и это всё.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from engine.log import StructuredLogger, get_logger, resolve_db_path
from engine.profile_pool import ProfilePool

# Env-контракт супервизора (engine/control_plane/supervisor.PROFILE_ENV):
# десятичный id строки profiles, который меняется при каждой выдаче профиля.
# Дублируется, а не импортируется, по той же причине, что и в воркере: читать
# его обязан и legacy-код, которому не нужен весь движок.
PROFILE_ID_ENV = "ADCLICKER_PROFILE_ID"

# Каталог с файлами cookies профилей. Пустое значение — как и его отсутствие:
# берётся дефолт, а не текущий каталог.
PROFILE_DATA_DIR_ENV = "ADCLICKER_PROFILE_DATA_DIR"
DEFAULT_PROFILE_DATA_DIR = "./profile_data/"

# Имя файла внутри каталога: по id, поэтому переименование профиля не теряет
# набор, а удаление строки не обязывает чистить файл руками.
COOKIES_FILE_NAME = "{profile_id}.cookies.json"

__all__ = [
    "COOKIES_FILE_NAME",
    "DEFAULT_PROFILE_DATA_DIR",
    "PROFILE_DATA_DIR_ENV",
    "PROFILE_ID_ENV",
    "add_profile_cookies",
    "current_profile",
    "load_profile_cookies",
    "profile_cookies_path",
    "profile_id_from_environ",
    "resolve_locale",
    "resolve_timezone",
    "resolve_user_agent",
    "save_profile_cookies",
    "should_apply_cookies",
]


def _log() -> StructuredLogger:
    """Логгер процесса — берётся на каждый вызов, а не на уровне модуля.

    Причина в порядке импорта: ``engine/worker.py`` импортирует этот модуль на
    верхнем уровне, а ``get_logger()`` при первом обращении открывает
    StoreWriter и мигрирует БД. Разбор аргументов (включая ``--help``) и
    юнит-тесты не должны ходить в SQLite ради одного ``import``. Вызов дешёвый:
    ``get_logger`` кэширует инстанс по пути к БД.
    """

    return get_logger()


# --- строка профиля -----------------------------------------------------------


def profile_id_from_environ(environ: Mapping[str, str] | None = None) -> int | None:
    """Id профиля, назначенного этому процессу. None — профиля нет.

    Как и ``proxy_from_environ``: мусор, ноль и пустое значение — «не
    назначено», а не ошибка воркера. Супервизор печатает ``str(id)``
    ``INTEGER PRIMARY KEY``, поэтому десятичная запись и есть контракт.
    """
    source = os.environ if environ is None else environ
    raw = source.get(PROFILE_ID_ENV, "").strip()
    if not raw.isdigit():
        return None
    value = int(raw)
    return value if value >= 1 else None


def current_profile() -> dict[str, Any] | None:
    """Строка профиля, назначенного этому процессу, или None.

    Читает env и БД воркера (тот же путь, что и у legacy-логгера:
    ``ADCLICKER_DB``, иначе ``adclicker.db`` в cwd). Возвращает ``None`` в
    трёх случаях:

    * профиль не назначён — обычный одиночный запуск, никаких обращений к БД;
    * строка исчезла (удалена оператором) — ``WARNING`` с id, чтобы причина
      «настройки не применились» была видна в логе;
    * БД не читается — ``WARNING`` с типом и текстом ошибки.

    Ни один из этих случаев не должен ронять сценарий: без профиля работает
    прежний legacy-путь (случайный UA, общий ``cookies.txt``), а фактическая
    ошибка назначения видна в логе и в статусах профиля.
    """
    profile_id = profile_id_from_environ()
    if profile_id is None:
        return None
    try:
        row = ProfilePool(resolve_db_path()).get_profile(profile_id)
    except Exception as exc:  # noqa: BLE001 - любая причина не должна валить прогон
        _log().warning(
            "browser",
            "profile settings were not read",
            fields={
                "profile_id": profile_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )
        return None
    if row is None:
        _log().warning(
            "browser",
            "assigned profile does not exist",
            fields={"profile_id": profile_id},
        )
    return row


# --- приоритеты настроек ------------------------------------------------------


def resolve_user_agent(profile: Mapping[str, Any] | None, fallback: str) -> str:
    """UA профиля, если задан, иначе фолбэк (случайный UA как раньше)."""
    return _profile_field(profile, "user_agent") or fallback


def resolve_locale(profile: Mapping[str, Any] | None, geo_locale: Any) -> Any:
    """Локаль профиля, если задана, иначе гео-значение.

    Фолбэк возвращается как есть, в том числе списком локалей из
    ``utils.get_locale_language``: legacy складывает его в ``str()`` для
    ``intl.accept_languages``, и менять тип здесь значило бы менять его
    поведение.
    """
    return _profile_field(profile, "locale") or geo_locale


def resolve_timezone(profile: Mapping[str, Any] | None, geo_timezone: Any) -> Any:
    """Часовой пояс профиля, если задан, иначе значение геолокации.

    Пустой профильный часовой пояс не отменяет гео-вычисление — иначе
    профиль без timezone внезапно оставлял бы браузер в UTC.
    """
    return _profile_field(profile, "timezone") or geo_timezone


def _profile_field(profile: Mapping[str, Any] | None, name: str) -> str | None:
    """Непустое текстовое поле строки профиля. None — поля нет или оно пусто."""

    if not profile:
        return None
    value = profile.get(name)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


# --- решение о применении cookies ---------------------------------------------


def should_apply_cookies(profile: Mapping[str, Any] | None, custom_cookies: bool) -> bool:
    """Применять ли cookies в этом прогоне (источник выбирает вызывающий).

    Правило (план.md, §5 «Фаза 6», «Персистентные cookie отдельно на
    профиль»):

    * **профиль назначен** → его набор применяется **всегда**, независимо от
      ``behavior.custom_cookies``: cookies — часть привязки профиля, и
      смысл фазы в том, что каждый работает со своим набором, а не с общим
      ``cookies.txt``. Флаг в этом случае не спрашивается: выключенный
      «custom cookies» не должен молча подменять профильный набор общим;
    * **профиля нет** → прежнее поведение: ``custom_cookies`` и есть ответ
      «применять cookies вообще», источник — общий ``cookies.txt``.
    """

    return profile is not None or bool(custom_cookies)


# --- путь к cookies -----------------------------------------------------------


def profile_cookies_path(profile_id: int, base_dir: str | Path | None = None) -> Path:
    """Путь файла cookies профиля: ``<каталог>/<id>.cookies.json``.

    Каталог: явный аргумент, иначе ``ADCLICKER_PROFILE_DATA_DIR``, иначе
    ``./profile_data/`` относительно текущего каталога (там же живёт и сам
    legacy-код с относительными путями).
    """

    return _cookies_dir(base_dir) / COOKIES_FILE_NAME.format(profile_id=profile_id)


def _cookies_dir(base_dir: str | Path | None) -> Path:
    if base_dir is not None and str(base_dir).strip():
        return Path(base_dir)
    from_env = os.environ.get(PROFILE_DATA_DIR_ENV, "").strip()
    if from_env:
        return Path(from_env)
    return Path(DEFAULT_PROFILE_DATA_DIR)


# --- чтение и запись ----------------------------------------------------------


def load_profile_cookies(
    profile_id: int, base_dir: str | Path | None = None
) -> list[dict[str, Any]]:
    """Набор cookies профиля. Пустой список — файла нет или он непригоден.

    Отсутствие файла — не ошибка, а первый запуск профиля: набор просто
    пуст. Всё остальное (не-JSON, не-список, не-объекты внутри, отказ
    чтения) — ``WARNING`` с путём и причиной и тот же пустой список:
    профиль должен стартовать с чистым набором, а не падать посреди
    сценария из-за файла, поправленного руками.
    """

    path = profile_cookies_path(profile_id, base_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        _warn_unreadable(path, exc)
        return []

    try:
        data = json.loads(raw)
    except ValueError as exc:
        _warn_unreadable(path, exc)
        return []

    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        _warn_unreadable(path, ValueError("ожидался список объектов cookie"))
        return []

    return data


def save_profile_cookies(
    profile_id: int,
    cookies: Sequence[Mapping[str, Any]],
    base_dir: str | Path | None = None,
) -> Path:
    """Атомарно записать cookies в файл профиля, возвращает путь.

    Содержимое пишется во временный файл рядом с целевым и подменяется через
    ``os.replace``: каталог один и тот же, поэтому замена атомарна даже на
    несвязанных файловых системах, а читатель (другой воркер, следующий
    прогон) не видит наполовину записанный JSON. Обрыв записи убирает
    временный файл и оставляет прежний на месте — исключение уходит вызывающему,
    который решает, что с ним делать (в teardown это ``WARNING``).
    """

    path = profile_cookies_path(profile_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(list(cookies), handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _warn_unreadable(path: Path, exc: Exception) -> None:
    _log().warning(
        "browser",
        "profile cookies were not loaded",
        fields={"path": str(path), "error": str(exc), "error_type": type(exc).__name__},
    )


# --- cookies в драйвер --------------------------------------------------------


def add_profile_cookies(driver: Any, cookies: Sequence[Mapping[str, Any]]) -> None:
    """Добавить набор в драйвер (нужен только метод ``add_cookie``).

    ``sameSite`` нормализуется так же, как в legacy ``utils.add_cookies``
    (``strict``/``lax``/прочее → ``Strict``/``Lax``/``None``|``Lax``), но по
    ``.get``: профильный файл может быть выгружен руками, и отсутствующие
    ``sameSite``/``secure`` обязаны переживаться, а не ронять прогон с
    ``KeyError`` — это тот самый legacy-баг, зафиксированный xfail-тестом.
    """

    for cookie in cookies:
        driver.add_cookie(_normalized_cookie(cookie))


def _normalized_cookie(cookie: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(cookie)
    if "sameSite" not in normalized:
        # Нет ключа — не додумываем: браузер применит своё поведение по умолчанию,
        # а вписанный от руки "Lax" менял бы смысл сохранённого набора.
        return normalized
    same_site = str(normalized.get("sameSite") or "").lower()
    if same_site == "strict":
        normalized["sameSite"] = "Strict"
    elif same_site == "lax":
        normalized["sameSite"] = "Lax"
    else:
        normalized["sameSite"] = "None" if normalized.get("secure") else "Lax"
    return normalized
