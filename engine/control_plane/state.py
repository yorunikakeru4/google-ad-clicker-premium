"""Состояние control plane в SQLite: воркеры, флаг паузы, логи, запуски.

Всё, что здесь живёт, читает UI через rusqlite, и всё это пишется из потока
супервизора, пока HTTP-обработчики читают то же самое. Отсюда три решения:

1. **Подключение на операцию, а не одно на объект.** sqlite3.Connection не
   переносится между потоками, а «горячий» объект с одним коннектом означал бы
   либо check_same_thread=False с гонками, либо сериализацию всего. Новое
   подключение на операцию стоит микросекунд и снимает вопрос целиком;
   busy_timeout из migrations.connect гасит конкуренцию за запись.
2. **Пауза — флаг в kv, а не поле в памяти.** Воркер это отдельный процесс,
   он не разделяет память с демоном, и единственный общий носитель состояния
   у них — БД.
3. **Идентификатор воркера — browser_id, а не суррогатный ключ.** Он же
   UNIQUE в схеме, и именно его показывает UI в логах и фильтрах.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from engine.db import migrations

# Ключи в таблице kv. Совпадают с тем, что ищет UI.
PAUSE_REQUESTED_KEY = "PAUSE_REQUESTED"
RUN_STATE_KEY = "RUN_STATE"

# Допустимые значения RUN_STATE. Неизвестное значение в БД (сторонняя правка,
# миграция данных) читается как "stopped", а не как мусор в UI.
RUN_STATES = ("stopped", "running", "paused", "stopping")


class WorkerStatus(str, Enum):
    """Жизненный цикл воркера в демоне.

    ``str, Enum`` — не украшение: статус пишется прямо в SQLite, и строковое
    значение в схеме совпадает с тем, что показывает UI.
    """

    STARTING = "starting"
    RUNNING = "running"
    BACKOFF = "backoff"
    STOPPED = "stopped"
    CIRCUIT_OPEN = "circuit_open"

    def __str__(self) -> str:
        return self.value


WORKER_STATUSES = frozenset(status.value for status in WorkerStatus)

# Статусы, означающие "процесса за этим идентификатором больше нет".
# Для них PID обнуляется: см. set_status.
_TERMINAL_STATUSES = frozenset({WorkerStatus.STOPPED, WorkerStatus.CIRCUIT_OPEN})


# Колонки, которые уходят в HTTP-снимок. Явный список, а не SELECT *, потому
# что схема может получить колонку для UI, которая в API не нужна, и её
# утечка в снимок была бы случайной, а не намеренной.
_SNAPSHOT_COLUMNS = (
    "browser_id",
    "pid",
    "status",
    "restart_count",
    "started_at",
    "heartbeat_at",
    "last_error",
)


class StateStore:
    """Чтение и запись состояния демона.

    Экземпляр можно свободно использовать из нескольких потоков: блокировка
    не нужна, потому что состояния соединения не хранится.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = migrations.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    # --- воркеры ---------------------------------------------------------

    def register_worker(self, browser_id: str, pid: int | None, now: float | None = None) -> int:
        """Регистрирует воркера и возвращает его id.

        Повторная регистрация того же browser_id обновляет существующую строку
        вместо вставки второй: после перезапуска это тот же воркер, и
        его история рестартов не должна раздваиваться.

        ``now`` передаётся снаружи, а не берётся из time.time(): время в БД
        должно совпадать с тем, по которому супервизор принимает решения, иначе
        в тестах и при разборе инцидентов два источника времени разойдутся.
        """
        stamp = time.time() if now is None else now
        with self._connect() as conn:
            with self._lock:
                conn.execute(
                    """
                    INSERT INTO workers (pid, browser_id, status, started_at, heartbeat_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (browser_id) DO UPDATE SET
                        pid = excluded.pid,
                        status = excluded.status,
                        started_at = excluded.started_at,
                        heartbeat_at = excluded.heartbeat_at
                    """,
                    (pid, browser_id, WorkerStatus.STARTING.value, stamp, stamp),
                )
                row = conn.execute(
                    "SELECT id FROM workers WHERE browser_id = ?", (browser_id,)
                ).fetchone()
                conn.commit()
        return int(row["id"])

    def get_worker(self, browser_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workers WHERE browser_id = ?", (browser_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_workers(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM workers ORDER BY browser_id").fetchall()
        return [dict(row) for row in rows]

    def set_status(
        self,
        browser_id: str,
        status: WorkerStatus,
        error: str | None = None,
    ) -> None:
        """Ставит статус воркеру.

        В статусах, означающих «процесса больше нет» (STOPPED, CIRCUIT_OPEN),
        дополнительно обнуляется pid: процесс уже не существует, и оставшийся
        в БД PID прочитается как «воркер работает» — и демон, и UI смотрят на
        это поле, чтобы понять, жив ли воркер.
        """
        if status in _TERMINAL_STATUSES:
            sql = "UPDATE workers SET status = ?, pid = NULL, last_error = ? WHERE browser_id = ?"
            params: tuple[Any, ...] = (status.value, error, browser_id)
        else:
            sql = "UPDATE workers SET status = ?, last_error = ? WHERE browser_id = ?"
            params = (status.value, error, browser_id)
        with self._connect() as conn:
            with self._lock:
                conn.execute(sql, params)
                conn.commit()


    def increment_restart_count(self, browser_id: str) -> int:
        """Увеличивает счётчик рестартов и возвращает новое значение.

        Возвращаемое значение нужно супервизору: именно оно сверяется с
        потолком рестартов, и решение о circuit breaker принимается по факту,
        записанному в БД, а не по счётчику в памяти.
        """
        with self._connect() as conn:
            with self._lock:
                conn.execute(
                    "UPDATE workers SET restart_count = restart_count + 1 WHERE browser_id = ?",
                    (browser_id,),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT restart_count FROM workers WHERE browser_id = ?", (browser_id,)
                ).fetchone()
        return int(row["restart_count"]) if row is not None else 0

    def reset_restart_count(self, browser_id: str) -> None:
        """Сбрасывает счётчик: воркер отработал без падений."""
        with self._connect() as conn:
            with self._lock:
                conn.execute(
                    "UPDATE workers SET restart_count = 0 WHERE browser_id = ?", (browser_id,)
                )
                conn.commit()

    def heartbeat(self, browser_id: str, now: float | None = None) -> None:
        """Обновляет отметку «воркер жив».

        Только heartbeat_at: остальные поля воркера в этот момент меняться не
        должны, иначе UI видел бы скачущий started_at.
        """
        stamp = time.time() if now is None else now
        with self._connect() as conn:
            with self._lock:
                conn.execute(
                    "UPDATE workers SET heartbeat_at = ? WHERE browser_id = ?",
                    (stamp, browser_id),
                )
                conn.commit()

    def delete_worker(self, browser_id: str) -> None:
        with self._connect() as conn:
            with self._lock:
                conn.execute("DELETE FROM workers WHERE browser_id = ?", (browser_id,))
                conn.commit()

    # --- флаги в kv ------------------------------------------------------

    def get_flag(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return default if row is None else row["value"]

    def set_flag(self, key: str, value: str) -> None:
        with self._connect() as conn:
            with self._lock:
                conn.execute(
                    """
                    INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (key, value, time.time()),
                )
                conn.commit()

    def is_pause_requested(self) -> bool:
        """Запрошена ли пауза.

        Читает БД, а не кэш: ответ нужен воркеру в другом процессе и должен
        отражать последний запрос, сделанный через HTTP.
        """
        return self.get_flag(PAUSE_REQUESTED_KEY, "0") == "1"

    def request_pause(self) -> None:
        self.set_flag(PAUSE_REQUESTED_KEY, "1")

    def clear_pause(self) -> None:
        self.set_flag(PAUSE_REQUESTED_KEY, "0")

    def get_run_state(self) -> str:
        value = self.get_flag(RUN_STATE_KEY, "stopped")
        return value if value in RUN_STATES else "stopped"

    def set_run_state(self, state: str) -> None:
        self.set_flag(RUN_STATE_KEY, state)

    # --- запуски ---------------------------------------------------------

    def start_run(self, worker_id: int) -> int:
        now = time.time()
        with self._connect() as conn:
            with self._lock:
                cursor = conn.execute(
                    "INSERT INTO runs (worker_id, started_at, status) VALUES (?, ?, 'running')",
                    (worker_id, now),
                )
                conn.commit()
        return int(cursor.lastrowid or 0)

    def finish_run(self, run_id: int, status: str, error: str | None = None) -> None:
        with self._connect() as conn:
            with self._lock:
                conn.execute(
                    "UPDATE runs SET ended_at = ?, status = ?, error = ? WHERE id = ?",
                    (time.time(), status, error, run_id),
                )
                conn.commit()

    def add_run_counts(
        self,
        run_id: int,
        clicks: int = 0,
        captcha_seen: int = 0,
        captcha_solved: int = 0,
    ) -> None:
        """Дописывает счётчики за��уска.

        Сложение, а не присваивание: воркер отчитывается частями по ходу
        работы, и перезапись обнулила бы всё, что накоплено.
        """
        with self._connect() as conn:
            with self._lock:
                conn.execute(
                    """
                    UPDATE runs SET
                        total_clicks = total_clicks + ?,
                        captcha_seen = captcha_seen + ?,
                        captcha_solved = captcha_solved + ?
                    WHERE id = ?
                    """,
                    (clicks, captcha_seen, captcha_solved, run_id),
                )
                conn.commit()

    def latest_run_id(self, worker_id: int) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM runs WHERE worker_id = ? ORDER BY id DESC LIMIT 1", (worker_id,)
            ).fetchone()
        return int(row["id"]) if row is not None else None

    # --- логи ------------------------------------------------------------

    def log(
        self,
        level: str = "INFO",
        category: str = "",
        message: str = "",
        fields: dict[str, Any] | None = None,
        browser_id: str | None = None,
    ) -> None:
        """Пишет строку в logs.

        fields уходит JSON-строкой: схема объявлена «message читаем человеком,
        fields — всё машинное», и разбирать это обратно на стороне UI дешевле,
        чем городить формат.
        """
        payload = json.dumps(fields, ensure_ascii=False, sort_keys=True) if fields else None
        with self._connect() as conn:
            with self._lock:
                conn.execute(
                    "INSERT INTO logs (ts, level, browser_id, category, message, fields) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (time.time(), level, browser_id, category, message, payload),
                )
                conn.commit()

    # --- снимок для /state ----------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Срез состояния для GET /state.

        Собирается из одной серии чтений под блокировкой, чтобы HTTP-ответ не
        поймал момент, где paused=True, а state=running из-за чужого запроса.
        """
        with self._lock:
            pause_requested = self.is_pause_requested()
            run_state = self.get_run_state()
            workers = self.list_workers()

        snapshot_workers = [
            {column: worker.get(column) for column in _SNAPSHOT_COLUMNS} for worker in workers
        ]
        alive = [w for w in snapshot_workers if w["status"] == WorkerStatus.RUNNING.value]

        return {
            "state": run_state,
            "paused": pause_requested,
            "workers": snapshot_workers,
            "worker_count": len(snapshot_workers),
            "alive_count": len(alive),
            "updated_at": time.time(),
        }
