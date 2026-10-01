"""Тесты ``engine/profile_apply``: приоритеты настроек профиля и его cookies.

Модуль — единственное место, где настройки строки профиля (UA, локаль,
часовой пояс) и её cookies превращаются в конкретные значения для
legacy-сценария. Сети и selenium в нём нет по построению, поэтому всё
проверяется на чистом Python: строки в SQLite, файлы во временном каталоге
и записывающий логгер-заглушка.

Три свойства здесь проверяются жёстко:

- **приоритет** — непустое поле профиля всегда главнее фолбэка, пустое и
  отсутствующее поле не меняют прежнего поведения;
- **устойчивость файлов** — битый или недоступный cookies-файл даёт пустой
  набор с записью в лог, а запись идёт через временный файл и ``os.replace``:
  читатель никогда не видит наполовину записанный JSON;
- **молчаливость** — в логи не попадают ни ``key_ref``, ни значения cookies.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from engine import profile_apply
from engine.db import migrations
from engine.profile_apply import (
    DEFAULT_PROFILE_DATA_DIR,
    PROFILE_DATA_DIR_ENV,
    PROFILE_ID_ENV,
    add_profile_cookies,
    current_profile,
    load_profile_cookies,
    profile_cookies_path,
    profile_id_from_environ,
    resolve_locale,
    resolve_timezone,
    resolve_user_agent,
    save_profile_cookies,
    should_apply_cookies,
)
from engine.profile_pool import ProfilePool


class LogRecorder:
    """Логгер-заглушка: пишет вызовы в список, в БД ничего не уходит."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, str, dict]] = []

    def _record(self, level: str, category: str, message: str, fields=None, **kwargs):
        self.records.append((level, category, message, fields or {}))

    def debug(self, category, message, **kwargs):
        self._record("DEBUG", category, message, **kwargs)

    def info(self, category, message, **kwargs):
        self._record("INFO", category, message, **kwargs)

    def warning(self, category, message, **kwargs):
        self._record("WARNING", category, message, **kwargs)

    def error(self, category, message, **kwargs):
        self._record("ERROR", category, message, **kwargs)

    @property
    def warnings(self) -> list[tuple[str, str, str, dict]]:
        return [record for record in self.records if record[0] == "WARNING"]

    @property
    def text(self) -> str:
        return str(self.records)


@pytest.fixture
def record_log(monkeypatch):
    """Логгер модуля: ленивый ``get_logger`` подменяется на записывающий.

    ``profile_apply`` не держит модульного логгера (иначе импорт открыл бы
    StoreWriter), поэтому подменяется сама фабрика.
    """

    recorder = LogRecorder()
    monkeypatch.setattr(profile_apply, "get_logger", lambda: recorder)
    return recorder


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "profiles.db"
    migrations.migrate(path)
    monkeypatch.setenv("ADCLICKER_DB", str(path))
    return path


@pytest.fixture
def assign(db_path, monkeypatch):
    """Завести профиль и назначить его этому процессу через env."""

    pool = ProfilePool(db_path)

    def _assign(**fields):
        pool.add_profiles([{"name": f"acc-{len(pool.list_profiles()) + 1}", **fields}])
        row = pool.list_profiles()[-1]
        monkeypatch.setenv(PROFILE_ID_ENV, str(row["id"]))
        return row

    return _assign


@pytest.fixture
def cookies_dir(tmp_path, monkeypatch):
    """Каталог cookies профилей, заданный через env (не через аргумент)."""

    directory = tmp_path / "profile_data"
    monkeypatch.setenv(PROFILE_DATA_DIR_ENV, str(directory))
    return directory


class CookieDriver:
    """Драйвер только для cookies: запоминает добавленное."""

    def __init__(self) -> None:
        self.cookies: list[dict] = []

    def add_cookie(self, cookie):
        self.cookies.append(cookie)


# --- env-контракт -------------------------------------------------------------


class TestProfileIdFromEnviron:
    """``ADCLICKER_PROFILE_ID``: десятичный id или «профиля нет»."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("7", 7),
            ("42", 42),
            (" 13 ", 13),
            ("", None),
            ("   ", None),
            ("abc", None),
            ("12.5", None),
            ("-3", None),
            ("0", None),
            ("0x10", None),
        ],
    )
    def test_value_is_parsed_or_absent(self, raw, expected):
        assert profile_id_from_environ({"ADCLICKER_PROFILE_ID": raw}) == expected

    def test_absent_variable_yields_none(self):
        assert profile_id_from_environ({}) is None

    def test_real_environ_is_used_by_default(self, monkeypatch):
        monkeypatch.setenv(PROFILE_ID_ENV, "5")

        assert profile_id_from_environ() == 5


# --- приоритеты настроек ------------------------------------------------------


class TestResolveUserAgent:
    """UA: профильный строкой, иначе фолбэк (дефолт прогона — None)."""

    def test_profile_value_wins_over_the_fallback(self):
        assert resolve_user_agent({"user_agent": "UA/Profile"}, "UA/Random") == "UA/Profile"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_profile_value_keeps_the_fallback(self, value):
        assert resolve_user_agent({"user_agent": value}, "UA/Random") == "UA/Random"

    def test_profile_without_the_field_keeps_the_fallback(self):
        assert resolve_user_agent({"id": 7}, "UA/Random") == "UA/Random"

    def test_no_profile_keeps_the_fallback(self):
        assert resolve_user_agent(None, "UA/Random") == "UA/Random"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_profile_value_with_none_fallback_means_no_override(self, value):
        assert resolve_user_agent({"user_agent": value}, None) is None

    def test_no_profile_with_none_fallback_means_no_override(self):
        assert resolve_user_agent(None, None) is None

    def test_whitespace_around_the_profile_value_is_trimmed(self):
        assert resolve_user_agent({"user_agent": " UA/Profile "}, "x") == "UA/Profile"


class TestResolveLocale:
    """Локаль: профильная строка главнее гео-списка из country_to_locale."""

    def test_profile_locale_wins_over_the_geo_one(self):
        geo = ["en-US", "en"]

        assert resolve_locale({"locale": "de-DE"}, geo) == "de-DE"

    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_empty_profile_locale_keeps_the_geo_one(self, value):
        geo = ["de-DE", "de"]

        assert resolve_locale({"locale": value}, geo) == geo

    def test_geo_value_is_passed_through_untouched(self):
        # Список остаётся списком: legacy складывает его в str() для prefs.
        geo = ["ps-AF", "uz-AF"]

        assert resolve_locale(None, geo) is geo

    def test_absent_geo_value_stays_absent(self):
        assert resolve_locale(None, None) is None
        assert resolve_locale({"id": 1}, None) is None


class TestResolveTimezone:
    """Часовой пояс: профильный главнее значения геолокации/timezonefinder."""

    def test_profile_timezone_wins_over_the_geo_one(self):
        assert resolve_timezone({"timezone": "Europe/Paris"}, "Europe/Berlin") == "Europe/Paris"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_profile_timezone_keeps_the_geo_one(self, value):
        assert resolve_timezone({"timezone": value}, "Europe/Berlin") == "Europe/Berlin"

    def test_no_profile_keeps_the_geo_one(self):
        assert resolve_timezone(None, "Europe/Berlin") == "Europe/Berlin"

    def test_absent_geo_value_stays_absent(self):
        assert resolve_timezone(None, None) is None


class TestShouldApplyCookies:
    """Правило применения cookies: профиль всегда, без профиля — флаг.

    (Тот же текст зафиксирован в докстринге правила внутри модуля и в
    ``SearchController._apply_cookies``.)
    """

    @pytest.mark.parametrize(
        ("profile", "flag", "expected"),
        [
            # Профиль назначен: набор применяется всегда, флаг не спрашивается.
            ({"id": 1}, False, True),
            ({"id": 1}, True, True),
            # Профиля нет: применять или нет — решает legacy-флаг custom_cookies.
            (None, False, False),
            (None, True, True),
        ],
    )
    def test_rule(self, profile, flag, expected):
        assert should_apply_cookies(profile, flag) is expected


# --- чтение строки профиля ----------------------------------------------------


class TestImportIsSideEffectFree:
    """Импорт модуля не ходит в БД (см. ``_log`` и ``--help`` воркера)."""

    def test_import_does_not_open_the_store(self, tmp_path, monkeypatch):
        import importlib

        missing = tmp_path / "never-created.db"
        monkeypatch.setenv("ADCLICKER_DB", str(missing))

        importlib.reload(profile_apply)

        assert not missing.exists(), "import не должен мигрировать и создавать БД"


class TestCurrentProfile:
    """Env + строка ``profiles``: что видит legacy-код при старте сценария."""

    def test_without_the_env_no_database_is_touched(self, tmp_path, monkeypatch, record_log):
        missing = tmp_path / "never-created.db"
        monkeypatch.setenv("ADCLICKER_DB", str(missing))
        monkeypatch.delenv(PROFILE_ID_ENV, raising=False)

        assert current_profile() is None
        assert not missing.exists(), "без профиля не должно открываться ни одной БД"
        assert record_log.records == []

    def test_assigned_profile_is_read_with_its_settings(self, assign):
        assign(user_agent="UA/Profile", locale="de-DE", timezone="Europe/Paris")

        profile = current_profile()

        assert profile is not None
        assert profile["user_agent"] == "UA/Profile"
        assert profile["locale"] == "de-DE"
        assert profile["timezone"] == "Europe/Paris"
        assert profile["status"] == "free"
        assert profile["fields"] == {}

    def test_deleted_profile_yields_none_and_a_warning(self, assign, monkeypatch, record_log):
        assign(user_agent="UA/Profile")
        monkeypatch.setenv(PROFILE_ID_ENV, "424242")

        assert current_profile() is None
        assert [record[2] for record in record_log.warnings], "причина должна быть в логе"
        assert record_log.warnings[0][1] == "browser"

    def test_unreadable_database_yields_none_and_a_warning(self, tmp_path, monkeypatch, record_log):
        broken = tmp_path / "broken.db"
        broken.write_bytes("это не sqlite".encode())
        monkeypatch.setenv("ADCLICKER_DB", str(broken))
        monkeypatch.setenv(PROFILE_ID_ENV, "3")

        assert current_profile() is None
        assert record_log.warnings, "отказ чтения обязан попасть в лог"

    def test_key_ref_and_settings_never_reach_the_log(self, assign, record_log):
        assign(key_ref="sk-super-secret", user_agent="UA/Profile")

        current_profile()

        assert "sk-super-secret" not in record_log.text
        assert "UA/Profile" not in record_log.text


# --- путь к cookies -----------------------------------------------------------


class TestProfileCookiesPath:
    """Один файл на профиль: ``<каталог>/<id>.cookies.json``."""

    def test_explicit_directory_is_used_as_is(self, tmp_path):
        assert profile_cookies_path(3, tmp_path) == tmp_path / "3.cookies.json"

    def test_directory_comes_from_the_env_by_default(self, cookies_dir):
        assert profile_cookies_path(9) == cookies_dir / "9.cookies.json"

    def test_fallback_directory_is_the_declared_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv(PROFILE_DATA_DIR_ENV, raising=False)
        monkeypatch.chdir(tmp_path)

        path = profile_cookies_path(7)

        assert path == Path(DEFAULT_PROFILE_DATA_DIR) / "7.cookies.json"
        assert str(path) == "profile_data/7.cookies.json"

    def test_empty_base_dir_argument_falls_back_to_the_env(self, cookies_dir):
        assert profile_cookies_path(4, "") == cookies_dir / "4.cookies.json"


# --- чтение cookies -----------------------------------------------------------


class TestLoadProfileCookies:
    """Чтение профильного набора: пусто, норма, порча, отказ файла."""

    def test_missing_file_yields_an_empty_set(self, tmp_path, record_log):
        assert load_profile_cookies(1, tmp_path) == []
        assert record_log.records == [], "первый запуск профиля — не ошибка"

    def test_saved_list_is_read_back(self, tmp_path):
        cookies = [{"name": "sid", "value": "42", "sameSite": "Lax"}]
        save_profile_cookies(2, cookies, tmp_path)

        assert load_profile_cookies(2, tmp_path) == cookies

    def test_broken_json_yields_empty_set_and_a_warning(self, tmp_path, record_log):
        path = tmp_path / "3.cookies.json"
        path.write_text("{ это не json", encoding="utf-8")

        assert load_profile_cookies(3, tmp_path) == []
        assert record_log.warnings, "порча файла должна быть видна в логе"
        level, category, message, fields = record_log.warnings[0]
        assert level == "WARNING"
        assert category == "browser"
        assert str(path) in str(fields.get("path"))
        assert fields.get("error"), "в логе должна быть причина, а не только факт"

    def test_json_that_is_not_a_list_yields_empty_set(self, tmp_path, record_log):
        (tmp_path / "4.cookies.json").write_text('{"name": "sid"}', encoding="utf-8")

        assert load_profile_cookies(4, tmp_path) == []
        assert record_log.warnings

    def test_non_object_entries_are_rejected_as_a_whole(self, tmp_path, record_log):
        (tmp_path / "5.cookies.json").write_text('["sid", {"name": "ok"}]', encoding="utf-8")

        assert load_profile_cookies(5, tmp_path) == []
        assert record_log.warnings

    def test_directory_in_place_of_the_file_is_reported(self, tmp_path, record_log):
        (tmp_path / "6.cookies.json").mkdir()

        assert load_profile_cookies(6, tmp_path) == []
        assert record_log.warnings

    def test_broken_file_never_puts_cookie_values_in_the_log(self, tmp_path, record_log):
        (tmp_path / "7.cookies.json").write_text('{"value": "secret-cookie"}', encoding="utf-8")

        load_profile_cookies(7, tmp_path)

        assert "secret-cookie" not in record_log.text


# --- запись cookies -----------------------------------------------------------


class TestSaveProfileCookies:
    """Запись атомарно: временный файл рядом с целевым + ``os.replace``."""

    def test_writes_a_json_list_and_returns_the_path(self, tmp_path):
        cookies = [{"name": "sid", "value": "42"}]

        path = save_profile_cookies(1, cookies, tmp_path)

        assert path == tmp_path / "1.cookies.json"
        assert json.loads(path.read_text(encoding="utf-8")) == cookies

    def test_missing_directories_are_created(self, tmp_path):
        target = tmp_path / "nested" / "deeper"

        save_profile_cookies(2, [], target)

        assert (target / "2.cookies.json").exists()

    def test_repeated_save_replaces_the_previous_file(self, tmp_path):
        save_profile_cookies(3, [{"name": "old"}], tmp_path)

        save_profile_cookies(3, [{"name": "new"}], tmp_path)

        assert load_profile_cookies(3, tmp_path) == [{"name": "new"}]

    def test_successful_write_leaves_no_temporary_files(self, tmp_path):
        save_profile_cookies(4, [{"name": "sid"}], tmp_path)

        assert [entry for entry in tmp_path.iterdir() if entry.name != "4.cookies.json"] == []

    def test_failed_serialization_keeps_the_previous_file_intact(self, tmp_path):
        """Ключевое свойство атомарности: битая запись не портит прочитанное."""
        save_profile_cookies(5, [{"name": "old", "value": "keep-me"}], tmp_path)

        with pytest.raises(TypeError):
            save_profile_cookies(5, [{"name": "new", "value": object()}], tmp_path)

        assert load_profile_cookies(5, tmp_path) == [{"name": "old", "value": "keep-me"}]
        leftovers = [entry for entry in tmp_path.iterdir() if entry.name != "5.cookies.json"]
        assert leftovers == [], "временный файл обязан убираться и при отказе"

    def test_save_goes_through_replace_not_overwrite(self, tmp_path, monkeypatch):
        """Проверяем механизм, а не результат: запись подменяется, а не пишется поверх."""
        calls = []
        real_replace = os.replace

        def spy(source, target):
            calls.append((str(source), str(target)))
            return real_replace(source, target)

        monkeypatch.setattr("engine.profile_apply.os.replace", spy)

        save_profile_cookies(6, [{"name": "sid"}], tmp_path)

        assert len(calls) == 1
        source, target = calls[0]
        assert target == str(tmp_path / "6.cookies.json")
        assert source != target, "цель замены — постоянный файл, источник — временный рядом"


# --- добавление cookies в драйвер ---------------------------------------------


class TestAddProfileCookies:
    """Нормализация sameSite та же, что у legacy cookies.txt, но без KeyError."""

    def test_samesite_is_normalized_like_the_legacy_file(self):
        driver = CookieDriver()

        add_profile_cookies(
            driver,
            [
                {"name": "a", "sameSite": "strict", "secure": True},
                {"name": "b", "sameSite": "lax", "secure": False},
                {"name": "c", "sameSite": "no_restriction", "secure": True},
                {"name": "d", "sameSite": "whatever", "secure": False},
            ],
        )

        same_site = {cookie["name"]: cookie["sameSite"] for cookie in driver.cookies}
        assert same_site == {"a": "Strict", "b": "Lax", "c": "None", "d": "Lax"}

    def test_selenium_written_values_pass_unchanged(self):
        # Так выглядят cookies после driver.get_cookies(): канонические значения.
        driver = CookieDriver()

        add_profile_cookies(
            driver,
            [
                {"name": "sid", "value": "42", "sameSite": "Strict"},
                {"name": "consent", "value": "1", "sameSite": "None", "secure": True},
            ],
        )

        assert [cookie["sameSite"] for cookie in driver.cookies] == ["Strict", "None"]

    def test_cookie_without_samesite_does_not_crash(self):
        # В отличие от legacy utils.add_cookies (xfail), профильный путь
        # обязан переживать выгруженный вручную файл.
        driver = CookieDriver()

        add_profile_cookies(driver, [{"name": "sid", "value": "42"}])

        assert driver.cookies == [{"name": "sid", "value": "42"}]

    def test_other_fields_are_passed_through(self):
        driver = CookieDriver()
        cookie = {"name": "sid", "value": "42", "domain": ".example.com", "path": "/", "sameSite": "lax"}

        add_profile_cookies(driver, [cookie])

        assert driver.cookies[0]["domain"] == ".example.com"
        assert driver.cookies[0]["value"] == "42"
        assert driver.cookies[0]["sameSite"] == "Lax"
