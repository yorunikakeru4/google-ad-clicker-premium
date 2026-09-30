"""Часовые метрики дашборда (план §2 таблица ``metrics_hourly``, §5 фаза 12).

Агрегат один: целый час UTC. Бакет фиксируется как ``floor(ts / 3600) * 3600``
и не зависит от локального пояса — аптайм-доля «был жив хотя бы один воркер»
это отношение секунд, и от TZ оно не меняется, в отличие от дневного ``day``
(там локальная дата нужна для экспорта и retention).

Два вида обновлений одной строки бакета:

* **исторические колонки** (``successes``/``failures``/``requests``/
  ``captchas``) — пересчёт из SQL, идемпотентен: повторный пересчёт
  заменяет значения;
* **``uptime_seconds``** — только аддитивные импульсы
  (:func:`record_uptime_impulse`), монотонный счётчик бакета: исторический
  пересчёт его не трогает вообще.

Старый механизм «интервалы жизни ``[heartbeat, heartbeat + 15с)``» удалён:
в ``workers`` хранится только последняя отметка heartbeat, поэтому интервалы
прошлых часов реконструировать не из чего, а текущий бакет давал бы ≤15 с —
для NFR «uptime ≥99% за окно» метрика была бесполезна. Живость в момент тика
берёт реестр супервизора (:meth:`Supervisor.has_live_workers`), а не SQL.

Проверяется:

* границы бакета (полуинтервал ``[bucket, bucket + 3600)``) и его UTC-смысл;
* классификация статусов ``runs`` — зеркало
  ``ui/src-tauri/src/metrics.rs::classify_run_status``;
* импульс uptime: создание строки с нулевой историей, сложение вместо
  перезаписи, сохранность исторических колонок, потолок в длину бакета;
* регрессия: исторический пересчёт (``write_bucket``/``refresh_range``)
  не затирает накопленный uptime;
* ``uptime_ratio``: пусто → ``None``, доля ровно 99%, пропуски окна входят в
  знаменатель как простой.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from engine.db import migrations
from engine.metrics import (
    BUCKET_SECONDS,
    BucketMetrics,
    aggregate_bucket,
    bucket_of,
    classify_run_status,
    record_uptime_impulse,
    refresh_range,
    uptime_ratio,
    write_bucket,
)

# Час, нарисованный вручную: тесты не должны зависеть от wall-clock.
B = 1_699_999_200  # 2023-11-14 16:00:00 UTC, кратно 3600


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "adclicker.db"
    migrations.migrate(path)
    return path


def insert_run(db_path, status, *, created_at, ended_at=None):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO runs (status, created_at, ended_at) VALUES (?, ?, ?)",
            (status, created_at, ended_at),
        )
        conn.commit()


def insert_request(db_path, ts):
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO network_requests (ts) VALUES (?)", (ts,))
        conn.commit()


def insert_captcha(db_path, ts):
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO captcha_events (ts) VALUES (?)", (ts,))
        conn.commit()


def rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT bucket, successes, failures, requests, captchas, uptime_seconds "
            "FROM metrics_hourly ORDER BY bucket"
        ).fetchall()


class TestBucketOf:
    def test_top_of_the_hour_belongs_to_the_next_bucket(self):
        assert bucket_of(BUCKET_SECONDS - 0.001) == 0
        assert bucket_of(BUCKET_SECONDS) == BUCKET_SECONDS
        assert bucket_of(BUCKET_SECONDS + 0.001) == BUCKET_SECONDS

    def test_bucket_is_the_utc_hour_start_of_the_timestamp(self):
        ts = B + 42 * 60
        expected = (
            datetime.fromtimestamp(ts, tz=timezone.utc)
            .replace(minute=0, second=0, microsecond=0)
            .timestamp()
        )
        assert bucket_of(ts) == expected

    def test_local_timezone_does_not_move_the_bucket(self):
        """Докстринг модуля: аптайм-доля от TZ не зависит, в отличие от day."""
        ts = B + 42 * 60
        expected = bucket_of(ts)

        previous = os.environ.get("TZ")
        os.environ["TZ"] = "Pacific/Kiritimati"  # UTC+14, пояс смещает час
        time.tzset()
        try:
            assert bucket_of(ts) == expected
        finally:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()


class TestClassifyRunStatus:
    @pytest.mark.parametrize("status", ["ok", "OK", " ok ", "completed", "Completed", "COMPLETED"])
    def test_ok_and_completed_are_successes(self, status):
        assert classify_run_status(status) == "success"

    @pytest.mark.parametrize("status", ["failed", "FAILED", " failed ", "crashed", "CRASHED"])
    def test_failed_and_crashed_are_failures(self, status):
        assert classify_run_status(status) == "failure"

    @pytest.mark.parametrize(
        "status", ["running", "stopped", "", "   ", "ok-ish", "paused", "Completed yesterday"]
    )
    def test_everything_else_is_not_counted(self, status):
        assert classify_run_status(status) == "other"

    def test_missing_status_is_not_counted(self):
        assert classify_run_status(None) == "other"


class TestAggregateBucket:
    def test_counts_only_classified_statuses_inside_the_bucket(self, db_path):
        insert_run(db_path, "ok", created_at=B + 1, ended_at=B + 10)
        insert_run(db_path, "completed", created_at=B + 1, ended_at=B + 20)
        insert_run(db_path, "failed", created_at=B + 1, ended_at=B + 30)
        insert_run(db_path, "crashed", created_at=B + 1, ended_at=B + 40)
        # running/stopped и прочее — не в счёт, как в Rust.
        insert_run(db_path, "running", created_at=B + 50, ended_at=None)
        insert_run(db_path, "stopped", created_at=B + 1, ended_at=B + 60)

        metrics = aggregate_bucket(db_path, B)

        assert metrics.successes == 2
        assert metrics.failures == 2

    def test_upper_edge_of_the_bucket_belongs_to_the_next_one(self, db_path):
        insert_run(db_path, "ok", created_at=B, ended_at=B + BUCKET_SECONDS - 0.5)
        insert_run(db_path, "ok", created_at=B, ended_at=B + BUCKET_SECONDS)

        assert aggregate_bucket(db_path, B).successes == 1
        assert aggregate_bucket(db_path, B + BUCKET_SECONDS).successes == 1

    def test_lower_edge_belongs_to_the_bucket(self, db_path):
        insert_run(db_path, "failed", created_at=B - 100, ended_at=B)

        assert aggregate_bucket(db_path, B).failures == 1
        assert aggregate_bucket(db_path, B - BUCKET_SECONDS).failures == 0

    def test_run_without_end_falls_back_to_created_at(self, db_path):
        insert_run(db_path, "ok", created_at=B + 5, ended_at=None)
        insert_run(db_path, "failed", created_at=B - 5, ended_at=None)

        metrics = aggregate_bucket(db_path, B)

        assert metrics.successes == 1
        assert metrics.failures == 0

    def test_end_wins_over_creation_time(self, db_path):
        insert_run(db_path, "ok", created_at=B - 5000, ended_at=B + 7)

        assert aggregate_bucket(db_path, B).successes == 1
        assert aggregate_bucket(db_path, B - BUCKET_SECONDS).successes == 0

    def test_requests_and_captchas_follow_their_own_ts_edges(self, db_path):
        insert_request(db_path, B - 1)
        insert_request(db_path, B)
        insert_request(db_path, B + BUCKET_SECONDS - 0.5)
        insert_request(db_path, B + BUCKET_SECONDS)
        insert_captcha(db_path, B + 3)
        insert_captcha(db_path, B + BUCKET_SECONDS)

        metrics = aggregate_bucket(db_path, B)

        assert metrics.requests == 2
        assert metrics.captchas == 1

    def test_empty_bucket_is_all_zeros(self, db_path):
        assert aggregate_bucket(db_path, B) == BucketMetrics(B, 0, 0, 0, 0)


class TestRecordUptimeImpulse:
    """Импульс — единственный путь записи ``uptime_seconds``."""

    def test_creates_the_bucket_row_with_zeroed_history(self, db_path):
        record_uptime_impulse(db_path, B, 15)

        assert rows(db_path) == [(B, 0, 0, 0, 0, 15)]

    def test_impulses_add_up_instead_of_overwriting(self, db_path):
        record_uptime_impulse(db_path, B, 15)
        record_uptime_impulse(db_path, B, 15)

        assert rows(db_path)[0][5] == 30

    def test_impulse_keeps_the_history_columns_intact(self, db_path):
        write_bucket(db_path, BucketMetrics(B, 2, 1, 30, 4))
        record_uptime_impulse(db_path, B, 15)
        record_uptime_impulse(db_path, B, 15)

        assert rows(db_path) == [(B, 2, 1, 30, 4, 30)]

    def test_zero_and_negative_impulses_write_nothing(self, db_path):
        record_uptime_impulse(db_path, B, 0)
        record_uptime_impulse(db_path, B, -15)

        assert rows(db_path) == []

    def test_uptime_never_exceeds_the_bucket_length(self, db_path):
        record_uptime_impulse(db_path, B, BUCKET_SECONDS + 400)
        record_uptime_impulse(db_path, B, BUCKET_SECONDS + 400)

        assert rows(db_path)[0][5] == BUCKET_SECONDS, "счётчик обязан упираться в длину часа"

    def test_buckets_do_not_share_the_counter(self, db_path):
        record_uptime_impulse(db_path, B, 15)
        record_uptime_impulse(db_path, B + BUCKET_SECONDS, 15)

        assert [row[5] for row in rows(db_path)] == [15, 15]


class TestHistoryRefreshKeepsUptime:
    """Регрессия: исторический пересчёт не имеет права трогать uptime."""

    def test_refresh_range_preserves_accumulated_uptime(self, db_path):
        record_uptime_impulse(db_path, B, 300)
        insert_run(db_path, "ok", created_at=B + 1, ended_at=B + 2)

        refresh_range(db_path, B, now=B + 1800)

        stored = {row[0]: row for row in rows(db_path)}[B]
        assert stored[1] == 1, "исторические колонки должны пересчитаться"
        assert stored[5] == 300, "…а uptime обязан пережить пересчёт"

    def test_write_bucket_preserves_accumulated_uptime(self, db_path):
        record_uptime_impulse(db_path, B, 120)

        write_bucket(db_path, BucketMetrics(B, 5, 0, 0, 0))

        assert rows(db_path) == [(B, 5, 0, 0, 0, 120)]


class TestWriteBucket:
    def test_writes_the_history_columns_only(self, db_path):
        write_bucket(db_path, BucketMetrics(B, 1, 2, 3, 4))

        assert rows(db_path) == [(B, 1, 2, 3, 4, 0)]

    def test_second_write_replaces_the_row_instead_of_duplicating(self, db_path):
        write_bucket(db_path, BucketMetrics(B, 7, 7, 7, 7))
        write_bucket(db_path, BucketMetrics(B, 1, 0, 0, 0))

        assert rows(db_path) == [(B, 1, 0, 0, 0, 0)]


class TestRefreshRange:
    def test_covers_the_window_exactly_without_gaps_or_duplicates(self, db_path):
        now = B + 1800  # середина часа

        written = refresh_range(db_path, now - 24 * 3600, now=now)

        assert written == [B - 24 * 3600 + i * BUCKET_SECONDS for i in range(25)]
        assert [row[0] for row in rows(db_path)] == written

    def test_recompute_overwrites_stale_values(self, db_path):
        write_bucket(db_path, BucketMetrics(B, 777, 777, 777, 777))
        insert_run(db_path, "ok", created_at=B + 1, ended_at=B + 2)

        refresh_range(db_path, B, now=B + 1800)

        stored = {row[0]: row for row in rows(db_path)}[B]
        assert stored[1] == 1, "старое значение должно быть пересчитано, а не слеплено заново"
        assert len(rows(db_path)) == 1

    def test_written_rows_equal_the_aggregate(self, db_path):
        insert_request(db_path, B + 1)
        insert_captcha(db_path, B + 2)
        insert_run(db_path, "crashed", created_at=B, ended_at=B + 3)

        refresh_range(db_path, B - 3600, now=B + 1800)

        expected = aggregate_bucket(db_path, B)
        stored = {row[0]: row for row in rows(db_path)}[B]
        assert stored == (
            expected.bucket,
            expected.successes,
            expected.failures,
            expected.requests,
            expected.captchas,
            0,  # uptime историческим пересчётом не записывается
        )

    def test_window_further_in_the_future_is_empty(self, db_path):
        assert refresh_range(db_path, B + 7200, now=B) == []
        assert rows(db_path) == []


class TestUptimeRatio:
    def test_no_rows_at_all_is_none_not_zero(self, db_path):
        assert uptime_ratio(db_path, B, now=B + 1800) is None

    def test_full_hour_of_uptime_is_one(self, db_path):
        record_uptime_impulse(db_path, B, BUCKET_SECONDS)

        assert uptime_ratio(db_path, B, now=B + 1800) == 1.0

    def test_exactly_ninety_nine_percent(self, db_path):
        record_uptime_impulse(db_path, B, int(BUCKET_SECONDS * 0.99))

        assert uptime_ratio(db_path, B, now=B + 1800) == pytest.approx(0.99)

    def test_zero_uptime_is_a_number_not_none(self, db_path):
        write_bucket(db_path, BucketMetrics(B, 0, 0, 0, 0))

        assert uptime_ratio(db_path, B, now=B + 1800) == 0.0

    def test_missing_buckets_count_as_downtime(self, db_path):
        record_uptime_impulse(db_path, B, BUCKET_SECONDS)

        # Окно из трёх часов: записан только последний — два первых простой,
        # включая часы до первого запуска демона (см. docstring uptime_ratio).
        assert uptime_ratio(db_path, B - 2 * BUCKET_SECONDS, now=B + 1800) == pytest.approx(1 / 3)

    def test_rows_outside_the_window_are_ignored(self, db_path):
        record_uptime_impulse(db_path, B - 10 * BUCKET_SECONDS, BUCKET_SECONDS)

        # В окне нет ни одного записанного бакета — данных ещё нет, не 0%.
        assert uptime_ratio(db_path, B - 2 * BUCKET_SECONDS, now=B + 1800) is None

    def test_window_further_in_the_future_is_none(self, db_path):
        record_uptime_impulse(db_path, B, BUCKET_SECONDS)

        assert uptime_ratio(db_path, B + 7200, now=B + 1800) is None

    def test_half_of_the_window_sums_over_both_buckets(self, db_path):
        record_uptime_impulse(db_path, B - BUCKET_SECONDS, BUCKET_SECONDS)
        write_bucket(db_path, BucketMetrics(B, 0, 0, 0, 0))

        assert uptime_ratio(db_path, B - BUCKET_SECONDS, now=B + 1800) == pytest.approx(0.5)

    def test_since_inside_the_bucket_is_still_a_whole_slot(self, db_path):
        record_uptime_impulse(db_path, B, BUCKET_SECONDS)

        assert uptime_ratio(db_path, B + 600, now=B + 1800) == 1.0
