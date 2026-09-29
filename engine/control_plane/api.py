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

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from engine.control_plane.config import Config, ConfigError
from engine.control_plane.supervisor import (
    AlreadyRunningError,
    InvalidWorkerCountError,
    NotPausedError,
    NotRunningError,
    Supervisor,
    SupervisorError,
    WorkerSpawnError,
)
from engine.proxy_health import CheckInProgressError, ProxyHealthChecker
from engine.proxy_pool import (
    ProxyError,
    ProxyImportError,
    ProxyInUseError,
    ProxyNotFoundError,
    ProxyPool,
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
    ConfigError: ("invalid_config", 400),
    # Подсистема прокси: конфликт состояния, отсутствующая строка и ошибка
    # импорта. Каждый код — из контракта /control/proxies*, а не выдуман.
    ProxyInUseError: ("proxy_in_use", 409),
    ProxyNotFoundError: ("proxy_not_found", 404),
    ProxyImportError: ("proxy_import_failed", 400),
    CheckInProgressError: ("check_in_progress", 409),
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
    config: Config
    config_path: Path
    token: str
    proxy_pool: ProxyPool
    # Any, а не ProxyHealthChecker: тесты подменяют проверяющий объект
    # фейком, а супервизорской нотации для duck-typing здесь не требуется.
    proxy_checker: Any

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
        updated = self.config.patch(patch)
        updated.save(self.config_path)
        # Замена целиком, а не обновление на месте: обработчики уже держат
        # ссылку на старый объект и должны увидеть новое состояние конфига.
        self.config = updated
        type(self).config = updated
        self._send_json(200, {"config": updated.to_dict()})

    def _handle_proxies_list(self) -> None:
        # Креды маскируются внутри list_proxies: ответ уходит в UI наружу.
        self._send_json(200, {"proxies": self.proxy_pool.list_proxies()})

    def _handle_proxies_add(self) -> None:
        self._send_json(200, self.proxy_pool.add_lines(self._requested_lines()))

    def _handle_proxies_import(self) -> None:
        # Тело читается, чтобы битый JSON дал привычный 400, но его
        # содержимое игнорируется: путь берётся только из конфига.
        self._read_optional_object()
        path = str(self.config.get("paths.proxy_file") or "")
        if not path:
            raise ProxyImportError(
                "paths.proxy_file не задан в конфиге — импорт из файла невозможен"
            )
        self._send_json(200, self.proxy_pool.import_file(path))

    def _handle_proxies_delete(self) -> None:
        self.proxy_pool.delete(self._requested_proxy_id())
        self._send_json(200, {"deleted": True})

    def _handle_proxies_check(self) -> None:
        self._read_optional_object()
        # CheckInProgressError отсюда уходит в 409 check_in_progress.
        self.proxy_checker.start()
        self._send_json(200, {"started": True})

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

    def _requested_proxy_id(self) -> int:
        raw = self._read_json_body()
        if not isinstance(raw, dict):
            raise InvalidRequestError("ожидается объект с полем id")
        proxy_id = raw.get("id")
        # bool — подкласс int: JSON true не должно пройти как id=1.
        if isinstance(proxy_id, bool) or not isinstance(proxy_id, int):
            raise InvalidRequestError("поле id должно быть целым числом")
        return proxy_id

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
    ("POST", "/control/proxies/delete"): "_handle_proxies_delete",
    ("POST", "/control/proxies/check"): "_handle_proxies_check",
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
        proxy_checker: Any = None,
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
        self.config = config
        self.token = token
        self.config_path = Path(config_path)
        # Пул прокси по умолчанию строится на той же БД, что и супервизор:
        # сервер не знает про путь к базе иначе, чем через его store.
        self.proxy_pool = (
            proxy_pool if proxy_pool is not None else ProxyPool(supervisor.store.db_path)
        )
        self.proxy_checker = (
            proxy_checker
            if proxy_checker is not None
            else ProxyHealthChecker(self.proxy_pool)
        )
        self.host = host
        self.requested_port = port
        self.port = port
        self._httpd: _ControlPlaneHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # Публичная ссылка на класс-обработчик: нужна и для диагностики, и
        # тестам, которые проверяют поведение при неожиданном исключении.
        self.handler_class: type[ControlPlaneHandler] | None = None

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
                "config": self.config,
                "config_path": self.config_path,
                "token": self.token,
                "proxy_pool": self.proxy_pool,
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
