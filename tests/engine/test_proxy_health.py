"""Тесты health-проверки прокси: настоящий сокет, только loopback.

Мини-прокси в тестах отвечает на ``CONNECT`` по настройке: 200, 407 с
проверкой пароля, тишина (таймаут), задержка (латентность), мусор вместо
HTTP. Сети за пределами 127.0.0.1 здесь нет ни в одном тесте.

Проверяется то, что ломает эксплуатацию: зависание на мёртвом прокси
(таймаут обязан срабатывать), потеря прогресса (результат пишется по мере
проверки) и утечка кредов в ``last_error``.
"""

from __future__ import annotations

import base64
import socket
import threading
import time

import pytest

from engine.db import migrations
from engine.proxy_health import (
    CHECK_THREADS,
    CHECK_TIMEOUT_SECONDS,
    DEFAULT_CHECK_TARGET,
    CheckInProgressError,
    ProxyHealthChecker,
    check_proxy,
    run_check,
)
from engine.proxy_pool import ProxyPool

# Цель проверки в тестах — тоже loopback: прокси в тестах ничего не дозваниваются,
# но и подменённая цель не должна указывать наружу.
LOOPBACK_TARGET = "http://127.0.0.1:9"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


@pytest.fixture
def pool(db_path):
    return ProxyPool(db_path)


class LoopbackProxy:
    """Минимальный HTTP-прокси на loopback для тестов.

    ``silent=True`` — принимает соединение и никогда не отвечает (проверка
    таймаута); ``require_auth=(user, password)`` — отвечает 407 на всё,
    кроме верного ``Proxy-Authorization``; ``delay`` — пауза перед ответом
    для измерения латентности.
    """

    def __init__(self, *, status=200, require_auth=None, delay=0.0, silent=False, raw=None):
        self.status = status
        self.require_auth = require_auth
        self.delay = delay
        self.silent = silent
        self.raw = raw
        self.requests = []
        self._stop = threading.Event()
        self._listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listen.bind(("127.0.0.1", 0))
        self._listen.listen(16)
        self.host, self.port = self._listen.getsockname()
        self._handlers = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def authorization(self):
        """``Proxy-Authorization`` последнего запроса (None, если не прислали)."""
        return self.requests[-1]["authorization"] if self.requests else None

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._listen.accept()
            except OSError:
                return
            handler = threading.Thread(target=self._handle, args=(conn,), daemon=True)
            handler.start()
            self._handlers.append(handler)

    def _handle(self, conn):
        with conn:
            conn.settimeout(30)
            try:
                data = b""
                while b"\r\n\r\n" not in data and not self._stop.is_set():
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    data += chunk
            except OSError:
                return
            lines = data.decode("utf-8", "replace").split("\r\n")
            request_line = lines[0] if lines else ""
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    key, _, value = line.partition(":")
                    headers[key.strip().lower()] = value.strip()
            self.requests.append(
                {
                    "request_line": request_line,
                    "target": request_line.split(" ")[1] if " " in request_line else "",
                    "authorization": headers.get("proxy-authorization"),
                }
            )
            if self.silent:
                self._stop.wait(30)
                return
            if self.delay:
                time.sleep(self.delay)
            expected = None
            if self.require_auth is not None:
                user, password = self.require_auth
                token = base64.b64encode(f"{user}:{password}".encode()).decode()
                expected = f"Basic {token}"
            if self.raw is not None:
                conn.sendall(self.raw)
                return
            if expected is not None and headers.get("proxy-authorization") != expected:
                conn.sendall(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b'Proxy-Authenticate: Basic realm="test"\r\n'
                    b"Content-Length: 0\r\n\r\n"
                )
                return
            if self.status == 200:
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                conn.sendall(
                    f"HTTP/1.1 {self.status} Error\r\nContent-Length: 0\r\n\r\n".encode()
                )

    def close(self):
        self._stop.set()
        try:
            self._listen.close()
        except OSError:
            pass
        self._thread.join(timeout=2)
        for handler in self._handlers:
            handler.join(timeout=2)


class SilentProxy(LoopbackProxy):
    def __init__(self):
        super().__init__(silent=True)


@pytest.fixture
def servers():
    """Все поднятые в тесте прокси закрываются даже при падении assert'а."""
    created = []

    def make(**kwargs):
        server = LoopbackProxy(**kwargs)
        created.append(server)
        return server

    yield make
    for server in created:
        server.close()


def closed_port():
    """Порт, на котором никто не слушает: соединение будет отклонено."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def add_proxy(pool, host, port, username="alice", password="s3cr3t"):
    """Добавляет прокси и возвращает его строку (свежая — последняя по id)."""
    result = pool.add_lines([f"{username}:{password}@{host}:{port}"])
    assert result["added"] == 1, result
    return pool.list_for_check()[-1]


def rows(db_path):
    conn = migrations.connect(db_path)
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM proxies ORDER BY id")]
    finally:
        conn.close()


class TestCheckProxy:
    def test_success_is_alive_with_latency(self, servers):
        proxy = servers()
        started = time.monotonic()

        result = check_proxy(
            {"id": 1, "host": proxy.host, "port": proxy.port, "username": None, "password": None},
            target=LOOPBACK_TARGET,
            timeout=2.0,
        )

        assert result.alive is True
        assert result.error is None
        assert result.latency_ms is not None
        assert 0 <= result.latency_ms <= (time.monotonic() - started + 0.1) * 1000

    def test_connect_request_uses_target(self, servers):
        proxy = servers()

        check_proxy(
            {"id": 1, "host": proxy.host, "port": proxy.port, "username": None, "password": None},
            target=LOOPBACK_TARGET,
            timeout=2.0,
        )

        assert proxy.requests[0]["request_line"].startswith("CONNECT 127.0.0.1:9 ")

    def test_sends_basic_authorization_when_credentials_present(self, servers):
        proxy = servers(require_auth=("alice", "s3cr3t"))

        result = check_proxy(
            {
                "id": 1,
                "host": proxy.host,
                "port": proxy.port,
                "username": "alice",
                "password": "s3cr3t",
            },
            target=LOOPBACK_TARGET,
            timeout=2.0,
        )

        expected = "Basic " + base64.b64encode(b"alice:s3cr3t").decode()
        assert proxy.authorization == expected
        assert result.alive is True

    def test_sends_no_authorization_without_credentials(self, servers):
        proxy = servers()

        check_proxy(
            {"id": 1, "host": proxy.host, "port": proxy.port, "username": None, "password": None},
            target=LOOPBACK_TARGET,
            timeout=2.0,
        )

        assert proxy.authorization is None

    def test_wrong_password_is_dead_407_without_leaking_credentials(self, servers):
        proxy = servers(require_auth=("alice", "correct-password"))

        result = check_proxy(
            {
                "id": 1,
                "host": proxy.host,
                "port": proxy.port,
                "username": "alice",
                "password": "WRONG-PASSWORD",
            },
            target=LOOPBACK_TARGET,
            timeout=2.0,
        )

        assert result.alive is False
        assert "407" in result.error
        assert "WRONG-PASSWORD" not in result.error
        assert "alice" not in result.error
        assert "s3cr3t" not in result.error

    def test_connection_refused_is_dead(self):
        result = check_proxy(
            {
                "id": 1,
                "host": "127.0.0.1",
                "port": closed_port(),
                "username": None,
                "password": None,
            },
            target=LOOPBACK_TARGET,
            timeout=2.0,
        )

        assert result.alive is False
        assert result.error
        assert result.latency_ms is None

    def test_silent_proxy_hits_timeout_and_does_not_hang(self, servers):
        proxy = servers(silent=True)
        started = time.monotonic()

        result = check_proxy(
            {"id": 1, "host": proxy.host, "port": proxy.port, "username": None, "password": None},
            target=LOOPBACK_TARGET,
            timeout=0.3,
        )

        elapsed = time.monotonic() - started
        assert result.alive is False
        assert "таймаут" in result.error
        assert elapsed < 2.0, f"проверка зависла на {elapsed:.1f}s"

    def test_non_http_response_is_dead(self, servers):
        proxy = servers(raw=b"not http at all\r\n\r\n")

        result = check_proxy(
            {"id": 1, "host": proxy.host, "port": proxy.port, "username": None, "password": None},
            target=LOOPBACK_TARGET,
            timeout=2.0,
        )

        assert result.alive is False
        assert "HTTP" in result.error

    def test_error_status_from_proxy_is_dead(self, servers):
        proxy = servers(status=502)

        result = check_proxy(
            {"id": 1, "host": proxy.host, "port": proxy.port, "username": None, "password": None},
            target=LOOPBACK_TARGET,
            timeout=2.0,
        )

        assert result.alive is False
        assert "502" in result.error

    def test_latency_is_measured_on_slow_proxy(self, servers):
        proxy = servers(delay=0.2)

        result = check_proxy(
            {"id": 1, "host": proxy.host, "port": proxy.port, "username": None, "password": None},
            target=LOOPBACK_TARGET,
            timeout=2.0,
        )

        assert result.alive is True
        assert result.latency_ms >= 190


class TestRunCheck:
    def test_writes_results_and_counts(self, pool, db_path, servers):
        proxy = servers()
        add_proxy(pool, "127.0.0.1", proxy.port)
        add_proxy(pool, "127.0.0.1", closed_port())

        summary = run_check(pool, target=LOOPBACK_TARGET, timeout=2.0)

        assert summary == {"checked": 2, "alive": 1, "dead": 1}
        stored = rows(db_path)
        assert [row["is_alive"] for row in stored] == [1, 0]
        assert all(row["last_checked_at"] for row in stored)
        assert stored[1]["last_error"]

    def test_empty_pool_is_a_noop(self, pool):
        assert run_check(pool, target=LOOPBACK_TARGET, timeout=2.0) == {
            "checked": 0,
            "alive": 0,
            "dead": 0,
        }

    def test_success_resets_fail_count_and_clears_error(self, pool, db_path, servers):
        proxy = servers()
        add_proxy(pool, "127.0.0.1", proxy.port)
        conn = migrations.connect(db_path)
        conn.execute(
            "UPDATE proxies SET fail_count = 3, is_alive = 0, last_error = 'старая ошибка'"
        )
        conn.commit()
        conn.close()

        run_check(pool, target=LOOPBACK_TARGET, timeout=2.0)

        stored = rows(db_path)[0]
        assert stored["fail_count"] == 0
        assert stored["is_alive"] == 1
        assert stored["last_error"] is None
        assert stored["latency_ms"] is not None

    def test_failure_increments_fail_count_and_keeps_latency(self, pool, db_path):
        add_proxy(pool, "127.0.0.1", closed_port())
        conn = migrations.connect(db_path)
        conn.execute("UPDATE proxies SET fail_count = 1, latency_ms = 111")
        conn.commit()
        conn.close()

        run_check(pool, target=LOOPBACK_TARGET, timeout=1.0)

        stored = rows(db_path)[0]
        assert stored["fail_count"] == 2
        assert stored["is_alive"] == 0
        assert stored["latency_ms"] == 111, "провальная проверка не должна затирать латентность"

    def test_results_are_written_while_check_is_running(self, pool, db_path, servers):
        """Прогресс виден в GET: первый прокси проверен, второй ещё нет."""
        fast = servers()
        slow = servers(silent=True)
        add_proxy(pool, "127.0.0.1", fast.port)
        add_proxy(pool, "127.0.0.1", slow.port)

        worker = threading.Thread(
            target=run_check,
            kwargs={"pool": pool, "target": LOOPBACK_TARGET, "timeout": 1.0, "threads": 1},
        )
        worker.start()
        try:
            seen_progress = False
            deadline = time.monotonic() + 0.8
            while time.monotonic() < deadline:
                first, second = rows(db_path)
                if first["last_checked_at"] and not second["last_checked_at"]:
                    seen_progress = True
                    break
                time.sleep(0.01)
            assert seen_progress, "первый результат должен быть записан до конца проверки"
        finally:
            worker.join(timeout=5)

    def test_concurrent_check_of_hanging_proxies_finishes(self, pool, db_path):
        """8 зависших прокси при 8 потоках: один таймаут, а не восемь подряд."""
        for _ in range(8):
            add_proxy(pool, "127.0.0.1", closed_port())
        silent = SilentProxy()
        try:
            started = time.monotonic()
            summary = run_check(pool, target=LOOPBACK_TARGET, timeout=0.4, threads=CHECK_THREADS)
            elapsed = time.monotonic() - started
        finally:
            silent.close()

        assert summary["checked"] == 8
        assert summary["dead"] == 8
        assert elapsed < 1.6, f"проверка шла {elapsed:.2f}s — потоки не работают параллельно"
        assert all(row["last_checked_at"] for row in rows(db_path))

    def test_results_update_only_the_checked_row(self, pool, db_path, servers):
        """Проверка не должна переписывать чужие колонки соседних строк."""
        proxy = servers()
        add_proxy(pool, "127.0.0.1", proxy.port)
        second = add_proxy(pool, "127.0.0.1", closed_port())
        conn = migrations.connect(db_path)
        conn.execute("UPDATE proxies SET label = 'нетронь' WHERE id = ?", (second["id"],))
        conn.commit()
        conn.close()

        run_check(pool, target=LOOPBACK_TARGET, timeout=1.0)

        assert {row["label"] for row in rows(db_path)} == {None, "нетронь"}
        assert rows(db_path)[1]["last_checked_at"], "проверенный прокси обязан получить отметку"


class TestProxyHealthChecker:
    def test_start_runs_in_background(self, db_path, servers):
        proxy = servers()
        pool = ProxyPool(db_path)
        add_proxy(pool, "127.0.0.1", proxy.port)
        checker = ProxyHealthChecker(pool, target=LOOPBACK_TARGET, timeout=2.0)

        checker.start()
        checker.join(timeout=5)

        assert checker.is_running is False
        assert checker.last_summary == {"checked": 1, "alive": 1, "dead": 0}
        assert checker.last_error is None

    def test_start_while_running_is_rejected(self, db_path, servers):
        proxy = servers(silent=True)
        pool = ProxyPool(db_path)
        add_proxy(pool, "127.0.0.1", proxy.port)
        checker = ProxyHealthChecker(pool, target=LOOPBACK_TARGET, timeout=1.5)

        checker.start()
        try:
            with pytest.raises(CheckInProgressError):
                checker.start()
            with pytest.raises(CheckInProgressError):
                checker.run()
        finally:
            checker.join(timeout=5)

    def test_run_is_blocking(self, db_path, servers):
        proxy = servers()
        pool = ProxyPool(db_path)
        add_proxy(pool, "127.0.0.1", proxy.port)
        checker = ProxyHealthChecker(pool, target=LOOPBACK_TARGET, timeout=2.0)

        summary = checker.run()

        assert summary == {"checked": 1, "alive": 1, "dead": 0}
        assert checker.is_running is False

    def test_thread_failure_is_recorded_and_flag_released(self, db_path, monkeypatch):
        """Упавшая проверка не оставляет «вечную работу» и не теряет причину."""
        import engine.proxy_health as health_module

        def boom(*args, **kwargs):
            raise RuntimeError("база недоступна")

        monkeypatch.setattr(health_module, "run_check", boom)
        seen = []
        checker = ProxyHealthChecker(
            ProxyPool(db_path),
            target=LOOPBACK_TARGET,
            timeout=1.0,
            on_error=seen.append,
        )

        checker.start()
        checker.join(timeout=5)

        assert checker.is_running is False
        assert "база недоступна" in checker.last_error
        assert seen and "база недоступна" in str(seen[0])

        monkeypatch.undo()
        assert checker.run() == {"checked": 0, "alive": 0, "dead": 0}, "флаг должен сняться"


class TestConstants:
    def test_default_target_is_scheme_host_port(self):
        assert "://" in DEFAULT_CHECK_TARGET
        host, _, port = DEFAULT_CHECK_TARGET.partition("://")[2].rpartition(":")
        assert host and port.isdigit()

    def test_defaults_are_sane(self):
        assert CHECK_THREADS >= 2
        assert CHECK_TIMEOUT_SECONDS > 0
