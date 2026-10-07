"""Репозиторий профилей: CRUD, импорт и машина статусов.

Модуль владелец таблицы ``profiles`` в control plane. Назначение профилей
воркерам живёт здесь же (``take_for_worker``/``release``), потому что статус
и ссылка ``workers.profile_id`` обязаны меняться в одной транзакции: иначе
пул показал бы профиль свободным, а воркер — назначенным, и профиль (то есть
аккаунт) достался бы двум браузерам сразу.

Четыре решения, которые стоит знать при чтении:

1. **Статус — машина, а не строка.** Пять значений ``free|assigned|active|
   error|blocked`` и явные переходы. Системные переходы (выдача, работа,
   освобождение) делает только этот модуль, оператору через API доступны
   ``free|blocked|error``. Недопустимый переход — ``ValueError``
   (:class:`ProfileInvalidError`): HTTP-слой переводит его в 400, а прямой
   вызов из кода получает исключение вместо тихой порчи состояния.
2. **Связка «профиль ↔ воркер» двусторонняя.** ``profiles.status = assigned``
   без ``workers.profile_id`` и наоборот — рассинхрон, из-за которого реапер и
   выдача начали бы спорить. Поэтому выдача ставит статус И ссылку, а
   :meth:`ProfilePool.reap` освобождает статус, но ссылку сознательно
   оставляет: это память о прошлом назначении, благодаря которой воркер,
   поднятый после падения, получает тот же профиль. Ссылку гасит выдача
   профиля другому воркеру, удаление строки (FK ``ON DELETE SET NULL``) и
   полный релиз.
3. **Живой держатель важнее статуса.** Оператор вправе сбросить профиль в
   ``free``, пока воркер работает с ним же. Поэтому и выдача, и назначение
   диапазона дополнительно проверяют, не ссылается ли на профиль живой воркер
   (``starting|running|backoff|degraded``) — тот же список, что в
   ``proxy_pool``: строка ``stopped``/``circuit_open`` ничего не держит.
4. **Импорт — построчный, а не «всё или ничего».** Формат строки
   ``key_ref`` либо ``key_ref | name``, а строка, значение которой
   начинается с ``Mozilla/``, — это User-Agent: она заводит профиль с
   колонкой ``user_agent`` (``key_ref`` остаётся NULL) и именем ``UA-N``.
   Простейший случай (одна строка = ключ) заводит профиль с уникально
   сгенерированным именем, дубликаты и кривые строки уходят в ``problems``
   и не роняют пачку из 300+ строк — та же политика, что у импорта прокси.

``key_ref`` — ссылка на ключ, а не секрет: он присутствует в HTTP-ответах и
в ``problems``, но никогда не пишется в ``logs`` (это делает вызывающая
сторона, здесь поле просто не передаётся в логи).
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from engine.db import migrations

# Все статусы профиля — контракт UI и воркера (план.md, §5 «Фаза 6»).
PROFILE_STATUSES = ("free", "assigned", "active", "error", "blocked")

# Статусы, которые оператор может выставить через API. assigned/active —
# системные: их ставит только выдача и работа воркера, API их отклоняет (400).
OPERATOR_STATUSES = ("free", "blocked", "error")

# Статусы воркера, при которых он считается живым держателем профиля.
# Список совпадает с proxy_pool._ALIVE_WORKER_STATUSES: строка stopped или
# circuit_open не держит ни прокси, ни профиль.
_ALIVE_WORKER_STATUSES = ("starting", "running", "backoff", "degraded")

# Разрешённые переходы машины статусов. Ключи — из PROFILE_STATUSES:
#
#   free     → назначение пулом либо решение оператора;
#   assigned → работа воркера (active), снятие назначения, решение оператора;
#   active   → конец работы (assigned либо error), снятие, решение оператора;
#   error    → оператор вернул профиль в работу (free) либо пометил blocked;
#   blocked  → только оператор (free|error): пул никогда не берёт такой профиль.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "free": frozenset({"assigned", "blocked", "error"}),
    "assigned": frozenset({"active", "free", "blocked", "error"}),
    "active": frozenset({"assigned", "free", "blocked", "error"}),
    "error": frozenset({"free", "blocked"}),
    "blocked": frozenset({"free", "error"}),
}

# Имя профиля, сгенерированное импортом User-Agent: ровно «UA-» и число.
# Суффикс таких имён — верхняя граница для следующего номера: существующее
# «UA-9» даёт нумерацию с 10, а имя вида «UA-9 backup» не считается.
_UA_NAME_RE = re.compile(r"UA-(\d+)")


class ProfileError(Exception):
    """Базовая ошибка подсистемы профилей.

    HTTP-слой переводит подклассы в коды ответа через ``_ERROR_STATUS``, а
    текст исключения уходит в JSON наружу — поэтому он собирается без
    значений ``key_ref``.
    """


class ProfileNotFoundError(ProfileError):
    """Строки с таким идентификатором в БД нет (404)."""


class ProfileInUseError(ProfileError):
    """Профиль назначен живому воркеру — удаление невозможно (409)."""


class ProfileInvalidError(ProfileError, ValueError):
    """Недопустимое значение или переход (400).

    Наследуется от ``ValueError`` не ради красивого имени: контракт требует,
    чтобы недопустимый переход был ``ValueError``, — а ``ProfileError``
    нужен HTTP-слою, чтобы вместо 500 отдать 400 с текстом ошибки.
    """


class ProfileImportError(ProfileError):
    """Файл для импорта не задан, не найден или не читается (400)."""


class ProfilePool:
    """CRUD над ``profiles`` с учётом воркеров.

    Экземпляр потокобезопасен: состояния соединения нет, а запись
    сериализуется внутренней блокировкой — выдача и назначение диапазона
    читают и пишут в одном заходе, а HTTP-запросы приходят из потоков сервера.
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

    def add_profiles(self, records: Sequence[Any]) -> dict[str, Any]:
        """Добавляет записи из ``POST /control/profiles``.

        Ошибка одной записи не роняет пачку: оператор завозил 40 профилей, и
        одна опечатка в имени не должна стоить остальных 39. Непредвиденное
        исключение посередине, наоборот, откатывает всё (см. докстринг
        ``proxy_pool.add_lines``: та же политика в обоих пулах).

        Возвращает ровно то, что уходит в HTTP: ``{"added", "skipped",
        "problems": [{"index", "message"}]}``.
        """
        added = 0
        skipped = 0
        problems: list[dict[str, Any]] = []

        with self._lock, self._connect() as conn:
            try:
                for index, record in enumerate(records):
                    if not isinstance(record, dict):
                        problems.append(
                            {"index": index, "message": "запись должна быть объектом"}
                        )
                        skipped += 1
                        continue
                    values, error = self._prepare_record(conn, record)
                    if values is None or error is not None:
                        problems.append(
                            {"index": index, "message": error or "запись не прошла проверку"}
                        )
                        skipped += 1
                        continue
                    if self._insert(conn, values):
                        added += 1
                    else:
                        problems.append(
                            {"index": index, "message": f"дубликат: имя {values['name']!r} уже есть"}
                        )
                        skipped += 1
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        return {"added": added, "skipped": skipped, "problems": problems}

    def import_lines(self, lines: Sequence[Any]) -> dict[str, Any]:
        """Импортирует строки из ``POST /control/profiles/import``.

        Формат строки (зафиксирован здесь, UI под него подстраивается):

        * ``key_ref`` — простейший случай: имя генерируется из ``key_ref``
          и при занятости получает суффикс ``-2``, ``-3``...;
        * ``key_ref | name`` — явное имя, оно должно быть уникальным;
        * значение, начинающееся с ``Mozilla/``, — User-Agent: в профиль
          уходит колонка ``user_agent`` (``key_ref`` остаётся NULL), а имя —
          явное после ``|`` либо следующий свободный номер ``UA-N``
          (``n = 1 + max`` суффиксов имён, целиком состоящих из ``UA-`` и
          числа, дальше только растёт: пока имя ``UA-n`` занято — ``n += 1``).

        Перед разбором строка режется по первому табу: первая колонка —
        значение, остальное — хвост из таблицы, вставленной целиком.

        Дубликат ``key_ref`` и дубликат явного имени (в БД или внутри
        пачки), а для User-Agent — дубликат по ``user_agent``, уходят в
        ``problems``; пустые строки не считаются ни добавленными, ни
        пропущенными. Возвращает ``{"added", "skipped", "problems":
        [{"line_index", "message"}]}``.
        """
        added = 0
        skipped = 0
        problems: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        seen_user_agents: set[str] = set()
        # Номер UA-N считается один раз на пачку при первой же
        # сгенерированной строке и дальше отслеживается здесь: БД в этой же
        # транзакции видит уже вставленные имена, но пересчитывать максимум
        # на каждую строку — лишний проход по всей таблице.
        ua_number: int | None = None

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
                    value, explicit_name, error = parse_import_line(line)
                    if error is not None or not value:
                        problems.append(
                            {"line_index": index, "message": error or "строка пустая"}
                        )
                        skipped += 1
                        continue
                    if value.startswith("Mozilla/"):
                        if value in seen_user_agents or self._user_agent_exists(conn, value):
                            # Полный User-Agent в сообщение не попадает: оно
                            # уходит в HTTP-ответ, а строка — идентификатор
                            # аккаунта, цитировать её целиком незачем.
                            problems.append(
                                {
                                    "line_index": index,
                                    "message": "дубликат: user_agent уже есть",
                                }
                            )
                            skipped += 1
                            continue
                        # Номер израсходован только на успешно вставленной
                        # строке: упавшая вставка оставляет его текущим.
                        taken_number: int | None = None
                        if explicit_name is None:
                            if ua_number is None:
                                ua_number = _next_ua_number(conn)
                            while self._name_exists(conn, f"UA-{ua_number}"):
                                ua_number += 1
                            name = f"UA-{ua_number}"
                            taken_number = ua_number
                        else:
                            name = explicit_name
                            if self._name_exists(conn, name):
                                problems.append(
                                    {
                                        "line_index": index,
                                        "message": f"дубликат: имя {name!r} уже есть",
                                    }
                                )
                                skipped += 1
                                continue
                        if not self._insert(conn, {"name": name, "user_agent": value}):
                            problems.append(
                                {"line_index": index, "message": "не удалось вставить запись"}
                            )
                            skipped += 1
                            continue
                        if taken_number is not None:
                            ua_number = taken_number + 1
                        seen_user_agents.add(value)
                        added += 1
                        continue

                    if value in seen_keys or self._key_ref_exists(conn, value):
                        problems.append(
                            {
                                "line_index": index,
                                "message": f"дубликат: key_ref {value!r} уже есть",
                            }
                        )
                        skipped += 1
                        continue
                    name = explicit_name
                    if name is None:
                        name = self._unique_name(conn, value)
                    elif self._name_exists(conn, name):
                        problems.append(
                            {"line_index": index, "message": f"дубликат: имя {name!r} уже есть"}
                        )
                        skipped += 1
                        continue
                    if not self._insert(conn, {"name": name, "key_ref": value}):
                        problems.append(
                            {"line_index": index, "message": "не удалось вставить запись"}
                        )
                        skipped += 1
                        continue
                    seen_keys.add(value)
                    added += 1
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        return {"added": added, "skipped": skipped, "problems": problems}

    def import_file(self, path: str | Path) -> dict[str, Any]:
        """Импортирует файл со списком User-Agent/key_ref.

        Путь приходит только из конфига (``paths.user_agents``) — запрос его
        передавать не может, иначе файловым полём открыли бы чтение чего
        угодно. Относительные пути разрешаются от текущего каталога, как в
        ``proxy_pool.import_file``.
        """
        source = Path(path)
        if not source.is_file():
            raise ProfileImportError(f"файл user_agents не найден: {source}")
        try:
            text = source.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ProfileImportError(f"файл user_agents не в кодировке UTF-8: {source}") from exc
        except OSError as exc:
            raise ProfileImportError(f"не удалось прочитать файл user_agents: {source}") from exc
        return self.import_lines(text.splitlines())

    def _prepare_record(
        self, conn: sqlite3.Connection, record: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Проверяет запись и собирает значения колонок.

        ``(values, None)`` — запись годна к вставке, ``(None, текст)`` —
        причина, по которой она уходит в ``problems``.
        """
        name = record.get("name")
        if not isinstance(name, str) or not name.strip():
            return None, "поле name обязательно и должно быть непустым текстом"
        name = name.strip()
        if self._name_exists(conn, name):
            return None, f"дубликат: имя {name!r} уже есть"

        values: dict[str, Any] = {"name": name}

        key_ref = record.get("key_ref")
        if key_ref is not None:
            if not isinstance(key_ref, str):
                return None, "поле key_ref должно быть текстом или отсутствовать"
            values["key_ref"] = key_ref

        proxy_id = record.get("proxy_id")
        if proxy_id is not None:
            if isinstance(proxy_id, bool) or not isinstance(proxy_id, int):
                return None, "поле proxy_id должно быть целым числом или отсутствовать"
            if conn.execute("SELECT 1 FROM proxies WHERE id = ?", (proxy_id,)).fetchone() is None:
                return None, f"прокси id={proxy_id} не найден"
            values["proxy_id"] = proxy_id

        for field in ("user_agent", "locale", "timezone"):
            value = record.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                return None, f"поле {field} должно быть текстом или отсутствовать"
            values[field] = value

        fields = record.get("fields")
        if fields is not None:
            if not isinstance(fields, dict):
                return None, "поле fields должно быть JSON-объектом"
            values["fields"] = json.dumps(fields, ensure_ascii=False, sort_keys=True)

        return values, None

    def _insert(self, conn: sqlite3.Connection, values: dict[str, Any]) -> bool:
        """Вставляет запись. False — дубликат имени (в т.ч. гонка с другим писателем)."""
        columns = ["name", "status"]
        params: list[Any] = [values["name"], "free"]
        # status пишется явно, а не через DEFAULT: в БД, переживших миграцию
        # 002, default колонки остался 'new', а 'new' — не статус профиля.
        for column in ("key_ref", "proxy_id", "user_agent", "locale", "timezone", "fields"):
            if column in values:
                columns.append(column)
                params.append(values[column])
        if "fields" not in values:
            columns.append("fields")
            params.append("{}")
        placeholders = ", ".join("?" for _ in columns)
        try:
            cursor = conn.execute(
                f"INSERT INTO profiles ({', '.join(columns)}) VALUES ({placeholders})",
                params,
            )
        except sqlite3.IntegrityError:
            # Единственное нарушение, возможное здесь, — UNIQUE(name): строку
            # добавил другой процесс между проверкой и вставкой.
            return False
        return cursor.rowcount != 0

    @staticmethod
    def _name_exists(conn: sqlite3.Connection, name: str) -> bool:
        return conn.execute("SELECT 1 FROM profiles WHERE name = ?", (name,)).fetchone() is not None

    @staticmethod
    def _key_ref_exists(conn: sqlite3.Connection, key_ref: str) -> bool:
        return (
            conn.execute("SELECT 1 FROM profiles WHERE key_ref = ?", (key_ref,)).fetchone()
            is not None
        )

    @staticmethod
    def _user_agent_exists(conn: sqlite3.Connection, user_agent: str) -> bool:
        """Есть ли профиль с таким User-Agent — дедуп импорта по ``user_agent``."""
        return (
            conn.execute(
                "SELECT 1 FROM profiles WHERE user_agent = ?", (user_agent,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def _unique_name(conn: sqlite3.Connection, base: str) -> str:
        """Имя, гарантированно свободное в текущей БД (в т.ч. в этой пачке)."""
        candidate = base
        suffix = 2
        while ProfilePool._name_exists(conn, candidate):
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    # --- чтение ----------------------------------------------------------

    def list_profiles(self) -> list[dict[str, Any]]:
        """Список для HTTP: поля контракта, ``fields`` — объект, а не текст.

        ``assigned_browser_id`` считается только живым воркером, как в
        ``proxy_pool.list_proxies``: строка остановленного воркера не держит
        профиль, и показывать её как «назначение» значило бы запрещать
        удаление того, что уже свободно. Тот же условие используется в
        ``delete()``, поэтому список и 409 не расходятся.
        """
        sql = f"""
            SELECT p.id, p.name, p.key_ref, p.proxy_id, p.user_agent, p.locale,
                   p.timezone, p.status, p.last_used_at, p.fields,
                   (SELECT w.browser_id FROM workers w
                     WHERE w.profile_id = p.id
                       AND w.status IN ({_status_list()})
                     ORDER BY w.id LIMIT 1) AS assigned_browser_id
            FROM profiles p
            ORDER BY p.id
        """
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [_row_to_dict(row) for row in rows]

    def get_profile(self, profile_id: Any) -> dict[str, Any] | None:
        """Строка профиля для чтения. None — строки нет или id вне контракта.

        Read-only путь для воркера (:mod:`engine.profile_apply`): ему нужны
        настройки профиля, а не разрешение работать — никаких проверок
        статуса и владения здесь нет, их делает машина статусов в
        :meth:`mark_active`/`:meth:`mark_done`. Логика ``fields`` та же, что
        в :meth:`list_profiles`: повреждённый JSON не валит чтение.
        """

        if isinstance(profile_id, bool) or not isinstance(profile_id, int) or profile_id < 1:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        return None if row is None else _row_to_dict(row)

    def assigned_profile_ids(self) -> set[int]:
        """Id профилей, которые должны держать воркеры (``assigned``/``active``).

        Точка входа для реапера: один запрос на тик вместо выборки всех
        строк, дальше супервизор сравнивает множество с реально живыми
        держателями. Пустое множество означает «освобождать нечего», и тик не
        тратит ни одной записи.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM profiles WHERE status IN ('assigned', 'active') ORDER BY id"
            ).fetchall()
        return {int(row["id"]) for row in rows}

    # --- удаление --------------------------------------------------------

    def delete(self, profile_id: Any) -> None:
        """Удаляет профиль; :class:`ProfileInUseError`, если его держит живой воркер.

        Проверка идёт внутри той же транзакции, что и удаление: между ними
        выдача не успеет отдать профиль воркеру. ``key_ref`` в текст ошибки
        не попадает — сообщение уходит в HTTP-ответ.
        """
        if isinstance(profile_id, bool) or not isinstance(profile_id, int):
            raise ProfileNotFoundError("профиль не найден")

        with self._lock, self._connect() as conn:
            try:
                row = conn.execute(
                    "SELECT 1 FROM profiles WHERE id = ?", (profile_id,)
                ).fetchone()
                if row is None:
                    raise ProfileNotFoundError(f"профиль id={profile_id} не найден")
                holder = _holder(conn, profile_id)
                if holder is not None:
                    raise ProfileInUseError(
                        f"профиль id={profile_id} назначен воркеру "
                        f"{holder} и не может быть удалён"
                    )
                conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def delete_many(self, profile_ids: Sequence[Any]) -> dict[str, Any]:
        """Удаляет несколько профилей за одну транзакцию, не падая на занятых.

        Семантика — как у импорта и у ``proxy_pool.delete_many``:
        назначенные живому воркеру и отсутствующие строки не роняют
        операцию, а попадают в ``skipped``/``problems``. Иначе один занятый
        профиль заблокировал бы «удалить все с ошибкой». Проверка
        назначения и удаление каждого id идут в одной транзакции — воркер
        не успеет получить профиль между ними.

        Дубли схлопываются с сохранением порядка: повторно запрошенный id
        не обязан исчезнуть из отчёта как «не найден». Пустой список —
        no-op, валидация тела (непустой список целых чисел) живёт в
        HTTP-слое.
        """
        if not profile_ids:
            return {"deleted": 0, "skipped": 0, "problems": []}

        deleted = 0
        problems: list[str] = []
        seen: set[int] = set()

        with self._lock, self._connect() as conn:
            try:
                for profile_id in profile_ids:
                    if isinstance(profile_id, bool) or not isinstance(profile_id, int):
                        problems.append(f"id={profile_id!r}: неверный идентификатор")
                        continue
                    if profile_id in seen:
                        continue
                    seen.add(profile_id)
                    row = conn.execute(
                        "SELECT 1 FROM profiles WHERE id = ?", (profile_id,)
                    ).fetchone()
                    if row is None:
                        problems.append(f"id={profile_id}: профиль не найден")
                        continue
                    holder = _holder(conn, profile_id)
                    if holder is not None:
                        problems.append(f"id={profile_id}: назначен воркеру {holder}")
                        continue
                    conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
                    deleted += 1
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        return {"deleted": deleted, "skipped": len(problems), "problems": problems}

    def all_ids(self) -> list[int]:
        """Все id пула по возрастанию — для удаления целиком.

        Отдельный запрос, а не ``list_profiles``: тому нужны воркер, поля и
        ``fields``, а для удаления нужен ровно список id.
        """
        with self._connect() as conn:
            rows = conn.execute("SELECT id FROM profiles ORDER BY id").fetchall()
        return [int(row["id"]) for row in rows]

    def delete_all(self) -> dict[str, Any]:
        """Удаляет весь пул; занятые живым воркером уходят в ``skipped``.

        Тот же best-effort, что и у батча (``delete_many``): удаление
        «всего» не должно падать из-за одного назначенного профиля —
        оператору важен и результат, и список того, что осталось. Пустой
        пул — no-op, а не ошибка: кнопка может быть нажата дважды.
        """
        return self.delete_many(self.all_ids())

    # --- машина статусов -------------------------------------------------

    def set_status(self, profile_id: Any, status: Any) -> dict[str, Any]:
        """Операторский переход: только ``free|blocked|error``.

        ``assigned|active`` системные — их ставит выдача и работа воркера,
        поэтому запись их через API отклоняется как :class:`ProfileInvalidError`
        (HTTP 400), а не молча принимается. Переход в ``free`` снимает
        статус, но не трогает ``workers.profile_id``: ссылка живого воркера
        защищает профиль от повторной выдачи (см. докстринг модуля).
        """
        if not isinstance(status, str) or status not in PROFILE_STATUSES:
            raise ProfileInvalidError(
                "статус должен быть одним из: " + "|".join(PROFILE_STATUSES)
            )
        if status not in OPERATOR_STATUSES:
            raise ProfileInvalidError(
                f"статус {status} назначается системой, оператору доступны: "
                + "|".join(OPERATOR_STATUSES)
            )

        with self._lock, self._connect() as conn:
            try:
                row = self._transition(conn, profile_id, status, allowed_from=PROFILE_STATUSES)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return row

    def mark_active(self, profile_id: Any, now: float | None = None) -> dict[str, Any]:
        """Воркер начал работать с профилем: ``assigned → active`` + ``last_used_at``.

        Заготовка для следующей волны: её вызывает воркер, когда реально
        открыл браузер с этим профилем. Отсюда и требование, чтобы профиль
        был назначен, — работать с чужим или свободным профилем нельзя.
        """
        stamp = time.time() if now is None else now
        with self._lock, self._connect() as conn:
            try:
                row = self._transition(
                    conn,
                    profile_id,
                    "active",
                    allowed_from=("assigned", "active"),
                    now=stamp,
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return row

    def mark_done(
        self, profile_id: Any, *, ok: bool = True, now: float | None = None
    ) -> dict[str, Any]:
        """Воркер закончил работу: успех — обратно в ``assigned``, иначе ``error``.

        Заготовка для следующей волны. ``assigned``, а не ``free``: env
        воркера выдаётся при спавне и живёт до перезапуска, поэтому после
        сценария профиль остаётся закреплённым за воркером, а освобождается
        только через :meth:`release`. ``error`` остаётся до решения оператора —
        он увидит его в списке и либо вернёт в ``free``, либо разберётся.
        """
        stamp = time.time() if now is None else now
        target = "assigned" if ok else "error"
        with self._lock, self._connect() as conn:
            try:
                row = self._transition(
                    conn,
                    profile_id,
                    target,
                    allowed_from=("assigned", "active"),
                    now=stamp,
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return row

    def _transition(
        self,
        conn: sqlite3.Connection,
        profile_id: Any,
        new_status: str,
        *,
        allowed_from: Sequence[str],
        now: float | None = None,
    ) -> dict[str, Any]:
        """Единственное место, где меняется ``profiles.status``.

        Проверка идёт по факту прочитанной строки, а не по ожиданиям
        вызывающего: два запроса могут спорить за один профиль, и решение
        обязано приниматься по актуальному статусу.
        """
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if row is None:
            raise ProfileNotFoundError(f"профиль id={profile_id} не найден")
        current = row["status"]
        if current == new_status:
            # Идемпотентный повторный вызов: статус уже целевой, меняется
            # только отметка последней работы, если её просили.
            if now is None:
                return _row_to_dict(row)
            conn.execute(
                "UPDATE profiles SET last_used_at = ? WHERE id = ?", (now, profile_id)
            )
            return _row_to_dict(
                conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
            )
        if current not in allowed_from or new_status not in _ALLOWED_TRANSITIONS.get(
            current, frozenset()
        ):
            raise ProfileInvalidError(
                f"недопустимый переход статуса профиля id={profile_id}: "
                f"{current} → {new_status}"
            )
        if now is None:
            conn.execute(
                "UPDATE profiles SET status = ? WHERE id = ?", (new_status, profile_id)
            )
        else:
            conn.execute(
                "UPDATE profiles SET status = ?, last_used_at = ? WHERE id = ?",
                (new_status, now, profile_id),
            )
        updated = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        return _row_to_dict(updated)

    # --- выдача и освобождение -------------------------------------------

    def take_for_worker(self, browser_id: str) -> dict[str, Any] | None:
        """Отдаёт свободный профиль воркеру. ``None`` — свободных нет.

        Порядок отбора:

        1. прошлое назначение воркера (``workers.profile_id``): упавший и
           поднятый заново продолжает работать с тем же профилем — приоритет
           есть даже у профиля, освобождённого реапером (ссылка на воркера
           при этом сохранена намеренно);
        2. случайный свободный — ``random.choice`` среди профилей в статусе
           ``free``, на которые не ссылается живой воркер (ровно та же
           проверка держателя, что и раньше). Берётся не первый по
           возрастанию id, иначе каждый запуск отдавал бы воркерам одни и те
           же профили по порядку — те самые User-Agent, по которым капча
           узнавала программу.

        Выданный профиль несёт свой ``proxy_id`` — супервизор делает его
        прокси воркера, приоритетнее пула (см. ``Supervisor._start_worker``).
        Статус и ссылка меняются здесь, в одной транзакции.
        """
        with self._lock, self._connect() as conn:
            try:
                previous = conn.execute(
                    "SELECT profile_id FROM workers WHERE browser_id = ?", (browser_id,)
                ).fetchone()
                if previous is not None and previous["profile_id"] is not None:
                    candidate = conn.execute(
                        "SELECT * FROM profiles WHERE id = ?", (previous["profile_id"],)
                    ).fetchone()
                    if candidate is not None and self._claimable(
                        conn, candidate, browser_id
                    ):
                        claimed = self._claim(conn, candidate, browser_id)
                        conn.commit()
                        return claimed

                free = conn.execute(
                    f"""
                    SELECT p.* FROM profiles p
                    WHERE p.status = 'free'
                      AND NOT EXISTS (
                          SELECT 1 FROM workers w
                          WHERE w.profile_id = p.id AND w.status IN ({_status_list()})
                      )
                    ORDER BY p.id
                    """
                ).fetchall()
                if not free:
                    return None
                claimed = self._claim(conn, random.choice(free), browser_id)
                conn.commit()
                return claimed
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _claimable(
        conn: sqlite3.Connection, row: sqlite3.Row, browser_id: str
    ) -> bool:
        """Можно ли отдать профиль ``row`` воркеру ``browser_id``.

        Заблокированный или профиль в ошибке не выдаётся никому. Назначенный
        и свободный профили выдаются, только если на них не ссылается живой
        воркер, кроме самого запросившего: это и есть защита от двойной
        выдачи аккаунта, когда оператор сбросил статус, а процесс работает.
        """
        if row["status"] not in ("free", "assigned", "active"):
            return False
        holder = _holder(conn, row["id"])
        return holder is None or holder == browser_id

    @staticmethod
    def _claim(
        conn: sqlite3.Connection, row: sqlite3.Row, browser_id: str
    ) -> dict[str, Any]:
        """Ставит ``assigned`` (если был ``free``) и связывает профиль с воркером."""
        profile_id = row["id"]
        if row["status"] == "free":
            conn.execute(
                "UPDATE profiles SET status = 'assigned' WHERE id = ? AND status = 'free'",
                (profile_id,),
            )
        # Ссылка чужого (в т.ч. мёртвого) воркера гасится в момент выдачи:
        # после неё у профиля ровно один держатель.
        conn.execute(
            "UPDATE workers SET profile_id = NULL WHERE profile_id = ? AND browser_id != ?",
            (profile_id, browser_id),
        )
        # UPDATE без строк — воркера ещё нет (первый спавн): его строку
        # допишет супервизор сразу после register_worker.
        conn.execute(
            "UPDATE workers SET profile_id = ? WHERE browser_id = ?",
            (profile_id, browser_id),
        )
        updated = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        return _row_to_dict(updated)

    def release(self, profile_id: Any) -> bool:
        """Освобождает профиль: ``assigned|active → free`` и снимает ссылку воркера.

        Возвращает True, если статус действительно стал ``free``. Профиль,
        заблокированный или ушедший в ``error``, освобождать не нужно — там
        ждёт решение оператора, — но ссылка воркера гасится в любом случае:
        воркер больше не держит профиль.
        """
        if isinstance(profile_id, bool) or not isinstance(profile_id, int):
            return False
        with self._lock, self._connect() as conn:
            try:
                freed = self._release_locked(conn, profile_id)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return freed

    def release_for_worker(self, browser_id: str) -> bool:
        """Релиз по строке воркера: ``workers.profile_id`` → :meth:`release`.

        Вызывается супервизором на stop/kill/circuit-open, когда известен
        только browser_id. Вызов для воркера без профиля — no-op, как и
        ``StateStore.release_assignment`` для несуществующего browser_id.
        """
        with self._lock, self._connect() as conn:
            try:
                row = conn.execute(
                    "SELECT profile_id FROM workers WHERE browser_id = ?", (browser_id,)
                ).fetchone()
                if row is None or row["profile_id"] is None:
                    return False
                freed = self._release_locked(conn, row["profile_id"])
                conn.commit()
                return freed
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _release_locked(conn: sqlite3.Connection, profile_id: int) -> bool:
        row = conn.execute(
            "SELECT status FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            return False
        freed = row["status"] in ("assigned", "active")
        if freed:
            conn.execute(
                "UPDATE profiles SET status = 'free' WHERE id = ? "
                "AND status IN ('assigned', 'active')",
                (profile_id,),
            )
        conn.execute(
            "UPDATE workers SET profile_id = NULL WHERE profile_id = ?", (profile_id,)
        )
        return freed

    # --- массовые операции -----------------------------------------------

    def assign_range(self, start_id: Any, end_id: Any) -> dict[str, Any]:
        """Назначает свободные профили диапазона свободным воркерам.

        Пары «профиль ↔ воркер» строятся в порядке возрастания id с обеих
        сторон, по одному профилю на воркера без профиля: порядок
        детерминирован и повторяется от прогона к прогону. Невостребованные
        профили остаются ``free``.

        Возвращает ``{"assigned": сколько выдано, "available": сколько
        свободных осталось в этом диапазоне}`` — оба числа описывают один и
        тот же диапазон, поэтому оператор видит результат своей команды, а не
        счётчик по всей таблице.

        Невалидный диапазон — :class:`ProfileInvalidError` (``ValueError``),
        HTTP-слой отдаёт 400.
        """
        for value in (start_id, end_id):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ProfileInvalidError("границы диапазона должны быть целыми числами id")
        if start_id < 1 or start_id > end_id:
            raise ProfileInvalidError(
                f"невалидный диапазон: ожидается 1 <= start_id <= end_id, "
                f"получено {start_id}..{end_id}"
            )

        with self._lock, self._connect() as conn:
            try:
                profiles = conn.execute(
                    f"""
                    SELECT p.id FROM profiles p
                    WHERE p.id BETWEEN ? AND ?
                      AND p.status = 'free'
                      AND NOT EXISTS (
                          SELECT 1 FROM workers w
                          WHERE w.profile_id = p.id AND w.status IN ({_status_list()})
                      )
                    ORDER BY p.id
                    """,
                    (start_id, end_id),
                ).fetchall()
                workers = conn.execute(
                    f"""
                    SELECT browser_id FROM workers
                    WHERE profile_id IS NULL AND status IN ({_status_list()})
                    ORDER BY id
                    """
                ).fetchall()

                pairs = list(
                    zip(
                        [row["id"] for row in profiles],
                        [row["browser_id"] for row in workers],
                    )
                )
                for profile_id, browser_id in pairs:
                    conn.execute(
                        "UPDATE profiles SET status = 'assigned' WHERE id = ?",
                        (profile_id,),
                    )
                    conn.execute(
                        "UPDATE workers SET profile_id = NULL "
                        "WHERE profile_id = ? AND browser_id != ?",
                        (profile_id, browser_id),
                    )
                    conn.execute(
                        "UPDATE workers SET profile_id = ? WHERE browser_id = ?",
                        (profile_id, browser_id),
                    )
                available = conn.execute(
                    "SELECT COUNT(*) FROM profiles WHERE id BETWEEN ? AND ? AND status = 'free'",
                    (start_id, end_id),
                ).fetchone()[0]
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        return {"assigned": len(pairs), "available": int(available)}

    def unassign_all(self) -> int:
        """Снимает все назначения: каждый профиль → ``free``, ссылки → NULL.

        Возвращает число профилей, чей статус реально изменился: повторный
        вызов ничего не освобождает. Операторская команда, поэтому в ``free``
        уходят и ``blocked``/``error`` — контракт unassign'а это «все профили
        → free», и частично сброшенный список выглядел бы как неработающая
        кнопка.
        """
        with self._lock, self._connect() as conn:
            try:
                cursor = conn.execute(
                    "UPDATE profiles SET status = 'free' WHERE status != 'free'"
                )
                released = int(cursor.rowcount)
                conn.execute(
                    "UPDATE workers SET profile_id = NULL WHERE profile_id IS NOT NULL"
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return released

    def reap(self, live_profile_ids: set[int]) -> int:
        """Страховка от потери назначения: без живого воркера профиль → ``free``.

        ``live_profile_ids`` — профили, которые держат живые воркеры; всё,
        что назначено, но не в этом множестве (воркер умер, удалён из
        реестра, протух heartbeat), освобождается. Вызывается супервизором
        раз в тик, после логики stale.

        ``workers.profile_id`` при этом НЕ очищается: это память о прошлом
        назначении, без которой воркер, поднятый после падения, получил бы
        чужой профиль. Ссылку гасит выдача профиля другому воркеру,
        ``unassign_all`` и удаление строки.
        """
        live = list(live_profile_ids)
        with self._lock, self._connect() as conn:
            try:
                if live:
                    placeholders = ", ".join("?" for _ in live)
                    cursor = conn.execute(
                        "UPDATE profiles SET status = 'free' "
                        f"WHERE status IN ('assigned', 'active') AND id NOT IN ({placeholders})",
                        live,
                    )
                else:
                    cursor = conn.execute(
                        "UPDATE profiles SET status = 'free' "
                        "WHERE status IN ('assigned', 'active')"
                    )
                reaped = int(cursor.rowcount)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return reaped


def parse_import_line(line: str) -> tuple[str | None, str | None, str | None]:
    """Разбирает строку импорта: ``(значение, явное имя | None, ошибка | None)``.

    Строка сначала режется по первому табу (первая колонка — значение,
    остальное — хвост вставленной таблицы), потом разбирается на значение и
    имя по ``|``. Значение может оказаться User-Agentом — решает вызывающий
    код (:meth:`ProfilePool.import_lines`), здесь только разбор.

    ``ValueError`` здесь не используется: строка целиком может быть ключом
    либо User-Agentом, поэтому текст ошибки не должен её цитировать — он
    уходит в ``problems``.
    """
    if not isinstance(line, str):
        return None, None, "строка должна быть текстом"
    text = line.split("\t", 1)[0].strip()
    if "|" not in text:
        return (text, None, None) if text else (None, None, "строка пустая")
    value, _, name = text.partition("|")
    value = value.strip()
    name = name.strip()
    if not value:
        return None, None, "в строке не задан key_ref"
    if not name:
        return None, None, "пустое имя после знака |"
    return value, name, None


def _row_to_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """Строка → объект ответа: ``fields`` из JSON-текста в объект.

    Повреждённое значение (правка руками, старый экспорт) не должно ронять
    весь список — в ответ уходит пустой объект, а не исключение посреди
    HTTP-ответа.
    """
    data = dict(row)
    raw = data.get("fields")
    if not raw:
        data["fields"] = {}
        return data
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        data["fields"] = {}
        return data
    data["fields"] = decoded if isinstance(decoded, dict) else {}
    return data


def _next_ua_number(conn: sqlite3.Connection) -> int:
    """Первый свободный номер для имени ``UA-N``: ``1 + max`` суффиксов.

    Считаются имена, целиком состоящие из ``UA-`` и числа: имя ``UA-9``
    даёт нумерацию с 10, а чужое (``alice``, ``UA-9 backup``) на границу
    не влияет. Пустой пул начинается с 1. Вызывается один раз на пачку —
    дальше номер растёт в :meth:`ProfilePool.import_lines`.
    """
    top = 0
    for row in conn.execute("SELECT name FROM profiles"):
        match = _UA_NAME_RE.fullmatch(row["name"] or "")
        if match is not None:
            top = max(top, int(match.group(1)))
    return top + 1


def _holder(conn: sqlite3.Connection, profile_id: int) -> str | None:
    """``browser_id`` живого воркера, держащего профиль, либо ``None``.

    Общая проверка для ``delete``/``delete_many`` и выдачи: все места
    обязаны считать профиль «занятым» одинаково, иначе одиночное удаление,
    батч и выдача разъезжались бы на одних и тех же данных. Вызывается
    внутри открытой транзакции — так проверка и удаление не расходятся по
    времени.
    """
    row = conn.execute(
        f"""
        SELECT w.browser_id FROM workers w
        WHERE w.profile_id = ? AND w.status IN ({_status_list()})
        ORDER BY w.id LIMIT 1
        """,
        (profile_id,),
    ).fetchone()
    return None if row is None else str(row["browser_id"])


def _status_list() -> str:
    """Список живых статусов воркера для SQL-шаблона.

    Функция, а не строковая константа: значения объявлены в
    ``_ALIVE_WORKER_STATUSES``, и расхождение между списком и проверкой
    ``_claimable`` невозможно по построению.
    """
    return ", ".join(f"'{status}'" for status in _ALIVE_WORKER_STATUSES)
