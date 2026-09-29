"""Батчевый writer SQLite: логи, клики, сетевые запросы, запуски, heartbeat и деградация.

Один writer на процесс, соединение держится открытым. Логи, клики и записи
``network_requests`` — частые append-операции, поэтому они буферизуются и
уходят в БД батчами: по размеру (``batch_size``) или по таймеру
(``flush_interval``), по любому из условий. Все три буфера делят один
размер батча: метрика запросов не должна откладывать запись логов.

Три решения, которые стоит знать:

1. **Своё соединение, а не StateStore.** StateStore открывает подключение на
   операцию — это правильно для редких операций супервизора, но ломает
   батчинг и порядок записей. Writer держит одно соединение открытым и
   сериализует доступ к нему блокировкой. Поэтому же соединение создаётся с
   ``check_same_thread=False``: таймер сброса живёт в отдельном потоке, а
   ``migrations.connect`` такого флага не выставляет. PRAGMA
   (``foreign_keys``, ``busy_timeout``) — те же, константа таймаута
   переиспользуется из ``migrations``.
2. **Запуски — через StateStore.** ``start_run``/``finish_run``/
   ``add_run_counts`` — редкие операции жизненного цикла, их SQL уже есть и
   покрыт тестами. Дублировать его в writer'е значило бы два места правды
   об одной таблице, поэтому writer делегирует их StateStore. Конфликта нет
   по построению: обе стороны пишут короткие транзакции под WAL.
3. **Heartbeat и деградация — свои upsert'ы, а не StateStore.** Супервизорский
   heartbeat — это ``UPDATE`` существующей строки: супервизор регистрирует
   воркера до спавна. Воркер стучит со своей стороны и не может зависеть от
   того, успел ли супервизор создать строку (гонка между процессами),
   поэтому writer делает ``INSERT ... ON CONFLICT DO UPDATE`` только колонки
   ``heartbeat_at``. Существующие строки при этом не трогаются: ``status``,
   ``started_at`` и ``pid`` остаются как их поставил супервизор.
   ``mark_degraded`` устроен так же, но пишет ``status='degraded'`` и
   ``last_error`` — сигнал о нерабочем прокси/CDP не должен ждать батча.

Политика ошибок записи: ошибка (закрытая БД, полный диск, битая схема) не
роняет вызывающего. Незаписанный батч считается потерянным: ``dropped``
растёт на число записей, ``last_error`` хранит текст последней ошибки,
writer продолжает принимать новые записи. Повторная попытка удержать тот же
батч превратила бы полный диск в бесконечный ретрай и растущую память,
поэтому батч отбрасывается сразу. Потеря при *штатном* завершении —
дефект: ``close()`` всегда сбрасывает остаток.

Политика чтения та же по духу: ``count_network_requests`` не бросает —
ошибка окна даёт ``0`` и текст в ``last_error``, потому что метрика не
должна ронять дашборд.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from engine.control_plane.state import StateStore
from engine.db.migrations import BUSY_TIMEOUT_MS

_LOG_INSERT = (
    "INSERT INTO logs (ts, level, browser_id, category, message, fields) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
_CLICK_INSERT = (
    "INSERT INTO clicks (ts, url, query, category, browser_id, proxy_id, http_status) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_NETWORK_INSERT = (
    "INSERT INTO network_requests (ts, browser_id, method, url, resource_type, status) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
_HEARTBEAT_UPSERT = (
    "INSERT INTO workers (browser_id, status, started_at, heartbeat_at) "
    "VALUES (?, 'starting', ?, ?) "
    "ON CONFLICT (browser_id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at"
)
# Сигнал деградации — второй немедленный upsert в workers. Как и heartbeat,
# воркер не может ждать регистрации супервизора, поэтому строка вставляется
# при необходимости, а из существующей меняются только status и last_error:
# pid, started_at, restart_count и heartbeat_at остаются супервизорскими.
_DEGRADED_UPSERT = (
    "INSERT INTO workers (browser_id, status, started_at, last_error, heartbeat_at) "
    "VALUES (?, 'degraded', ?, ?, ?) "
    "ON CONFLICT (browser_id) DO UPDATE SET "
    "status = 'degraded', last_error = excluded.last_error"
)


class StoreWriter:
    """Потокобезопасный батчевый writer. Методы никогда не бросают исключений
    из-за ошибок хранения: см. политику в docstring модуля."""

    def __init__(
        self,
        db_path: str | Path,
        batch_size: int = 200,
        flush_interval: float = 1.0,
    ):
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError(f"batch_size должен быть целым >= 1, получено {batch_size!r}")
        if (
            isinstance(flush_interval, bool)
            or not isinstance(flush_interval, (int, float))
            or flush_interval <= 0
        ):
            raise ValueError(
                f"flush_interval должен быть числом > 0, получено {flush_interval!r}"
            )
        self.db_path = Path(db_path)
        self._batch_size = batch_size
        self._flush_interval = float(flush_interval)
        self._lock = threading.Lock()
        self._logs: list[tuple[Any, ...]] = []
        self._clicks: list[tuple[Any, ...]] = []
        self._network: list[tuple[Any, ...]] = []
        self._conn = sqlite3.connect(
            self.db_path,
            timeout=BUSY_TIMEOUT_MS / 1000,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        self._state: StateStore | None = StateStore(self.db_path)
        self._dropped = 0
        self._last_error: str | None = None
        self._closed = False
        self._stop = threading.Event()
        self._flusher = threading.Thread(
            target=self._flush_loop, name="store-flusher", daemon=True
        )
        self._flusher.start()

    # --- чтение состояния ------------------------------------------------

    @property
    def dropped(self) -> int:
        """Число записей, потерянных из-за ошибок записи и записи после close."""
        with self._lock:
            return self._dropped

    @property
    def last_error(self) -> str | None:
        """Текст последней ошибки записи. None — ошибок ещё не было."""
        with self._lock:
            return self._last_error

    # --- частый путь: буферизация ----------------------------------------

    def log(
        self,
        level: str = "INFO",
        category: str = "",
        message: str = "",
        browser_id: str | None = None,
        fields: str | dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> None:
        stamp = time.time() if ts is None else ts
        payload = self._coerce_fields(fields)
        with self._lock:
            if self._closed:
                self._record_loss_locked(1, "writer закрыт: запись в logs отброшена")
                return
            self._logs.append((stamp, level, browser_id, category, message, payload))
            if len(self._logs) + len(self._clicks) + len(self._network) >= self._batch_size:
                self._flush_locked()

    def record_click(
        self,
        url: str,
        query: str | None = None,
        category: str | None = None,
        browser_id: str | None = None,
        proxy_id: int | None = None,
        http_status: int | None = None,
        ts: float | None = None,
    ) -> None:
        stamp = time.time() if ts is None else ts
        with self._lock:
            if self._closed:
                self._record_loss_locked(1, "writer закрыт: запись в clicks отброшена")
                return
            self._clicks.append(
                (stamp, url, query, category, browser_id, proxy_id, http_status)
            )
            if len(self._logs) + len(self._clicks) + len(self._network) >= self._batch_size:
                self._flush_locked()

    def record_network_request(
        self,
        method: str,
        url: str,
        resource_type: str | None = None,
        status: int | None = None,
        browser_id: str | None = None,
        ts: float | None = None,
    ) -> None:
        """Буферизовать строку ``network_requests`` — источник запросов/час.

        Делит буфер, батч и таймер с логами и кликами и соблюдает ту же
        политику ошибок: запись не роняет вызывающего, потерянный батч идёт
        в ``dropped``/``last_error``. Новое соединение не открывается —
        идёт в тот же writer, что и остальные записи процесса.
        """
        stamp = time.time() if ts is None else ts
        with self._lock:
            if self._closed:
                self._record_loss_locked(
                    1, "writer закрыт: запись в network_requests отброшена"
                )
                return
            self._network.append(
                (stamp, browser_id, method, url, resource_type, status)
            )
            if len(self._logs) + len(self._clicks) + len(self._network) >= self._batch_size:
                self._flush_locked()

    def count_network_requests(
        self,
        since: float,
        *,
        until: float | None = None,
        browser_id: str | None = None,
    ) -> int:
        """Число запросов в скользящем окне ``[since, until]``.

        Границы включительно; ``until=None`` — от ``since`` до конца истории,
        ``browser_id`` — фильтр по воркеру (без него считаются все). Окно
        считается на том же соединении, что и запись: перед запросом writer
        сбрасывает свой буфер, чтобы только что записанные запросы попали в
        счёт. Конкурентные читатели уживаются за счёт ``busy_timeout`` —
        отдельных блокировок нет.

        Ошибка чтения не бросает: возвращается 0, причина остаётся в
        ``last_error``. Счётчик ``dropped`` при этом не растёт — терялись
        записи, а не чтения.
        """
        with self._lock:
            if self._closed:
                self._record_loss_locked(0, "count_network_requests: writer закрыт")
                return 0
            self._flush_locked()
            sql = "SELECT COUNT(*) FROM network_requests WHERE ts >= ?"
            params: list[Any] = [since]
            if until is not None:
                sql += " AND ts <= ?"
                params.append(until)
            if browser_id is not None:
                sql += " AND browser_id = ?"
                params.append(browser_id)
            try:
                row = self._conn.execute(sql, params).fetchone()
            except Exception as exc:
                self._record_loss_locked(0, f"count_network_requests: {exc}")
                return 0
            return int(row[0]) if row is not None else 0

    def flush(self) -> None:
        """Сбросить накопленное в БД. После close() — no-op, не исключение."""
        with self._lock:
            if self._closed:
                return
            self._flush_locked()

    # --- heartbeat: немедленная запись -----------------------------------

    def heartbeat(self, browser_id: str, now: float | None = None) -> None:
        stamp = time.time() if now is None else now
        with self._lock:
            if self._closed:
                self._record_loss_locked(1, "writer закрыт: heartbeat отброшен")
                return
            try:
                self._conn.execute(_HEARTBEAT_UPSERT, (browser_id, stamp, stamp))
                self._conn.commit()
            except Exception as exc:
                self._rollback_quietly_locked()
                self._record_loss_locked(1, f"heartbeat {browser_id}: {exc}")

    def mark_degraded(self, browser_id: str, reason: str) -> None:
        """Пометить воркер деградировавшим: ``status='degraded'``, ``last_error``.

        Немедленная запись, как у heartbeat: сигнал о нерабочем прокси/CDP не
        должен ждать батча, иначе супервизор увидит его уже после смерти
        воркера. Существующая строка меняется только в двух колонках — ``pid``,
        ``started_at``, ``restart_count`` и ``heartbeat_at`` остаются как их
        поставил супервизор: воркер не знает ни жизни процесса, ни числа
        рестартов. Повторный вызов — не ошибка: причина заменяется на новую,
        строка не дублируется. Политика ошибок та же, что у heartbeat.
        """
        stamp = time.time()
        with self._lock:
            if self._closed:
                self._record_loss_locked(1, "writer закрыт: mark_degraded отброшен")
                return
            try:
                self._conn.execute(_DEGRADED_UPSERT, (browser_id, stamp, reason, stamp))
                self._conn.commit()
            except Exception as exc:
                self._rollback_quietly_locked()
                self._record_loss_locked(1, f"mark_degraded {browser_id}: {exc}")

    # --- запуски: делегирование StateStore --------------------------------

    def start_run(self, worker_id: int) -> int | None:
        """Id строки runs. None — записать не удалось, детали в last_error."""
        state = self._state
        if state is None:
            with self._lock:
                self._record_loss_locked(1, "start_run: хранилище запусков недоступно")
            return None
        try:
            return state.start_run(worker_id)
        except Exception as exc:
            with self._lock:
                self._record_loss_locked(1, f"start_run: {exc}")
            return None

    def finish_run(self, run_id: int, status: str, error: str | None = None) -> None:
        state = self._state
        if state is None:
            with self._lock:
                self._record_loss_locked(1, "finish_run: хранилище запусков недоступно")
            return
        try:
            state.finish_run(run_id, status, error)
        except Exception as exc:
            with self._lock:
                self._record_loss_locked(1, f"finish_run: {exc}")

    def add_run_counts(
        self,
        run_id: int,
        clicks: int = 0,
        captcha_seen: int = 0,
        captcha_solved: int = 0,
    ) -> None:
        state = self._state
        if state is None:
            with self._lock:
                self._record_loss_locked(1, "add_run_counts: хранилище запусков недоступно")
            return
        try:
            state.add_run_counts(run_id, clicks, captcha_seen, captcha_solved)
        except Exception as exc:
            with self._lock:
                self._record_loss_locked(1, f"add_run_counts: {exc}")

    # --- завершение ---------------------------------------------------------

    def close(self) -> None:
        """Сбросить остаток и закрыть соединение. Повторный вызов безопасен."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        if threading.current_thread() is not self._flusher:
            self._flusher.join(timeout=10)
        with self._lock:
            self._flush_locked()
            try:
                self._conn.close()
            except Exception as exc:
                self._record_loss_locked(0, f"close: {exc}")

    # --- внутренности --------------------------------------------------------

    def _flush_loop(self) -> None:
        # flush() сам гасит все ошибки хранения в счётчики, поэтому цикл
        # не нуждается в собственной обработке: любое исключение здесь было
        # бы багом, а не ошибкой БД, и молчать о нём нельзя.
        while not self._stop.wait(self._flush_interval):
            self.flush()

    def _flush_locked(self) -> None:
        """Сброс под блокировкой. Ошибки — в счётчики, не наружу."""
        logs = self._logs
        clicks = self._clicks
        network = self._network
        if not logs and not clicks and not network:
            return
        self._logs = []
        self._clicks = []
        self._network = []
        try:
            if logs:
                self._conn.executemany(_LOG_INSERT, logs)
            if clicks:
                self._conn.executemany(_CLICK_INSERT, clicks)
            if network:
                self._conn.executemany(_NETWORK_INSERT, network)
            self._conn.commit()
        except Exception as exc:
            self._rollback_quietly_locked()
            self._record_loss_locked(len(logs) + len(clicks) + len(network), str(exc))

    def _rollback_quietly_locked(self) -> None:
        # Откат после упавшего executemany — best effort: соединение может
        # быть уже закрыто, тогда фиксируем и это, но исходная ошибка важнее
        # и уже учтена вызывающим кодом отдельно.
        try:
            self._conn.rollback()
        except Exception as rollback_exc:
            self._record_loss_locked(0, f"rollback после ошибки записи: {rollback_exc}")

    def _record_loss_locked(self, count: int, message: str) -> None:
        self._dropped += count
        self._last_error = message

    @staticmethod
    def _coerce_fields(fields: str | dict[str, Any] | None) -> str | None:
        if fields is None or isinstance(fields, str):
            return fields
        try:
            return json.dumps(fields, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return repr(fields)
