"""Тесты репозитория и машины статусов профилей.

Сети и браузера нет: строки лежат в SQLite, воркеры создаются через
``StateStore.register_worker`` (тот же SQL, что в супервизоре). Три вещи,
которые здесь проверяются жёстко:

- **статусы** — замкнутый набор ``free|assigned|active|error|blocked`` и
  явная машина переходов: недопустимый переход это ``ValueError``, а не
  молчаливая запись чего угодно;
- **выдача** — ``take_for_worker`` отдаёт профиль по одному на воркера, в
  детерминированном порядке, с приоритетом прошлого назначения, и никогда
  не выдаёт профиль, который держит живой воркер;
- **импорт** — одна строка = ``key_ref`` (необязательно ``key_ref | name``)
  либо User-Agent (``Mozilla/...`` → колонка ``user_agent`` и имя ``UA-N``),
  300+ строк одной операцией, дубликаты и кривые строки уходят в
  ``problems`` и не роняют пачку.
"""

from __future__ import annotations

import pytest

from engine.control_plane.state import StateStore
from engine.db import migrations
from engine.profile_pool import (
    OPERATOR_STATUSES,
    PROFILE_STATUSES,
    ProfileImportError,
    ProfileInUseError,
    ProfileInvalidError,
    ProfileNotFoundError,
    ProfilePool,
)
from engine.proxy_pool import ProxyPool


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


@pytest.fixture
def pool(db_path):
    return ProfilePool(db_path)


def table_rows(db_path, table):
    """Сырые строки таблицы: проверяем БД, а не собственные методы пула."""
    conn = migrations.connect(db_path)
    try:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]
    finally:
        conn.close()


def statuses(pool):
    return [row["status"] for row in table_rows(pool.db_path, "profiles")]


def profile_ids(pool):
    return [row["id"] for row in table_rows(pool.db_path, "profiles")]


def make_worker(db_path, browser_id, status="running", pid=4242):
    """Строка воркера тем же SQL, что пишет супервизор."""
    StateStore(db_path).register_worker(browser_id, pid)
    conn = migrations.connect(db_path)
    try:
        conn.execute("UPDATE workers SET status = ? WHERE browser_id = ?", (status, browser_id))
        conn.commit()
    finally:
        conn.close()


def worker_row(db_path, browser_id):
    conn = migrations.connect(db_path)
    try:
        row = conn.execute("SELECT * FROM workers WHERE browser_id = ?", (browser_id,)).fetchone()
        return None if row is None else dict(row)
    finally:
        conn.close()


def bind(db_path, browser_id, profile_id):
    conn = migrations.connect(db_path)
    try:
        conn.execute(
            "UPDATE workers SET profile_id = ? WHERE browser_id = ?", (profile_id, browser_id)
        )
        conn.commit()
    finally:
        conn.close()


def set_profile_status(db_path, profile_id, status):
    conn = migrations.connect(db_path)
    try:
        conn.execute("UPDATE profiles SET status = ? WHERE id = ?", (status, profile_id))
        conn.commit()
    finally:
        conn.close()


def claim(pool, profile_id, browser_id="br-1"):
    """Назначение — системный переход; в тестах он идёт через пул."""
    make_worker(pool.db_path, browser_id)
    taken = pool.take_for_worker(browser_id)
    assert taken is not None and taken["id"] == profile_id
    return taken


def add_free_profiles(pool, count):
    """``count`` свободных профилей, возвращает их id по возрастанию."""
    result = pool.add_profiles([{"name": f"p{index}"} for index in range(count)])
    assert result["added"] == count, result
    return profile_ids(pool)


class TestAddProfiles:
    """POST /control/profiles: пачка записей, проблемы одной не валят другие."""

    def test_adds_a_profile_with_every_contract_field(self, pool, db_path):
        result = pool.add_profiles(
            [
                {
                    "name": "alice",
                    "key_ref": "hooks:Alice",
                    "user_agent": "UA/1.0",
                    "locale": "en-US",
                    "timezone": "Europe/London",
                    "fields": {"cookie_set": "alice.txt", "notes": "первый"},
                }
            ]
        )

        assert result == {"added": 1, "skipped": 0, "problems": []}
        (row,) = table_rows(db_path, "profiles")
        assert row["name"] == "alice"
        assert row["key_ref"] == "hooks:Alice"
        assert row["user_agent"] == "UA/1.0"
        assert row["locale"] == "en-US"
        assert row["timezone"] == "Europe/London"
        assert row["status"] == "free", "новый профиль начинает жизнь свободным"
        assert row["fields"] == '{"cookie_set": "alice.txt", "notes": "первый"}'

    def test_defaults_are_null_and_empty_fields(self, pool, db_path):
        pool.add_profiles([{"name": "bare"}])

        (row,) = table_rows(db_path, "profiles")
        assert row["key_ref"] is None
        assert row["proxy_id"] is None
        assert row["user_agent"] is None
        assert row["last_used_at"] is None
        assert row["fields"] == "{}"

    def test_skips_duplicate_name_and_keeps_the_rest(self, pool, db_path):
        pool.add_profiles([{"name": "alice"}])

        result = pool.add_profiles(
            [
                {"name": "alice"},
                {"name": "bob"},
                {"name": "alice"},
            ]
        )

        assert result["added"] == 1
        assert result["skipped"] == 2
        assert [problem["index"] for problem in result["problems"]] == [0, 2]
        assert all("дубликат" in problem["message"] for problem in result["problems"])
        assert [row["name"] for row in table_rows(db_path, "profiles")] == ["alice", "bob"]

    def test_duplicate_inside_one_batch_is_skipped(self, pool):
        result = pool.add_profiles([{"name": "alice"}, {"name": "alice"}])

        assert result["added"] == 1
        assert result["skipped"] == 1
        assert len(result["problems"]) == 1

    @pytest.mark.parametrize(
        "record",
        [
            {},
            {"name": ""},
            {"name": "   "},
            {"name": None},
            {"name": 7},
            {"name": "alice", "fields": []},
            {"name": "alice", "fields": "{}"},
            {"name": "alice", "fields": "any"},
            {"name": "alice", "proxy_id": "5"},
            {"name": "alice", "proxy_id": True},
            {"name": "alice", "proxy_id": 9999},
            {"name": "alice", "key_ref": 5},
            {"name": "alice", "locale": 7},
        ],
    )
    def test_invalid_record_is_skipped_with_a_problem(self, pool, record):
        result = pool.add_profiles([record])

        assert result["added"] == 0
        assert result["skipped"] == 1
        assert len(result["problems"]) == 1
        assert result["problems"][0]["index"] == 0
        assert result["problems"][0]["message"], "у каждой проблемы должен быть текст"

    def test_non_object_record_is_skipped(self, pool):
        result = pool.add_profiles(["alice"])

        assert result["added"] == 0
        assert result["skipped"] == 1
        assert result["problems"][0]["index"] == 0

    def test_unknown_keys_are_ignored_not_rejected(self, pool):
        result = pool.add_profiles([{"name": "alice", "status": "blocked", "id": 5}])

        assert result == {"added": 1, "skipped": 0, "problems": []}

    def test_fields_are_stored_as_a_json_object(self, pool, db_path):
        pool.add_profiles([{"name": "alice", "fields": {"a": [1, 2], "b": {"c": True}}}])

        listed = pool.list_profiles()

        assert listed[0]["fields"] == {"a": [1, 2], "b": {"c": True}}

    def test_proxy_id_is_written_when_the_proxy_exists(self, pool, db_path):
        ProxyPool(db_path).add_lines(["10.0.0.1:8080"])
        proxy_id = table_rows(db_path, "proxies")[0]["id"]

        pool.add_profiles([{"name": "alice", "proxy_id": proxy_id}])

        assert table_rows(db_path, "profiles")[0]["proxy_id"] == proxy_id

    def test_error_message_does_not_echo_the_whole_record(self, pool):
        """Проблема уходит в HTTP-ответ: сообщение — не дамп записи."""
        result = pool.add_profiles([{"key_ref": "s3cr3t-key"}])

        assert result["problems"]
        assert "s3cr3t-key" not in result["problems"][0]["message"]


class TestImportLines:
    """Формат строки импорта: ``key_ref`` либо ``key_ref | name``."""

    def test_single_token_line_uses_key_ref_and_generates_a_unique_name(self, pool, db_path):
        result = pool.import_lines(["key-001", "key-002"])

        assert result == {"added": 2, "skipped": 0, "problems": []}
        rows = table_rows(db_path, "profiles")
        assert [(row["name"], row["key_ref"], row["status"]) for row in rows] == [
            ("key-001", "key-001", "free"),
            ("key-002", "key-002", "free"),
        ]

    def test_second_column_is_the_name(self, pool, db_path):
        result = pool.import_lines(["key-001 | alice account", "key-002|bob"])

        assert result["added"] == 2
        rows = table_rows(db_path, "profiles")
        assert [(row["name"], row["key_ref"]) for row in rows] == [
            ("alice account", "key-001"),
            ("bob", "key-002"),
        ]

    def test_generated_name_avoids_a_collision_with_an_existing_row(self, pool, db_path):
        pool.add_profiles([{"name": "key-001", "key_ref": "hooks:other"}])

        result = pool.import_lines(["key-001"])

        assert result == {"added": 1, "skipped": 0, "problems": []}
        rows = table_rows(db_path, "profiles")
        assert rows[1]["key_ref"] == "key-001"
        assert rows[1]["name"] != "key-001", "имя обязано остаться уникальным"
        assert rows[1]["name"].startswith("key-001")

    def test_duplicate_key_ref_is_skipped_against_batch_and_database(self, pool, db_path):
        pool.import_lines(["key-001"])

        result = pool.import_lines(["key-001", "key-002", "key-002"])

        assert result["added"] == 1
        assert result["skipped"] == 2
        assert [problem["line_index"] for problem in result["problems"]] == [0, 2]
        assert all("дубликат" in problem["message"] for problem in result["problems"])
        assert len(table_rows(db_path, "profiles")) == 2

    def test_duplicate_explicit_name_is_skipped(self, pool):
        pool.add_profiles([{"name": "alice", "key_ref": "hooks:alice"}])

        result = pool.import_lines(["other-key | alice"])

        assert result["added"] == 0
        assert result["skipped"] == 1
        assert "дубликат" in result["problems"][0]["message"]

    @pytest.mark.parametrize(
        "line",
        [
            "| alice",  # пустой key_ref
            " | ",  # обе части пустые
            "key-001 |",  # пустое имя при явно указанной колонке
            7,  # не текст
            None,
        ],
    )
    def test_bad_lines_go_to_problems_and_do_not_fail_the_batch(self, pool, line):
        result = pool.import_lines([line, "key-001"])

        assert result["added"] == 1
        assert result["skipped"] == 1
        assert result["problems"][0]["line_index"] == 0
        assert result["problems"][0]["message"]

    def test_blank_lines_are_ignored_entirely(self, pool):
        result = pool.import_lines(["", "   ", "key-001"])

        assert result == {"added": 1, "skipped": 0, "problems": []}

    def test_import_of_three_hundred_lines_is_a_single_operation(self, pool, db_path):
        lines = [f"key-{index:04d}" for index in range(300)]

        result = pool.import_lines(lines)

        assert result == {"added": 300, "skipped": 0, "problems": []}
        assert len(table_rows(db_path, "profiles")) == 300

    def test_half_of_a_broken_file_still_imports_the_rest(self, pool, db_path):
        lines = ["ok-1", "|", "ok-2", "key-x |", "ok-3"]

        result = pool.import_lines(lines)

        assert result["added"] == 3
        assert result["skipped"] == 2
        assert [row["key_ref"] for row in table_rows(db_path, "profiles")] == [
            "ok-1",
            "ok-2",
            "ok-3",
        ]


class TestImportUserAgents:
    """Строка ``Mozilla/...`` — профиль с ``user_agent`` и именем ``UA-N``."""

    UA_ONE = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"
    UA_TWO = "Mozilla/5.0 (X11; Linux x86_64) Chrome/121.0"

    def test_ua_line_becomes_a_profile_without_key_ref(self, pool, db_path):
        result = pool.import_lines([self.UA_ONE])

        assert result == {"added": 1, "skipped": 0, "problems": []}
        row = table_rows(db_path, "profiles")[0]
        assert row["name"] == "UA-1"
        assert row["user_agent"] == self.UA_ONE
        assert row["key_ref"] is None, "у User-Agent нет key_ref — колонка «Ключ» покажет «—»"
        assert row["status"] == "free"

    def test_names_follow_sequential_ua_numbers(self, pool, db_path):
        result = pool.import_lines([self.UA_ONE, self.UA_TWO])

        assert result["added"] == 2
        rows = table_rows(db_path, "profiles")
        assert [row["name"] for row in rows] == ["UA-1", "UA-2"]
        assert [row["user_agent"] for row in rows] == [self.UA_ONE, self.UA_TWO]

    def test_numbering_continues_after_an_existing_ua_9(self, pool, db_path):
        pool.add_profiles([{"name": "UA-9", "user_agent": "Mozilla/5.0 старый"}])

        result = pool.import_lines([self.UA_ONE, self.UA_TWO])

        assert result["added"] == 2
        assert [row["name"] for row in table_rows(db_path, "profiles")] == [
            "UA-9",
            "UA-10",
            "UA-11",
        ]

    def test_generated_name_skips_a_name_taken_mid_batch(self, pool, db_path):
        """Явное имя UA-2 занято уже в пачке — следующий номер обязан его обойти."""
        result = pool.import_lines([self.UA_ONE, "some-key | UA-2", self.UA_TWO])

        assert result["added"] == 3
        assert [row["name"] for row in table_rows(db_path, "profiles")] == [
            "UA-1",
            "UA-2",
            "UA-3",
        ]

    def test_explicit_name_wins_over_the_number(self, pool, db_path):
        result = pool.import_lines([f"{self.UA_ONE} | alice"])

        assert result == {"added": 1, "skipped": 0, "problems": []}
        row = table_rows(db_path, "profiles")[0]
        assert row["name"] == "alice"
        assert row["user_agent"] == self.UA_ONE
        assert row["key_ref"] is None

    def test_duplicate_user_agent_in_the_database_is_skipped(self, pool, db_path):
        pool.import_lines([self.UA_ONE])

        result = pool.import_lines([self.UA_ONE, self.UA_TWO])

        assert result["added"] == 1
        assert result["skipped"] == 1
        assert result["problems"] == [
            {"line_index": 0, "message": "дубликат: user_agent уже есть"}
        ]
        assert len(table_rows(db_path, "profiles")) == 2

    def test_duplicate_user_agent_inside_one_batch_is_skipped(self, pool, db_path):
        result = pool.import_lines([self.UA_ONE, self.UA_TWO, self.UA_ONE])

        assert result["added"] == 2
        assert result["skipped"] == 1
        assert result["problems"][0]["line_index"] == 2
        assert result["problems"][0]["message"] == "дубликат: user_agent уже есть"
        assert self.UA_ONE not in result["problems"][0]["message"], (
            "полный User-Agent не должен цитироваться в ответе"
        )

    def test_first_tab_column_is_the_value(self, pool, db_path):
        result = pool.import_lines(
            [f"{self.UA_ONE}\thвост из таблицы", "key-001\tвторая колонка"]
        )

        assert result == {"added": 2, "skipped": 0, "problems": []}
        rows = table_rows(db_path, "profiles")
        assert [(row["name"], row["key_ref"], row["user_agent"]) for row in rows] == [
            ("UA-1", None, self.UA_ONE),
            ("key-001", "key-001", None),
        ]

    def test_a_line_without_the_mozilla_prefix_stays_a_key_ref(self, pool, db_path):
        result = pool.import_lines(["Chrome/5.0 (Windows)"])

        assert result["added"] == 1
        row = table_rows(db_path, "profiles")[0]
        assert row["name"] == "Chrome/5.0 (Windows)"
        assert row["key_ref"] == "Chrome/5.0 (Windows)"
        assert row["user_agent"] is None

    def test_mixed_batch_keeps_key_ref_lines_unchanged(self, pool, db_path):
        result = pool.import_lines(["key-001 | Имя", self.UA_ONE, "key-002"])

        assert result == {"added": 3, "skipped": 0, "problems": []}
        rows = table_rows(db_path, "profiles")
        assert [(row["name"], row["key_ref"], row["user_agent"]) for row in rows] == [
            ("Имя", "key-001", None),
            ("UA-1", None, self.UA_ONE),
            ("key-002", "key-002", None),
        ]

    @pytest.mark.parametrize("line", ["", "   ", "\t\t"])
    def test_blank_lines_are_ignored_entirely(self, pool, line):
        result = pool.import_lines([line, self.UA_ONE])

        assert result == {"added": 1, "skipped": 0, "problems": []}

    @pytest.mark.parametrize(
        "line",
        [
            "| Mozilla/5.0 x",  # пустое значение до |
            "Mozilla/5.0 x |",  # пустое имя после |
            "\tMozilla/5.0 x",  # пустая первая колонка после нарезки по табу
            7,  # не текст
            None,
        ],
    )
    def test_broken_lines_go_to_problems_and_do_not_fail_the_batch(self, pool, line):
        result = pool.import_lines([line, "key-001"])

        assert result["added"] == 1
        assert result["skipped"] == 1
        assert result["problems"][0]["line_index"] == 0
        assert result["problems"][0]["message"]


class TestImportFile:
    """Путь приходит только из конфига — тот же контракт, что у прокси."""

    def test_imports_ua_and_key_ref_lines_from_a_file(self, pool, tmp_path, db_path):
        source = tmp_path / "user_agents.txt"
        source.write_text(
            "Mozilla/5.0 (Windows NT 10.0) Chrome/120.0\n"
            "\n"
            "key-001 | Имя\n"
            "Mozilla/5.0 (Windows NT 10.0) Chrome/120.0\n",
            encoding="utf-8",
        )

        result = pool.import_file(source)

        assert result["added"] == 2
        assert result["skipped"] == 1
        assert result["problems"][0]["line_index"] == 3
        rows = table_rows(db_path, "profiles")
        assert [(row["name"], row["user_agent"] is not None) for row in rows] == [
            ("UA-1", True),
            ("Имя", False),
        ]

    def test_missing_file_is_rejected(self, pool, tmp_path):
        with pytest.raises(ProfileImportError):
            pool.import_file(tmp_path / "nope.txt")

    def test_directory_is_rejected(self, pool, tmp_path):
        with pytest.raises(ProfileImportError):
            pool.import_file(tmp_path)


class TestListProfiles:
    """GET /control/profiles: ровно поля контракта, fields — объект."""

    def test_returns_exactly_the_contract_fields(self, pool, db_path):
        ProxyPool(db_path).add_lines(["10.0.0.1:8080"])
        proxy_id = table_rows(db_path, "proxies")[0]["id"]
        pool.add_profiles(
            [
                {
                    "name": "alice",
                    "key_ref": "hooks:alice",
                    "proxy_id": proxy_id,
                    "user_agent": "UA/1.0",
                    "locale": "en-US",
                    "timezone": "UTC",
                    "fields": {"cookie_set": "alice.txt"},
                }
            ]
        )

        listed = pool.list_profiles()

        assert len(listed) == 1
        assert set(listed[0]) == {
            "id",
            "name",
            "key_ref",
            "proxy_id",
            "user_agent",
            "locale",
            "timezone",
            "status",
            "last_used_at",
            "fields",
            "assigned_browser_id",
        }
        assert listed[0]["fields"] == {"cookie_set": "alice.txt"}
        assert listed[0]["status"] == "free"
        assert listed[0]["assigned_browser_id"] is None

    def test_empty_pool_gives_an_empty_list(self, pool):
        assert pool.list_profiles() == []

    def test_assigned_browser_id_follows_a_live_worker_only(self, pool, db_path):
        (profile_id,) = add_free_profiles(pool, 1)
        make_worker(db_path, "br-1", status="running")
        bind(db_path, "br-1", profile_id)

        assert pool.list_profiles()[0]["assigned_browser_id"] == "br-1"

        make_worker(db_path, "br-1", status="stopped")
        assert pool.list_profiles()[0]["assigned_browser_id"] is None, (
            "строка остановленного воркера не держит профиль"
        )

    def test_order_is_by_id(self, pool):
        pool.add_profiles([{"name": "c"}, {"name": "a"}, {"name": "b"}])

        assert [row["name"] for row in pool.list_profiles()] == ["c", "a", "b"]


class TestGetProfile:
    """Read-only строка профиля для воркера (engine.profile_apply).

    Воркеру нужны настройки, а не решение о праве работать: никаких
    проверок статуса и владения здесь нет — их делает машина статусов в
    ``mark_active``/``mark_done``.
    """

    def test_returns_the_row_with_fields_as_an_object(self, pool):
        pool.add_profiles(
            [
                {
                    "name": "alice",
                    "user_agent": "UA/1.0",
                    "locale": "en-US",
                    "timezone": "UTC",
                    "fields": {"cookie_set": "alice.txt"},
                }
            ]
        )

        row = pool.get_profile(profile_ids(pool)[0])

        assert row is not None
        assert row["name"] == "alice"
        assert row["user_agent"] == "UA/1.0"
        assert row["locale"] == "en-US"
        assert row["timezone"] == "UTC"
        assert row["status"] == "free"
        assert row["fields"] == {"cookie_set": "alice.txt"}

    def test_null_settings_stay_null_not_empty_strings(self, pool):
        add_free_profiles(pool, 1)

        row = pool.get_profile(profile_ids(pool)[0])

        assert row["user_agent"] is None
        assert row["locale"] is None
        assert row["timezone"] is None

    def test_unknown_id_yields_none(self, pool):
        add_free_profiles(pool, 1)

        assert pool.get_profile(424242) is None

    @pytest.mark.parametrize("bad_id", [True, "3", 3.0, None, 0, -1])
    def test_ids_outside_the_contract_yield_none(self, pool, bad_id):
        assert pool.get_profile(bad_id) is None


class TestDelete:
    def test_deletes_a_free_profile(self, pool, db_path):
        (profile_id,) = add_free_profiles(pool, 1)

        pool.delete(profile_id)

        assert table_rows(db_path, "profiles") == []

    def test_unknown_id_raises_not_found(self, pool):
        with pytest.raises(ProfileNotFoundError):
            pool.delete(424242)

    @pytest.mark.parametrize("value", ["5", None, True, 1.5, []])
    def test_non_integer_id_raises_not_found(self, pool, value):
        with pytest.raises(ProfileNotFoundError):
            pool.delete(value)

    def test_profile_held_by_a_live_worker_is_in_use(self, pool, db_path):
        (profile_id,) = add_free_profiles(pool, 1)
        make_worker(db_path, "br-1", status="running")
        bind(db_path, "br-1", profile_id)

        with pytest.raises(ProfileInUseError) as excinfo:
            pool.delete(profile_id)

        assert "br-1" in str(excinfo.value)
        assert len(table_rows(db_path, "profiles")) == 1

    @pytest.mark.parametrize("status", ["stopped", "circuit_open"])
    def test_dead_worker_does_not_block_delete(self, pool, db_path, status):
        (profile_id,) = add_free_profiles(pool, 1)
        make_worker(db_path, "br-1", status=status, pid=None)
        bind(db_path, "br-1", profile_id)

        pool.delete(profile_id)

        assert table_rows(db_path, "profiles") == []
        assert worker_row(db_path, "br-1")["profile_id"] is None, (
            "внешний ключ должен обнулить ссылку воркера"
        )


class TestDeleteMany:
    """Батчевое удаление: ни одно исключение не должно ронять весь запрос."""

    def test_deletes_all_listed(self, pool, db_path):
        ids = add_free_profiles(pool, 3)

        result = pool.delete_many(ids)

        assert result == {"deleted": 3, "skipped": 0, "problems": []}
        assert table_rows(db_path, "profiles") == []

    def test_busy_and_missing_are_reported_not_raised(self, pool, db_path):
        busy_id, free_id = add_free_profiles(pool, 2)
        make_worker(db_path, "br-1", status="running")
        bind(db_path, "br-1", busy_id)

        result = pool.delete_many([busy_id, free_id, 424242])

        assert result["deleted"] == 1
        assert result["skipped"] == 2
        assert f"id={busy_id}: назначен воркеру br-1" in result["problems"]
        assert "id=424242: профиль не найден" in result["problems"]
        assert [row["id"] for row in table_rows(db_path, "profiles")] == [busy_id]

    def test_duplicate_ids_count_once(self, pool, db_path):
        (profile_id,) = add_free_profiles(pool, 1)

        result = pool.delete_many([profile_id, profile_id])

        assert result == {"deleted": 1, "skipped": 0, "problems": []}
        assert table_rows(db_path, "profiles") == []

    def test_empty_list_is_a_noop(self, pool):
        assert pool.delete_many([]) == {"deleted": 0, "skipped": 0, "problems": []}

    @pytest.mark.parametrize("value", ["5", True, None, 1.5])
    def test_non_integer_entry_is_a_problem_not_an_exception(self, pool, db_path, value):
        (profile_id,) = add_free_profiles(pool, 1)

        result = pool.delete_many([value, profile_id])

        assert result["deleted"] == 1
        assert result["skipped"] == 1
        assert "неверный идентификатор" in result["problems"][0]
        assert table_rows(db_path, "profiles") == []

    def test_dead_worker_does_not_block_the_batch(self, pool, db_path):
        (profile_id,) = add_free_profiles(pool, 1)
        make_worker(db_path, "br-1", status="stopped", pid=None)
        bind(db_path, "br-1", profile_id)

        result = pool.delete_many([profile_id])

        assert result == {"deleted": 1, "skipped": 0, "problems": []}
        assert table_rows(db_path, "profiles") == []


class TestDeleteAll:
    """«Удалить всё» — тот же best-effort, что и у батча."""

    def test_removes_the_whole_pool(self, pool, db_path):
        add_free_profiles(pool, 3)

        result = pool.delete_all()

        assert result == {"deleted": 3, "skipped": 0, "problems": []}
        assert table_rows(db_path, "profiles") == []

    def test_assigned_profile_is_reported_not_raised(self, pool, db_path):
        busy_id, free_id = add_free_profiles(pool, 2)
        make_worker(db_path, "br-1", status="running")
        bind(db_path, "br-1", busy_id)

        result = pool.delete_all()

        assert result["deleted"] == 1
        assert result["skipped"] == 1
        assert f"id={busy_id}: назначен воркеру br-1" in result["problems"]
        assert [row["id"] for row in table_rows(db_path, "profiles")] == [busy_id]

    def test_empty_pool_is_a_noop(self, pool):
        assert pool.delete_all() == {"deleted": 0, "skipped": 0, "problems": []}

    def test_all_ids_returns_rows_in_order(self, pool):
        ids = add_free_profiles(pool, 3)

        assert pool.all_ids() == ids


class TestStatusMachine:
    """Переходы: системные делает пул, оператору доступны только три."""

    def test_status_sets_match_the_contract(self):
        assert set(PROFILE_STATUSES) == {"free", "assigned", "active", "error", "blocked"}
        assert set(OPERATOR_STATUSES) == {"free", "blocked", "error"}
        assert set(OPERATOR_STATUSES) < set(PROFILE_STATUSES)

    @pytest.mark.parametrize("status", ["free", "blocked", "error"])
    def test_operator_can_set_any_of_his_three_statuses(self, pool, status):
        (profile_id,) = add_free_profiles(pool, 1)

        pool.set_status(profile_id, status)

        assert statuses(pool) == [status]

    @pytest.mark.parametrize("status", ["assigned", "active"])
    def test_system_statuses_are_rejected_as_value_error(self, pool, status):
        (profile_id,) = add_free_profiles(pool, 1)

        with pytest.raises(ValueError) as excinfo:
            pool.set_status(profile_id, status)

        assert isinstance(excinfo.value, ProfileInvalidError), (
            "HTTP обязан получить 400 именно от этого типа"
        )

    @pytest.mark.parametrize("status", ["new", "", None, 5])
    def test_unknown_status_is_a_value_error(self, pool, status):
        (profile_id,) = add_free_profiles(pool, 1)

        with pytest.raises(ValueError):
            pool.set_status(profile_id, status)

    def test_unknown_profile_is_not_found(self, pool):
        with pytest.raises(ProfileNotFoundError):
            pool.set_status(424242, "blocked")

    def test_setting_the_same_status_is_not_an_error(self, pool):
        (profile_id,) = add_free_profiles(pool, 1)

        pool.set_status(profile_id, "free")

        assert statuses(pool) == ["free"]

    def test_mark_active_requires_an_assigned_profile(self, pool):
        (profile_id,) = add_free_profiles(pool, 1)

        with pytest.raises(ValueError):
            pool.mark_active(profile_id)

    @pytest.mark.parametrize("status", ["blocked", "error"])
    def test_mark_active_rejects_operator_statuses(self, pool, status):
        (profile_id,) = add_free_profiles(pool, 1)
        pool.set_status(profile_id, status)

        with pytest.raises(ValueError):
            pool.mark_active(profile_id)

    def test_mark_active_moves_assigned_to_active_and_stamps_last_used(self, pool):
        (profile_id,) = add_free_profiles(pool, 1)
        claim(pool, profile_id)

        pool.mark_active(profile_id, now=1_700_000_123.0)

        row = table_rows(pool.db_path, "profiles")[0]
        assert row["status"] == "active"
        assert row["last_used_at"] == 1_700_000_123.0

    def test_mark_done_success_returns_the_profile_to_assigned(self, pool):
        (profile_id,) = add_free_profiles(pool, 1)
        claim(pool, profile_id)
        pool.mark_active(profile_id)

        pool.mark_done(profile_id, now=1_700_000_456.0)

        row = table_rows(pool.db_path, "profiles")[0]
        assert row["status"] == "assigned"
        assert row["last_used_at"] == 1_700_000_456.0

    def test_mark_done_failure_puts_the_profile_into_error(self, pool):
        (profile_id,) = add_free_profiles(pool, 1)
        claim(pool, profile_id)
        pool.mark_active(profile_id)

        pool.mark_done(profile_id, ok=False)

        assert statuses(pool) == ["error"]

    @pytest.mark.parametrize("status", ["free", "blocked"])
    def test_mark_done_on_a_profile_that_is_not_worked_is_rejected(self, pool, status):
        (profile_id,) = add_free_profiles(pool, 1)
        if status == "blocked":
            pool.set_status(profile_id, "blocked")

        with pytest.raises(ValueError):
            pool.mark_done(profile_id)

    def test_operator_can_return_an_errored_profile_to_the_pool(self, pool):
        (profile_id,) = add_free_profiles(pool, 1)
        claim(pool, profile_id)
        pool.mark_active(profile_id)
        pool.mark_done(profile_id, ok=False)

        pool.set_status(profile_id, "free")

        assert statuses(pool) == ["free"]


class TestTakeForWorker:
    def test_without_profiles_returns_none(self, pool):
        make_worker(pool.db_path, "br-1")

        assert pool.take_for_worker("br-1") is None

    def test_takes_the_first_free_profile_in_id_order(self, pool, db_path):
        ids = add_free_profiles(pool, 2)
        make_worker(db_path, "br-1")

        taken = pool.take_for_worker("br-1")

        assert taken["id"] == ids[0]
        assert table_rows(db_path, "profiles")[0]["status"] == "assigned"
        assert worker_row(db_path, "br-1")["profile_id"] == ids[0]

    def test_one_profile_per_worker(self, pool, db_path):
        add_free_profiles(pool, 1)
        make_worker(db_path, "br-1")
        make_worker(db_path, "br-2")

        assert pool.take_for_worker("br-1") is not None
        assert pool.take_for_worker("br-2") is None

    def test_taking_the_same_profile_twice_for_one_worker_is_a_no_op(self, pool, db_path):
        add_free_profiles(pool, 1)
        make_worker(db_path, "br-1")

        first = pool.take_for_worker("br-1")
        second = pool.take_for_worker("br-1")

        assert first["id"] == second["id"]
        assert statuses(pool) == ["assigned"]

    def test_prefers_the_previous_assignment_of_the_same_worker(self, pool, db_path):
        ids = add_free_profiles(pool, 3)
        make_worker(db_path, "br-1")
        bind(db_path, "br-1", ids[2])
        set_profile_status(db_path, ids[2], "assigned")

        taken = pool.take_for_worker("br-1")

        assert taken["id"] == ids[2], "воркер обязан получить свой прошлый профиль"

    def test_reclaims_a_previous_assignment_that_was_reaped_to_free(self, pool, db_path):
        ids = add_free_profiles(pool, 2)
        make_worker(db_path, "br-1")
        bind(db_path, "br-1", ids[1])
        # Реапер освободил профиль (воркер был мёртв), но память о назначении
        # осталась в workers.profile_id — respawn должен вернуть тот же профиль.

        taken = pool.take_for_worker("br-1")

        assert taken["id"] == ids[1]
        assert table_rows(db_path, "profiles")[1]["status"] == "assigned"

    def test_does_not_take_a_profile_held_by_another_live_worker(self, pool, db_path):
        ids = add_free_profiles(pool, 2)
        make_worker(db_path, "br-9", status="running")
        # Оператор сбросил статус, но живой воркер ещё работает с профилем.
        bind(db_path, "br-9", ids[0])
        make_worker(db_path, "br-1")

        taken = pool.take_for_worker("br-1")

        assert taken["id"] == ids[1], "живое назначение соседа неприкосновенно"

    @pytest.mark.parametrize("status", ["blocked", "error"])
    def test_blocked_or_errored_previous_profile_is_not_reclaimed(self, pool, db_path, status):
        ids = add_free_profiles(pool, 2)
        make_worker(db_path, "br-1")
        bind(db_path, "br-1", ids[0])
        set_profile_status(db_path, ids[0], status)

        taken = pool.take_for_worker("br-1")

        assert taken["id"] == ids[1], "заблокированный профиль не выдаётся никому"

    def test_claiming_clears_the_pointer_of_other_workers(self, pool, db_path):
        (profile_id,) = add_free_profiles(pool, 1)
        make_worker(db_path, "br-9", status="stopped", pid=None)
        bind(db_path, "br-9", profile_id)
        make_worker(db_path, "br-1")

        pool.take_for_worker("br-1")

        assert worker_row(db_path, "br-9")["profile_id"] is None
        assert worker_row(db_path, "br-1")["profile_id"] == profile_id

    def test_taken_profile_carries_its_own_proxy(self, pool, db_path):
        ProxyPool(db_path).add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        proxy_id = table_rows(db_path, "proxies")[0]["id"]
        pool.add_profiles([{"name": "alice", "proxy_id": proxy_id}])
        make_worker(db_path, "br-1")

        taken = pool.take_for_worker("br-1")

        assert taken["proxy_id"] == proxy_id, (
            "выданный профиль несёт свой прокси — супервизор передаст его в env"
        )


class TestRelease:
    def test_release_frees_the_profile_and_the_worker_pointer(self, pool, db_path):
        (profile_id,) = add_free_profiles(pool, 1)
        make_worker(db_path, "br-1")
        pool.take_for_worker("br-1")

        assert pool.release(profile_id) is True

        assert statuses(pool) == ["free"]
        assert worker_row(db_path, "br-1")["profile_id"] is None

    def test_release_keeps_a_blocked_profile_blocked(self, pool, db_path):
        (profile_id,) = add_free_profiles(pool, 1)
        claim(pool, profile_id)
        pool.set_status(profile_id, "blocked")

        assert pool.release(profile_id) is False

        assert statuses(pool) == ["blocked"], (
            "остановка воркера не должна разблокировать то, что заблокировал оператор"
        )
        assert worker_row(db_path, "br-1")["profile_id"] is None

    def test_release_of_an_unknown_profile_is_not_an_error(self, pool):
        assert pool.release(424242) is False

    @pytest.mark.parametrize("value", ["5", None, True, []])
    def test_release_of_a_non_integer_id_is_not_an_error(self, pool, value):
        assert pool.release(value) is False

    def test_release_for_worker_follows_the_pointer(self, pool, db_path):
        add_free_profiles(pool, 1)
        make_worker(db_path, "br-1")
        pool.take_for_worker("br-1")

        assert pool.release_for_worker("br-1") is True

        assert statuses(pool) == ["free"]
        assert worker_row(db_path, "br-1")["profile_id"] is None

    def test_release_for_a_worker_without_a_profile_is_a_no_op(self, pool, db_path):
        make_worker(db_path, "br-1")

        assert pool.release_for_worker("br-1") is False

    def test_release_for_an_unknown_worker_is_a_no_op(self, pool):
        assert pool.release_for_worker("ghost") is False


class TestUnassignAll:
    def test_every_profile_becomes_free_and_pointers_are_dropped(self, pool, db_path):
        ids = add_free_profiles(pool, 4)
        make_worker(db_path, "br-1")
        make_worker(db_path, "br-2")
        pool.take_for_worker("br-1")
        pool.take_for_worker("br-2")
        pool.set_status(ids[3], "blocked")

        released = pool.unassign_all()

        assert released == 3, "сброшены должны быть только те, что не были free"
        assert statuses(pool) == ["free", "free", "free", "free"]
        assert worker_row(db_path, "br-1")["profile_id"] is None
        assert worker_row(db_path, "br-2")["profile_id"] is None

    def test_repeated_unassign_releases_nothing(self, pool):
        add_free_profiles(pool, 1)

        assert pool.unassign_all() == 0
        assert pool.unassign_all() == 0


class TestAssignRange:
    """Назначить диапазон: по одному профилю на свободного воркера."""

    def test_gives_free_profiles_of_the_range_to_free_workers(self, pool, db_path):
        ids = add_free_profiles(pool, 3)
        for browser_id in ("br-1", "br-2", "br-3"):
            make_worker(db_path, browser_id)

        result = pool.assign_range(ids[0], ids[2])

        assert result == {"assigned": 3, "available": 0}
        assert statuses(pool) == ["assigned", "assigned", "assigned"]
        assert [worker_row(db_path, f"br-{i}")["profile_id"] for i in (1, 2, 3)] == ids

    def test_pairing_follows_profile_id_order(self, pool, db_path):
        ids = add_free_profiles(pool, 3)
        make_worker(db_path, "br-1")
        make_worker(db_path, "br-2")

        pool.assign_range(ids[1], ids[2])

        assert worker_row(db_path, "br-1")["profile_id"] == ids[1]
        assert worker_row(db_path, "br-2")["profile_id"] == ids[2]

    def test_profiles_outside_the_range_are_untouched(self, pool, db_path):
        ids = add_free_profiles(pool, 3)
        make_worker(db_path, "br-1")

        result = pool.assign_range(ids[0], ids[1])

        assert result == {"assigned": 1, "available": 1}
        assert statuses(pool) == ["assigned", "free", "free"]

    def test_range_without_free_workers_assigns_nothing(self, pool, db_path):
        ids = add_free_profiles(pool, 2)
        make_worker(db_path, "br-1")
        make_worker(db_path, "br-2", status="stopped")
        pool.take_for_worker("br-1")  # у br-1 уже есть профиль, br-2 остановлен

        result = pool.assign_range(ids[0], ids[1])

        assert result == {"assigned": 0, "available": 1}, (
            "свободных воркеров нет — выдавать некому, профили остаются как были"
        )
        assert statuses(pool) == ["assigned", "free"]

    def test_non_free_profiles_in_the_range_are_not_handed_out(self, pool, db_path):
        ids = add_free_profiles(pool, 2)
        pool.set_status(ids[0], "blocked")
        make_worker(db_path, "br-1")

        result = pool.assign_range(ids[0], ids[1])

        assert result == {"assigned": 1, "available": 0}
        assert statuses(pool) == ["blocked", "assigned"]

    def test_empty_range_is_not_an_error(self, pool, db_path):
        add_free_profiles(pool, 2)
        make_worker(db_path, "br-1")

        assert pool.assign_range(100, 200) == {"assigned": 0, "available": 0}

    @pytest.mark.parametrize("bounds", [(2, 1), (0, 1), (-1, 5)])
    def test_invalid_range_is_a_value_error(self, pool, bounds):
        add_free_profiles(pool, 2)

        with pytest.raises(ValueError):
            pool.assign_range(*bounds)

    @pytest.mark.parametrize("bounds", [(1.5, 2.5), ("1", "2"), (None, 2), (True, 2)])
    def test_non_integer_bounds_are_rejected(self, pool, bounds):
        with pytest.raises(ValueError):
            pool.assign_range(*bounds)

    def test_assignment_clears_pointers_of_other_workers(self, pool, db_path):
        (profile_id,) = add_free_profiles(pool, 1)
        make_worker(db_path, "br-9", status="stopped", pid=None)
        bind(db_path, "br-9", profile_id)
        make_worker(db_path, "br-1")

        pool.assign_range(profile_id, profile_id)

        assert worker_row(db_path, "br-9")["profile_id"] is None
        assert worker_row(db_path, "br-1")["profile_id"] == profile_id


class TestReap:
    """Страховка: назначение без живого воркера не должно висеть вечно."""

    def test_profile_of_a_live_worker_is_kept(self, pool, db_path):
        (profile_id,) = add_free_profiles(pool, 1)
        make_worker(db_path, "br-1")
        pool.take_for_worker("br-1")

        assert pool.reap({profile_id}) == 0
        assert statuses(pool) == ["assigned"]
        assert worker_row(db_path, "br-1")["profile_id"] == profile_id

    @pytest.mark.parametrize("status", ["assigned", "active"])
    def test_profile_without_a_live_holder_is_freed(self, pool, db_path, status):
        (profile_id,) = add_free_profiles(pool, 1)
        make_worker(db_path, "br-1")
        pool.take_for_worker("br-1")
        set_profile_status(db_path, profile_id, status)

        assert pool.reap(set()) == 1

        assert statuses(pool) == ["free"]

    def test_reap_keeps_the_pointer_so_a_respawn_finds_its_profile(self, pool, db_path):
        (profile_id,) = add_free_profiles(pool, 1)
        make_worker(db_path, "br-1", status="backoff")
        pool.take_for_worker("br-1")

        pool.reap(set())

        assert worker_row(db_path, "br-1")["profile_id"] == profile_id, (
            "память о прошлом назначении нужна, чтобы респавн получил тот же профиль"
        )

    def test_reap_ignores_free_and_operator_statuses(self, pool, db_path):
        ids = add_free_profiles(pool, 2)
        pool.set_status(ids[1], "blocked")

        assert pool.reap(set()) == 0
        assert statuses(pool) == ["free", "blocked"]

    def test_reap_frees_only_profiles_outside_the_live_set(self, pool, db_path):
        ids = add_free_profiles(pool, 2)
        make_worker(db_path, "br-1")
        make_worker(db_path, "br-2")
        set_profile_status(db_path, ids[0], "assigned")
        set_profile_status(db_path, ids[1], "assigned")
        bind(db_path, "br-1", ids[0])
        bind(db_path, "br-2", ids[1])

        assert pool.reap({ids[0]}) == 1

        assert statuses(pool) == ["assigned", "free"]
        assert worker_row(db_path, "br-2")["profile_id"] == ids[1]
