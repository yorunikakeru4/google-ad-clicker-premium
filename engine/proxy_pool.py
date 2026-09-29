"""Репозиторий прокси: строки из UI и файла, дедуп, join для списка, использование.

Модуль — владелец таблиц ``proxies`` и ``proxy_usage`` в control plane.
Назначение и ротацию прокси делает супервизор (другая ветка), поэтому здесь
только то, что нужно обоим: хранение, чтение списка и запись использования.

Четыре решения, которые стоит знать:

1. **Соединение на операцию.** Тот же приём, что в ``StateStore``:
   ``sqlite3.Connection`` не переносится между потоками, а health-проверка
   пишет результаты из N потоков. Новое соединение на операцию стоит
   микросекунд, ``busy_timeout`` из ``migrations.connect`` гасит конкуренцию
   за запись, а внутренняя блокировка сериализует пачки внутри процесса.
2. **Дедуп по ``host:port + username``, а не по строке.** Ключ совпадает с
   ``UNIQUE (host, port, username)`` в схеме, но проверяется кодом: в SQLite
   ``NULL`` в ``UNIQUE`` считается уникальным, и два одинаковых прокси без
   кредов прошли бы как разные. Проверка идёт внутри одной транзакции, поэтому
   дубликаты ловятся и против БД, и внутри одной пачки.
3. **Пачка атомарна.** Непредвиденная ошибка посреди импорта откатывает всё:
   наполовину импортированный список выглядит в UI как «всё добавлено, но
   чего-то не хватает» и хуже явной ошибки.
4. **Креды существуют в двух видах.** ``list_proxies()`` маскирует
   ``username``/``password`` (это единственный список, который уходит в
   HTTP), а ``get()`` и ``list_for_check()`` отдают настоящие значения —
   без них супервизор не назначит прокси воркеру, а health-проверка не
   пришлёт ``Proxy-Authorization``. Сообщения об ошибках строятся без
   обращения к значениям кредов вообще.

``assigned_browser_id`` считает только живого воркера (``starting``,
``running``, ``backoff``): строка остановленного воркера вида
``stopped``/``circuit_open`` не держит прокси, и показывать её как
«назначение» значило бы запрещать удаление того, что уже свободно. Тот же
условие используется в ``delete()``, поэтому список и 409 не расходятся.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from engine.control_plane.config import SECRET_MASK
from engine.db import migrations

# Схемы, которые понимает пул. Порт по умолчанию не подставляется: строка
# без порта — это ошибка ввода, а не прокси, который «сам как-нибудь».
SUPPORTED_SCHEMES = ("http", "https", "socks4", "socks5")

MIN_PORT = 1
MAX_PORT = 65535

# Единственная формулировка для неразбранной строки: в ней нет ни куска
# входных данных, поэтому пароль из кривой строки не может утечь в ответ.
_FORMAT_ERROR = (
    "не удалось разобрать строку: ожидается [scheme://]host:port "
    "или user:password@host:port"
)

# Статусы воркера, при которых прокси считается занятым.
_ALIVE_WORKER_STATUSES = ("starting", "running", "backoff")


class ProxyError(Exception):
    """Базовая ошибка подсистемы прокси.

    HTTP-слой переводит подклассы в коды ответа через ``_ERROR_STATUS``,
    а текст исключения уходит в JSON наружу — поэтому он собирается без
    значений ``username``/``password``.
    """


class ProxyInUseError(ProxyError):
    """Прокси назначен живому воркеру — удаление невозможно (409)."""


class ProxyNotFoundError(ProxyError):
    """Строки с таким идентификатором в БД нет (404)."""


class ProxyImportError(ProxyError):
    """Файл для импорта не задан, не найден или не читается (400)."""


@dataclass(frozen=True)
class ParsedProxy:
    """Разобранная строка прокси. Пустые креды — ``None``, а не пустая строка."""

    scheme: str
    host: str
    port: int
    username: str | None
    password: str | None


def parse_proxy_line(line: str) -> ParsedProxy:
    """Разбирает ``[scheme://][user:password@]host:port``.

    ``ValueError`` — единственный исход ошибки, и его текст не содержит ни
    строки, ни её частей: сообщение уходит в HTTP-ответ и в ``problems``,
    а строка целиком может быть паролем, вставленным не в то поле.
    """
    if not isinstance(line, str):
        raise ValueError(_FORMAT_ERROR)

    text = line.strip()
    # Кавычки вокруг значения: файлы списков нередко выгружают так, а legacy
    # proxy.get_proxies их снимает. Снимаются только крайние пары —
    # одиночная кавычка внутри пароля остаётся частью пароля.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()

    scheme = "http"
    if "://" in text:
        candidate, _, text = text.partition("://")
        if candidate.lower() not in SUPPORTED_SCHEMES:
            raise ValueError(
                "неподдерживаемая схема прокси: допустимы " + ", ".join(SUPPORTED_SCHEMES)
            )
        scheme = candidate.lower()

    username: str | None = None
    password: str | None = None
    if "@" in text:
        if text.count("@") != 1:
            raise ValueError(_FORMAT_ERROR)
        credentials, _, host_port = text.partition("@")
        if ":" not in credentials:
            raise ValueError(_FORMAT_ERROR)
        username, _, password = credentials.partition(":")
        if not username or not password:
            raise ValueError(_FORMAT_ERROR)
    else:
        host_port = text

    if host_port.count(":") != 1:
        raise ValueError(_FORMAT_ERROR)
    host, _, port_text = host_port.partition(":")
    if not host:
        raise ValueError(_FORMAT_ERROR)
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(f"номер порта должен быть числом от {MIN_PORT} до {MAX_PORT}") from None
    if not MIN_PORT <= port <= MAX_PORT:
        raise ValueError(f"номер порта должен быть числом от {MIN_PORT} до {MAX_PORT}")

    return ParsedProxy(
        scheme=scheme, host=host, port=port, username=username, password=password
    )


def record_usage(
    db_path: str | Path,
    *,
    proxy_id: int,
    browser_id: str | None = None,
    result: str | None = None,
    latency_ms: int | None = None,
    ts: float | None = None,
) -> int:
    """Пишет строку в ``proxy_usage``. Точка входа для супервизора.

    Вызывается на каждое использование прокси воркером; ``browser_id``
    обязателен по смыслу (иначе счётчик в UI не с чем связать), но схема
    его не требует, поэтому проверка — на вызывающей стороне.

    Запись идёт отдельным соединением: супервизор и health-проверка работают
    в разных потоках, а ``busy_timeout`` уже выставлен ``migrations.connect``.
    """
    stamp = time.time() if ts is None else ts
    conn = migrations.connect(db_path)
    try:
        try:
            cursor = conn.execute(
                "INSERT INTO proxy_usage (ts, proxy_id, browser_id, result, latency_ms) "
                "VALUES (?, ?, ?, ?, ?)",
                (stamp, proxy_id, browser_id, result, latency_ms),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            # Единственное нарушение, возможное здесь, — внешний ключ
            # proxy_id: прокси удалили между выбором и записью.
            raise ProxyNotFoundError(
                "прокси не найден, использование не записано"
            ) from exc
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


class ProxyPool:
    """CRUD над ``proxies`` с учётом воркеров и использований.

    Экземпляр потокобезопасен: состояния соединения нет, а запись
    сериализуется внутренней блокировкой — health-проверка пишет результаты
    из нескольких потоков, HTTP-запросы приходят из потоков сервера.
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

    # --- добавление ------------------------------------------------------

    def add_lines(self, lines: Sequence[Any]) -> dict[str, Any]:
        """Добавляет строки, пропуская кривые и дубликаты.

        Ошибка одной строки не роняет пачку: пользователь импортирует файл
        на 84 строки, и одна опечатка не должна стоить остальных 83.
        Непредвиденное исключение посередине, наоборот, откатывает всё —
        см. докстринг модуля.

        Возвращает ровно то, что отдаёт ``POST /control/proxies``:
        ``{"added", "skipped", "problems": [{"line_index", "message"}]}``.
        Пустые строки не считаются ни добавленными, ни пропущенными.
        """
        added = 0
        skipped = 0
        problems: list[dict[str, Any]] = []

        with self._lock, self._connect() as conn:
            try:
                for index, line in enumerate(lines):
                    if not isinstance(line, str):
                        problems.append(
                            {"line_index": index, "message": "строка должна быть текстом"}
                        )
                        skipped += 1
                        continue
                    if not line.strip():
                        continue
                    try:
                        parsed = parse_proxy_line(line)
                    except ValueError as exc:
                        problems.append({"line_index": index, "message": str(exc)})
                        skipped += 1
                        continue
                    if self._insert(conn, parsed):
                        added += 1
                    else:
                        problems.append(
                            {
                                "line_index": index,
                                "message": (
                                    f"дубликат: {parsed.host}:{parsed.port} "
                                    "с тем же пользователем уже в пуле"
                                ),
                            }
                        )
                        skipped += 1
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        return {"added": added, "skipped": skipped, "problems": problems}

    def import_file(self, path: str | Path) -> dict[str, Any]:
        """Импортирует файл со списком прокси.

        Путь приходит только из конфига (``paths.proxy_file``) — запрос его
        передавать не может, иначе файловым полём открыли бы чтение чего
        угодно. Относительные пути разрешаются от текущего каталога, как в
        legacy ``proxy.get_proxies``.
        """
        source = Path(path)
        if not source.is_file():
            raise ProxyImportError(f"файл прокси не найден: {source}")
        try:
            text = source.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ProxyImportError(f"файл прокси не в кодировке UTF-8: {source}") from exc
        except OSError as exc:
            raise ProxyImportError(f"не удалось прочитать файл прокси: {source}") from exc
        return self.add_lines(text.splitlines())

    def _insert(self, conn: sqlite3.Connection, parsed: ParsedProxy) -> bool:
        """Вставляет строку; False — дубликат (в т.ч. гонка с другим писателем)."""
        if self._find_id(conn, parsed) is not None:
            return False
        cursor = conn.execute(
            "INSERT OR IGNORE INTO proxies (scheme, host, port, username, password) "
            "VALUES (?, ?, ?, ?, ?)",
            (parsed.scheme, parsed.host, parsed.port, parsed.username, parsed.password),
        )
        return cursor.rowcount != 0

    @staticmethod
    def _find_id(conn: sqlite3.Connection, parsed: ParsedProxy) -> int | None:
        row = conn.execute(
            "SELECT id FROM proxies WHERE host = ? AND port = ? "
            "AND COALESCE(username, '') = COALESCE(?, '') LIMIT 1",
            (parsed.host, parsed.port, parsed.username),
        ).fetchone()
        return None if row is None else int(row["id"])

    # --- чтение ----------------------------------------------------------

    def list_proxies(self) -> list[dict[str, Any]]:
        """Список для HTTP: креды замаскированы, воркер и счётчик — join'ом.

        ``usage_count`` считается подзапросом, а не отдельным SELECT: список
        обязан собираться одним проходом, иначе счётчик успел бы вырасти между
        чтением прокси и чтением использований.
        """
        sql = f"""
            SELECT p.id, p.label, p.scheme, p.host, p.port, p.username, p.password,
                   p.country, p.latency_ms, p.is_alive, p.fail_count,
                   p.last_checked_at, p.last_error,
                   (SELECT w.browser_id FROM workers w
                     WHERE w.proxy_id = p.id
                       AND w.status IN ({_status_list()})
                     ORDER BY w.id LIMIT 1) AS assigned_browser_id,
                   (SELECT COUNT(*) FROM proxy_usage u WHERE u.proxy_id = p.id)
                       AS usage_count
            FROM proxies p
            ORDER BY p.id
        """
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [self._mask(dict(row)) for row in rows]

    @staticmethod
    def _mask(row: dict[str, Any]) -> dict[str, Any]:
        """Креды → ``SECRET_MASK``; пустые остаются пустыми, как в контракте."""
        for key in ("username", "password"):
            if row.get(key):
                row[key] = SECRET_MASK
        return row

    def get(self, proxy_id: Any) -> dict[str, Any] | None:
        """Строка с настоящими кредами — для назначения и health-проверки."""
        if isinstance(proxy_id, bool) or not isinstance(proxy_id, int):
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM proxies WHERE id = ?", (proxy_id,)).fetchone()
        return None if row is None else dict(row)

    def list_for_check(self) -> list[dict[str, Any]]:
        """Минимальный набор полей с настоящими кредами для health-проверки."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, host, port, username, password FROM proxies ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    # --- удаление --------------------------------------------------------

    def delete(self, proxy_id: Any) -> bool:
        """Удаляет прокси; ``ProxyInUseError``, если его держит живой воркер.

        Проверка идёт внутри той же транзакции, что и удаление: между ними
        супервизор не успеет назначить прокси воркеру.
        """
        if isinstance(proxy_id, bool) or not isinstance(proxy_id, int):
            raise ProxyNotFoundError("прокси не найден")

        with self._lock, self._connect() as conn:
            try:
                row = conn.execute(
                    "SELECT 1 FROM proxies WHERE id = ?", (proxy_id,)
                ).fetchone()
                if row is None:
                    raise ProxyNotFoundError(f"прокси id={proxy_id} не найден")
                holder = conn.execute(
                    f"""
                    SELECT w.browser_id FROM workers w
                    WHERE w.proxy_id = ? AND w.status IN ({_status_list()})
                    ORDER BY w.id LIMIT 1
                    """,
                    (proxy_id,),
                ).fetchone()
                if holder is not None:
                    raise ProxyInUseError(
                        f"прокси id={proxy_id} назначен воркеру "
                        f"{holder['browser_id']} и не может быть удалён"
                    )
                conn.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return True

    # --- health-проверка -------------------------------------------------

    def record_check_result(
        self,
        proxy_id: int,
        *,
        alive: bool,
        latency_ms: int | None = None,
        error: str | None = None,
        now: float | None = None,
    ) -> None:
        """Фиксирует результат одной проверки.

        Политика ``fail_count``: счётчик считает проверки, не прошедшие
        **подряд**, и обнуляется первым успехом — в ``last_error`` остаётся
        причина последней неудачи, а прокси, снова ответивший 200, сразу
        возвращается в ротацию. Накопительный счётчик сделал бы живой прокси
        «виноватым» навсегда и прятал бы причину, по которой он вообще числится
        мёртвым. ``latency_ms`` при неудаче не трогается: это последнее
        измеренное значение, а не результат провальной попытки.

        Результат пишется по мере проверки (а не по её завершении), поэтому
        ``GET /control/proxies`` показывает прогресс: у уже проверенных
        прокси обновился ``last_checked_at``.
        """
        stamp = time.time() if now is None else now
        with self._lock, self._connect() as conn:
            if alive:
                conn.execute(
                    "UPDATE proxies SET is_alive = 1, latency_ms = ?, last_checked_at = ?, "
                    "last_error = NULL, fail_count = 0 WHERE id = ?",
                    (latency_ms, stamp, proxy_id),
                )
            else:
                conn.execute(
                    "UPDATE proxies SET is_alive = 0, last_checked_at = ?, last_error = ?, "
                    "fail_count = fail_count + 1 WHERE id = ?",
                    (stamp, error, proxy_id),
                )
            conn.commit()

    # --- использование ---------------------------------------------------

    def record_usage(
        self,
        proxy_id: int,
        *,
        browser_id: str | None = None,
        result: str | None = None,
        latency_ms: int | None = None,
        ts: float | None = None,
    ) -> int:
        """Запись использования через этот пул (см. :func:`record_usage`)."""
        return record_usage(
            self.db_path,
            proxy_id=proxy_id,
            browser_id=browser_id,
            result=result,
            latency_ms=latency_ms,
            ts=ts,
        )


def _status_list() -> str:
    """Список живых статусов воркера для SQL-шаблона.

    Функция, а не строковая константа: значения фиксированы в
    ``_ALIVE_WORKER_STATUSES``, и расхождение между списком и проверкой
    в ``delete()`` невозможно по построению.
    """
    return ", ".join(f"'{status}'" for status in _ALIVE_WORKER_STATUSES)
