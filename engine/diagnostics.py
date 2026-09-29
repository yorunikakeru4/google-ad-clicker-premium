"""Диагностика сессии: снимок параметров браузера и проверка согласованности.

Снимок отвечает на один вопрос оператора: **как сайт видит эту сессию** — и
какие в её параметрах есть несостыковки. Данные собираются в три шага:

1. **Страница** (``PAGE_SNIPPET``, ``execute_script``): UA, языки, часовой
   пояс, экран и окно, ``navigator.webdriver``, ядра/память, WebGL
   vendor/renderer — всё, что читается из контекста страницы.
2. **Echo** (``ECHO_SNIPPET``, ``execute_async_script``): fetch к
   :data:`ECHO_URL` **из контекста страницы** — то есть через тот же прокси и
   с теми же заголовками, что уйдут наружу реально. Ответ даёт
   фактические заголовки и внешний IP. URL недоступен или не ответил за
   :data:`ECHO_TIMEOUT_MS` — поля остаются NULL, а причина идёт строкой в
   ``suspicion_flags``: отказ echo не роняет ни сбор, ни воркер.
   Oговорка: запрос идёт с текущего origin (на старте сессии — ``about:blank``,
   то есть ``Origin: null``), поэтому echo-сервис обязан отдавать CORS. Если
   не отдаёт — срабатывает ровно тот же путь «поля null + флаг», что
   закреплён тестами: работа или честный отказ, но не молчаливая полуснимка.
3. **Внутренний IP** (``LOCAL_IP_SNIPPET``): кандидат ``RTCPeerConnection``
   с коротким таймаутом, иначе ``None`` — best effort, Chrome часто прячет
   локальные адреса за mDNS.

Тесты сети не используют: ``echo_fetcher``/``local_ip_fetcher`` инжектируются,
драйвер — заглушка.

Согласованность — четыре чистых правила (:mod:`см. ниже`), каждое из которых
при нехватке данных возвращает ``None``, а не флаг: ложное «подозрение»
хуже его отсутствия. Результат — список строк в колонке
``suspicion_flags``; пустой список означает «нарушений не найдено».

Триггеры (см. :func:`session_checkpoint` и :func:`observe_signal`):

* **автосбор** — один раз на сессию воркера, при первом чекпоинте с живым
  драйвером (сразу после ``create_webdriver``);
* **kv-сигнал** ``DIAGNOSTICS_REQUESTED_<browser_id>`` — ставит API, видит
  воркер. Чекпоинт с драйвером его собирает и снимает после ЛЮБОЙ попытки;
  чекпоинт цикла (пауза, между итерациями) — только наблюдает и логирует:
  живого браузера там нет, флаг остаётся до следующего сценария, поэтому
  запрос оператора не теряется и не превращается в вечный ретрай.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from engine.db import migrations

# --- константы -----------------------------------------------------------------

# Echo «headers + ip»: тело ответа содержит фактические заголовки запроса и
# внешний IP (``origin``). Меняется только здесь — контракт для тестов и для
# UI один: ответ разбирается как ``{"headers": {...}, "origin": "ip"}``.
ECHO_URL = "https://httpbin.org/anything"

# Таймауты живут внутри JS (Promise.race/ setTimeout), а не в драйвере:
# collect не меняет настройки чужого WebDriver, а результат приходит в
# отведённое время даже при висящей странице.
ECHO_TIMEOUT_MS = 5000
LOCAL_IP_TIMEOUT_MS = 1500

# Ключ kv-сигнала: значение — метка времени (см. request_signal), снимается
# воркером после попытки сбора. Паттерн тот же, что у PAUSE_REQUESTED.
DIAGNOSTICS_FLAG_PREFIX = "DIAGNOSTICS_REQUESTED_"

# Таблица локалей по странам (legacy читает её в utils.get_locale_language).
COUNTRY_LOCALES_FILE = Path(__file__).resolve().parents[1] / "country_to_locale.json"

# Снимок страницы. Возвращает объект — Selenium сериализует его в dict.
PAGE_SNIPPET = """
const gl = (() => {
  try {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
    if (!ctx) { return {vendor: null, renderer: null}; }
    let info = null;
    try { info = ctx.getExtension('WEBGL_debug_renderer_info'); } catch (e) { info = null; }
    const vendor = info
      ? ctx.getParameter(info.UNMASKED_VENDOR_WEBGL)
      : ctx.getParameter(ctx.VENDOR);
    const renderer = info
      ? ctx.getParameter(info.UNMASKED_RENDERER_WEBGL)
      : ctx.getParameter(ctx.RENDERER);
    return {vendor: vendor || null, renderer: renderer || null};
  } catch (e) {
    return {vendor: null, renderer: null};
  }
})();
const languages = (navigator.languages && navigator.languages.length)
  ? Array.from(navigator.languages).join(', ')
  : (navigator.language || null);
return {
  user_agent: navigator.userAgent || null,
  accept_language: languages || null,
  timezone_id: (() => {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || null; }
    catch (e) { return null; }
  })(),
  screen_w: (screen && screen.width) || null,
  screen_h: (screen && screen.height) || null,
  window_w: window.innerWidth || null,
  window_h: window.innerHeight || null,
  platform: navigator.platform || null,
  webdriver: typeof navigator.webdriver === 'boolean' ? navigator.webdriver : null,
  hardware_concurrency: typeof navigator.hardwareConcurrency === 'number'
    ? navigator.hardwareConcurrency : null,
  device_memory: typeof navigator.deviceMemory === 'number'
    ? navigator.deviceMemory : null,
  webgl_vendor: gl.vendor,
  webgl_renderer: gl.renderer
};
"""

# echo: последний аргумент execute_async_script — колбэк Selenium.
ECHO_SNIPPET = """
const done = arguments[arguments.length - 1];
const url = arguments[0];
const timeoutMs = arguments[1];
let settled = false;
const finish = (value) => {
  if (settled) { return; }
  settled = true;
  done(value);
};
const timer = setTimeout(
  () => finish({ok: false, error: 'timeout after ' + timeoutMs + 'ms'}),
  timeoutMs
);
try {
  fetch(url, {credentials: 'omit', headers: {'Accept': 'application/json'}})
    .then((response) => response.json())
    .then((body) => { clearTimeout(timer); finish({ok: true, body: body}); })
    .catch((error) => { clearTimeout(timer); finish({ok: false, error: String(error)}); });
} catch (error) {
  clearTimeout(timer);
  finish({ok: false, error: String(error)});
}
"""

# Внутренний IP: кандидат ICE без внешних серверов. Chrome отдаёт локальные
# адреса в obfuscated-виде (mDNS) — тогда результат остаётся null, и это
# нормально: поле best effort, флагов на его отсутствие не предусмотрено.
LOCAL_IP_SNIPPET = """
const done = arguments[arguments.length - 1];
const timeoutMs = arguments[0];
let pc = null;
let settled = false;
const finish = (value) => {
  if (settled) { return; }
  settled = true;
  try { if (pc) { pc.close(); } } catch (e) {}
  done(value);
};
try {
  pc = new RTCPeerConnection({iceServers: []});
  pc.createDataChannel('diagnostics');
  pc.onicecandidate = (event) => {
    if (!event || !event.candidate) { finish(null); return; }
    const match = /(\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3})/.exec(
      event.candidate.candidate
    );
    if (match) { finish(match[1]); }
  };
  setTimeout(() => finish(null), timeoutMs);
  pc.createOffer()
    .then((offer) => pc.setLocalDescription(offer))
    .catch(() => finish(null));
} catch (error) {
  finish(null);
}
"""

# Язык из заголовка: первый тег из possibly-мусорной строки. Legacy складывает
# список локалей в str() ("['de-DE', 'en-US']") — regex разбирает и это.
_LOCALE_TAG = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*")

_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

# ОС в UA проверяется в этом порядке: Android содержит «Linux», Windows Phone —
# «Windows», поэтому общие паттерны обязаны идти последними.
_OS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("windows", re.compile(r"Windows", re.IGNORECASE)),
    ("android", re.compile(r"Android", re.IGNORECASE)),
    ("ios", re.compile(r"iPhone|iPad|iPod", re.IGNORECASE)),
    ("mac", re.compile(r"Mac OS X|Macintosh", re.IGNORECASE)),
    ("linux", re.compile(r"X11|Linux|CrOS", re.IGNORECASE)),
)

# Что navigator.platform обязан начинаться с для каждой ОС. Android Chrome
# отдаёт «Linux armv8l», поэтому для android принят и linux-префикс: это не
# рассинхрон, а исторический ответ платформы.
_PLATFORM_PREFIXES: dict[str, tuple[str, ...]] = {
    "windows": ("win",),
    "mac": ("mac",),
    "ios": ("iphone", "ipad", "ipod"),
    "android": ("android", "linux"),
    "linux": ("linux", "x11"),
}

# Приоритет ядра в UA: Edge и Opera живут поверх Chrome и содержат его токен.
_UA_CORE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("edge", re.compile(r"Edg[A-Za-z]*/")),
    ("opera", re.compile(r"(?:OPR|Opera)/")),
    ("chrome", re.compile(r"(?:Chrome|CriOS)/")),
    ("firefox", re.compile(r"(?:Firefox|FxiOS)/")),
    ("safari", re.compile(r"Safari/")),
)

_UA_VERSION_PATTERNS = (
    r"Chrome/(\d+(?:\.\d+)+)",
    r"CriOS/(\d+(?:\.\d+)+)",
    r"Firefox/(\d+(?:\.\d+)+)",
    r"FxiOS/(\d+(?:\.\d+)+)",
    r"Version/(\d+(?:\.\d+)+)",
)


# --- модели данных ---------------------------------------------------------------


@dataclass(frozen=True)
class EchoResult:
    """Разобранный ответ echo: ``None`` в полях — данных нет, не «пусто»."""

    headers: dict[str, Any] | None
    ip: str | None
    error: str | None


@dataclass(frozen=True)
class PageSnapshot:
    """Поля ``PAGE_SNIPPET``, приведённые к типам. Неизвестные ключи
    ответа отбрасываются: в снимок попадает только белый список полей."""

    user_agent: str | None = None
    accept_language: str | None = None
    timezone_id: str | None = None
    screen_w: int | None = None
    screen_h: int | None = None
    window_w: int | None = None
    window_h: int | None = None
    platform: str | None = None
    webdriver: bool | None = None
    hardware_concurrency: int | None = None
    device_memory: float | None = None
    webgl_vendor: str | None = None
    webgl_renderer: str | None = None


@dataclass
class DiagnosticSnapshot:
    """Один снимок сессии: колонки таблицы ``diagnostics`` + контекст.

    Поля сверх колонок (``window_*``, ``local_ip``, ``browser_core``,
    ``webdriver``, ядра/память, ``geo_timezone``) участвуют в правилах
    согласованности и в диагностике, но в таблицу не пишутся — контракт
    колонок зафиксирован планом (§2) и его читает Rust-читалка UI.
    """

    ts: float
    browser_id: str
    proxy_id: int | None = None
    ip: str | None = None
    country: str | None = None
    user_agent: str | None = None
    accept_language: str | None = None
    timezone_id: str | None = None
    screen_w: int | None = None
    screen_h: int | None = None
    platform: str | None = None
    webgl_vendor: str | None = None
    webgl_renderer: str | None = None
    browser_version: str | None = None
    headers: dict[str, Any] | None = None
    suspicion_flags: list[str] = field(default_factory=list)
    # Контекст и поля без колонок.
    window_w: int | None = None
    window_h: int | None = None
    local_ip: str | None = None
    browser_core: str | None = None
    webdriver: bool | None = None
    hardware_concurrency: int | None = None
    device_memory: float | None = None
    geo_timezone: str | None = None

    def record(self, logger: Any) -> None:
        """Записать снимок через логгер (биндинг ``browser_id`` + политика
        ошибок ``StoreWriter``). В строку уходят только колонки."""
        logger.record_diagnostic(
            browser_id=self.browser_id,
            ts=self.ts,
            proxy_id=self.proxy_id,
            ip=self.ip,
            country=self.country,
            user_agent=self.user_agent,
            accept_language=self.accept_language,
            timezone_id=self.timezone_id,
            screen_w=self.screen_w,
            screen_h=self.screen_h,
            platform=self.platform,
            webgl_vendor=self.webgl_vendor,
            webgl_renderer=self.webgl_renderer,
            browser_version=self.browser_version,
            headers=self.headers,
            suspicion_flags=self.suspicion_flags,
        )


# --- разбор сырых ответов ---------------------------------------------------------


def _text(value: Any) -> str | None:
    """Непустая строка или None. Не-строки — нет данных, а не ``str(value)``."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _as_mapping(raw: Any) -> Mapping[str, Any] | None:
    """dict как есть, JSON-строку — разобрать, остальное — None."""
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return None


def _as_number(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_page_payload(raw: Any) -> PageSnapshot:
    """Разобрать ответ ``PAGE_SNIPPET``: полный, частичный или битый.

    Любое битое значение становится ``None`` — снимок с одним нулевым полем
    полезнее, чем упавший сбор целиком; исключение здесь означало бы
    отказ из-за одной опечатки в ответе страницы.
    """
    data = _as_mapping(raw)
    if data is None:
        return PageSnapshot()
    return PageSnapshot(
        user_agent=_text(data.get("user_agent")),
        accept_language=_text(data.get("accept_language")),
        timezone_id=_text(data.get("timezone_id")),
        screen_w=_int(data.get("screen_w")),
        screen_h=_int(data.get("screen_h")),
        window_w=_int(data.get("window_w")),
        window_h=_int(data.get("window_h")),
        platform=_text(data.get("platform")),
        webdriver=_bool(data.get("webdriver")),
        hardware_concurrency=_int(data.get("hardware_concurrency")),
        device_memory=_float(data.get("device_memory")),
        webgl_vendor=_text(data.get("webgl_vendor")),
        webgl_renderer=_text(data.get("webgl_renderer")),
    )


def parse_echo_payload(raw: Any) -> EchoResult:
    """Разобрать результат ``ECHO_SNIPPET``.

    ``{"ok": true, "body": {...}}`` — успех; ``{"ok": false, "error": ...}``
    либо ``{"error": ...}`` (таймаут из сниппета) — отказ. Всё остальное
    считается некорректным ответом и тоже становится ``error``: молча
    принять мусор за пустой echo значило бы потерять причину.
    """
    data = _as_mapping(raw)
    if data is None:
        return EchoResult(None, None, f"некорректный ответ echo: {type(raw).__name__}")

    error = data.get("error")
    if error is not None and data.get("ok") is not True:
        return EchoResult(None, None, str(error))
    if data.get("ok") is not True:
        return EchoResult(None, None, "некорректный ответ echo: нет признака ok")

    body = data.get("body")
    if not isinstance(body, Mapping):
        return EchoResult(None, None, "некорректный ответ echo: тело не объект")

    headers = body.get("headers")
    origin = body.get("origin")
    ip = None
    if isinstance(origin, str) and origin.strip():
        # При цепочке прокси httpbin отдаёт "ip1, ip2" — наружу ушёл первый.
        ip = origin.split(",")[0].strip() or None
    return EchoResult(headers if isinstance(headers, Mapping) else None, ip, None)


def browser_core_from_ua(user_agent: str | None) -> str | None:
    """Ядро браузера из UA — то, что видит сайт. None — токенов не найдено."""
    ua = _text(user_agent)
    if ua is None:
        return None
    for name, pattern in _UA_CORE_PATTERNS:
        if pattern.search(ua):
            return name
    return None


def browser_version(capabilities: Any, user_agent: str | None) -> str | None:
    """Версия браузера. **Решение: сначала capabilities драйвера, затем UA.**

    UA может быть подменён профилем и соврать о версии реально запущенного
    Chrome, а ``browserVersion`` из capabilities сообщает её честно. Принимается
    только непустая строка: число в capabilities — не версия, а мусор, и по
    нему сверяться нельзя. Fallback — версия из UA (Chrome/Firefox/Safari).
    """
    if isinstance(capabilities, Mapping):
        value = _text(capabilities.get("browserVersion"))
        if value is not None:
            return value
    ua = _text(user_agent)
    if ua is None:
        return None
    for pattern in _UA_VERSION_PATTERNS:
        match = re.search(pattern, ua)
        if match:
            return match.group(1)
    return None


# --- fetcher'ы: сеть только за пределами тестов -----------------------------------


def fetch_echo(driver: Any, *, url: str = ECHO_URL, timeout_ms: int = ECHO_TIMEOUT_MS) -> EchoResult:
    """Запрос echo **из контекста страницы**: те же заголовки, тот же прокси.

    Не бросает: отказ драйвера или таймаут — это ``EchoResult(error=...)``,
    по которому колонки останутся NULL, а причина уйдёт в suspicion_flags.
    """
    try:
        raw = driver.execute_async_script(ECHO_SNIPPET, url, timeout_ms)
    except Exception as exc:
        return EchoResult(None, None, f"{type(exc).__name__}: {exc}")
    return parse_echo_payload(raw)


def fetch_local_ip(driver: Any, *, timeout_ms: int = LOCAL_IP_TIMEOUT_MS) -> str | None:
    """Внутренний IP через ICE-кандидат. Best effort: любая неудача — None."""
    try:
        raw = driver.execute_async_script(LOCAL_IP_SNIPPET, timeout_ms)
    except Exception:
        return None
    if not isinstance(raw, str) or not _IPV4.match(raw.strip()):
        return None
    return raw.strip()


# --- правила согласованности -------------------------------------------------------


def check_language_vs_country(
    accept_language: str | None,
    country: str | None,
    locales: Mapping[str, Sequence[str]],
) -> str | None:
    """Язык Accept-Language ↔ страна прокси.

    Страна не найдена в таблице локалей — данных нет, вывода тоже нет:
    ``utils.get_locale_language`` для неизвестных стран возвращает ``["en"]``,
    и по нему флаговался бы каждый не-английский браузер.
    """
    text = _text(accept_language)
    code = _text(country)
    if text is None or code is None or not locales:
        return None
    allowed = locales.get(code.upper())
    if not allowed:
        return None
    match = _LOCALE_TAG.search(text)
    if match is None:
        return None
    tag = match.group(0)
    language = re.split(r"[-_]", tag)[0].lower()
    allowed_languages = {re.split(r"[-_]", str(item))[0].lower() for item in allowed}
    if language in allowed_languages:
        return None
    return f"Accept-Language {tag!r} не соответствует стране прокси {code.upper()}"


def check_timezone_vs_geo(timezone_id: str | None, geo_timezone: str | None) -> str | None:
    """Часовой пояс страницы ↔ гео-пояс прокси (из get_location).

    Регистр не важен: IANA-идентификаторы приходят из разных сервисов и
    могут отличаться только регистром. Гео-пояса нет (координат/lookup не
    было) — правила нет.
    """
    browser_tz = _text(timezone_id)
    geo_tz = _text(geo_timezone)
    if browser_tz is None or geo_tz is None:
        return None
    if browser_tz.casefold() == geo_tz.casefold():
        return None
    return f"Часовой пояс {browser_tz} не соответствует гео {geo_tz}"


def check_platform_vs_user_agent(user_agent: str | None, platform: str | None) -> str | None:
    """``navigator.platform`` ↔ ОС, заявленная в UA.

    ОС в UA не распознана — данных нет и правила нет; несовпадение
    префикса — реальное расхождение (Windows-UI под macOS-UA и наоборот).
    """
    ua = _text(user_agent)
    plat = _text(platform)
    if ua is None or plat is None:
        return None
    for os_name, pattern in _OS_PATTERNS:
        if pattern.search(ua):
            break
    else:
        return None
    lowered = plat.casefold()
    if any(lowered.startswith(prefix) for prefix in _PLATFORM_PREFIXES[os_name]):
        return None
    return f"Платформа {plat!r} не соответствует ОС {os_name} из User-Agent"


def check_screen_vs_window(
    screen_w: Any, screen_h: Any, window_w: Any, window_h: Any
) -> str | None:
    """Экран ↔ окно: окно обязано помещаться в экран, размеры — положительные.

    Отсутствие любого из четырёх значений — не данных, а не флаг: headless
    и редкие драйверы не отдают ``innerWidth`` до первого layout'а.
    """
    values = (screen_w, screen_h, window_w, window_h)
    if any(value is None for value in values):
        return None
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        return None
    sw, sh, ww, wh = values
    if sw <= 0 or sh <= 0 or ww <= 0 or wh <= 0:
        return f"Некорректные размеры: экран {sw}x{sh}, окно {ww}x{wh}"
    if ww > sw or wh > sh:
        return f"Окно {ww}x{wh} не помещается в экран {sw}x{sh}"
    return None


def compute_suspicion_flags(
    snapshot: DiagnosticSnapshot,
    *,
    locales: Mapping[str, Sequence[str]] | None = None,
    collection_errors: Sequence[str] = (),
) -> list[str]:
    """Собрать ``suspicion_flags``: четыре правила + ошибки сбора.

    Порядок стабилен (язык, пояс, платформа, экран, затем ошибки) — UI
    показывает список как есть, и его порядок не должен прыгать между
    снимками с одинаковыми проблемами.
    """
    flags = [
        flag
        for flag in (
            check_language_vs_country(
                snapshot.accept_language, snapshot.country, locales or {}
            ),
            check_timezone_vs_geo(snapshot.timezone_id, snapshot.geo_timezone),
            check_platform_vs_user_agent(snapshot.user_agent, snapshot.platform),
            check_screen_vs_window(
                snapshot.screen_w, snapshot.screen_h, snapshot.window_w, snapshot.window_h
            ),
        )
        if flag
    ]
    flags.extend(message for message in (str(item) for item in collection_errors) if message)
    return flags


def load_country_locales(path: str | Path | None = None) -> dict[str, list[str]]:
    """Таблица ``страна → локали``. Нет файла или он битый — пустой словарь:

    нехватка данных обязана гасить правила, а не ронять сбор и тем более не
    порождать флаги по догадке.
    """
    source = Path(path) if path is not None else COUNTRY_LOCALES_FILE
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, list[str]] = {}
    for country, locales in raw.items():
        if not isinstance(country, str) or not isinstance(locales, list):
            continue
        cleaned = [item for item in locales if isinstance(item, str) and item.strip()]
        if cleaned:
            result[country.upper()] = cleaned
    return result


# --- сбор снимка --------------------------------------------------------------------


def _safe_echo(fetcher: Callable[[Any], Any], driver: Any) -> EchoResult:
    """Echo не должен ронять сбор: и сам fetcher, и его результат проходят
    через разбор с ошибкой вместо исключения."""
    try:
        result = fetcher(driver)
    except Exception as exc:
        return EchoResult(None, None, f"{type(exc).__name__}: {exc}")
    if isinstance(result, EchoResult):
        return result
    return parse_echo_payload(result)


def _safe_local_ip(fetcher: Callable[[Any], Any], driver: Any) -> str | None:
    try:
        return fetcher(driver)
    except Exception:
        return None


def collect_snapshot(
    driver: Any,
    *,
    browser_id: str,
    ts: float | None = None,
    proxy_id: int | None = None,
    country: str | None = None,
    echo_fetcher: Callable[[Any], Any] | None = None,
    local_ip_fetcher: Callable[[Any], Any] | None = None,
    locales: Mapping[str, Sequence[str]] | None = None,
) -> DiagnosticSnapshot:
    """Собрать полный снимок у живого драйвера.

    Страница обязательна (без неё собрано нечего, исключение уходит вызывающему
    чекпоинту), echo и внутренний IP — best effort. ``country`` без явного
    значения берётся из гео прокси, запомненного драйвером
    (``_geo_country``, см. ``webdriver.create_webdriver``); ``geo_timezone``
    читается оттуда же — это значение из ``get_location`` до приоритета профиля.
    """
    page = parse_page_payload(driver.execute_script(PAGE_SNIPPET))
    echo = _safe_echo(echo_fetcher or fetch_echo, driver)
    local_ip = _safe_local_ip(local_ip_fetcher or fetch_local_ip, driver)

    capabilities = getattr(driver, "capabilities", None)
    geo_timezone = _text(getattr(driver, "_geo_timezone", None))
    effective_country = _text(country) or _text(getattr(driver, "_geo_country", None))

    snapshot = DiagnosticSnapshot(
        ts=time.time() if ts is None else ts,
        browser_id=browser_id,
        proxy_id=proxy_id,
        ip=echo.ip,
        country=effective_country,
        user_agent=page.user_agent,
        accept_language=page.accept_language,
        timezone_id=page.timezone_id,
        screen_w=page.screen_w,
        screen_h=page.screen_h,
        platform=page.platform,
        webgl_vendor=page.webgl_vendor,
        webgl_renderer=page.webgl_renderer,
        browser_version=browser_version(capabilities, page.user_agent),
        headers=dict(echo.headers) if echo.headers is not None else None,
        window_w=page.window_w,
        window_h=page.window_h,
        local_ip=local_ip,
        browser_core=browser_core_from_ua(page.user_agent),
        webdriver=page.webdriver,
        hardware_concurrency=page.hardware_concurrency,
        device_memory=page.device_memory,
        geo_timezone=geo_timezone,
    )
    errors = [f"внешний echo-запрос не выполнен: {echo.error}"] if echo.error else []
    snapshot.suspicion_flags = compute_suspicion_flags(
        snapshot,
        locales=load_country_locales() if locales is None else locales,
        collection_errors=errors,
    )
    return snapshot


# --- прокси воркера ------------------------------------------------------------------


def proxy_context(store: Any, browser_id: str) -> tuple[int | None, str | None]:
    """``(proxy_id, country)`` текущего прокси воркера.

    Источник — строка воркера в БД: ``proxy_id`` закрепляет супервизор при
    спавне (``StateStore.assign_proxy``), а страна читается из строки прокси.
    В снимок уходят только эти два значения — ни кредов, ни адреса прокси
    (их в БД нет причини, а API прокси их маскирует).
    """
    worker = store.get_worker(browser_id)
    proxy_id = worker.get("proxy_id") if worker else None
    if not isinstance(proxy_id, int) or isinstance(proxy_id, bool):
        return None, None
    return proxy_id, _proxy_country(getattr(store, "db_path", None), proxy_id)


def _proxy_country(db_path: Any, proxy_id: int) -> str | None:
    if db_path is None:
        return None
    conn = migrations.connect(db_path)
    try:
        row = conn.execute("SELECT country FROM proxies WHERE id = ?", (proxy_id,)).fetchone()
    finally:
        conn.close()
    return _text(row["country"]) if row is not None else None


# --- kv-сигнал ------------------------------------------------------------------------


def diagnostics_flag_key(browser_id: str) -> str:
    return f"{DIAGNOSTICS_FLAG_PREFIX}{browser_id}"


def request_signal(store: Any, browser_id: str, *, now: float | None = None) -> str:
    """Выставить сигнал: значение — метка времени с микросекундами.

    Новое значение обязано быть строго больше уже выставленного: свежесть
    воркер определяет сравнением «значение > последнего обработанного», и
    повторное нажатие кнопки не должно маскироваться совпадением меток.
    """
    key = diagnostics_flag_key(browser_id)
    value = float(time.time() if now is None else now)
    current = _as_number(store.get_flag(key))
    if current is not None and value <= current:
        value = current + 1e-6
    encoded = f"{value:.6f}"
    store.set_flag(key, encoded)
    return encoded


def read_signal(store: Any, browser_id: str) -> str | None:
    """Значение сигнала или None (нет запроса / он уже снят)."""
    value = store.get_flag(diagnostics_flag_key(browser_id))
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def clear_signal(store: Any, browser_id: str) -> None:
    """Снять сигнал. Пустое значение, а не DELETE: тот же паттерн, что у
    ``PAUSE_REQUESTED`` — ключ на месте, запросов нет."""
    store.set_flag(diagnostics_flag_key(browser_id), "")


def _clear_after_attempt(store: Any, browser_id: str, handled: Any) -> None:
    """Снять сигнал после попытки, но только если он не продвинулся.

    Сбор — это секунды (драйвер, echo с таймаутом), и оператор может нажать
    кнопку ещё раз, пока идёт первый запрос. Слепое снятие затёрло бы свежую
    метку: UI показал бы «requested: 1», а снимка не было бы никогда. Новое
    значение (строго больше обработанного) остаётся в kv — его соберёт
    следующий чекпоинт, и это не вечный ретрай, а второй реальный запрос.
    """
    current = read_signal(store, browser_id)
    if current is None or not is_fresh_signal(current, handled):
        clear_signal(store, browser_id)


def is_fresh_signal(value: Any, last_handled: Any) -> bool:
    """Свежий ли сигнал: непустое значение строго больше последнего
    обработанного. Нечисловое значение считается новым запросом — потерять
    запрос из-за непонятного формата хуже, чем собрать лишний раз.
    """
    text = _text(value)
    if text is None:
        return False
    if last_handled is None or not str(last_handled).strip():
        return True
    previous = str(last_handled).strip()
    if text == previous:
        return False
    new_number = _as_number(text)
    old_number = _as_number(previous)
    if new_number is None or old_number is None:
        return True
    return new_number > old_number


# --- состояние сессии -------------------------------------------------------------------


@dataclass
class _SessionState:
    """Состояние процесса на один browser_id.

    Сессия воркера = процесс: ``auto_collected`` не даёт собирать авто-снимок
    на каждой итерации, ``last_handled`` — метка последнего обработанного
    сигнала (защита от повтора и от гонки «API поставил новое, пока я
    снимал старое»), ``notified`` — за какой сигнал уже отчитался цикл, чтобы
    пауза-опрос каждые 0.5 с не писала в лог одно и то же.

    Поля меняются только с того же потока, что и цикл: чекпоинт цикла и
    чекпоинт сцены выполняются последовательно в одном процессе воркера,
    поэтому блокировка нужна лишь словарю состояний (создание и сброс).
    """

    auto_collected: bool = False
    last_handled: str | None = None
    notified: str | None = None


_session_states: dict[str, _SessionState] = {}
_session_lock = threading.Lock()


def reset_session_state() -> None:
    """Забыть состояние сессий. Нужен тестам: состояние процессное."""
    with _session_lock:
        _session_states.clear()


def _state_for(browser_id: str) -> _SessionState:
    with _session_lock:
        state = _session_states.get(browser_id)
        if state is None:
            state = _SessionState()
            _session_states[browser_id] = state
        return state


def _warn(logger: Any, browser_id: str, message: str, exc: BaseException) -> None:
    """Единая точка отказа сбора: WARNING в ``browser``, без исключений.

    Вызов идёт через ``log(level, category, ...)`` StructuredLogger, а не
    через legacy-обёртки: линт-контракт (tests/test_no_direct_logger_calls.py)
    отличает структурированную запись от прямых вызовов legacy-логгера по
    самому виду вызова, и здесь важно не спрятаться от него, а говорить с
    БД на её языке.
    """
    if logger is None:
        return
    logger.log(
        "WARNING",
        "browser",
        message,
        browser_id=browser_id,
        fields={"error": str(exc), "error_type": type(exc).__name__},
    )


# --- чекпоинты -------------------------------------------------------------------------


def observe_signal(browser_id: str, store: Any, logger: Any = None) -> str | None:
    """Чекпоинт цикла воркера (пауза, между итерациями): увидеть сигнал.

    Живого браузера в цикле нет, поэтому флаг здесь НЕ снимается — он ждёт
    первого драйвера (:func:`session_checkpoint`). Задача наблюдения —
    отчитаться один раз о пришедшем запросе и не спамить при паузе-опросе.
    Возвращает увиденное значение, если это новое уведомление, и None иначе.
    Ошибки чтения не гасятся здесь: их ловит чекпоинт воркера и пишет
    WARNING — глушить их молча значило бы прятать сломанную БД.
    """
    value = read_signal(store, browser_id)
    if value is None:
        return None
    state = _state_for(browser_id)
    if value == state.notified:
        return None
    state.notified = value
    if logger is not None:
        logger.log(
            "INFO",
            "browser",
            "diagnostics requested, waiting for a live browser",
            browser_id=browser_id,
            fields={"signal": value},
        )
    return value


def session_checkpoint(
    driver: Any,
    *,
    browser_id: str,
    store: Any,
    logger: Any = None,
    echo_fetcher: Callable[[Any], Any] | None = None,
    local_ip_fetcher: Callable[[Any], Any] | None = None,
    locales: Mapping[str, Sequence[str]] | None = None,
    now: float | None = None,
) -> bool:
    """Чекпоинт с живым браузером: автосбор за сессию + свежий kv-сигнал.

    Возвращает True, если снимок собран и записан.

    Контракт по отказам:

    * любая ошибка (мёртвый драйвер, упавший fetcher, сломанная БД, отказ
      записи) превращается в WARNING ``browser`` — сбор не роняет ни
      воркер, ни сценарий;
    * свежий сигнал снимается после попытки **в любом случае**: сбор упал —
      снимаем и логируем, иначе каждый следующий чекпоинт повторял бы одну
      и ту же обречённую попытку (вечный ретрай). Исключение — метка,
      продвинутая самим API, пока шёл сбор: её снимать нельзя, иначе второй
      запрос оператора пропал бы (см. :func:`_clear_after_attempt`);
    * автосбор пытается ровно один раз за сессию, даже если первая попытка
      провалилась: повтор на каждом сценарии превратил бы сбой драйвера
      в бесконечные попытки.
    """
    if not isinstance(browser_id, str) or not browser_id.strip():
        return False

    state = _state_for(browser_id)
    try:
        signal = read_signal(store, browser_id)
        fresh = is_fresh_signal(signal, state.last_handled)
    except Exception as exc:
        _warn(logger, browser_id, "diagnostics signal check failed", exc)
        return False

    was_auto = not state.auto_collected
    if not was_auto and not fresh:
        return False

    recorded = False
    try:
        proxy_id, country = proxy_context(store, browser_id)
        snapshot = collect_snapshot(
            driver,
            browser_id=browser_id,
            ts=now,
            proxy_id=proxy_id,
            country=country,
            echo_fetcher=echo_fetcher,
            local_ip_fetcher=local_ip_fetcher,
            locales=load_country_locales() if locales is None else locales,
        )
        if logger is not None:
            snapshot.record(logger)
        recorded = logger is not None
    except Exception as exc:
        _warn(logger, browser_id, "diagnostics snapshot failed", exc)
    finally:
        state.auto_collected = True
        if fresh:
            try:
                _clear_after_attempt(store, browser_id, signal)
            except Exception as exc:
                _warn(logger, browser_id, "diagnostics signal was not cleared", exc)
            state.last_handled = signal

    if recorded:
        reason = "auto+signal" if (was_auto and fresh) else ("auto" if was_auto else "signal")
        fields: dict[str, Any] = {"reason": reason}
        if fresh:
            fields["signal"] = signal
        logger.log(
            "INFO",
            "browser",
            "diagnostics snapshot recorded",
            browser_id=browser_id,
            fields=fields,
        )
    return recorded
