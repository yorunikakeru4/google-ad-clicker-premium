"""Live-применение конфига: ``POST /control/config`` действует без рестарта.

До фикса патч из UI обновлял только объект HTTP-обработчика (instance + bound
класс): ``Daemon.config`` — его читает job порога CAPTCHA, — и
``ControlPlaneServer.config`` — через него ``_current_config`` читают
retention/db-size/day-close, — оставались со старыми значениями до рестарта.
Файловый уровень ``log_file_level`` применялся только при чтении конфига
(старт демона/воркера) и после POST не переприменялся вовсе.

Три свойства, которые здесь фиксируются (красные до фикса):

1. после успешного POST **все** читатели демона — job порога CAPTCHA,
   retention-job и ``Daemon.config`` — видят новое значение без рестарта;
2. ``behavior.log_file_level`` переприменяется сразу в том же процессе, где
   обновился конфиг; неудача применения не роняет запрос: старый уровень
   остаётся, причина уходит в таблицу ``logs``;
3. конкурентные POST и чтения не теряют поля и не отдают «рваный» конфиг:
   замена ссылки на конфиг происходит под замком записи, читатели получают
   либо старый, либо новый объект целиком.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

from engine.control_plane.api import TOKEN_HEADER
from tests.engine.control_plane.test_captcha_check import FakePolicy
from tests.engine.control_plane.test_log_jobs import make_daemon

TOKEN = "config-live-apply-token"


@pytest.fixture
def db_path(tmp_path):
    from engine.db import migrations

    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


@pytest.fixture
def config_path(tmp_path):
    return tmp_path / "config.json"


@pytest.fixture
def registry():
    from tests.engine.control_plane.test_supervisor import FakeProcessRegistry

    return FakeProcessRegistry()


def post_config(daemon, body):
    """POST /control/config к живому демону. Возвращает (status, json)."""
    url = f"http://127.0.0.1:{daemon.port}/control/config"
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST"
    )
    request.add_header(TOKEN_HEADER, TOKEN)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def log_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT level, category, message FROM logs").fetchall()


class TestPostReachesDaemonReaders:
    """POST меняет конфиг всех потребителей демона, а не только обработчика."""

    def test_captcha_threshold_job_sees_the_new_value(self, db_path, config_path, registry):
        """Нить captcha-check читает конфиг на каждом тике — после POST тик
        обязан уйти в политику с новыми порогом и действием, без рестарта."""
        policy = FakePolicy()
        daemon = make_daemon(
            db_path,
            config_path,
            registry,
            captcha_check_interval=0.05,
            captcha_policy=policy,
            config_data={
                "behavior": {
                    "captcha_threshold_percent": 5.0,
                    "captcha_threshold_action": "warn",
                }
            },
        )
        daemon.start()
        try:
            status, _ = post_config(
                daemon,
                {
                    "behavior": {
                        "captcha_threshold_percent": 42.0,
                        "captcha_threshold_action": "pause",
                    }
                },
            )
            assert status == 200

            assert daemon._captcha_threshold_settings() == (42.0, "pause"), (
                "порог и действие обязаны читаться из нового конфига"
            )
            assert wait_until(lambda: (42.0, "pause") in policy.checks, timeout=5), (
                "job captcha-check не увидел новое значение до рестарта"
            )
        finally:
            daemon.shutdown()

    def test_retention_job_sees_the_new_value(self, db_path, config_path, registry, monkeypatch):
        """retention-job читает через ``_current_config`` — тот же путь, что и
        day-close/db-size: его источник тоже обязан обновляться по POST."""
        import engine.control_plane.daemon as daemon_module

        calls = []
        real = daemon_module.run_retention

        def capture(*args, **kwargs):
            calls.append(args[2])
            return real(*args, **kwargs)

        monkeypatch.setattr(daemon_module, "run_retention", capture)
        daemon = make_daemon(db_path, config_path, registry, retention_interval=0.05)
        daemon.start()
        try:
            assert int(daemon._current_config().get("behavior.log_retention_days")) == 30, (
                "до POST источник retention — дефолт"
            )
            status, _ = post_config(daemon, {"behavior": {"log_retention_days": 7}})
            assert status == 200

            assert int(daemon._current_config().get("behavior.log_retention_days")) == 7, (
                "_current_config обязан видеть применённый патч"
            )
            assert wait_until(lambda: 7 in calls, timeout=5), (
                "retention-job читал старое значение после POST"
            )
        finally:
            daemon.shutdown()

    def test_daemon_config_is_the_posted_object(self, db_path, config_path, registry):
        """``Daemon.config`` — та же ссылка, которую меняет POST, а не копия."""
        daemon = make_daemon(db_path, config_path, registry)
        daemon.start()
        try:
            status, _ = post_config(daemon, {"behavior": {"log_retention_days": 3}})
            assert status == 200

            assert int(daemon.config.get("behavior.log_retention_days")) == 3, (
                "у демона не должно остаться собственной устаревшей копии"
            )
            assert daemon.config is daemon._current_config(), (
                "читатели обязаны ходить к одному объекту"
            )
        finally:
            daemon.shutdown()


class TestLogFileLevelLiveApply:
    """``log_file_level`` действует сразу после POST, а не при следующем старте."""

    def test_post_changes_the_file_handler_level(self, db_path, config_path, registry, monkeypatch):
        import logger as legacy_logger

        # Уровень — глобальное состояние процесса: откат после теста обязателен.
        monkeypatch.setattr(
            legacy_logger.file_handler, "level", legacy_logger.file_handler.level
        )
        daemon = make_daemon(db_path, config_path, registry)
        daemon.start()
        try:
            status, _ = post_config(daemon, {"behavior": {"log_file_level": "WARNING"}})
            assert status == 200

            assert legacy_logger.file_handler.level == logging.WARNING, (
                "уровень файла должен смениться сразу после POST, без рестарта"
            )
        finally:
            daemon.shutdown()

    def test_failed_apply_keeps_the_level_and_is_logged(
        self, db_path, config_path, registry, monkeypatch
    ):
        """Неудача применения — не ошибка запроса: конфиг принят, уровень
        остался прежним, причина видна в логе демона."""
        import logger as legacy_logger

        before = legacy_logger.file_handler.level

        def boom(name):
            raise ValueError(f"неизвестный уровень файлового лога: {name!r}")

        monkeypatch.setattr(legacy_logger, "apply_file_level", boom)
        daemon = make_daemon(db_path, config_path, registry)
        daemon.start()
        try:
            status, _ = post_config(daemon, {"behavior": {"log_file_level": "ERROR"}})
            assert status == 200, "конфиг применяется; сбой уровня — не 500"

            assert legacy_logger.file_handler.level == before, (
                "старый уровень должен остаться, если новый не применился"
            )
            assert wait_until(
                lambda: any(
                    level == "WARNING" and "log_file_level" in message
                    for level, _, message in log_rows(db_path)
                )
            ), "невалидное значение уровня обязано попасть в лог"
        finally:
            daemon.shutdown()


class TestConcurrentConfigAccess:
    """Конкурентные POST и чтения: ни потерянных полей, ни «рваных» конфигов."""

    def test_parallel_posts_and_reads_never_lose_a_field(
        self, db_path, config_path, registry
    ):
        daemon = make_daemon(db_path, config_path, registry, captcha_check_interval=0.0)
        daemon.start()
        try:
            stop = threading.Event()
            observed: list[int] = []
            failures: list[str] = []

            def reader():
                while not stop.is_set():
                    try:
                        config = daemon._current_config()
                        observed.append(int(config.get("behavior.click_order")))
                    except Exception as exc:  # noqa: BLE001 - собираем факты для assert
                        failures.append(type(exc).__name__)

            def writer(field, values):
                for value in values:
                    status, _ = post_config(daemon, {"behavior": {field: value}})
                    if status != 200:
                        failures.append(f"POST {field}={value}: {status}")

            orders = list(range(1, 31))
            counts = list(range(1, 9))
            readers = [threading.Thread(target=reader) for _ in range(3)]
            writers = [
                threading.Thread(target=writer, args=("click_order", orders)),
                threading.Thread(target=writer, args=("browser_count", counts)),
            ]
            for thread in readers:
                thread.start()
            for thread in writers:
                thread.start()
            for thread in writers:
                thread.join()
            stop.set()
            for thread in readers:
                thread.join()

            assert not failures, failures
            assert observed, "читатели работали и обязаны были что-то прочитать"
            assert set(observed) <= set(orders), (
                "наблюдено значение вне писанных — чтение неполного конфига"
            )

            # После joins писателей замен больше нет: каждое поле обязано
            # дожить до последнего патча своего писателя — lost update означал
            # бы, что конкурентный POST откатил чужое поле к дефолту.
            assert int(daemon.config.get("behavior.click_order")) == orders[-1], (
                "потерянное обновление: click_order откатился"
            )
            assert int(daemon.config.get("behavior.browser_count")) == counts[-1], (
                "потерянное обновление: browser_count откатился"
            )
            assert daemon.config is daemon._current_config(), (
                "после завершения писателей читатели видят один и тот же объект"
            )
        finally:
            daemon.shutdown()
