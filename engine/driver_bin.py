"""Подготовка chromedriver для воркеров (поймано живым e2e-прогоном).

Две проблемы одного места — ``webdriver._get_driver_exe_path``:

1. UC 3.5.5 при старте **слепо патчит байты драйвера** (заменяет блок
   ``{window.cdc...;}`` на ``{console.log("undetected chromedriver...")}``).
   На CfT-драйвере 153 эта замена ломает путь ``maximize_window()`` —
   хромодрайвер отвечает ``'Runtime.evaluate' wasn't found``, и каждый
   сценарий умирает на первой же команде после создания браузера.
   Версии драйвера и браузера здесь совпадают (153/153), поэтому обход
   версионной проверки UC не нужен: детект ``is_binary_patched`` ищет
   строку ``b"undetected chromedriver"`` — добавляем её в конец файла
   (ELF игнорирует хвост), и UC патч пропускает, а сам байткод остаётся
   нетронутым. Стелс-часть не страдает: ``--disable-blink-features=
   AutomationControlled`` legacy передаёт сам.

2. Если файла нет, UC качает **последний** релиз (154) под браузер 153 —
   ``session not created``. Скачиваем соответственно версии браузера из
   Chrome-for-Testing: точный версии, а при 404 — ближайший той же
   ветки/major из ``known-good-versions-with-downloads.json``.

Все сетевые ходы инжектируются (``fetch_bytes``/``fetch_text``), поэтому
логика тестируется без сети, а реальная сеть живёт только в ``prepare_driver``.
"""

from __future__ import annotations

import io
import os
import platform
import re
import ssl
import subprocess
import zipfile
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

# Корневые сертификаты для HTTPS-загрузок драйвера.
#
# В замороженном бинарнике (PyInstaller one-file) `ssl` не находит системные
# сертификаты: пути OpenSSL указывают на сборочную машину, и каждый urlopen
# падает с «CERTIFICATE_VERIFY_FAILED / unable to get local issuer» — поймано
# живым прогоном на macOS, где воркер умирал на «драйвер не найден, каталог
# known-good недоступен». Импорт стоит на верхнем уровне (а не в функции) не
# ради красоты: PyInstaller кладёт в бандл только видимый графу импорт, и
# ленивый `import certifi` внутри функции в бандл бы не попал.
try:
    import certifi
except ImportError:  # без requests certifi нет — спасут системные bundle'ы
    certifi = None

# Системные CA-бандлы на случай, если certifi недоступен.
_SYSTEM_CAFILES = (
    "/etc/ssl/cert.pem",  # macOS (системный LibreSSL bundle)
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu/NixOS
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL/Fedora
)

# Маркер, который ищет UC-Patcher.is_binary_patched.
UC_MARKER = b"undetected chromedriver"

CFT_BASE = "https://storage.googleapis.com/chrome-for-testing-public"
KGV_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "known-good-versions-with-downloads.json"
)

FetchBytes = Callable[[str], bytes]
FetchText = Callable[[str], str]

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+)")


def browser_version(executable: str, runner: Callable | None = None) -> str:
    """Версия браузера из ``<binary> --version``. Ошибка запуска — RuntimeError."""
    run = runner or subprocess.run
    try:
        result = run([executable, "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"не удалось получить версию браузера {executable}: {exc}") from exc
    output = f"{result.stdout}{result.stderr}"
    match = _VERSION_RE.search(output)
    if match is None:
        raise RuntimeError(f"в выводе '--version' нет версии: {output.strip()[:120]!r}")
    return match.group(1)


def installed_driver_version(path: str | Path) -> str | None:
    """Версия уже лежащего драйвера из ``<driver> --version``. None — не вышло.

    Кэш драйвера живёт отдельно от браузера и не пересоздаётся при смене
    браузера: подменить Chromium 153 на Chrome 140 и взять старый драйвер
    значит получить ``session not created: only supports Chrome version 153``
    уже на старте Chrome. Версию читаем с самого файла, а не запоминаем в
    маркере: файл мог положить туда прежний код или оператор руками.

    Нечитаемый файл (не бинарь, нет прав, обрезан) — None, а не ошибка:
    такое сравнивать не с чем, и правило «уже лежит → не трогаем» важнее
    лишней перекачки.
    """

    try:
        result = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search(f"{result.stdout}{result.stderr}")
    return None if match is None else match.group(1)


def _same_major(left: str | None, right: str | None) -> bool:
    """Совпадение major: chromedriver связан с major браузера жёстко."""
    if left is None or right is None:
        return True
    return left.split(".", 1)[0] == right.split(".", 1)[0]



def cft_platform() -> str:
    """Платформенная метка CfT: ``linux64`` | ``mac-x64`` | ``mac-arm64`` | ``win64``."""
    import sys

    if sys.platform.startswith("linux"):
        return "linux64"
    if sys.platform == "darwin":
        return "mac-arm64" if platform.machine().lower() in ("arm64", "aarch64") else "mac-x64"
    return "win64"


def _platform_zip() -> str:
    return f"chromedriver-{cft_platform()}.zip"


def _cafile_candidates() -> tuple[str | None, ...]:
    """Файлы корневых сертификатов в порядке предпочтения."""
    first: tuple[str | None, ...] = (certifi.where(),) if certifi is not None else ()
    return first + _SYSTEM_CAFILES


def _ssl_context() -> ssl.SSLContext:
    """HTTPS-контекст с CA-файлом, который действительно существует.

    Первый существующий кандидат из :func:`_cafile_candidates`; если ни
    certifi, ни системные bundle'ы не найдены — дефолтные пути OpenSSL
    (в dev-окружении они и так работают).
    """
    for candidate in _cafile_candidates():
        if candidate and Path(candidate).is_file():
            return ssl.create_default_context(cafile=candidate)
    return ssl.create_default_context()


def _default_fetch_bytes(url: str) -> bytes:
    with urlopen(url, timeout=30, context=_ssl_context()) as response:  # noqa: S310 — свои домены CfT/gh
        return response.read()


def _default_fetch_text(url: str) -> str:
    with urlopen(url, timeout=30, context=_ssl_context()) as response:  # noqa: S310
        return response.read().decode("utf-8")


def resolve_chromedriver_url(
    version: str,
    fetch_bytes: FetchBytes = _default_fetch_bytes,
    fetch_text: FetchText = _default_fetch_text,
) -> str:
    """URL zip'а драйвера под версию браузера.

    Точная версия → ``.../{version}/chromedriver-<platform>.zip``.
    CfT не хранит каждую патч-версию браузера (на машине 153.0.8010.36,
    а в каталоге — 153.0.8010.52), поэтому 404/сетевая ошибка → берём из
    ``known-good-versions-with-downloads.json`` последний релиз того же
    major с текущей платформой.
    """
    import json

    exact = f"{CFT_BASE}/{version}/{_platform_zip()}"
    try:
        fetch_bytes(exact)
    except (HTTPError, URLError, OSError, RuntimeError):
        pass
    else:
        return exact

    try:
        catalog = json.loads(fetch_text(KGV_URL))
    except Exception as exc:
        raise RuntimeError(
            f"драйвер {version} не найден, каталог known-good недоступен: {exc}"
        ) from exc

    major = version.split(".", 1)[0]
    plat = cft_platform()
    best: tuple[str, str] | None = None
    for row in catalog.get("versions", []):
        candidate_version = str(row.get("version", ""))
        if not candidate_version.startswith(f"{major}."):
            continue
        for item in row.get("downloads", {}).get("chromedriver", []):
            if item.get("platform") == plat:
                best = (candidate_version, item["url"])
    if best is None:
        raise RuntimeError(f"в known-good нет chromedriver для major {major} ({plat})")
    return best[1]


def _append_marker(path: Path) -> None:
    """Дописывает маркер UC в конец файла (идемпотентно)."""
    data = path.read_bytes()
    if UC_MARKER in data:
        return
    with path.open("ab") as handle:
        handle.write(b"\n" + UC_MARKER + b"\n")


def prepare_driver(
    dest: str | Path,
    *,
    fetch_bytes: FetchBytes = _default_fetch_bytes,
    fetch_text: FetchText = _default_fetch_text,
    browser_ver: Callable[[], str] | None = None,
) -> Path:
    """Готовит драйвер по пути ``dest``: скачивает при отсутствии, ставит маркер.

    Скачивание атомарно (tmp + ``os.replace``), поэтому параллельные воркеры
    не ловят частичный файл. Маркер ставится после любой поставки — UC патч
    пропущен, байткод нетронут.

    Файл на месте — возвращается без сети, но только пока его major совпадает
    с major браузера: кэш переживает смену браузера (154 → 140), а драйвер
    от чужого major даёт ``session not created`` уже на старте Chrome. Если
    сверить не с чего (драйвер не отдаёт версию, браузер не отвечает),
    действует прежнее правило «лежит → не трогаем».
    """
    dest = Path(dest)
    version_fn = browser_ver or (lambda: browser_version(_find_browser()))

    if dest.is_file():
        installed = installed_driver_version(dest)
        browser = None
        if installed is not None:
            try:
                browser = version_fn()
            except RuntimeError:
                browser = None
        if browser is None or _same_major(installed, browser):
            _append_marker(dest)
            return dest
        # Major разошёлся — старый драйвер уже бесполезен, перекачиваем под
        # новый: версия браузера тут же понадобится для URL, переиспользуем.
        version = browser
    else:
        version = version_fn()

    url = resolve_chromedriver_url(version, fetch_bytes=fetch_bytes, fetch_text=fetch_text)
    try:
        payload = fetch_bytes(url)
    except (HTTPError, URLError, OSError) as exc:
        raise RuntimeError(f"не удалось скачать chromedriver с {url}: {exc}") from exc

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        member = next(
            (n for n in archive.namelist() if n.rsplit("/", 1)[-1].startswith("chromedriver")),
            None,
        )
        if member is None:
            raise RuntimeError(f"в zip нет chromedriver: {archive.namelist()}")
        binary = archive.read(member)

    tmp = dest.with_name(dest.name + f".tmp{os.getpid()}")
    tmp.write_bytes(binary)
    tmp.chmod(0o755)
    os.replace(tmp, dest)
    _append_marker(dest)
    return dest


def _find_browser() -> str:
    """Бинарь браузера: окружение → UC-поиск → известные пути."""
    import undetected_chromedriver as uc

    found = uc.find_chrome_executable()
    if found:
        return found
    for candidate in ("/run/current-system/sw/bin/chromium", "/usr/bin/chromium", "/usr/bin/google-chrome"):
        if Path(candidate).is_file():
            return candidate
    raise RuntimeError("браузер не найден: нет chromium/chrome в PATH")


def driver_ready(path: str | Path) -> bool:
    """Драйвер установлен и несёт маркер UC (значит, UC патч не нужен)."""
    p = Path(path)
    return p.is_file() and UC_MARKER in p.read_bytes()
