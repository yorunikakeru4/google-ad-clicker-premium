"""Тесты репозитория прокси: разбор строк, дедуп, join, 409-логика.

Сети здесь нет вообще: строки вставляются в SQLite и читаются обратно.
Главный предмет проверки — креды. Они обязаны сохраняться в БД (иначе
супервизор не сможет назначить прокси воркеру) и обязаны не появляться
ни в одном сообщении об ошибке, которое уходит в HTTP-ответ.
"""

from __future__ import annotations

import pytest

from engine.control_plane.config import SECRET_MASK
from engine.control_plane.state import StateStore
from engine.db import migrations
from engine.proxy_pool import (
    ParsedProxy,
    ProxyImportError,
    ProxyInUseError,
    ProxyNotFoundError,
    ProxyPool,
    parse_proxy_line,
    record_usage,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


@pytest.fixture
def pool(db_path):
    return ProxyPool(db_path)


def table_rows(db_path, table):
    """Сырые строки таблицы: проверяем БД, а не собственные методы пула."""
    conn = migrations.connect(db_path)
    try:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]
    finally:
        conn.close()


def assign_worker(db_path, proxy_id, browser_id, status="running", pid=4321):
    """Назначает воркера на прокси напрямую в БД.

    Назначением владеет ветка супервизора, поэтому здесь нет метода assign:
    тест получает нужное состояние тем же SQL, которым оно будет создано там.
    """
    StateStore(db_path).register_worker(browser_id, pid)
    conn = migrations.connect(db_path)
    try:
        conn.execute(
            "UPDATE workers SET proxy_id = ?, status = ? WHERE browser_id = ?",
            (proxy_id, status, browser_id),
        )
        conn.commit()
    finally:
        conn.close()


class TestParseProxyLine:
    def test_parses_credentials_and_defaults_scheme(self):
        parsed = parse_proxy_line("alice:s3cr3t@10.0.0.1:8080")

        assert parsed == ParsedProxy(
            scheme="http", host="10.0.0.1", port=8080, username="alice", password="s3cr3t"
        )

    def test_parses_explicit_scheme(self):
        parsed = parse_proxy_line("socks5://alice:s3cr3t@10.0.0.1:1080")

        assert parsed.scheme == "socks5"
        assert parsed.port == 1080

    def test_parses_line_without_credentials(self):
        parsed = parse_proxy_line("10.0.0.1:8080")

        assert parsed.username is None
        assert parsed.password is None
        assert parsed.host == "10.0.0.1"
        assert parsed.port == 8080

    def test_password_may_contain_colon(self):
        parsed = parse_proxy_line("alice:pa:ss@10.0.0.1:8080")

        assert parsed.username == "alice"
        assert parsed.password == "pa:ss"

    def test_surrounding_quotes_and_whitespace_are_tolerated(self):
        parsed = parse_proxy_line('  "alice:s3cr3t@10.0.0.1:8080"  ')

        assert parsed.host == "10.0.0.1"
        assert parsed.password == "s3cr3t"

    @pytest.mark.parametrize(
        "line",
        [
            "10.0.0.1",  # без порта
            "10.0.0.1:",  # пустой порт
            "10.0.0.1:http",  # порт не число
            "10.0.0.1:0",  # порт вне диапазона
            "10.0.0.1:70000",  # порт вне диапазона
            "alice:@10.0.0.1:80",  # пустой пароль
            ":s3cr3t@10.0.0.1:80",  # пустой логин
            "alice:s3cr3t@",  # без host:port
            "alice:s3cr3t@10.0.0.1",  # без порта
            "alice@10.0.0.1:80",  # без пароля
            "a@b@10.0.0.1:80",  # два @
            "10.0.0.1:80:90",  # двойной двоеточие без кредов
            "",  # пустая строка
        ],
    )
    def test_malformed_lines_are_rejected(self, line):
        with pytest.raises(ValueError):
            parse_proxy_line(line)

    def test_error_message_never_contains_password(self):
        """Текст ошибки уходит в HTTP-ответ и обязан быть без кредов."""
        with pytest.raises(ValueError) as excinfo:
            parse_proxy_line("alice:SUPER-SECRET@")

        assert "SUPER-SECRET" not in str(excinfo.value)
        assert "alice" not in str(excinfo.value)

    def test_error_message_never_contains_line_with_bad_port(self):
        with pytest.raises(ValueError) as excinfo:
            parse_proxy_line("alice:SUPER-SECRET@10.0.0.1:port")

        assert "SUPER-SECRET" not in str(excinfo.value)

    def test_unsupported_scheme_is_rejected(self):
        with pytest.raises(ValueError):
            parse_proxy_line("ftp://10.0.0.1:8080")


class TestAddLines:
    def test_adds_valid_lines(self, pool, db_path):
        result = pool.add_lines(["alice:s3cr3t@10.0.0.1:8080", "10.0.0.2:9090"])

        assert result["added"] == 2
        assert result["skipped"] == 0
        assert result["problems"] == []
        stored = table_rows(db_path, "proxies")
        assert [(row["host"], row["port"]) for row in stored] == [
            ("10.0.0.1", 8080),
            ("10.0.0.2", 9090),
        ]
        assert stored[0]["password"] == "s3cr3t", "креды должны сохраниться для назначения"
        assert stored[0]["scheme"] == "http"

    def test_malformed_line_is_skipped_with_problem(self, pool):
        result = pool.add_lines(["alice:s3cr3t@10.0.0.1:8080", "не прокси", "10.0.0.3:0"])

        assert result["added"] == 1
        assert result["skipped"] == 2
        assert [p["line_index"] for p in result["problems"]] == [1, 2]
        assert all(p["message"] for p in result["problems"])

    def test_problem_message_does_not_echo_credentials(self, pool):
        result = pool.add_lines(["alice:SUPER-SECRET@"])

        text = str(result["problems"])
        assert "SUPER-SECRET" not in text
        assert "alice" not in text

    def test_duplicate_against_database_is_skipped(self, pool):
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])

        result = pool.add_lines(["alice:OTHER-PASSWORD@10.0.0.1:8080"])

        assert result["added"] == 0
        assert result["skipped"] == 1
        assert result["problems"][0]["line_index"] == 0

    def test_duplicate_within_one_batch_is_skipped(self, pool):
        result = pool.add_lines(
            ["alice:s3cr3t@10.0.0.1:8080", "alice:s3cr3t@10.0.0.1:8080"]
        )

        assert result["added"] == 1
        assert result["skipped"] == 1
        assert result["problems"][0]["line_index"] == 1

    def test_same_host_with_other_user_is_not_a_duplicate(self, pool):
        result = pool.add_lines(["alice:s3cr3t@10.0.0.1:8080", "bob:s3cr3t@10.0.0.1:8080"])

        assert result["added"] == 2
        assert result["problems"] == []

    def test_lines_without_credentials_deduplicate_by_host_port(self, pool):
        result = pool.add_lines(["10.0.0.1:8080", "10.0.0.1:8080"])

        assert result["added"] == 1
        assert result["skipped"] == 1

    def test_blank_lines_are_ignored_silently(self, pool):
        """Хвостовой перевод строки в proxies.txt — не ошибка и не дубль."""
        result = pool.add_lines(["", "   ", "alice:s3cr3t@10.0.0.1:8080", ""])

        assert result == {"added": 1, "skipped": 0, "problems": []}

    def test_non_string_line_is_reported(self, pool):
        result = pool.add_lines(["alice:s3cr3t@10.0.0.1:8080", None, 42])

        assert result["added"] == 1
        assert result["skipped"] == 2
        assert [p["line_index"] for p in result["problems"]] == [1, 2]

    def test_failed_batch_is_rolled_back(self, pool, db_path, monkeypatch):
        """Непредвиденная ошибка посреди пачки не оставляет половину записей."""
        import engine.proxy_pool as proxy_pool_module

        original = proxy_pool_module.parse_proxy_line

        def explode_on_second(line):
            if "10.0.0.2" in str(line):
                raise RuntimeError("сломанная база")
            return original(line)

        monkeypatch.setattr(proxy_pool_module, "parse_proxy_line", explode_on_second)

        with pytest.raises(RuntimeError):
            pool.add_lines(["alice:s3cr3t@10.0.0.1:8080", "10.0.0.2:9090"])

        assert table_rows(db_path, "proxies") == []

    def test_pool_accepts_separate_instances_on_same_db(self, pool, db_path):
        """Каждая операция открывает своё соединение — два пула не мешают друг другу."""
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])

        other = ProxyPool(db_path)
        result = other.add_lines(["alice:s3cr3t@10.0.0.1:8080"])

        assert result["skipped"] == 1


class TestListProxies:
    def test_returns_contract_fields(self, pool):
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])

        listed = pool.list_proxies()

        assert set(listed[0]) == {
            "id",
            "label",
            "scheme",
            "host",
            "port",
            "username",
            "password",
            "country",
            "latency_ms",
            "is_alive",
            "fail_count",
            "last_checked_at",
            "last_error",
            "assigned_browser_id",
            "usage_count",
        }

    def test_credentials_are_masked_in_list(self, pool):
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])

        listed = pool.list_proxies()

        assert listed[0]["username"] == SECRET_MASK
        assert listed[0]["password"] == SECRET_MASK

    def test_empty_credentials_stay_empty(self, pool):
        pool.add_lines(["10.0.0.1:8080"])

        listed = pool.list_proxies()

        assert not listed[0]["username"]
        assert not listed[0]["password"]

    def test_assigned_browser_id_comes_from_workers_join(self, pool, db_path):
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        proxy_id = pool.list_proxies()[0]["id"]
        assign_worker(db_path, proxy_id, "br-7")

        assert pool.list_proxies()[0]["assigned_browser_id"] == "br-7"

    def test_unassigned_proxy_has_null_browser_id(self, pool):
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])

        assert pool.list_proxies()[0]["assigned_browser_id"] is None

    def test_usage_count_comes_from_proxy_usage(self, pool, db_path):
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        proxy_id = pool.list_proxies()[0]["id"]
        record_usage(db_path, proxy_id=proxy_id, browser_id="br-1", result="ok", latency_ms=120)
        record_usage(db_path, proxy_id=proxy_id, browser_id="br-2", result="timeout")

        assert pool.list_proxies()[0]["usage_count"] == 2

    def test_list_is_ordered_by_id(self, pool):
        pool.add_lines(
            [
                "alice:s3cr3t@10.0.0.1:8080",
                "10.0.0.2:8080",
                "10.0.0.3:8080",
            ]
        )

        assert [row["id"] for row in pool.list_proxies()] == sorted(
            row["id"] for row in pool.list_proxies()
        )


class TestGet:
    def test_get_returns_raw_credentials_for_assignment(self, pool):
        """Список маскируется, одна строка — нет: супервизору нужны настоящие."""
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        proxy_id = pool.list_proxies()[0]["id"]

        row = pool.get(proxy_id)

        assert row["username"] == "alice"
        assert row["password"] == "s3cr3t"

    def test_get_unknown_returns_none(self, pool):
        assert pool.get(424242) is None


class TestDelete:
    def test_delete_unassigned_proxy(self, pool, db_path):
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        proxy_id = pool.list_proxies()[0]["id"]

        assert pool.delete(proxy_id) is True
        assert table_rows(db_path, "proxies") == []

    def test_delete_conflicts_with_running_worker(self, pool, db_path):
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        proxy_id = pool.list_proxies()[0]["id"]
        assign_worker(db_path, proxy_id, "br-1", status="running")

        with pytest.raises(ProxyInUseError):
            pool.delete(proxy_id)

        assert len(table_rows(db_path, "proxies")) == 1, "конфликт не должен удалять строку"

    def test_delete_conflicts_with_starting_worker(self, pool, db_path):
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        proxy_id = pool.list_proxies()[0]["id"]
        assign_worker(db_path, proxy_id, "br-1", status="starting")

        with pytest.raises(ProxyInUseError):
            pool.delete(proxy_id)

    def test_delete_allowed_when_worker_is_stopped(self, pool, db_path):
        """Остановленный воркер не держит прокси: его строка остаётся в БД."""
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        proxy_id = pool.list_proxies()[0]["id"]
        assign_worker(db_path, proxy_id, "br-1", status="stopped", pid=None)

        assert pool.delete(proxy_id) is True
        assert table_rows(db_path, "proxies") == []

    def test_delete_unknown_id_raises(self, pool):
        with pytest.raises(ProxyNotFoundError):
            pool.delete(424242)

    def test_delete_error_message_has_no_credentials(self, pool, db_path):
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        proxy_id = pool.list_proxies()[0]["id"]
        assign_worker(db_path, proxy_id, "br-1", status="running")

        with pytest.raises(ProxyInUseError) as excinfo:
            pool.delete(proxy_id)

        assert "s3cr3t" not in str(excinfo.value)


class TestRecordUsage:
    def test_inserts_usage_row(self, pool, db_path):
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        proxy_id = pool.list_proxies()[0]["id"]

        record_usage(
            db_path, proxy_id=proxy_id, browser_id="br-1", result="ok", latency_ms=150
        )

        usage = table_rows(db_path, "proxy_usage")
        assert len(usage) == 1
        assert usage[0]["browser_id"] == "br-1"
        assert usage[0]["result"] == "ok"
        assert usage[0]["latency_ms"] == 150
        assert usage[0]["ts"] > 0

    def test_method_matches_module_function(self, pool, db_path):
        pool.add_lines(["alice:s3cr3t@10.0.0.1:8080"])
        proxy_id = pool.list_proxies()[0]["id"]

        pool.record_usage(proxy_id, browser_id="br-1", result="ok", latency_ms=10)

        assert len(table_rows(db_path, "proxy_usage")) == 1

    def test_unknown_proxy_is_rejected(self, pool, db_path):
        with pytest.raises(ProxyNotFoundError):
            record_usage(db_path, proxy_id=424242, browser_id="br-1", result="ok")

        assert table_rows(db_path, "proxy_usage") == []


class TestImportFile:
    def test_imports_lines_from_file(self, pool, tmp_path, db_path):
        source = tmp_path / "proxies.txt"
        source.write_text(
            "alice:s3cr3t@10.0.0.1:8080\n"
            "\n"
            "10.0.0.2:9090\n"
            "кривая строка\n",
            encoding="utf-8",
        )

        result = pool.import_file(source)

        assert result["added"] == 2
        assert result["skipped"] == 1
        assert result["problems"][0]["line_index"] == 3
        assert len(table_rows(db_path, "proxies")) == 2

    def test_missing_file_is_rejected(self, pool, tmp_path):
        with pytest.raises(ProxyImportError):
            pool.import_file(tmp_path / "nope.txt")

    def test_directory_is_rejected(self, pool, tmp_path):
        with pytest.raises(ProxyImportError):
            pool.import_file(tmp_path)

    def test_non_utf8_file_is_rejected_without_content_leak(self, pool, tmp_path):
        source = tmp_path / "proxies.txt"
        source.write_bytes(b"\xff\xfe\x00alice:s3cr3t@")

        with pytest.raises(ProxyImportError) as excinfo:
            pool.import_file(source)

        assert "s3cr3t" not in str(excinfo.value)

    def test_repeated_import_skips_everything(self, pool, tmp_path):
        source = tmp_path / "proxies.txt"
        source.write_text("alice:s3cr3t@10.0.0.1:8080\n", encoding="utf-8")

        pool.import_file(source)
        second = pool.import_file(source)

        assert second["added"] == 0
        assert second["skipped"] == 1
