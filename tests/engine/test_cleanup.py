"""Тесты модуля очистки ``engine.cleanup`` (план §5, фаза 10).

Модуль отвечает за инвентарь целей и безопасное удаление: каталоги профилей
``uc_profiles``/``sb_profiles`` и папки ``proxy_auth_plugin_*`` в системном
tempdir, старые CAPTCHA-скриншоты в ``engine/screenshots``. Проверяется в
первую очередь безопасность: whitelist корней, отказ от симлинков, невыход
за пределы корня, исключение каталогов, которые держат живые процессы, и
грейс по mtime для свежих кандидатов.

Все кандидаты создаются в ``tmp_path``: тест не пишет ни в системный
tempdir, ни в каталог репозитория и не может там ничего оставить. Единственное
обращение к системному tempdir — ``default_targets()`` в тесте метаданных,
где ничего не создаётся.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from engine.cleanup import (
    DEFAULT_CLEANUP_INTERVAL_DAYS,
    DEFAULT_CLEANUP_TIME,
    DEFAULT_GRACE_SECONDS,
    PLUGIN_DIR_RE,
    PROFILE_DIR_RE,
    SCREENSHOT_FILE_RE,
    CleanupService,
    default_targets,
    is_cleanup_due,
    is_within_root,
    last_run_ts,
    seconds_until_cleanup,
)
from engine.control_plane.config import Config
from engine.control_plane.state import StateStore

# Возраст кандидата «давно enough»: больше грейса (10 минут) и больше
# разумного срока хранения скриншотов в тестах.
OLD_SECONDS = 7200
OLD_SCREENSHOT_SECONDS = 40 * 86400


@pytest.fixture
def db_path(tmp_path):
    from engine.db import migrations

    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


@pytest.fixture
def store(db_path):
    return StateStore(db_path)


@pytest.fixture
def roots(tmp_path):
    """Каталоги-цели в tmp: два профиля, tempdir под плагины, скриншоты."""
    temp_root = tmp_path / "temp"
    (temp_root / "uc_profiles").mkdir(parents=True)
    (temp_root / "sb_profiles").mkdir(parents=True)
    screenshots_root = tmp_path / "shots"
    screenshots_root.mkdir()
    return temp_root, screenshots_root


def make_service(store, roots, **kwargs):
    temp_root, screenshots_root = roots
    return CleanupService(
        store, targets=default_targets(temp_root, screenshots_root), **kwargs
    )


def config_with(**behavior):
    return Config.from_dict({"behavior": {"browser_count": 2, **behavior}})


def age_path(path: Path, seconds: float) -> None:
    """Состарить mtime каталога или файла — так задаётся грейс/retention."""
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def make_candidate(roots, category: str, name: str | None = None, age: float | None = None):
    """Создать кандидата нужной категории и состарить его. Возвращает путь.

    Без явного ``age`` берётся возраст «кандидат на удаление»: каталоги старше
    грейса, скриншоты старше ``log_retention_days`` (30 по умолчанию).
    """
    temp_root, screenshots_root = roots
    if category == "uc_profile":
        path = temp_root / "uc_profiles" / (name or "profile_1234")
        path.mkdir()
        (path / "Cookies").write_bytes(b"C" * 100)
        (path / "Default").mkdir()
        (path / "Default" / "History").write_bytes(b"H" * 50)
    elif category == "sb_profile":
        path = temp_root / "sb_profiles" / (name or "profile_4321")
        path.mkdir()
        (path / "Cookies").write_bytes(b"C" * 80)
    elif category == "proxy_plugin":
        path = temp_root / (name or "proxy_auth_plugin_a1b2c3d4")
        path.mkdir()
        (path / "manifest.json").write_bytes(b"{}" * 10)
    elif category == "screenshot":
        path = screenshots_root / (name or "br-1_1711954800.png")
        path.write_bytes(b"PNG" * 40)
        if age is None:
            age = OLD_SCREENSHOT_SECONDS
    else:  # pragma: no cover - защита от опечатки в тесте
        raise AssertionError(f"неизвестная категория {category!r}")
    age_path(path, OLD_SECONDS if age is None else age)
    return path


def cleanup_rows(db_path):
    """Все записи категории cleanup: ``(level, message, fields)`` по порядку."""
    with sqlite3.connect(db_path) as conn:
        raw = conn.execute(
            "SELECT level, message, fields FROM logs "
            "WHERE category = 'cleanup' ORDER BY id"
        ).fetchall()
    return [
        (level, message, json.loads(fields) if fields else {})
        for level, message, fields in raw
    ]


def rows_with_message(db_path, message):
    return [row for row in cleanup_rows(db_path) if row[1] == message]


class TestTargets:
    """Инвентарь целей: что именно модуль считает мусором."""

    def test_default_targets_cover_the_phase10_inventory(self):
        targets = {target.kind: target for target in default_targets()}

        assert set(targets) == {"uc_profile", "sb_profile", "proxy_plugin", "screenshot"}
        assert targets["uc_profile"].root.name == "uc_profiles"
        assert targets["sb_profile"].root.name == "sb_profiles"
        assert targets["proxy_plugin"].root.name != "uc_profiles"
        assert targets["screenshot"].root.parts[-2:] == ("engine", "screenshots")

    def test_profiles_and_plugins_are_directories_and_screenshots_are_files(self):
        targets = {target.kind: target for target in default_targets()}

        assert targets["uc_profile"].is_dir is True
        assert targets["sb_profile"].is_dir is True
        assert targets["proxy_plugin"].is_dir is True
        assert targets["screenshot"].is_dir is False

    @pytest.mark.parametrize(
        "name,matches",
        [
            ("profile_1234", True),
            ("profile_9999", True),
            ("profile_123", False),
            ("profile_12345", False),
            ("profile_abcd", False),
            ("uc_profiles", False),
        ],
    )
    def test_profile_name_pattern_matches_what_webdriver_creates(self, name, matches):
        assert (PROFILE_DIR_RE.fullmatch(name) is not None) is matches

    @pytest.mark.parametrize(
        "name,matches",
        [
            ("proxy_auth_plugin_a1b2c3d4", True),
            ("proxy_auth_plugin_1__th_7x", True),
            ("proxy_auth_plugin", False),
            ("proxy_auth_plugin_", False),
            ("other_plugin_a1b2c3d4", False),
        ],
    )
    def test_plugin_pattern_matches_mkdtemp_names_only(self, name, matches):
        assert (PLUGIN_DIR_RE.fullmatch(name) is not None) is matches

    @pytest.mark.parametrize(
        "name,matches",
        [
            ("br-1_1711954800.png", True),
            ("unknown_1711954800.png", True),
            ("br_1_with_underscore_1711954800.png", True),
            ("br-1_123.png", False),
            ("page.png", False),
            ("br-1_1711954800.png.bak", False),
        ],
    )
    def test_screenshot_pattern_matches_the_written_names_only(self, name, matches):
        assert (SCREENSHOT_FILE_RE.fullmatch(name) is not None) is matches


class TestRemovalPerCategory:
    """Отчёт и удаление по каждой категории целей."""

    @pytest.mark.parametrize("category", ["uc_profile", "sb_profile", "proxy_plugin", "screenshot"])
    def test_orphan_of_each_category_is_removed(self, category, store, roots, db_path):
        path = make_candidate(roots, category)
        service = make_service(store, roots)

        report = service.run(config_with())

        assert report["removed"] == 1, f"категория {category} не удалена"
        assert report["removed_bytes"] > 0
        assert report["errors"] == 0
        assert report["skipped_active"] == 0
        assert not path.exists()

        removed = rows_with_message(db_path, "cleanup item removed")
        assert len(removed) == 1
        fields = removed[0][2]
        assert fields["path"] == str(path)
        assert fields["result"] == "removed"
        assert fields["bytes"] == report["removed_bytes"]
        assert fields["target"] == category

    @pytest.mark.parametrize("category", ["uc_profile", "sb_profile", "proxy_plugin"])
    def test_root_of_a_directory_target_survives(self, category, store, roots, db_path):
        path = make_candidate(roots, category)
        root = path.parent

        make_service(store, roots).run(config_with())

        assert root.is_dir(), "корень целевого каталога нельзя удалять"

    def test_names_outside_the_pattern_are_left_alone(self, store, roots, db_path):
        temp_root, screenshots_root = roots
        foreign_dir = temp_root / "uc_profiles" / "profile_abc"
        foreign_dir.mkdir()
        notes = temp_root / "uc_profiles" / "notes.txt"
        notes.write_text("not a profile", encoding="utf-8")
        legacy_plugin = temp_root / "proxy_auth_plugin"
        legacy_plugin.mkdir()
        stray_shot = screenshots_root / "page.png"
        stray_shot.write_text("not a screenshot", encoding="utf-8")
        make_candidate(roots, "uc_profile")

        report = make_service(store, roots).run(config_with())

        assert report["removed"] == 1
        assert foreign_dir.is_dir()
        assert notes.is_file()
        assert legacy_plugin.is_dir()
        assert stray_shot.is_file()

    def test_missing_target_root_is_not_an_error(self, store, roots, db_path):
        """Установка без скриншотов: каталога нет — это не ошибка прогона."""
        temp_root, screenshots_root = roots
        screenshots_root.rmdir()
        make_candidate(roots, "uc_profile")

        report = make_service(store, roots).run(config_with())

        assert report["removed"] == 1
        assert report["errors"] == 0


class TestSafetyContainment:
    """Безопасность: ничего за пределами whitelisted-корней и без симлинков."""

    def test_symlink_replacing_a_profile_is_not_followed(self, store, roots, db_path, tmp_path):
        victim = tmp_path / "victim"
        victim.mkdir()
        (victim / "important.txt").write_text("do not delete", encoding="utf-8")
        link = roots[0] / "uc_profiles" / "profile_7777"
        link.symlink_to(victim, target_is_directory=True)

        report = make_service(store, roots).run(config_with())

        assert report["removed"] == 0
        assert report["errors"] == 0
        assert link.is_symlink(), "симлинк нельзя удалять вместо цели"
        assert (victim / "important.txt").is_file(), "цель симлинка должна выжить"

        skipped = rows_with_message(db_path, "cleanup item skipped")
        assert skipped, "симлинк обязан попасть в лог"
        assert skipped[0][2]["result"] == "skipped"
        assert skipped[0][2]["reason"] == "symlink"
        assert skipped[0][2]["path"] == str(link)

    def test_symlink_inside_the_tree_does_not_pull_files_out(
        self, store, roots, db_path, tmp_path
    ):
        """rmtree не должен следовать за симлинком внутри удаляемого каталога."""
        victim = tmp_path / "outside.txt"
        victim.write_text("survives", encoding="utf-8")
        orphan = roots[0] / "uc_profiles" / "profile_5555"
        orphan.mkdir()
        (orphan / "Cookies").write_bytes(b"C" * 10)
        (orphan / "escape").symlink_to(victim)
        age_path(orphan, OLD_SECONDS)

        report = make_service(store, roots).run(config_with())

        assert report["removed"] == 1
        assert not orphan.exists()
        assert victim.read_text(encoding="utf-8") == "survives"

    def test_candidate_matching_the_pattern_outside_the_roots_is_untouched(
        self, store, roots, db_path, tmp_path
    ):
        """Whitelist: одноимённый каталог вне корней не перечисляется вовсе."""
        outside = tmp_path / "elsewhere" / "uc_profiles"
        outside.mkdir(parents=True)
        foreign = outside / "profile_1234"
        foreign.mkdir()
        make_candidate(roots, "uc_profile")

        report = make_service(store, roots).run(config_with())

        assert report["removed"] == 1
        assert foreign.is_dir()
        assert not any(str(foreign) in row[2].get("path", "") for row in cleanup_rows(db_path))

    def test_is_within_root_accepts_only_paths_under_the_root(self, roots):
        root = roots[0] / "uc_profiles"

        assert is_within_root(root, root / "profile_1234") is True
        assert is_within_root(root, root) is True
        assert is_within_root(root, root.parent / "sb_profiles" / "profile_1") is False
        assert is_within_root(root, Path("/etc/passwd")) is False

    def test_is_within_root_rejects_a_symlink_pointing_out(self, roots, tmp_path):
        root = roots[0] / "uc_profiles"
        outside = tmp_path / "outside"
        outside.mkdir()
        link = root / "profile_1234"
        link.symlink_to(outside, target_is_directory=True)

        assert is_within_root(root, link) is False


class TestActiveExclusion:
    """Активные каталоги не трогаются; сироты после Kill — убираются."""

    def test_dir_held_by_an_open_file_is_kept(self, store, roots, db_path):
        path = make_candidate(roots, "uc_profile")
        held = path / "Default" / "History"
        handle = held.open("r+b")
        try:
            report = make_service(store, roots).run(config_with())
        finally:
            handle.close()

        assert report["removed"] == 0
        assert report["skipped_active"] == 1
        assert path.is_dir()

        skipped = rows_with_message(db_path, "cleanup item skipped")
        assert skipped[0][2]["reason"] == "active_process"
        assert skipped[0][2]["pid"] == os.getpid()

    def test_dir_named_in_a_live_process_cmdline_is_kept(self, store, roots, db_path):
        """Связь «каталог ↔ воркер»: Chrome получает --user-data-dir в argv."""
        import psutil

        path = make_candidate(roots, "sb_profile")
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", f"--user-data-dir={path}"]
        )
        try:
            watcher = psutil.Process(process.pid)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    if str(path) in " ".join(watcher.cmdline()):
                        break
                except psutil.Error:
                    pass
                time.sleep(0.02)
            else:  # pragma: no cover - процесс обязан подняться
                pytest.fail("подпроцесс не поднялся")

            report = make_service(store, roots).run(config_with())
        finally:
            process.terminate()
            process.wait(timeout=10)

        assert report["removed"] == 0
        assert report["skipped_active"] == 1
        assert path.is_dir()

        skipped = rows_with_message(db_path, "cleanup item skipped")
        assert skipped[0][2]["reason"] == "active_process"
        assert skipped[0][2]["pid"] == process.pid

    def test_dir_younger_than_the_grace_is_kept(self, store, roots, db_path):
        path = make_candidate(roots, "uc_profile", age=30)

        report = make_service(store, roots).run(config_with())

        assert report["removed"] == 0
        assert report["skipped_active"] == 1
        assert path.is_dir()

        skipped = rows_with_message(db_path, "cleanup item skipped")
        assert skipped[0][2]["reason"] == "recent_mtime"
        assert skipped[0][2]["result"] == "skipped"

    def test_orphan_older_than_the_grace_is_removed(self, store, roots, db_path):
        path = make_candidate(roots, "uc_profile", age=OLD_SECONDS)
        assert OLD_SECONDS > DEFAULT_GRACE_SECONDS

        report = make_service(store, roots).run(config_with())

        assert report["removed"] == 1
        assert not path.exists()

    def test_fresh_dir_becomes_removable_after_the_grace(self, store, roots, db_path):
        """Грейс считается по mtime: свежий каталог ждёт, старый — сирота."""
        path = make_candidate(roots, "uc_profile", age=30)
        service = make_service(store, roots, grace_seconds=0.0)

        report = service.run(config_with())

        assert report["removed"] == 1
        assert not path.exists()

    def test_both_an_active_and_an_orphan_are_counted_separately(
        self, store, roots, db_path
    ):
        active = make_candidate(roots, "uc_profile", name="profile_1111")
        orphan = make_candidate(roots, "uc_profile", name="profile_2222")
        handle = (active / "Cookies").open("rb")
        try:
            report = make_service(store, roots).run(config_with())
        finally:
            handle.close()

        assert report["removed"] == 1
        assert report["skipped_active"] == 1
        assert active.is_dir()
        assert not orphan.exists()


class TestScreenshotRetention:
    """Срок жизни скриншота привязан к ``log_retention_days``."""

    def test_screenshot_within_retention_is_kept(self, store, roots, db_path):
        path = make_candidate(roots, "screenshot", age=3600)

        report = make_service(store, roots).run(config_with(log_retention_days=30))

        assert report["removed"] == 0
        assert path.is_file()

        skipped = rows_with_message(db_path, "cleanup item skipped")
        assert skipped[0][2]["reason"] == "within_retention"

    @pytest.mark.parametrize(
        "retention_days,age_seconds,expect_removed",
        [
            (1, 2 * 86400, True),
            (30, 2 * 86400, False),
            (30, OLD_SCREENSHOT_SECONDS, True),
        ],
    )
    def test_screenshot_age_follows_log_retention_days(
        self, retention_days, age_seconds, expect_removed, store, roots, db_path
    ):
        path = make_candidate(roots, "screenshot", age=age_seconds)

        report = make_service(store, roots).run(
            config_with(log_retention_days=retention_days)
        )

        assert (report["removed"] == 1) is expect_removed
        assert path.exists() is not expect_removed

    def test_fresh_screenshot_is_not_counted_as_active(self, store, roots, db_path):
        """Retention главнее грейса: свежий файл пропускается по сроку, не как активный."""
        path = make_candidate(roots, "screenshot", age=60)

        report = make_service(store, roots).run(config_with(log_retention_days=30))

        assert report["skipped_active"] == 0
        assert report["removed"] == 0
        assert path.is_file()


class TestReportAndLogging:
    """Формат отчёта и лог каждой операции."""

    def test_report_has_the_contract_shape(self, store, roots):
        report = make_service(store, roots).run(config_with())

        assert set(report) == {
            "removed",
            "removed_bytes",
            "skipped_active",
            "errors",
            "duration_ms",
            "dry_run",
        }
        assert isinstance(report["removed"], int)
        assert isinstance(report["removed_bytes"], int)
        assert isinstance(report["skipped_active"], int)
        assert isinstance(report["errors"], int)
        assert isinstance(report["duration_ms"], int)
        assert report["duration_ms"] >= 0
        assert report["dry_run"] is False

    def test_every_logged_operation_carries_path_bytes_and_result(
        self, store, roots, db_path
    ):
        removed = make_candidate(roots, "uc_profile")
        kept = make_candidate(roots, "sb_profile", age=30)
        old_shot = make_candidate(roots, "screenshot")

        make_service(store, roots).run(config_with())

        operations = rows_with_message(db_path, "cleanup item removed") + rows_with_message(
            db_path, "cleanup item skipped"
        )
        assert len(operations) == 3
        for _, _, fields in operations:
            assert set(fields) >= {"path", "bytes", "result"}
            assert fields["result"] in {"removed", "skipped"}
        paths = {fields["path"] for _, _, fields in operations}
        assert paths == {str(removed), str(kept), str(old_shot)}

    def test_removal_failure_is_counted_and_does_not_abort_the_run(
        self, store, roots, db_path, monkeypatch
    ):
        import engine.cleanup as cleanup_module

        first = make_candidate(roots, "uc_profile", name="profile_1111")
        second = make_candidate(roots, "uc_profile", name="profile_2222")
        real_remove = cleanup_module._remove_path
        attempted = []

        def failing_remove(path, root):
            attempted.append(path)
            if path == first:
                raise PermissionError("нет прав")
            return real_remove(path, root)

        monkeypatch.setattr(cleanup_module, "_remove_path", failing_remove)

        report = make_service(store, roots).run(config_with())

        assert report["errors"] == 1
        assert report["removed"] == 1, "сбой одной операции не должен ронять прогон"
        assert first.is_dir()
        assert not second.exists()

        failed = rows_with_message(db_path, "cleanup item failed")
        assert failed[0][0] == "ERROR"
        assert failed[0][2]["result"] == "error"
        assert failed[0][2]["reason"] == "PermissionError"
        assert failed[0][2]["path"] == str(first)

    def test_run_finishes_with_a_summary_carrying_the_counters(
        self, store, roots, db_path
    ):
        make_candidate(roots, "uc_profile")

        report = make_service(store, roots).run(config_with())

        summaries = rows_with_message(db_path, "cleanup finished")
        assert len(summaries) == 1
        assert summaries[0][2]["removed"] == report["removed"]
        assert summaries[0][2]["errors"] == report["errors"]
        assert summaries[0][2]["dry_run"] is False

    def test_empty_inventory_is_a_quiet_successful_run(self, store, roots, db_path):
        report = make_service(store, roots).run(config_with())

        assert report == {
            "removed": 0,
            "removed_bytes": 0,
            "skipped_active": 0,
            "errors": 0,
            "duration_ms": report["duration_ms"],
            "dry_run": False,
        }
        assert rows_with_message(db_path, "cleanup item failed") == []


class TestDryRun:
    """Пробный прогон: перечисляет, но ничего не удаляет."""

    def test_dry_run_removes_nothing_but_counts_the_candidates(
        self, store, roots, db_path
    ):
        path = make_candidate(roots, "uc_profile")
        report = make_service(store, roots).run(config_with(), dry_run=True)

        assert report["dry_run"] is True
        assert report["removed"] == 1
        assert report["removed_bytes"] > 0
        assert path.is_dir(), "dry_run не имеет права удалять"

    def test_dry_run_is_logged_as_skipped_with_the_dry_run_reason(
        self, store, roots, db_path
    ):
        make_candidate(roots, "uc_profile")

        make_service(store, roots).run(config_with(), dry_run=True)

        skipped = rows_with_message(db_path, "cleanup item skipped")
        assert skipped[0][2]["reason"] == "dry_run"
        assert rows_with_message(db_path, "cleanup item removed") == []

    def test_dry_run_does_not_touch_the_last_run_state(self, store, roots, db_path):
        make_candidate(roots, "uc_profile")

        make_service(store, roots).run(config_with(), dry_run=True)

        assert last_run_ts(store) is None
        assert store.get_flag("CLEANUP_LAST_REPORT") is None


class TestTimeBudget:
    """Прогон ограничен по времени: защита от долгого синхронного запроса."""

    def test_exhausted_budget_stops_before_touching_candidates(
        self, store, roots, db_path
    ):
        path = make_candidate(roots, "uc_profile")

        report = make_service(store, roots, time_budget_seconds=0.0).run(config_with())

        assert report["removed"] == 0
        assert path.is_dir(), "после исчерпания бюджета ничего не удаляется"
        warnings = rows_with_message(db_path, "cleanup time budget exhausted")
        assert warnings, "исчерпание бюджета обязано быть видно в логе"
        assert warnings[0][2]["remaining"] >= 1

    def test_work_within_the_budget_runs_normally(self, store, roots, db_path):
        make_candidate(roots, "uc_profile")

        report = make_service(store, roots, time_budget_seconds=30.0).run(config_with())

        assert report["removed"] == 1
        assert rows_with_message(db_path, "cleanup time budget exhausted") == []


class TestStatus:
    """``GET /control/cleanup/status``: последний отчёт и ближайший запуск."""

    def test_last_is_null_before_the_first_run_and_next_run_falls_back(self, store, roots):
        status = make_service(store, roots).status(config_with())

        assert status["last"] is None
        assert status["next_run"] is not None, "до первого прогона next_run — по расписанию"

    def test_run_stores_the_last_report(self, store, roots, db_path):
        make_candidate(roots, "uc_profile")
        service = make_service(store, roots)

        expected = service.run(config_with())
        status = service.status(config_with())

        assert status["last"]["ts"] == pytest.approx(last_run_ts(store))
        assert status["last"]["report"] == expected

    def test_next_run_is_read_from_kv(self, store, roots):
        service = make_service(store, roots)
        store.set_flag("CLEANUP_NEXT_RUN", "1711954800.5")

        assert service.status(config_with())["next_run"] == 1711954800.5

    def test_empty_next_run_in_kv_means_the_job_is_off(self, store, roots):
        service = make_service(store, roots)
        store.set_flag("CLEANUP_NEXT_RUN", "")

        assert service.status(config_with())["next_run"] is None

    def test_missing_next_run_falls_back_to_the_schedule(self, store, roots):
        """До первой записи демона status показывает ближайшее ЧЧ:ММ из конфига."""
        service = make_service(store, roots)
        now = time.time()
        config = config_with(cleanup_time="04:00")

        next_run = service.status(config)["next_run"]

        assert next_run is not None
        assert next_run > now
        expected = now + seconds_until_cleanup(now, "04:00", 1, None)
        assert next_run == pytest.approx(expected, abs=1.0)

    def test_unreadable_report_is_null_and_logged(self, store, roots, db_path):
        service = make_service(store, roots)
        store.set_flag("CLEANUP_LAST_TS", "1711954800")
        store.set_flag("CLEANUP_LAST_REPORT", "{broken json")

        status = service.status(config_with())

        assert status["last"] is None
        warnings = rows_with_message(db_path, "cleanup status unreadable")
        assert warnings, "битый kv обязан оставить след в логе"

    def test_unreadable_timestamp_falls_back_to_no_last_run(self, store, roots):
        service = make_service(store, roots)
        store.set_flag("CLEANUP_LAST_TS", "not-a-number")

        assert service.status(config_with())["last"] is None


class TestSecondsUntilCleanup:
    """Расписание: ближайшие разрешённые локальные ЧЧ:ММ, цель всегда в будущем."""

    @staticmethod
    def at(year, month, day, hour=0, minute=0, second=0) -> float:
        return time.mktime((year, month, day, hour, minute, second, 0, 0, -1))

    @staticmethod
    def target(now, cleanup_time="04:00", interval_days=1, last_ts=None):
        return now + seconds_until_cleanup(now, cleanup_time, interval_days, last_ts)

    def test_before_the_time_it_points_to_todays_target(self):
        now = self.at(2024, 4, 1, 3, 59, 59)

        assert time.localtime(self.target(now))[:6] == (2024, 4, 1, 4, 0, 0)

    def test_at_the_time_the_target_jumps_to_the_next_day(self):
        """Переход: ровно в 04:00 запуск уже позади, цель — завтра."""
        now = self.at(2024, 4, 1, 4, 0, 0)

        target = self.target(now)

        assert time.localtime(target)[:6] == (2024, 4, 2, 4, 0, 0)
        assert seconds_until_cleanup(now, "04:00") >= 23 * 3600

    def test_right_after_midnight_the_target_is_todays_later_time(self):
        now = self.at(2024, 4, 2, 0, 0, 0)

        assert time.localtime(self.target(now))[:6] == (2024, 4, 2, 4, 0, 0)

    def test_the_time_is_configurable_not_only_2359(self):
        now = self.at(2024, 4, 1, 12, 0, 0)

        assert time.localtime(self.target(now, "23:30"))[:6] == (2024, 4, 1, 23, 30, 0)

    def test_a_recent_run_delays_the_next_target_by_the_interval(self):
        now = self.at(2024, 4, 1, 12, 0, 0)
        last_ts = self.at(2024, 4, 1, 4, 0, 0)

        assert time.localtime(self.target(now, last_ts=last_ts))[:6] == (2024, 4, 2, 4, 0, 0)

    def test_a_run_beyond_the_interval_does_not_delay_anything(self):
        now = self.at(2024, 4, 1, 12, 0, 0)
        last_ts = self.at(2024, 3, 30, 4, 0, 0)

        assert time.localtime(self.target(now, last_ts=last_ts))[:6] == (2024, 4, 2, 4, 0, 0)

    def test_interval_of_several_days_waits_for_the_full_interval(self):
        now = self.at(2024, 4, 1, 4, 30, 0)
        last_ts = self.at(2024, 4, 1, 4, 0, 0)

        target = self.target(now, interval_days=3, last_ts=last_ts)

        assert time.localtime(target)[:6] == (2024, 4, 4, 4, 0, 0)

    @pytest.mark.parametrize(
        "now",
        [
            time.mktime((2024, 4, 1, 0, 0, 0, 0, 0, -1)),
            time.mktime((2024, 4, 1, 3, 59, 59, 0, 0, -1)),
            time.mktime((2024, 4, 1, 4, 0, 0, 0, 0, -1)),
            time.mktime((2024, 4, 1, 23, 59, 59, 0, 0, -1)),
            time.mktime((2024, 4, 2, 12, 34, 56, 0, 0, -1)),
        ],
    )
    def test_wait_is_always_positive(self, now):
        assert seconds_until_cleanup(now, "04:00") > 0

    @pytest.mark.parametrize("bad", ["4:00", "24:00", "04:60", "", "04-00", "noon"])
    def test_malformed_time_raises(self, bad):
        with pytest.raises(ValueError):
            seconds_until_cleanup(time.time(), bad)

    @pytest.mark.parametrize("bad", [0, -1, 31])
    def test_interval_outside_the_schema_range_raises(self, bad):
        with pytest.raises(ValueError):
            seconds_until_cleanup(time.time(), "04:00", bad)

    def test_default_constants_match_the_contract(self):
        assert DEFAULT_CLEANUP_TIME == "04:00"
        assert DEFAULT_CLEANUP_INTERVAL_DAYS == 1


class TestIsCleanupDue:
    """Проверка интервала перед плановым запуском."""

    def test_first_ever_run_is_due(self):
        assert is_cleanup_due(time.time(), None, 1) is True

    def test_a_run_moments_ago_is_not_due(self):
        now = time.time()
        assert is_cleanup_due(now, now - 100, 1) is False

    def test_a_run_beyond_the_interval_is_due(self):
        now = time.time()
        assert is_cleanup_due(now, now - 2 * 86400, 1) is True

    def test_wake_up_latency_inside_the_slack_is_still_due(self):
        """Копеечная задержка пробуждения не должна переносить запуск на сутки."""
        now = time.time()
        assert is_cleanup_due(now, now - (86400 - 30), 1) is True

    def test_a_run_half_a_day_ago_with_two_day_interval_is_not_due(self):
        now = time.time()
        assert is_cleanup_due(now, now - 12 * 3600, 2) is False


class TestLastRunTs:
    """``last_run_ts``: чтение метки последнего прогона из kv."""

    def test_missing_key_means_no_run_yet(self, store):
        assert last_run_ts(store) is None

    def test_key_is_parsed_as_epoch(self, store):
        store.set_flag("CLEANUP_LAST_TS", "1711954800.25")

        assert last_run_ts(store) == pytest.approx(1711954800.25)

    def test_unparsable_key_means_no_run_yet(self, store):
        store.set_flag("CLEANUP_LAST_TS", "yesterday")

        assert last_run_ts(store) is None
