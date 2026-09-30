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

**Два вида обновлений, у каждого — свой контракт.**

*Исторические колонки* пересчитываются из SQL (:func:`refresh_range`,
:func:`write_bucket`): повторный пересчёт заменяет значения, поэтому запись
идемпотентна — на этом стоит догон после рестарта. Пересчёт трогает ровно
свои четыре колонки.

*Uptime* пишется только аддитивными импульсами (:func:`record_uptime_impulse`):
``uptime_seconds += elapsed``, монотонный счётчик бакета с потолком в длину
часа. Исторический пересчёт его **не трогает вообще** — иначе SQL затирал бы
накопленное. Живость в момент тика определяет реестр супервизора
(``Supervisor.has_live_workers``: процесс по ``poll()`` плюс heartbeat не
старше ``DEFAULT_STALE_AFTER_SECONDS``), а не SQL по ``workers``: тикающий
реестр эквивалентен ``EXISTS (... heartbeat_at >= now - stale_after)`` в
момент тика (та же колонка, тот же порог, наблюдение супервизора отстаёт от
БД максимум на свой интервал, который лежит внутри stale-окна), но не требует
второго запроса и видит воркеров так, как их видит надзор.

Старый механизм «интервалы жизни ``[heartbeat, heartbeat + 15с)``» удалён:
в ``workers`` хранится только последняя отметка, поэтому интервалы прошлых
часов реконструировать не из чего, а текущий бакет давал бы ≤15 с на воркера —
для NFR «uptime ≥99% за окно» такие цифры бессмысленны. Прошлые бакеты без
импульсов честно остаются простоем: замер живёт с первого запуска демона,
окна до старта в доле считаются простоем (см. :func:`uptime_ratio`).

Модуль обязан оставаться лёгким: только стандартная библиотека и
``engine.db.migrations``. Ни логгера, ни открытия БД на уровне импорта —
сбой тика логирует вызывающий job.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

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
    "classify_run_status",
    "record_uptime_impulse",
    "refresh_range",
    "uptime_ratio",
    "write_bucket",
]

# Длина бакета, секунды. Час — и метрика «запросы/час», и период барьера
# исторического пересчёта, и потолок счётчика uptime.
BUCKET_SECONDS = 3600

# Итоги классификации: те же три значения, что RunOutcome в metrics.rs.
RUN_OUTCOME_SUCCESS = "success"
RUN_OUTCOME_FAILURE = "failure"
RUN_OUTCOME_OTHER = "other"

# Статусы, идущие в счёт, — зеркало Rust-матча. Множества, а не кортежи:
# важна только принадлежность, порядок не определён и не наблюдаем.
_SUCCESSES = frozenset({"ok", "completed"})
_FAILURES = frozenset({"failed", "crashed"})

# Исторический upsert: только четыре своих колонки. uptime_seconds в списке
# намеренно нет — пересчёт не имеет права трогать накопленный счётчик.
_HISTORY_UPSERT_SQL = (
    "INSERT INTO metrics_hourly (bucket, successes, failures, requests, captchas) "
    "VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT(bucket) DO UPDATE SET "
    "successes = excluded.successes, "
    "failures = excluded.failures, "
    "requests = excluded.requests, "
    "captchas = excluded.captchas"
)

# Импульс uptime: аддитивная запись с потолком в длину бакета. Потолок —
# страховка от двойного счёта (два демона на одной БД) и от прыжков часов:
# честная сумма за час физически не превышает 3600. Строка создаётся при
# необходимости, остальные колонки берут DEFAULT 0.
_UPTIME_IMPULSE_SQL = (
    "INSERT INTO metrics_hourly (bucket, uptime_seconds) VALUES (?, ?) "
    "ON CONFLICT(bucket) DO UPDATE SET "
    "uptime_seconds = MIN(metrics_hourly.uptime_seconds + excluded.uptime_seconds, ?)"
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


@dataclass(frozen=True)
class BucketMetrics:
    """Историческая часть часа ``metrics_hourly``: четыре колонки плюс бакет.

    Без ``uptime_seconds`` намеренно: у этого поля другой писатель
    (аддитивные импульсы) и другой контракт (монотонность), и смешивать оба
    в одном значении — прямой путь к затёртому счётчику.
    """

    bucket: int
    successes: int
    failures: int
    requests: int
    captchas: int


def aggregate_bucket(db_path: str | Path, bucket: int) -> BucketMetrics:
    """Считает историческую часть бакета по трём таблицам одним соединением.

    Один ``conn`` на все чтения: два отдельных соединения могли бы увидеть
    разные снимки и записать бакет, которого не было ни в одном из них.
    ``uptime_seconds`` здесь не участвует — его источник импульсы, а не SQL.
    """
    conn = migrations.connect(db_path)
    try:
        return _aggregate(conn, bucket)
    finally:
        conn.close()


def _aggregate(conn: sqlite3.Connection, bucket: int) -> BucketMetrics:
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
    return BucketMetrics(
        bucket=bucket,
        successes=successes,
        failures=failures,
        requests=requests,
        captchas=captchas,
    )


def write_bucket(db_path: str | Path, metrics: BucketMetrics) -> None:
    """Кладёт историческую часть бакета, не трогая ``uptime_seconds``.

    Идемпотентность — контракт исторических колонок: повторный пересчёт
    перезаписывает их и не оставляет ни дублей (PK ``bucket``), ни
    устаревших цифр. Uptime при этом переживает запись — его писать здесь
    нельзя (см. :func:`record_uptime_impulse`).
    """
    conn = migrations.connect(db_path)
    try:
        with conn:
            conn.execute(_HISTORY_UPSERT_SQL, _values(metrics))
    finally:
        conn.close()


def record_uptime_impulse(db_path: str | Path, bucket: int, seconds: float) -> None:
    """Прибавляет ``seconds`` живости к счётчику ``uptime_seconds`` бакета.

    Аддитивно и атомарно: строка создаётся при необходимости (исторические
    колонки — нули), существующая — увеличивается, поэтому рестарт демона
    продолжает счёт с того места, где он остановился. Потолок — длина бакета
    (``BUCKET_SECONDS``): честная сумма за час не превышает его, а вот два
    демона на одной БД или прыжок часов упрутся именно в потолок.

    ``seconds <= 0`` — ничего не пишет: нулевой импульс не создаёт строку,
    отрицательный был бы багом вызывающего, а не «отрицательным простоем».
    """
    if seconds <= 0:
        return
    conn = migrations.connect(db_path)
    try:
        with conn:
            conn.execute(_UPTIME_IMPULSE_SQL, (bucket, seconds, BUCKET_SECONDS))
    finally:
        conn.close()


def refresh_range(
    db_path: str | Path, since: float, *, now: float | None = None
) -> list[int]:
    """Пересчитывает историю бакетов ``floor(since/3600)..floor(now/3600)``.

    Оба края включительно, окно непрерывно: возвращает перечень бакетов по
    возрастанию — ровно то, что записано в БД. Пустой час получает строку
    нулей, а не пропуск, иначе он выпал бы из доли (см. :func:`uptime_ratio`);
    повторный вызов поверх уже записанных строк их пересчитывает, а не
    дублирует. ``uptime_seconds`` не трогается — это контракт
    :func:`record_uptime_impulse`. ``now=None`` — текущее время; тесты
    передают своё, чтобы окно не уезжало вместе с часами.
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
                conn.execute(_HISTORY_UPSERT_SQL, _values(_aggregate(conn, bucket)))
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

    **Замер идёт с первого запуска демона**: uptime — аддитивный счётчик, и
    бакеты, относящиеся к времени до старта (включая часы, которые догон
    записал только что), остаются нулевыми — это простой по построению, а
    не пропуск измерения. Чем длиннее окно ``since`` до первого запуска,
    тем ниже доля: окна честно измеряются такими, какими их застал замер.

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


def _values(metrics: BucketMetrics) -> tuple[int, int, int, int, int]:
    """Параметры ``_HISTORY_UPSERT_SQL`` в порядке колонок таблицы."""
    return (
        metrics.bucket,
        metrics.successes,
        metrics.failures,
        metrics.requests,
        metrics.captchas,
    )
