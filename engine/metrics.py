"""Часовые метрики дашборда: агрегаты ``metrics_hourly``, uptime-доля, догон.

Бакет — целый час **UTC**: ``bucket = floor(ts / 3600) * 3600``. Пояс в формулу
не входит вовсе: аптайм-доля «хотя бы один живой воркер» — это отношение
секунд, и от локального TZ оно не меняется, в отличие от дневного ``day``
(там локальная дата нужна экспорту и retention, см.
``engine.log_rotation.local_day``). Поэтому «час UTC» здесь — не настройка,
а просто метка начала часа в шкале unix-секунд ``ts``.

Состав бакета (план §2, таблица ``metrics_hourly``):

* ``successes``/``failures`` — строки ``runs`` с
  ``COALESCE(ended_at, created_at)`` внутри бакета. Классификация статуса
  зеркалит ``ui/src-tauri/src/metrics.rs::classify_run_status``:
  ``ok``/``completed`` — успех, ``failed``/``crashed`` — падение, всё
  прочее (``running``, ``stopped``, неизвестный статус) в счёт не идёт.
  Время берётся по завершению, а не по старту (в Rust-сводке там
  ``started_at``): час запуска — это час, когда прогон закончился, иначе
  длинный сценарий попадал бы в час старта;
* ``requests``/``captchas`` — ``network_requests`` и ``captcha_events`` по
  их ``ts``;
* ``uptime_seconds`` — секунды бакета, в которые жив был хотя бы один
  воркер.

**Формула uptime.** Воркер жив в момент ``t`` ⇔ найдётся отметка
``workers.heartbeat_at`` с ``t - stale_after <= heartbeat_at <= t``, где
``stale_after`` — ``supervisor.DEFAULT_STALE_AFTER_SECONDS``. У каждой
отметки берётся интервал жизни ``[heartbeat_at, heartbeat_at + stale_after)``,
интервалы одного воркера сортируются и сливаются (пересекающиеся и смежные —
в один), объединение пересекается с бакетом ``[bucket, bucket + 3600)`` и
суммируется. Точка ``heartbeat_at + stale_after`` имеет нулевую меру, поэтому
полузамкнутый интервал и незамкнутое ``<=`` в определении дают одну и ту же
сумму; смежные интервалы сливаются именно чтобы «касание» не выглядело
разрывом при разборе вручную.

**Запись идемпотентна**: ``INSERT ... ON CONFLICT(bucket) DO UPDATE`` всех
колонок — повторный пересчёт заменяет значения, а не плодит дубли и не
оставляет устаревшего. На этом стоит догон после рестарта.

Модуль обязан оставаться лёгким: только стандартная библиотека,
``engine.db.migrations`` и константа супервизора. Ни логгера, ни открытия БД
на уровне импорта — сбой тика логирует вызывающий job.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from engine.control_plane.supervisor import DEFAULT_STALE_AFTER_SECONDS
from engine.db import migrations

if TYPE_CHECKING:
    import sqlite3

__all__ = [
    "BUCKET_SECONDS",
    "RUN_OUTCOME_FAILURE",
    "RUN_OUTCOME_OTHER",
    "RUN_OUTCOME_SUCCESS",
    "BucketMetrics",
    "aggregate_bucket",
    "bucket_of",
    "bucket_uptime_seconds",
    "classify_run_status",
    "merge_intervals",
    "refresh_range",
    "uptime_ratio",
    "write_bucket",
]

# Длина бакета, секунды. Час — и метрика «запросы/час», и период job'а.
BUCKET_SECONDS = 3600

# Итоги классификации: те же три значения, что RunOutcome в metrics.rs.
RUN_OUTCOME_SUCCESS = "success"
RUN_OUTCOME_FAILURE = "failure"
RUN_OUTCOME_OTHER = "other"

# Статусы, идущие в счёт, — зеркало Rust-матча. Множества, а не кортежи:
# важна только принадлежность, порядок не определён и не наблюдаем.
_SUCCESSES = frozenset({"ok", "completed"})
_FAILURES = frozenset({"failed", "crashed"})

# Одна строка на бакет: PK bucket плюс пересчёт всех пяти колонок.
_UPSERT_SQL = (
    "INSERT INTO metrics_hourly "
    "(bucket, successes, failures, requests, captchas, uptime_seconds) "
    "VALUES (?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(bucket) DO UPDATE SET "
    "successes = excluded.successes, "
    "failures = excluded.failures, "
    "requests = excluded.requests, "
    "captchas = excluded.captchas, "
    "uptime_seconds = excluded.uptime_seconds"
)


def bucket_of(ts: float) -> int:
    """Начало часа UTC, содержащего ``ts``: ``floor(ts / 3600) * 3600``.

    Часовой пояс не участвует: unix-секунды сами по себе не имеют локали,
    поэтому смена ``TZ`` не сдвигает бакет (в отличие от ``day``). Отрицательные
    ``ts`` обрезаются вниз тем же ``floor`` — на них метрик не пишется, но и
    ошибка деления от них не появляется.
    """
    return math.floor(ts / BUCKET_SECONDS) * BUCKET_SECONDS


def classify_run_status(status: str | None) -> str:
    """Итог запуска для агрегата: успех, падение или «не в счёт».

    Зеркало ``classify_run_status`` из ``ui/src-tauri/src/metrics.rs``:
    пробелы по краям и регистр не важны (ASCII-статусы), ``ok`` и
    ``completed`` — успех, ``failed`` и ``crashed`` — падение, любое другое
    значение (``running``, ``stopped``, опечатка, пустая строка) не
    участвует в дашборде. ``None`` тоже «не в счёт»: незаполненный статус —
    это неизвестность, а не падение.
    """
    if status is None:
        return RUN_OUTCOME_OTHER
    normalized = status.strip().lower()
    if normalized in _SUCCESSES:
        return RUN_OUTCOME_SUCCESS
    if normalized in _FAILURES:
        return RUN_OUTCOME_FAILURE
    return RUN_OUTCOME_OTHER


def merge_intervals(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Сливает пересекающиеся и смежные интервалы в отсортированный список.

    Полуинтервалы ``[start, end)``: касание (``start`` равен концу
    предыдущего) — непрерывная жизнь, а не разрыв, поэтому условие слияния
    ``start <= конец``. Пустые интервалы (``end <= start``) отбрасываются —
    нулевое время жизни это не жизнь.
    """
    merged: list[tuple[float, float]] = []
    for start, end in sorted((float(start), float(end)) for start, end in intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def bucket_uptime_seconds(
    heartbeats: Iterable[float], bucket: int, stale_after: float = DEFAULT_STALE_AFTER_SECONDS
) -> int:
    """Секунды бакета, в которые жив хотя бы один воркер (0..3600).

    Каждой отметке соответствует интервал жизни
    ``[heartbeat_at, heartbeat_at + stale_after)``; интервалы всех воркеров
    сливаются (см. :func:`merge_intervals`), объединение пересекается с
    бакетом ``[bucket, bucket + 3600)`` — то есть clamp к бакету, — и длины
    суммируются. Дольки секунды округляются до целой: колонка INTEGER, а
    сумма из дробных heartbeat'ов держит погрешность порядка 1e-9.

    ``stale_after <= 0`` — жить некому, возвращается 0, а не «бесконечная
    жизнь из-ста нулевых интервалов».
    """
    if stale_after <= 0:
        return 0
    bucket_end = bucket + BUCKET_SECONDS
    total = 0.0
    for start, end in merge_intervals((hb, hb + stale_after) for hb in heartbeats):
        low = max(start, float(bucket))
        high = min(end, float(bucket_end))
        if high > low:
            total += high - low
    return math.floor(min(max(total, 0.0), float(BUCKET_SECONDS)) + 0.5)


@dataclass(frozen=True)
class BucketMetrics:
    """Один час ``metrics_hourly``: пять колонок плюс метка бакета."""

    bucket: int
    successes: int
    failures: int
    requests: int
    captchas: int
    uptime_seconds: int


def aggregate_bucket(
    db_path: str | Path, bucket: int, *, stale_after: float = DEFAULT_STALE_AFTER_SECONDS
) -> BucketMetrics:
    """Считает агрегат бакета по четырём таблицам одним соединением.

    Один ``conn`` на все чтения: два отдельных соединения могли бы увидеть
    разные снимки и записать бакет, которого не было ни в одном из них.
    """
    conn = migrations.connect(db_path)
    try:
        return _aggregate(conn, bucket, stale_after)
    finally:
        conn.close()


def _aggregate(conn: sqlite3.Connection, bucket: int, stale_after: float) -> BucketMetrics:
    """Внутренний разбор бакета на открытом соединении (см. aggregate_bucket)."""
    bucket_end = bucket + BUCKET_SECONDS
    successes = 0
    failures = 0
    # GROUP BY status, а не CASE в SQL: классификация одна —
    # classify_run_status, иначе Python и SQL разъехались бы на новых статусах.
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM runs "
        "WHERE COALESCE(ended_at, created_at) >= ? AND COALESCE(ended_at, created_at) < ? "
        "GROUP BY status",
        (bucket, bucket_end),
    ).fetchall()
    for row in rows:
        outcome = classify_run_status(row["status"])
        count = int(row["n"])
        if outcome == RUN_OUTCOME_SUCCESS:
            successes += count
        elif outcome == RUN_OUTCOME_FAILURE:
            failures += count
    requests = int(
        conn.execute(
            "SELECT COUNT(*) FROM network_requests WHERE ts >= ? AND ts < ?",
            (bucket, bucket_end),
        ).fetchone()[0]
    )
    captchas = int(
        conn.execute(
            "SELECT COUNT(*) FROM captcha_events WHERE ts >= ? AND ts < ?",
            (bucket, bucket_end),
        ).fetchone()[0]
    )
    heartbeats = [
        float(row[0])
        for row in conn.execute(
            "SELECT heartbeat_at FROM workers WHERE heartbeat_at IS NOT NULL"
        ).fetchall()
    ]
    return BucketMetrics(
        bucket=bucket,
        successes=successes,
        failures=failures,
        requests=requests,
        captchas=captchas,
        uptime_seconds=bucket_uptime_seconds(heartbeats, bucket, stale_after),
    )


def write_bucket(db_path: str | Path, metrics: BucketMetrics) -> None:
    """Кладёт бакет: ``INSERT ... ON CONFLICT(bucket) DO UPDATE`` всех колонок.

    Идемпотентность — контракт: повторный пересчёт перезаписывает значения и
    не оставляет ни дублей (PK ``bucket``), ни устаревших цифр. Поэтому
    догон после рестарта безопасен и его можно гонять сколько угодно раз.
    """
    conn = migrations.connect(db_path)
    try:
        with conn:
            conn.execute(_UPSERT_SQL, _values(metrics))
    finally:
        conn.close()


def refresh_range(
    db_path: str | Path,
    since: float,
    *,
    now: float | None = None,
    stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
) -> list[int]:
    """Пересчитывает и записывает бакеты ``floor(since/3600)..floor(now/3600)``.

    Оба края включительно, окно непрерывно: возвращает перечень бакетов по
    возрастанию — ровно то, что записано в БД. Пустой час получает строку
    нулей, а не пропуск, иначе он выпал бы из доли (см. :func:`uptime_ratio`);
    повторный вызов поверх уже записанных строк их пересчитывает, а не
    дублирует. ``now=None`` — текущее время; тесты передают своё, чтобы окно
    не уезжало вместе с часами.
    """
    now_ts = time.time() if now is None else now
    start = bucket_of(since)
    stop = bucket_of(now_ts)
    if stop < start:
        return []
    conn = migrations.connect(db_path)
    written: list[int] = []
    try:
        with conn:
            for bucket in range(start, stop + 1, BUCKET_SECONDS):
                conn.execute(_UPSERT_SQL, _values(_aggregate(conn, bucket, stale_after)))
                written.append(bucket)
    finally:
        conn.close()
    return written


def uptime_ratio(
    db_path: str | Path, since: float, *, now: float | None = None
) -> float | None:
    """Доля времени с живым воркером за окно ``[since, now]``, 0..1.

    Формула: ``sum(uptime_seconds) / (3600 * число бакетов окна)``, где
    окно — все часовые слоты от ``floor(since/3600)`` до текущего бакета
    включительно. Знаменатель считается по слотам, а не по записанным
    строкам: час без строки в ``metrics_hourly`` — это простой (демон не
    писал метрики ровно тогда, когда их не было), и выкидывать его из доли
    значило бы прятать ночные обрывы.

    ``None`` — в окне нет ни одного записанного бакета: метрики ещё не
    считались вовсе, и «0%» врало бы так же, как «100%». Ноль строк в
    таблице, окно в будущем и окно, целиком предшествующее первой записи, —
    все три случая ``None``, а не 0.0.
    """
    now_ts = time.time() if now is None else now
    since_bucket = bucket_of(since)
    current_bucket = bucket_of(now_ts)
    if current_bucket < since_bucket:
        return None
    conn = migrations.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(uptime_seconds), 0), COUNT(*) FROM metrics_hourly "
            "WHERE bucket >= ? AND bucket <= ?",
            (since_bucket, current_bucket),
        ).fetchone()
    finally:
        conn.close()
    total, recorded = int(row[0]), int(row[1])
    if recorded == 0:
        return None
    slots = (current_bucket - since_bucket) // BUCKET_SECONDS + 1
    return total / (BUCKET_SECONDS * slots)


def _values(metrics: BucketMetrics) -> tuple[int, int, int, int, int, int]:
    """Параметры ``_UPSERT_SQL`` в порядке колонок таблицы."""
    return (
        metrics.bucket,
        metrics.successes,
        metrics.failures,
        metrics.requests,
        metrics.captchas,
        metrics.uptime_seconds,
    )
