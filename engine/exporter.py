"""Постоянный экспорт данных из локальной SQLite во внешнюю PostgreSQL (план §9).

Демон раз в интервал (``ADCLICKER_EXPORT_INTERVAL``) делает один проход:
читает строки из ``adclicker.db`` и upsert'ит их в зеркальные таблицы
PostgreSQL. Модуль не открывает ни одной БД на уровне импорта: ``psycopg``
импортируется лениво в ``Exporter.__init__``, а соединение вовсе не
создаётся при ``enabled=False`` — проект без psycopg и без export-секции
обязан работать как раньше.

Два вида таблиц
---------------
*Инкрементальные* (``runs``, ``logs``, ``clicks``, ``network_requests``,
``captcha_events``, ``proxy_usage``, ``diagnostics``) идут по курсору
``export_state.last_id``: ``WHERE id > last_id ORDER BY id LIMIT batch_size``.
Курсор двигается только после успешного батча: сбой сети оставляет его на
месте, следующий тик повторяет батч, а идемпотентный upsert не плодит дубли.

*Справочники* (``proxies``, ``profiles``, ``workers``, ``metrics_hourly``)
выгружаются целиком каждый тик: таблицы маленькие, строки в них меняются
(статус воркера, latency прокси), и поймать такие изменения курсором по id
нельзя.

``runs`` дополнительно повторно выгружает строки со ``status='running'``:
они дописываются уже после экспорта (``ended_at``, счётчики), поэтому один
раз взятый батч обязан проезжать повторно — иначе завершившийся запуск
навсегда остался бы в PG со статусом ``running``.

Секреты
-------
``proxies.username``/``proxies.password`` в схеме существуют (совместимость
колонок), но в PG всегда пишутся как NULL: креды прокси не покидают машину.
Значения к тому же не читаются из SQLite — в ``SELECT`` этих колонок нет.

Ошибки
------
Сетевые и SQL-ошибки превращаются в :class:`ExportError`: сбой не глотается
(тик демона логирует его в ``store.log(category="export")`` и продолжает
расписание) и не оставляет соединение в упавшей транзакции — перед
повышением выполняется rollback. ``close()`` ошибок не поднимает: его зовут
на остановке демона, и падение там оставило бы процесс без чистого выхода.

Время в PG хранится как ``double precision`` (unix-секунды), ``day`` — как
``text``: состав колонок 1:1 с ``engine/db/schema.sql``, меняется только
тип (``REAL`` → ``double precision``, ``INTEGER`` → ``bigint``, ``TEXT`` →
``text``) и убираются FK. Справочный DDL собран из тех же спецификаций в
``_REFERENCE_DDL``; настоящий ``init.sql`` — владение агента B.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engine.db import migrations

if TYPE_CHECKING:
    import sqlite3

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "EXPORT_TABLES",
    "ExportError",
    "ExportSettings",
    "Exporter",
]

# Размер батча по умолчанию: план §9.3 (50..5000, шаг за проход по таблице).
# Дефолт держится здесь, а не копируется в config.py: два числа «500»
# однажды разойдутся, и лимит из конфига перестанет совпадать с реальным
# размером запроса.
DEFAULT_BATCH_SIZE = 500

# Типы колонок в PG: план §9.2 (REAL -> double precision, INTEGER -> bigint,
# TEXT -> text). Имя «bigint» выбрано и для не-id INTEGER-колонок: счётчики
# и флаги в PG-зеркале — тоже bigint.
_BIGINT = "bigint"
_DOUBLE = "double precision"
_TEXT = "text"

# Колонки, которых не бывает в ``SELECT``: значения всегда пишутся в PG как
# NULL (см. модульный докстринг, «Секреты»).
_NULL_COLUMNS = frozenset({"username", "password"})


@dataclass(frozen=True)
class _TableSpec:
    """Состав одной таблицы-зеркала и её правила экспорта.

    ``columns`` — колонки в порядке ``schema.sql``: тот же порядок уходит в
    ``SELECT``/``INSERT``, поэтому SQL и DDL не могут разойтись.
    ``conflict`` — колонка ``ON CONFLICT`` (PK целевой таблицы); для
    курсорных таблиц это же поле — и водяной знак в ``export_state``.
    ``state_key`` — колонка, ``max()`` которой пишется в
    ``export_state.last_id``: у ``workers`` PK — ``browser_id`` (text), а
    курсорное поле в export_state обязано быть целым.
    """

    name: str
    columns: tuple[tuple[str, str], ...]
    conflict: str
    cursor: bool
    state_key: str

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.columns)

    @property
    def readable_names(self) -> tuple[str, ...]:
        """Колонки для ``SELECT``: без тех, что экспортируются как NULL."""
        return tuple(name for name in self.names if name not in _NULL_COLUMNS)


# Порядок экспорта — план §9.3: сначала справочники (FK-ссылки на прокси,
# профили и воркеров уже существуют в PG), потом инкрементальные таблицы.
_SPECS: dict[str, _TableSpec] = {
    "proxies": _TableSpec(
        name="proxies",
        columns=(
            ("id", _BIGINT),
            ("label", _TEXT),
            ("scheme", _TEXT),
            ("host", _TEXT),
            ("port", _BIGINT),
            ("username", _TEXT),
            ("password", _TEXT),
            ("latency_ms", _BIGINT),
            ("is_alive", _BIGINT),
            ("fail_count", _BIGINT),
            ("last_checked_at", _DOUBLE),
            ("last_error", _TEXT),
            ("created_at", _DOUBLE),
        ),
        conflict="id",
        cursor=False,
        state_key="id",
    ),
    "profiles": _TableSpec(
        name="profiles",
        columns=(
            ("id", _BIGINT),
            ("name", _TEXT),
            ("key_ref", _TEXT),
            ("proxy_id", _BIGINT),
            ("user_agent", _TEXT),
            ("locale", _TEXT),
            ("timezone", _TEXT),
            ("status", _TEXT),
            ("last_used_at", _DOUBLE),
            ("fields", _TEXT),
            ("created_at", _DOUBLE),
        ),
        conflict="id",
        cursor=False,
        state_key="id",
    ),
    "workers": _TableSpec(
        name="workers",
        columns=(
            ("id", _BIGINT),
            ("pid", _BIGINT),
            ("browser_id", _TEXT),
            ("profile_id", _BIGINT),
            ("proxy_id", _BIGINT),
            ("status", _TEXT),
            ("started_at", _DOUBLE),
            ("heartbeat_at", _DOUBLE),
            ("restart_count", _BIGINT),
            ("last_error", _TEXT),
            ("created_at", _DOUBLE),
        ),
        conflict="browser_id",
        cursor=False,
        state_key="id",
    ),
    "metrics_hourly": _TableSpec(
        name="metrics_hourly",
        columns=(
            ("bucket", _BIGINT),
            ("successes", _BIGINT),
            ("failures", _BIGINT),
            ("requests", _BIGINT),
            ("captchas", _BIGINT),
            ("uptime_seconds", _BIGINT),
        ),
        conflict="bucket",
        cursor=False,
        state_key="bucket",
    ),
    "runs": _TableSpec(
        name="runs",
        columns=(
            ("id", _BIGINT),
            ("worker_id", _BIGINT),
            ("started_at", _DOUBLE),
            ("ended_at", _DOUBLE),
            ("status", _TEXT),
            ("error", _TEXT),
            ("total_clicks", _BIGINT),
            ("captcha_seen", _BIGINT),
            ("captcha_solved", _BIGINT),
            ("created_at", _DOUBLE),
        ),
        conflict="id",
        cursor=True,
        state_key="id",
    ),
    "logs": _TableSpec(
        name="logs",
        columns=(
            ("id", _BIGINT),
            ("ts", _DOUBLE),
            ("day", _TEXT),
            ("level", _TEXT),
            ("browser_id", _TEXT),
            ("category", _TEXT),
            ("message", _TEXT),
            ("fields", _TEXT),
            ("created_at", _DOUBLE),
        ),
        conflict="id",
        cursor=True,
        state_key="id",
    ),
    "clicks": _TableSpec(
        name="clicks",
        columns=(
            ("id", _BIGINT),
            ("ts", _DOUBLE),
            ("url", _TEXT),
            ("query", _TEXT),
            ("category", _TEXT),
            ("browser_id", _TEXT),
            ("proxy_id", _BIGINT),
            ("http_status", _BIGINT),
            ("created_at", _DOUBLE),
        ),
        conflict="id",
        cursor=True,
        state_key="id",
    ),
    "network_requests": _TableSpec(
        name="network_requests",
        columns=(
            ("id", _BIGINT),
            ("ts", _DOUBLE),
            ("browser_id", _TEXT),
            ("method", _TEXT),
            ("url", _TEXT),
            ("resource_type", _TEXT),
            ("status", _BIGINT),
            ("created_at", _DOUBLE),
        ),
        conflict="id",
        cursor=True,
        state_key="id",
    ),
    "captcha_events": _TableSpec(
        name="captcha_events",
        columns=(
            ("id", _BIGINT),
            ("ts", _DOUBLE),
            ("browser_id", _TEXT),
            ("proxy_id", _BIGINT),
            ("page_url", _TEXT),
            ("sitekey", _TEXT),
            ("screenshot_path", _TEXT),
            ("solved", _BIGINT),
            ("solver", _TEXT),
            ("elapsed_ms", _BIGINT),
            ("created_at", _DOUBLE),
        ),
        conflict="id",
        cursor=True,
        state_key="id",
    ),
    "proxy_usage": _TableSpec(
        name="proxy_usage",
        columns=(
            ("id", _BIGINT),
            ("ts", _DOUBLE),
            ("proxy_id", _BIGINT),
            ("browser_id", _TEXT),
            ("result", _TEXT),
            ("latency_ms", _BIGINT),
            ("created_at", _DOUBLE),
        ),
        conflict="id",
        cursor=True,
        state_key="id",
    ),
    "diagnostics": _TableSpec(
        name="diagnostics",
        columns=(
            ("id", _BIGINT),
            ("ts", _DOUBLE),
            ("browser_id", _TEXT),
            ("proxy_id", _BIGINT),
            ("ip", _TEXT),
            ("country", _TEXT),
            ("user_agent", _TEXT),
            ("accept_language", _TEXT),
            ("timezone_id", _TEXT),
            ("screen_w", _BIGINT),
            ("screen_h", _BIGINT),
            ("platform", _TEXT),
            ("webgl_vendor", _TEXT),
            ("webgl_renderer", _TEXT),
            ("browser_version", _TEXT),
            ("headers", _TEXT),
            ("suspicion_flags", _TEXT),
            ("created_at", _DOUBLE),
        ),
        conflict="id",
        cursor=True,
        state_key="id",
    ),
}

# Порядок таблиц за тик — ключи _SPECS в порядке объявления (план §9.3).
EXPORT_TABLES: tuple[str, ...] = tuple(_SPECS)


def _insert_sql(spec: _TableSpec) -> str:
    """Upsert-запрос в PG: все колонки из ``excluded`` (идемпотентный тик)."""
    names = ", ".join(spec.names)
    placeholders = ", ".join(["%s"] * len(spec.columns))
    updates = ", ".join(f"{name} = excluded.{name}" for name in spec.names)
    return (
        f"INSERT INTO {spec.name} ({names}) VALUES ({placeholders}) "
        f"ON CONFLICT ({spec.conflict}) DO UPDATE SET {updates}"
    )


def _snapshot_select_sql(spec: _TableSpec) -> str:
    """Полный снимок таблицы из SQLite (секретные колонки не читаются)."""
    return f"SELECT {', '.join(spec.readable_names)} FROM {spec.name}"


def _cursor_select_sql(spec: _TableSpec) -> str:
    """Следующий батч инкрементальной таблицы из SQLite (``?`` — SQLite)."""
    return (
        f"SELECT {', '.join(spec.readable_names)} FROM {spec.name} "
        f"WHERE {spec.conflict} > ? ORDER BY {spec.conflict} LIMIT ?"
    )


# Живые запуски: дописываются после экспорта, поэтому повторяются каждый тик.
# Лимит тот же, что у батча: слишком длинный список «running» не должен
# превращать тик в выгрузку всего файла разом.
_RUNNING_RUNS_SQL = (
    f"SELECT {', '.join(_SPECS['runs'].readable_names)} FROM runs "
    "WHERE status = 'running' ORDER BY id LIMIT ?"
)

# Курсор в PG. Пишется upsert'ом: на первой тике строки ещё нет, а повторный
# тик обязан обновлять отметку, а не падать на UPDATE без строк.
_STATE_SELECT_SQL = "SELECT table_name, last_id FROM export_state"
_STATE_UPSERT_SQL = (
    "INSERT INTO export_state (table_name, last_id, updated_at) VALUES (%s, %s, %s) "
    "ON CONFLICT (table_name) DO UPDATE SET "
    "last_id = excluded.last_id, updated_at = excluded.updated_at"
)


def _build_reference_ddl() -> tuple[str, ...]:
    """Справочный DDL зеркальных таблиц из тех же спецификаций.

    Не выполняется ни в коде, ни в тестах: настоящий ``init.sql`` в корне
    репозитория — владение агента B. Функция существует, чтобы состав
    колонок и типов жил ровно в одном месте (``_SPECS``), а DDL для
    разбора контракта нельзя было забыть обновить.
    """
    statements = []
    for spec in _SPECS.values():
        body = ",\n".join(
            f"    {name} {pg_type}" + (" PRIMARY KEY" if name == spec.conflict else "")
            for name, pg_type in spec.columns
        )
        statements.append(f"CREATE TABLE IF NOT EXISTS {spec.name} (\n{body}\n);")
    statements.append(
        "CREATE TABLE IF NOT EXISTS export_state (\n"
        "    table_name text PRIMARY KEY,\n"
        "    last_id bigint NOT NULL DEFAULT 0,\n"
        "    updated_at double precision\n"
        ");"
    )
    return tuple(statements)


_REFERENCE_DDL: tuple[str, ...] = _build_reference_ddl()


@dataclass(frozen=True)
class ExportSettings:
    """Настройки экспорта — ровно секция ``export`` config.json (план §13.1).

    Неизменяемость нужна демону для сравнения: тик строит настройки заново
    из текущего конфига, и по неравенству с ``Exporter.settings`` понимает,
    что соединение пора пересоздать.
    """

    enabled: bool
    host: str
    port: int
    dbname: str
    user: str
    password: str
    sslmode: str
    batch_size: int


class ExportError(Exception):
    """Экспорт не удался: сеть, PostgreSQL или отсутствующий psycopg.

    Тик демона логирует её и продолжает расписание; курсор при этом остаётся
    на месте, поэтому следующий тик повторит неудавшийся батч.
    """


class Exporter:
    """Проход SQLite → PostgreSQL: курсор по инкрементальным таблицам.

    Соединение открывается в конструкторе (и только при ``enabled=True``),
    поэтому демон создаёт экземпляр лениво при первом тике с актуальными
    настройками, а выключенный экспорт не держит ни psycopg, ни сокет.
    """

    def __init__(self, db_path: str | Path, settings: ExportSettings):
        self.db_path = Path(db_path)
        self.settings = settings
        self._conn: Any | None = None
        if not settings.enabled:
            return
        try:
            import psycopg
        except ImportError as exc:
            raise ExportError(
                "psycopg не установлен: экспорт в PostgreSQL невозможен"
            ) from exc
        try:
            self._conn = psycopg.connect(
                host=settings.host,
                port=settings.port,
                dbname=settings.dbname,
                user=settings.user,
                password=settings.password,
                sslmode=settings.sslmode,
                connect_timeout=5,
            )
        except Exception as exc:
            raise ExportError(f"не удалось подключиться к PostgreSQL: {exc}") from exc

    def export_pass(self, *, now: float | None = None) -> dict[str, int]:
        """Один проход: ``{таблица: сколько строк записано}`` за тик.

        ``enabled=False`` → ``{}`` без единого обращения к БД. Список
        таблиц полный, включая нули: сводка в логе демона показывает, что
        таблица прошла тик, даже когда новых строк не было. Счётчик считает
        уникальные строки (``runs`` выгружается двумя порциями — курсором и
        повтором живых запусков, одна строка не должна считаться дважды).

        ``now`` — метка времени записей ``export_state``; тесты передают
        своё, чтобы updated_at не зависел от системных часов.
        """
        if not self.settings.enabled:
            return {}
        if self._conn is None:
            raise ExportError("соединение с PostgreSQL закрыто")
        stamp = time.time() if now is None else now
        sqlite_conn = migrations.connect(self.db_path)
        try:
            return self._export_all(sqlite_conn, stamp)
        except ExportError:
            self._rollback()
            raise
        except Exception as exc:
            self._rollback()
            raise ExportError(f"экспорт не удался: {exc}") from exc
        finally:
            sqlite_conn.close()

    def close(self) -> None:
        """Закрывает соединение с PostgreSQL; повторный вызов безопасен.

        Ошибки закрытия глотаются: метод зовут из shutdown демона, и падение
        там оставило бы процесс без чистого выхода.
        """
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass

    # --- внутренности прохода ---------------------------------------------

    def _export_all(self, sqlite_conn: sqlite3.Connection, stamp: float) -> dict[str, int]:
        """Читает курсоры, выгружает таблицы по порядку, закрывает транзакции."""
        pg_cur = self._conn.cursor()
        try:
            state = self._read_state(pg_cur)
            # Транзакция чтения закрывается сразу: тик без данных не должен
            # держать соединение в состоянии idle in transaction до батча.
            self._conn.commit()
            summary: dict[str, int] = {}
            for name in EXPORT_TABLES:
                spec = _SPECS[name]
                summary[name] = self._export_table(
                    pg_cur, sqlite_conn, spec, int(state.get(name, 0)), stamp
                )
            return summary
        finally:
            try:
                pg_cur.close()
            except Exception:
                pass

    def _read_state(self, pg_cur: Any) -> dict[str, int]:
        """Водяные знаки из ``export_state``; отсутствие строки = ноль."""
        pg_cur.execute(_STATE_SELECT_SQL)
        return {str(row[0]): int(row[1]) for row in pg_cur.fetchall()}

    def _export_table(
        self,
        pg_cur: Any,
        sqlite_conn: sqlite3.Connection,
        spec: _TableSpec,
        last_id: int,
        stamp: float,
    ) -> int:
        """Один батч одной таблицы плюс его курсор, один PG-коммит.

        Возвращает число уникальных записанных строк. ``0`` — батча не было:
        ни upsert'а, ни записи курсора, ни коммита (тиком без новых данных
        PG вообще не затрагивается, кроме чтения ``export_state``).
        """
        if spec.cursor:
            rows = sqlite_conn.execute(
                _cursor_select_sql(spec), (last_id, self.settings.batch_size)
            ).fetchall()
        else:
            rows = sqlite_conn.execute(_snapshot_select_sql(spec)).fetchall()
        if not rows:
            return 0

        params = [_row_params(spec, row) for row in rows]
        written = {row[spec.conflict] for row in rows}
        if spec.name == "runs":
            # Живые запуски идут второй порцией после курсора: их значения
            # читаются позже, поэтому более свежие ended_at/счётчики пишутся
            # последними и не перетираются курсорным снимком.
            running = sqlite_conn.execute(
                _RUNNING_RUNS_SQL, (self.settings.batch_size,)
            ).fetchall()
            running_params = [_row_params(spec, row) for row in running]
            written.update(row[spec.conflict] for row in running)
            params.extend(running_params)

        pg_cur.executemany(_insert_sql(spec), params)
        # Курсор двигается только по основному батчу: строка из повтора
        # живых запусков могла оказаться за пределами LIMIT и тогда её id
        # перепрыгнул бы необработанные строки.
        new_last_id = max(row[spec.state_key] for row in rows)
        pg_cur.execute(_STATE_UPSERT_SQL, (spec.name, new_last_id, stamp))
        self._conn.commit()
        return len(written)

    def _rollback(self) -> None:
        """Гасит упавшую транзакцию, чтобы следующий тик начал с чистого листа."""
        conn = self._conn
        if conn is None:
            return
        try:
            conn.rollback()
        except Exception:
            pass


def _row_params(spec: _TableSpec, row: sqlite3.Row) -> tuple[Any, ...]:
    """Параметры ``INSERT`` из строки SQLite в порядке колонок спецификации.

    Секретные колонки (``_NULL_COLUMNS``) подставляются как NULL и в
    ``SELECT`` не входят вовсе: креды прокси не покидают машину даже
    на время прохода.
    """
    return tuple(None if name in _NULL_COLUMNS else row[name] for name in spec.names)
