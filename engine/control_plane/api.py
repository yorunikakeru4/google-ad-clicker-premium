"""HTTP API control plane поверх ``http.server``.

Модуль написан на стандартной библиотеке намеренно: Flask/FastAPI в nix-окружении
нет, а добавлять зависимость ради восьми endpoint'ов — лишняя зависимость в
поставке и в сборке.

Четыре решения, которые стоит знать при чтении:

1. **Токен обязателен, а не опционален.** Нет переменной окружения — демон не
   стартует. Альтернатива (стартовать и слушать только loopback) выглядит
   безопасно, но не является: любой процесс на машине пользователя, включая
   вкладку в браузере через DNS-rebinding, достучится до порта без препятствий.
2. **Сравнение токена — только ``hmac.compare_digest``.** Обычный ``==``
   останавливается на первом различии и по времени ответа выдаёт длину верного
   префикса.
3. **Только loopback.** Адрес биндинга проверяется в конструкторе, а не
   "по умолчанию": один параметр, протухший настройкой, и демон с токеном в
   руках оказывается доступен из сети.
4. **Любая ошибка — JSON.** Обработчик не отдаёт HTML-трейсбек: в нём пути
   файлов и, при неудачном стеку, значения из окружения.
"""

from __future__ import annotations

import hmac
import json
import os
import threading

from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from engine.cleanup import CleanupService
from engine.control_plane.config import Config, ConfigError
from engine.control_plane.supervisor import (
    AlreadyRunningError,
    InvalidWorkerCountError,
    NoAliveProxyError,
    NotPausedError,
    NotRunningError,
    Supervisor,
    SupervisorError,
    WorkerSpawnError,
)
from engine.diagnostics import request_signal
from engine.profile_pool import (
    ProfileError,
    ProfileInUseError,
    ProfileInvalidError,
    ProfileNotFoundError,
    ProfilePool,
)
from engine.proxy_health import CheckInProgressError, ProxyHealthChecker
from engine.proxy_pool import (
    ProxyError,
    ProxyImportError,
    ProxyInUseError,
    ProxyNotFoundError,
    ProxyPool,
)
from engine.wordlist import (
    WordlistError,
    add_items,
    clean_domain,
    clean_query,
    delete_items,
    read_items,
)

# Имя переменной окружения с токеном.
TOKEN_ENV_VAR = "ADCLICKER_CONTROL_TOKEN"

# Заголовок с токеном. Authorization: Bearer тоже принимается — клиенту
# удобнее не изобретать свой заголовок.
TOKEN_HEADER = "X-Auth-Token"
BEARER_PREFIX = "bearer "

# Версия протокола. Помещается в /health, чтобы UI мог отказаться от старого
# демона, а не гадать по форме ответа.
PROTOCOL_VERSION = 1

# Единственный адрес, на котором демон себя слушает.
LOOPBACK_HOST = "127.0.0.1"

# Имена файлов списков по умолчанию — из той же логики, что и дефолты
# ``_SCHEMA``: используются, когда путь в конфиге пуст (чистая установка).
DEFAULT_QUERIES_FILE = "queries.txt"
DEFAULT_DOMAINS_FILE = "domains.txt"

# Потолок тела запроса. Конфиг небольшой, а лимит защищает демон от запроса,
# который съел бы память целиком.
MAX_BODY_BYTES = 64 * 1024

# Как часто serve_forever проверяет флаг остановки. Стандартные 0.5 с
# превращают stop() в 0.5 с ожидания на каждый вызов, а он дергается и при
# SIGTERM, и в каждом тесте.
SHUTDOWN_POLL_INTERVAL = 0.02

# Соответствие классов ошибок супервизора кодам ответа.
# 409, а не 400: запрос корректен, но конфликтует с текущим состоянием.
_ERROR_STATUS = {
    AlreadyRunningError: ("already_running", 409),
    NotRunningError: ("not_running", 409),
    NotPausedError: ("not_paused", 409),
    InvalidWorkerCountError: ("invalid_worker_count", 400),
    WorkerSpawnError: ("worker_spawn_failed", 503),
    # Пул непуст, но живых строк нет: старт/рестарт без прокси дал бы воркеру
    # строку из файла в обход health-проверки. 503, а не 400 — тело запроса
    # корректно, отклоняет состояние пула.
    NoAliveProxyError: ("no_alive_proxy", 503),
    ConfigError: ("invalid_config", 400),
    # Подсистема прокси: конфликт состояния, отсутствующая строка и ошибка
    # импорта. Каждый код — из контракта /control/proxies*, а не выдуман.
    ProxyInUseError: ("proxy_in_use", 409),
    ProxyNotFoundError: ("proxy_not_found", 404),
    ProxyImportError: ("proxy_import_failed", 400),
    CheckInProgressError: ("check_in_progress", 409),
    # Подсистема профилей: те же три статуса, что у прокси, плюс ошибка
    # машины статусов. ProfileInvalidError — это «тело корректно, но
    # запрошенное в нём бессмысленно» (системный статус, кривой диапазон),
    # поэтому код ответа общий с остальными проверками тела — invalid_request.
    ProfileInUseError: ("profile_in_use", 409),
    ProfileNotFoundError: ("profile_not_found", 404),
    ProfileInvalidError: ("invalid_request", 400),
}


class AuthError(PermissionError):
    """Токен не предъявлен или не совпал."""


class MissingTokenError(RuntimeError):
    """Токен не задан в окружении — демон не должен стартовать."""


class PayloadTooLargeError(ValueError):
    """Тело запроса больше MAX_BODY_BYTES."""


class InvalidJSONError(ValueError):
    """Тело запроса не разобралось как JSON."""


class InvalidRequestError(ValueError):
    """Тело — корректный JSON, но запрошенное в нём не имеет смысла.

    Отдельный тип от InvalidJSONError, потому что это разные ошибки для
    пользователя: сломанный синтаксис он починит сам, а неверное поле в
    теле — вопрос к значениям.
    """


def token_from_environ(environ: dict[str, str] | None = None) -> str:
    """Читает токен из переменной окружения.

    Пустая и состоящая из пробелов строка считается отсутствием токена: демон,
    защищённый пробелами, не защищён.
    """
    source = os.environ if environ is None else environ
    token = source.get(TOKEN_ENV_VAR, "")
    if not token.strip():
        raise MissingTokenError(
            f"{TOKEN_ENV_VAR} не задана. Задайте её, чтобы запустить демон: "
            f"export {TOKEN_ENV_VAR}=<длинная случайная строка>"
        )
    return token


def _is_loopback(host: str) -> bool:
    """Только loopback. 0.0.0.0 и ::  — это "все интерфейсы", а не loopback."""
    return host in {LOOPBACK_HOST, "::1", "localhost"}


def _constant_time_equals(given: str, expected: str) -> bool:
    """Сравнение токенов без утечки длины по времени выполнения."""
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    body = {"error": {"code": code, "message": message}}
    if extra:
        body["error"].update(extra)
    return body


class ControlPlaneHandler(BaseHTTPRequestHandler):
    """Обработчик запросов.

    Настройки экземпляра приходят через атрибуты класса, которые
    ControlPlaneServer проставляет до старта: BaseHTTPRequestHandler создаётся
    сервером, и передать ему параметры иначе нельзя.
    """

    server_version = "adclicker-control-plane"
    sys_version = ""  # не публикуем версию Python в заголовках
    protocol_version = "HTTP/1.1"

    # Проставляются ControlPlaneServer.
    supervisor: Supervisor
    # Ссылка на ControlPlaneServer: обработчик не хранит собственную копию
    # конфига, а читает её оттуда — единый источник, который меняет POST
    # /control/config под замком.
    control: ControlPlaneServer
    token: str
    proxy_pool: ProxyPool
    profile_pool: ProfilePool
    # Any, а не ProxyHealthChecker: тесты подменяют проверяющий объект
    # фейком, а супервизорской нотации для duck-typing здесь не требуется.
    proxy_checker: Any

    @property
    def config(self) -> Config:
        """Текущий конфиг демона — живая ссылка с сервера, а не копия.

        Патч из UI обновляет конфиг под замком ``ControlPlaneServer``, поэтому
        обработчики всех последующих запросов видят применённое значение, а
        через сервер его же читают job'ы демона. Раньше патч оставался только
        на этом обработчике (instance + bound-класс), и демон видел старое
        значение до рестарта.
        """
        return self.control.config

    def do_GET(self) -> None:  # noqa: N802 - имя задано BaseHTTPRequestHandler
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    # --- маршрутизация ---------------------------------------------------

    def _dispatch(self, method: str) -> None:
        """Единая точка входа: авторизация, маршрут, ответ.

        Порядок проверок значим: авторизация идёт до маршрутизации, иначе по
        коду ответа любой процесс перечислил бы существующие endpoint'ы.
        """
        try:
            self._authenticate()
            handler_name = _ROUTES.get((method, self.path))
            if handler_name is None:
                self._handle_unknown_route(method)
                return
            getattr(self, handler_name)()
        except AuthError as exc:
            self._send_json(401, _error("unauthorized", str(exc)))
        except PayloadTooLargeError as exc:
            self._send_json(413, _error("payload_too_large", str(exc)))
        except InvalidJSONError as exc:
            self._send_json(400, _error("invalid_json", str(exc)))
        except InvalidRequestError as exc:
            self._send_json(400, _error("invalid_request", str(exc)))
        except WordlistError as exc:
            # Причина из файловой системы (нет каталога, нет прав): сообщение
            # уходит как есть, статус — из контракта самого исключения.
            self._send_json(exc.status, _error(exc.code, str(exc)))
        except ConfigError as exc:
            # Отдельная ветка, а не вместе с SupervisorError: ConfigError —
            # это ValueError, и без неё невалидный конфиг уехал бы в 500.
            # 400 с перечнем полей — единственный полезный ответ здесь.
            self._send_json(400, _error("invalid_config", str(exc), problems=exc.problems))
        except ProxyError as exc:
            # Ошибки пула прокси: сообщения уже безопасны (без кредов),
            # здесь только перевод типа в код и статус.
            code, status = _error_status(exc)
            self._send_json(status, _error(code, str(exc)))
        except ProfileError as exc:
            # Ошибки пула профилей: текст построен без key_ref, здесь только
            # перевод типа в код и статус — как у прокси.
            code, status = _error_status(exc)
            self._send_json(status, _error(code, str(exc)))
        except SupervisorError as exc:
            code, status = _error_status(exc)
            self._send_json(status, _error(code, str(exc)))
        except Exception:  # noqa: BLE001 - трейсбек наружу не отдаётся намеренно
            self._log_internal_error()
            self._send_json(500, _error("internal_error", "внутренняя ошибка демона"))

    def _handle_unknown_route(self, method: str) -> None:
        """Отличает "нет такого пути" от "путь есть, но не для этого метода"."""
        allowed = [m for (m, path) in _ROUTES if path == self.path]
        if allowed:
            self._send_json(
                405,
                _error("method_not_allowed", f"{method} не поддерживается для {self.path}"),
                extra_headers={"Allow": ", ".join(sorted(allowed))},
            )
            return
        self._send_json(404, _error("not_found", f"неизвестный путь {self.path}"))

    # --- авторизация -----------------------------------------------------

    def _authenticate(self) -> None:
        presented = self._extract_token()
        if not presented:
            raise AuthError(f"нужен заголовок {TOKEN_HEADER} с токеном демона")
        if not _constant_time_equals(presented, self.token):
            raise AuthError("неверный токен")

    def _extract_token(self) -> str:
        custom = self.headers.get(TOKEN_HEADER)
        if custom:
            return custom.strip()
        authorization = self.headers.get("Authorization", "")
        if authorization.lower().startswith(BEARER_PREFIX):
            return authorization[len(BEARER_PREFIX) :].strip()
        return ""

    # --- endpoint'ы -------------------------------------------------------

    def _handle_health(self) -> None:
        health = self.supervisor.health()
        payload = {
            "status": "ok",
            "version": PROTOCOL_VERSION,
            "state": self.supervisor.store.get_run_state(),
            **health,
        }
        self._send_json(200, payload)

    def _handle_state(self) -> None:
        self._send_json(200, self.supervisor.store.snapshot())

    def _handle_start(self) -> None:
        count = self._requested_worker_count()
        browser_ids = self.supervisor.start(count)
        self._send_json(200, {"state": "running", "workers": browser_ids})

    def _handle_pause(self) -> None:
        self.supervisor.pause()
        self._send_json(200, {"state": "paused", "paused": True})

    def _handle_resume(self) -> None:
        self.supervisor.resume()
        self._send_json(200, {"state": "running", "paused": False})

    def _handle_stop(self) -> None:
        self.supervisor.stop()
        self._send_json(200, {"state": "stopped"})

    def _handle_restart(self) -> None:
        count = self._requested_worker_count()
        self.supervisor.restart(count)
        self._send_json(200, {"state": "running", "workers": count})

    def _handle_config_get(self) -> None:
        self._send_json(200, {"config": self.config.to_dict()})

    def _handle_config_post(self) -> None:
        patch = self._read_json_object()
        # Валидация, запись файла и замена ссылки — под одним замком сервера:
        # два конкурентных POST не теряют поля друг друга, а читатели (job'ы
        # демона, следующие запросы) получают объект целиком — старый или новый.
        updated = self.control.patch_config(patch)
        self._apply_file_level(updated)
        self._send_json(200, {"config": updated.to_dict()})

    def _apply_file_level(self, config: Config) -> None:
        """Переприменяет уровень файлового лога после каждого успешного POST.

        ``behavior.log_file_level`` — поле config.json, но файловый
        обработчик живёт в этом процессе: без этого вызова демон писал бы
        ``adclicker.log`` старым уровнем до рестарта. Невалидное значение
        запрос не роняет: старый уровень остаётся, причина уходит в лог
        демона (сам POST уже прошёл валидацию схемы, поэтому ветка —
        защитная). Импорт ленивый — ``logger.py`` создаёт каталог ``logs/``
        при импорте, и модуль API не должен делать этого до первого POST.

        Для воркеров live-уровень не действует: они перечитывают
        ``config.json`` в своём процессе (``config_reader``) и подхватывают
        новое значение при следующем старте/перечитывании настроек.
        """
        from logger import apply_file_level

        try:
            apply_file_level(str(config.get("behavior.log_file_level")))
        except ValueError as exc:
            self.supervisor.store.log(
                "WARNING",
                "config",
                "log_file_level not applied",
                {"error": str(exc)},
            )

    def _handle_proxies_list(self) -> None:
        # Креды маскируются внутри list_proxies: ответ уходит в UI наружу.
        self._send_json(200, {"proxies": self.proxy_pool.list_proxies()})

    def _handle_proxies_add(self) -> None:
        self._send_json(200, self.proxy_pool.add_lines(self._requested_lines()))

    def _handle_proxies_import(self) -> None:
        # Тело читается, чтобы битый JSON дал привычный 400, но его
        # содержимое игнорируется: путь берётся только из конфига.
        self._read_optional_object()
        path = self._proxy_file_setting()
        if not path:
            raise ProxyImportError(
                "paths.proxy_file не задан в конфиге — импорт из файла невозможен"
            )
        self._send_json(200, self.proxy_pool.import_file(path))

    def _handle_proxies_file(self) -> None:
        """Абсолютный путь к файлу прокси — для «открыть в системе» из UI.

        Путь отдаёт демон, а не UI: в конфиге он обычно относительный
        (``proxies.txt``), а разрешается он от текущего каталога демона —
        того же, от которого читает ``proxy_pool.import_file``. UI этот
        каталог не знает и не должен знать: opener-плагину нужен готовый
        абсолютный путь.

        ``exists`` грузится в ответ намеренно: файла может ещё не быть
        (чистая установка), и UI показывает понятное «файл не найден»
        вместо ошибки системы при открытии.
        """
        path = self._proxy_file_setting()
        if not path:
            raise InvalidRequestError("paths.proxy_file не задан в конфиге")
        self._send_json(200, self._file_payload(path))

    def _proxy_file_setting(self) -> str:
        """``paths.proxy_file`` из конфига; пустая строка — значение не задано.

        Один источник пути для импорта и для «открыть файл»: две ветки,
        читающие конфиг по-своему, могли бы разъехаться. Пустое значение не
        поднимает исключение здесь — код ответа у импорта и у чтения пути
        разный (``proxy_import_failed`` против ``invalid_request``).
        """
        return str(self.config.get("paths.proxy_file") or "")

    def _handle_proxies_delete(self) -> None:
        """Одиночное удаление — строго, батч и «всё» — best-effort с отчётом.

        ``{"id": n}`` (легаси-контракт UI) сохраняет 404/409: одна строка,
        которую держит воркер, — понятная и нужная ошибка. ``{"ids": [...]}``
        и ``{"all": true}`` занятые и отсутствующие строки не роняют:
        иначе один занятый прокси заблокировал бы и «удалить все с ошибкой»,
        и «удалить все», а оператор ждёт ровно обратного — удалить всё, что
        можно, и увидеть, что мешало. Тело на «всё» не растёт с размером
        пула: список id собирает демон, а не UI (MAX_BODY_BYTES — 64 КБ).
        """
        ids, mode = self._requested_proxy_ids()
        if mode == "single":
            self.proxy_pool.delete(ids[0])
            # Счётчик, а не булево: UI (как и add/import) ждёт число.
            self._send_json(200, {"deleted": 1})
            return
        if mode == "all":
            self._send_json(200, self.proxy_pool.delete_all())
            return
        self._send_json(200, self.proxy_pool.delete_many(ids))

    def _handle_proxies_check(self) -> None:
        self._read_optional_object()
        # CheckInProgressError отсюда уходит в 409 check_in_progress.
        self.proxy_checker.start()
        self._send_json(200, {"started": True})

    # --- списки: запросы (Key Words) и домены -------------------------------

    # Контракт обоих списков (план §5, фаза 13):
    #
    #   GET  /control/queries         — {queries, source, query_file, query}
    #   POST /control/queries         — {lines: [...]}  → {added, skipped, ...}
    #   POST /control/queries/delete  — {queries: [...]} | {all: true}
    #   GET  /control/queries/file    — {path, exists}
    #
    #   GET  /control/domains         — {domains, filtered_domains, own_domain, excludes}
    #   POST /control/domains         — {lines: [...]}
    #   POST /control/domains/delete  — {domains: [...]} | {all: true}
    #   GET  /control/domains/file    — {path, exists}
    #
    # Источник истины — файлы (``paths.query_file``, ``paths.filtered_domains``):
    # движок читает их напрямую, поэтому список живёт в файле, а не в БД.
    # Настройки вокруг списков (``behavior.own_domain``, ``behavior.excludes``)
    # — поля config.json и идут общим POST /control/config.

    def _queries_file_setting(self) -> str:
        """``paths.query_file``; пустая строка — работает одиночный запрос."""
        return str(self.config.get("paths.query_file") or "")

    def _domains_file_setting(self) -> str:
        """``paths.filtered_domains``; пустая строка — значение не задано."""
        return str(self.config.get("paths.filtered_domains") or "")

    @staticmethod
    def _resolve_path(setting: str) -> Path:
        """Путь файла из конфига относительно каталога демона.

        Относительные пути в ``config.json`` читает legacy-код от текущего
        каталога процесса — резолвить их нужно от того же каталога, иначе
        демон писал бы список туда, откуда воркер его не прочитает.
        """
        source = Path(setting)
        return source if source.is_absolute() else Path.cwd() / source

    @classmethod
    def _file_payload(cls, setting: str) -> dict[str, Any]:
        """``{path, exists}`` для кнопки «открыть в системе».

        Абсолютный путь отдаёт демон (см. ``_handle_proxies_file``), а
        ``exists`` грузится намеренно: файла может ещё не быть, и UI
        показывает «файл не найден» вместо ошибки opener'а.
        """
        source = cls._resolve_path(setting)
        return {"path": str(source), "exists": source.is_file()}

    def _requested_wordlist_targets(self, key: str) -> tuple[list[str], bool]:
        """Тело удаления списка: значения по ключу либо ``{"all": true}``.

        Пустой массив — ошибка: это не «удалить всё» (для этого есть
        ``all``), а запрос без содержимого, и молчаливое превращение его в
        no-op скрыло бы опечатку в UI.
        """
        body = self._read_optional_object()
        if body.get("all") is True:
            return [], True
        targets = body.get(key)
        if (
            not isinstance(targets, list)
            or not targets
            or not all(isinstance(item, str) for item in targets)
        ):
            raise InvalidRequestError(
                f'ожидается непустой массив строк "{key}" либо {{"all": true}}'
            )
        return targets, False

    def _handle_queries_list(self) -> None:
        path = self._queries_file_setting()
        self._send_json(
            200,
            {
                "queries": read_items(self._resolve_path(path), clean_query) if path else [],
                "source": "file" if path else "single",
                "query_file": path,
                "query": str(self.config.get("behavior.query") or ""),
            },
        )

    def _handle_queries_add(self) -> None:
        """Добавить запросы; при пустом пути или одиночном запросе источник переключается на файл.

        Одиночный запрос (``behavior.query``) не теряется: он первым строкой
        переезжает в файл, который создаётся до патча конфига — воркер
        перечитывает путь после замены ссылки и не должен наткнуться на
        отсутствующий файл.
        """
        lines = self._requested_lines()
        current_path = self._queries_file_setting()
        single = str(self.config.get("behavior.query") or "")
        path = current_path or DEFAULT_QUERIES_FILE

        seed = [single] if single else []
        result = add_items(self._resolve_path(path), [*seed, *lines], clean=clean_query)

        switched = not current_path or bool(single)
        if switched:
            self.control.patch_config(
                {"paths": {"query_file": path}, "behavior": {"query": ""}}
            )

        self._send_json(
            200,
            {
                **result,
                "source": "file",
                "query_file": path,
                "switched": switched,
            },
        )

    def _handle_queries_delete(self) -> None:
        path = self._queries_file_setting()
        if not path:
            raise InvalidRequestError("paths.query_file не задан — список запросов не ведётся")
        targets, delete_all = self._requested_wordlist_targets("queries")
        self._send_json(
            200,
            delete_items(
                self._resolve_path(path), targets, clean=clean_query, delete_all=delete_all
            ),
        )

    def _handle_queries_file(self) -> None:
        path = self._queries_file_setting()
        if not path:
            raise InvalidRequestError("paths.query_file не задан в конфиге")
        self._send_json(200, self._file_payload(path))

    def _handle_domains_list(self) -> None:
        path = self._domains_file_setting()
        self._send_json(
            200,
            {
                "domains": read_items(self._resolve_path(path), clean_domain) if path else [],
                "filtered_domains": path,
                # Настройки чёрного списка — поля config.json; отдаются здесь
                # для карточки настроек на том же экране.
                "own_domain": str(self.config.get("behavior.own_domain") or ""),
                "excludes": str(self.config.get("behavior.excludes") or ""),
            },
        )

    def _handle_domains_add(self) -> None:
        lines = self._requested_lines()
        current_path = self._domains_file_setting()
        path = current_path or DEFAULT_DOMAINS_FILE

        result = add_items(self._resolve_path(path), lines, clean=clean_domain)

        switched = not current_path
        if switched:
            self.control.patch_config({"paths": {"filtered_domains": path}})

        self._send_json(
            200,
            {**result, "filtered_domains": path, "switched": switched},
        )

    def _handle_domains_delete(self) -> None:
        path = self._domains_file_setting()
        if not path:
            raise InvalidRequestError(
                "paths.filtered_domains не задан — список доменов не ведётся"
            )
        targets, delete_all = self._requested_wordlist_targets("domains")
        self._send_json(
            200,
            delete_items(
                self._resolve_path(path), targets, clean=clean_domain, delete_all=delete_all
            ),
        )

    def _handle_domains_file(self) -> None:
        path = self._domains_file_setting()
        if not path:
            raise InvalidRequestError("paths.filtered_domains не задан в конфиге")
        self._send_json(200, self._file_payload(path))

    # --- профили ---------------------------------------------------------

    def _handle_profiles_list(self) -> None:
        # key_ref в ответе — ссылка, а не секрет, поэтому уходит как есть;
        # в logs его не пишет никто (и это проверяется тестами пула).
        self._send_json(200, {"profiles": self.profile_pool.list_profiles()})

    def _handle_profiles_add(self) -> None:
        self._send_json(200, self.profile_pool.add_profiles(self._requested_profiles()))

    def _handle_profiles_import(self) -> None:
        # Формат строки (key_ref либо "key_ref | name") и генерация имени
        # живут в ProfilePool.import_lines — здесь только проверка тела.
        self._send_json(200, self.profile_pool.import_lines(self._requested_lines()))

    def _handle_profiles_delete(self) -> None:
        # ProfileNotFoundError → 404, ProfileInUseError → 409 profile_in_use.
        self.profile_pool.delete(self._requested_id())
        self._send_json(200, {"deleted": True})

    def _handle_profiles_assign(self) -> None:
        # Границы диапазона валидирует пул: единый источник правил, а ошибка
        # идёт сюда как ProfileInvalidError (400) через _ERROR_STATUS.
        raw = self._read_optional_object()
        self._send_json(
            200, self.profile_pool.assign_range(raw.get("start_id"), raw.get("end_id"))
        )

    def _handle_profiles_unassign(self) -> None:
        # Тело читается, чтобы битый JSON дал привычный 400; его содержимое
        # не используется — команда массовая и без параметров.
        self._read_optional_object()
        self._send_json(200, {"released": self.profile_pool.unassign_all()})

    def _handle_profiles_status(self) -> None:
        raw = self._read_json_body()
        if not isinstance(raw, dict):
            raise InvalidRequestError("ожидается объект с полями id и status")
        profile_id = raw.get("id")
        # bool — подкласс int: JSON true не должно пройти как id=1.
        if isinstance(profile_id, bool) or not isinstance(profile_id, int):
            raise InvalidRequestError("поле id должно быть целым числом")
        status = raw.get("status")
        if not isinstance(status, str):
            raise InvalidRequestError("поле status должно быть текстом")
        # assigned|active системные и неизвестные значения пул отклоняет сам
        # (ProfileInvalidError → 400), здесь только форма тела.
        row = self.profile_pool.set_status(profile_id, status)
        self._send_json(200, {"id": row["id"], "status": row["status"]})

    # --- диагностика -------------------------------------------------------

    def _handle_diagnostics_collect(self) -> None:
        """``POST /control/diagnostics/collect``: выставить kv-сигнал.

        Контракт: тело — ровно одно из ``{"browser_id": str}`` или
        ``{"all": true}``, ответ — ``{"requested": n}``, где ``n`` — число
        ЖИВЫХ воркеров, которым выставлен сигнал. Сигнал — метка времени в
        ``DIAGNOSTICS_REQUESTED_<id>``; сам снимок приходит в БД позже,
        когда у воркера появится живой браузер (UI поллит ``diagnostics``).

        Мёртвому или неизвестному воркеру сигнал не ставится: он и так не
        прочитает его, а ``requested: 0`` честно говорит UI, что ничего не
        ждёт. Прежний сигнал перетирается новой меткой — свежесть воркер
        определяет сравнением со своим «последним обработанным».
        """
        raw = self._read_json_body()
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise InvalidRequestError("ожидается объект с полями browser_id или all")

        has_browser_id = "browser_id" in raw
        has_all = "all" in raw
        if has_browser_id == has_all:
            raise InvalidRequestError("нужно ровно одно из полей: browser_id или all")

        if has_all:
            if raw["all"] is not True:
                raise InvalidRequestError("поле all должно быть true")
            targets = self.supervisor.alive_browser_ids()
        else:
            browser_id = raw["browser_id"]
            # Не-строка (true, 5, список) — ошибка тела: browser_id идёт в
            # ключ kv-флага, и любое другое значение превратило бы его в мусор.
            if not isinstance(browser_id, str) or not browser_id.strip():
                raise InvalidRequestError("поле browser_id должно быть непустой строкой")
            wanted = browser_id.strip()
            targets = [wanted] if wanted in set(self.supervisor.alive_browser_ids()) else []

        for target in targets:
            request_signal(self.supervisor.store, target)
        self._send_json(200, {"requested": len(targets)})

    # --- очистка профилей (план §5, фаза 10) --------------------------------

    def _handle_cleanup_run(self) -> None:
        """``POST /control/cleanup/run``: синхронный прогон очистки.

        Тело — объект; ``{"dry_run": true}`` перечисляет кандидатов и пишет
        каждый в лог, но ничего не удаляет и не двигает метку последнего
        прогона. Долгая работа внутри сервиса ограничена бюджетом по времени,
        поэтому ответ приходит за секунды, а не «когда кончится tempdir».

        После реального прогона kv-цель пересчитывается: ручной запуск
        сдвигает интервал ``cleanup_interval_days``, и статус обязан показать
        следующий запуск с учётом этого, а не цель, посчитанную до кнопки.
        """
        raw = self._read_optional_object()
        dry_run = raw.get("dry_run", False)
        if not isinstance(dry_run, bool):
            raise InvalidRequestError("поле dry_run должно быть true или false")
        # Сервис и провайдер цели читаются через control (тот же приём, что
        # у config): подкласс обработчика не должен был бы жить в словаре
        # биндинга — обычную функцию там Python превратил бы в метод и передал
        # бы self первым аргументом.
        service = self.control.cleanup_service
        report = service.run(self.config, dry_run=dry_run)
        if not dry_run and self.control.cleanup_next_run is not None:
            service.set_next_run(self.control.cleanup_next_run())
        self._send_json(200, {"report": report})

    def _handle_cleanup_status(self) -> None:
        """``GET /control/cleanup/status``: последний отчёт и ближайший запуск.

        Оба значения из kv: отчёт пишет сервис после каждого реального
        прогона, цель — нить расписания (и ручной запуск). ``last: null`` —
        прогона ещё не было, ``next_run: null`` — job выключен. Ответ ровно
        по контракту, без внутренностей демона.
        """
        self._send_json(200, self.control.cleanup_service.status(self.config))

    # --- вспомогательное -------------------------------------------------

    def _requested_worker_count(self) -> int:
        """Сколько воркеров просят запустить.

        Тело запроса необязательно: без него берётся browser_count из конфига,
        и клиенту не нужно знать дефолт демона.
        """
        raw = self._read_json_body()
        if raw is None or raw == {}:
            return int(self.config.get("behavior.browser_count"))
        if not isinstance(raw, dict):
            raise InvalidRequestError("ожидается объект с полем workers")
        workers = raw.get("workers", self.config.get("behavior.browser_count"))
        if isinstance(workers, bool) or not isinstance(workers, int):
            raise InvalidRequestError("поле workers должно быть целым числом")
        return workers

    def _requested_lines(self) -> list[str]:
        """Список строк из тела запроса.

        Пустое тело — ошибка, а не пустой импорт: молчаливый ``added: 0``
        на нажатую кнопку выглядел бы как «всё добавлено, ничего не найдено».
        """
        raw = self._read_json_body()
        if not isinstance(raw, dict):
            raise InvalidRequestError("ожидается объект с полем lines")
        lines = raw.get("lines")
        if not isinstance(lines, list):
            raise InvalidRequestError("поле lines должно быть списком строк")
        if not all(isinstance(item, str) for item in lines):
            raise InvalidRequestError("поле lines должно быть списком строк")
        return lines

    def _requested_profiles(self) -> list[dict[str, Any]]:
        """Список записей из тела ``POST /control/profiles``.

        Пустое тело — ошибка, а не пустой импорт: молчаливый ``added: 0``
        на нажатую кнопку выглядел бы как «всё добавлено, ничего не
        найдено». Семантика каждой записи (имя, поля, дубликаты) — забота
        пула, здесь проверяется только форма.
        """
        raw = self._read_json_body()
        if not isinstance(raw, dict):
            raise InvalidRequestError("ожидается объект с полем profiles")
        records = raw.get("profiles")
        if not isinstance(records, list) or not all(
            isinstance(item, dict) for item in records
        ):
            raise InvalidRequestError("поле profiles должно быть списком объектов")
        return records

    def _requested_id(self) -> int:
        """Поле ``id`` из тела: удаление прокси, удаление профиля, статус."""
        raw = self._read_json_body()
        if not isinstance(raw, dict):
            raise InvalidRequestError("ожидается объект с полем id")
        profile_id = raw.get("id")
        # bool — подкласс int: JSON true не должно пройти как id=1.
        if isinstance(profile_id, bool) or not isinstance(profile_id, int):
            raise InvalidRequestError("поле id должно быть целым числом")
        return profile_id

    def _requested_proxy_ids(self) -> tuple[list[int], str]:
        """Что удалить: ``(ids, режим)``, режим — ``single`` / ``batch`` / ``all``.

        ``{"id": 7}`` — одиночное удаление (строгие 404/409),
        ``{"ids": [...]}`` — батч best-effort, ``{"all": true}`` — весь пул.
        В батче дубли схлопываются с сохранением порядка: повтор в списке
        не должен удалить строку, а потом пожаловаться, что её уже нет.

        ``all`` проверяется первым и не смешивается с остальными: запрос
        «удалить всё» не должен зависеть от того, есть ли в нём случайно
        чужое поле.
        """
        raw = self._read_json_body()
        if not isinstance(raw, dict):
            raise InvalidRequestError("ожидается объект с полем id, ids или all")
        if raw.get("all") is True:
            return [], "all"
        if "ids" in raw:
            ids = raw["ids"]
            if not isinstance(ids, list) or not ids:
                raise InvalidRequestError("поле ids должно быть непустым списком")
            if not all(
                isinstance(item, int) and not isinstance(item, bool) for item in ids
            ):
                raise InvalidRequestError("поле ids должно быть списком целых чисел")
            return list(dict.fromkeys(ids)), "batch"
        single = raw.get("id")
        if isinstance(single, bool) or not isinstance(single, int):
            raise InvalidRequestError("поле id должно быть целым числом")
        return [single], "single"

    def _read_optional_object(self) -> dict[str, Any]:
        """Тело-объект для endpoint'ов с пустым или необязательным телом."""
        raw = self._read_json_body()
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise InvalidRequestError("ожидается JSON-объект")
        return raw

    def _read_json_object(self) -> dict[str, Any]:
        raw = self._read_json_body()
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise InvalidRequestError("ожидается объект конфигурации")
        return raw

    def _read_json_body(self) -> Any:
        """Читает и разбирает тело запроса. None — тела не было."""
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return None
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise InvalidJSONError("Content-Length не является числом") from exc
        if length < 0:
            raise InvalidJSONError("Content-Length отрицателен")
        if length > MAX_BODY_BYTES:
            raise PayloadTooLargeError(
                f"тело запроса больше {MAX_BODY_BYTES} байт: {length}"
            )
        if length == 0:
            return None

        payload = self.rfile.read(length)
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidJSONError(f"тело запроса не является корректным JSON: {exc}") from exc

    def _send_json(
        self,
        status: int,
        payload: Any,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _log_internal_error(self) -> None:
        """Пишет трейсбек в таблицу logs, а не в ответ.

        Смысл: демон, упавший на неожиданном запросе, обязан оставить след для
        разбора, но не отдать этот след тому, кто запросил.
        """
        import traceback

        self.supervisor.store.log(
            "ERROR",
            "api",
            "unhandled error in HTTP handler",
            {"path": self.path, "traceback": traceback.format_exc()},
        )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Глушит стандартный access-лог BaseHTTPRequestHandler.

        Он пишет в stderr, где демон запущен как сервис, и загромождает вывод;
        запросы, которые стоит видеть, логируются явно через store.log.
        """
        return


def _error_status(exc: Exception) -> tuple[str, int]:
    """Подбирает код и статус по типу ошибки."""
    for error_type, mapping in _ERROR_STATUS.items():
        if isinstance(exc, error_type):
            return mapping
    return "internal_error", 500


# Маршруты хранят ИМЕНА методов, а не ссылки на функции.
#
# Со ссылками подкласс, созданный ControlPlaneServer, не смог бы ничего
# переопределить: словарь был бы собран из методов базового класса на этапе
# импорта, и подмена обработчика в подклассе молча ничего не делала бы.
_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/health"): "_handle_health",
    ("GET", "/state"): "_handle_state",
    ("POST", "/control/start"): "_handle_start",
    ("POST", "/control/pause"): "_handle_pause",
    ("POST", "/control/resume"): "_handle_resume",
    ("POST", "/control/stop"): "_handle_stop",
    ("POST", "/control/restart"): "_handle_restart",
    ("GET", "/control/config"): "_handle_config_get",
    ("POST", "/control/config"): "_handle_config_post",
    ("GET", "/control/proxies"): "_handle_proxies_list",
    ("POST", "/control/proxies"): "_handle_proxies_add",
    ("POST", "/control/proxies/import"): "_handle_proxies_import",
    ("GET", "/control/proxies/file"): "_handle_proxies_file",
    ("POST", "/control/proxies/delete"): "_handle_proxies_delete",
    ("POST", "/control/proxies/check"): "_handle_proxies_check",
    ("GET", "/control/queries"): "_handle_queries_list",
    ("POST", "/control/queries"): "_handle_queries_add",
    ("POST", "/control/queries/delete"): "_handle_queries_delete",
    ("GET", "/control/queries/file"): "_handle_queries_file",
    ("GET", "/control/domains"): "_handle_domains_list",
    ("POST", "/control/domains"): "_handle_domains_add",
    ("POST", "/control/domains/delete"): "_handle_domains_delete",
    ("GET", "/control/domains/file"): "_handle_domains_file",
    ("GET", "/control/profiles"): "_handle_profiles_list",
    ("POST", "/control/profiles"): "_handle_profiles_add",
    ("POST", "/control/profiles/import"): "_handle_profiles_import",
    ("POST", "/control/profiles/delete"): "_handle_profiles_delete",
    ("POST", "/control/profiles/assign"): "_handle_profiles_assign",
    ("POST", "/control/profiles/unassign"): "_handle_profiles_unassign",
    ("POST", "/control/profiles/status"): "_handle_profiles_status",
    ("POST", "/control/diagnostics/collect"): "_handle_diagnostics_collect",
    ("POST", "/control/cleanup/run"): "_handle_cleanup_run",
    ("GET", "/control/cleanup/status"): "_handle_cleanup_status",
}


class _ControlPlaneHTTPServer(ThreadingHTTPServer):
    """Сервер, который забывает обработчик connections при остановке.

    ThreadingHTTPServer по умолчанию не делает daemon_threads, из-за чего
    server_close() висит до обрыва keep-alive соединений от UI.
    """

    daemon_threads = True
    allow_reuse_address = True


class ControlPlaneServer:
    """Демон: супервизор воркеров плюс HTTP-управление.

    Разделение на класс, а не функция main(), нужно по двум причинам: тесты
    поднимают несколько независимых серверов одновременно, а демон должен
    уметь корректно остановиться по SIGTERM, а не только по Ctrl+C.
    """

    def __init__(
        self,
        supervisor: Supervisor,
        config: Config,
        token: str,
        config_path: str | Path,
        host: str = LOOPBACK_HOST,
        port: int = 0,
        proxy_pool: ProxyPool | None = None,
        profile_pool: ProfilePool | None = None,
        proxy_checker: Any = None,
        cleanup_service: CleanupService | None = None,
        cleanup_next_run: Callable[[], float | None] | None = None,
    ):
        if not _is_loopback(host):
            raise ValueError(
                f"демон слушает только loopback ({LOOPBACK_HOST}), "
                f"а указан {host}. Снаружи доступ к управлению закрыт намеренно."
            )
        if not token.strip():
            raise MissingTokenError(
                f"токен не задан. Передайте токен или экспортируйте {TOKEN_ENV_VAR}"
            )
        self.supervisor = supervisor
        # Живой конфиг демона: единый источник для обработчиков HTTP и для
        # job'ов демона (он читает его через свойство config). Замок держит
        # только запись — патч это read-modify-write, и два конкурентных POST
        # не должны читать один и тот же базовый конфиг и терять поля.
        # Чтение идёт без замка: замена ссылки атомарна, читатель получает
        # либо старый, либо новый объект Config целиком.
        self._config = config
        self._config_lock = threading.Lock()
        self.token = token
        self.config_path = Path(config_path)
        # Пул прокси по умолчанию строится на той же БД, что и супервизор:
        # сервер не знает про путь к базе иначе, чем через его store.
        # Профили — та же схема: пул в демоне один на три потребителя
        # (HTTP, спавн супервизора, реапер), иначе выдача и список в UI
        # разъезжались бы.
        self.proxy_pool = (
            proxy_pool if proxy_pool is not None else ProxyPool(supervisor.store.db_path)
        )
        self.profile_pool = (
            profile_pool if profile_pool is not None else ProfilePool(supervisor.store.db_path)
        )
        self.proxy_checker = (
            proxy_checker
            if proxy_checker is not None
            else ProxyHealthChecker(self.proxy_pool)
        )
        # Очистка профилей: сервис по умолчанию строится на том же store, что
        # и супервизор (сервер не знает пути к базе иначе, чем через него).
        # ``cleanup_next_run`` без демона — None: сборка сервера в тестах не
        # имеет расписания, и ручной запуск тогда не трогает kv-цель.
        self.cleanup_service = (
            cleanup_service if cleanup_service is not None else CleanupService(store=supervisor.store)
        )
        self.cleanup_next_run = cleanup_next_run
        self.host = host
        self.requested_port = port
        self.port = port
        self._httpd: _ControlPlaneHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # Публичная ссылка на класс-обработчик: нужна и для диагностики, и
        # тестам, которые проверяют поведение при неожиданном исключении.
        self.handler_class: type[ControlPlaneHandler] | None = None

    @property
    def config(self) -> Config:
        """Текущий конфиг демона. Единственный источник для всех читателей.

        Значение меняется только внутри ``patch_config`` (под замком), поэтому
        ``Daemon.config``, ``_current_config`` и ``ControlPlaneHandler.config``
        — все читают одну и ту же ссылку и видят патч из UI без рестарта.
        """
        return self._config

    def patch_config(self, patch: dict[str, Any]) -> Config:
        """Применяет патч из UI: валидация → запись файла → замена ссылки.

        Всё под одним замком: конкурентные POST сериализуются и не теряют
        поля друг друга. Сохранение идёт до замены: упавшая запись файла
        оставляет демон со старым конфигом, а ``ConfigError`` из валидации
        не доходит до замены вовсе — в ответ уходит 400, как и раньше.
        """
        with self._config_lock:
            updated = self._config.patch(patch)
            updated.save(self.config_path)
            self._config = updated
        return updated

    def start(self) -> None:
        """Поднимает сервер в фоновом потоке и возвращает управление.

        Нужна неблокирующая версия, а не serve_forever() в главном потоке:
        главный поток демона отдан фоновому циклу супервизора.
        """
        handler = self._build_handler()
        self.handler_class = handler
        self._httpd = _ControlPlaneHTTPServer((self.host, self.requested_port), handler)
        # Порт 0 означает "выбери свободный": выданный порт читаем отсюда,
        # иначе клиент не узнает, куда подключаться.
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": SHUTDOWN_POLL_INTERVAL},
            name="control-plane-http",
            daemon=True,
        )
        self._thread.start()

    def _build_handler(self) -> type[ControlPlaneHandler]:
        """Класс-обработчик, замкнутый на конкретные экземпляры.

        Атрибуты ставятся на подкласс, а не на общий ControlPlaneHandler:
        иначе два сервера в одном процессе (что делают тесты и что возможно при
        перезапуске) перетирали бы друг другу конфиг и токен.
        """
        bound = type(
            "BoundControlPlaneHandler",
            (ControlPlaneHandler,),
            {
                "supervisor": self.supervisor,
                "control": self,
                "token": self.token,
                "proxy_pool": self.proxy_pool,
                "profile_pool": self.profile_pool,
                "proxy_checker": self.proxy_checker,
            },
        )
        return bound

    def stop(self) -> None:
        """Гасит сервер и дожидается завершения его потока."""
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._httpd = None
        self._thread = None

    def serve_forever(self) -> None:
        """Блокирующий режим для запуска демона как самостоятельного процесса."""
        if self._httpd is None:
            self.start()
        assert self._httpd is not None
        try:
            while True:
                self._httpd.handle_request()
        finally:
            self.stop()

    def address(self) -> str:
        return f"http://{self.host}:{self.port}"
