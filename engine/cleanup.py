"""Очистка мусора профилей и файлов (план §5, фаза 10).

**Инвентарь целей.** Воркеры создают мусор в двух местах, и оба — в системном
tempdir: каталоги профилей ``uc_profiles/profile_NNNN`` и
``sb_profiles/profile_NNNN`` (``webdriver.py``) и папки расширения прокси
``proxy_auth_plugin_<8 символов>`` (``proxy._plugins_base_dir`` →
``tempfile.mkdtemp``). Третья категория — CAPTCHA-скриншоты
``engine/screenshots/<browser_id>_<epoch>.png`` (``search_controller``),
которые лежат в рабочем каталоге и переживают и ротацию логов, и рестарты.

**Безопасность — не настраивается и не отключается:**

* удаляются только записи, попавшие в whitelisted-корень *и* совпавшие с
  шаблоном имени цели (``is_within_root`` сравнивает разрешённые пути, поэтому
  ``..`` и подмена компонента симлинком не дают выйти за корень);
* симлинк-кандидат не удаляется и не преследуется: каталог профиля, заменённый
  ссылкой на чужие данные, уходит в лог как ``skipped/symlink``, а внутри
  удаляемого дерева ``shutil.rmtree`` unlink'ит симлинк, не доходя до цели;
* каждая операция логируется категорией ``cleanup`` с полями ``path``,
  ``bytes``, ``result`` (``removed|skipped|error``) и ``reason`` для
  пропусков; исключение одной операции увеличивает ``errors`` и не роняет
  прогон.

**Исключение активных.** Связь «каталог ↔ воркер» устанавливается по самому
надёжному источнику — самому живому процессу: Chrome получает
``--user-data-dir=<каталог>`` в argv, а ``proxy_auth_plugin_*`` держит открытыми
файлы расширения. Поэтому один проход ``psutil`` собирает и ``cmdline()``, и
``open_files()`` живых PID, и любой кандидат, упомянутый внутри, считается
удерживаемым. Дополнительно — грейс по mtime: каталог моложе
``grace_seconds`` пропускается даже без найденного держателя (окно между
созданием каталога и первым открытием в нём файла). Такой подход честен и
без таблицы ``workers``: после аварийного Kill ни PID, ни heartbeat не
сохраняются, а «не открыт никем и старше грейса» — это ровно определение
осиротевшего профиля.

**Срок скриншотов.** Файл считается кандидатом, только если он старше
``behavior.log_retention_days``: упоминание ``screenshot_path`` живёт в
записях лога, которые retention держит ровно N дней, — раньше срока файл
оставил бы битую ссылку в UI, позже — превратился бы в мусор без ссылок.
Вторая константа срока здесь недопустима: два числа про один и тот же
артефact неизбежно разъедутся.

**Ограничения прогона.** Синхронный ``POST /control/cleanup/run`` ограничен
``time_budget_seconds`` (защита от долгого запроса: бюджет исчерпан → дальше
не удаляем, причина уходит в лог) и замок ``threading.Lock`` (плановый и
ручной запуски не гуляют по tempdir одновременно). ``dry_run`` перечисляет и
логирует, но ничего не удаляет и не трогает метку последнего прогона в kv.

Конфигурация полей (``cleanup_time``, ``cleanup_interval_days``) живёт в
``engine.control_plane.config`` и ``config_reader``; расписание — в
``engine.control_plane.daemon``; HTTP — в ``engine.control_plane.api``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import psutil

# --- поля конфига (план §5, фаза 10) -----------------------------------------
#
# Дефолты и границы объявлены здесь же, где живёт потребитель, — по образцу
# engine.log_rotation: config.py и config_reader импортируют их отсюда, а не
# дублируют строками, иначе два источника правды о значении по умолчанию.
DEFAULT_CLEANUP_TIME = "04:00"
DEFAULT_CLEANUP_INTERVAL_DAYS = 1
MIN_CLEANUP_INTERVAL_DAYS = 1
MAX_CLEANUP_INTERVAL_DAYS = 30

# --- kv: последний отчёт, его время и ближайший запуск -----------------------
#
# Ключи таблицы kv, как и у PAUSE_REQUESTED/RUN_STATE, — общий с UI канал.
CLEANUP_LAST_TS_KEY = "CLEANUP_LAST_TS"
CLEANUP_LAST_REPORT_KEY = "CLEANUP_LAST_REPORT"
CLEANUP_NEXT_RUN_KEY = "CLEANUP_NEXT_RUN"

# Грейс по mtime: моложе — значит каталог только что создаётся воркером и
# браузер ещё не успел открыть в нём файлы. 10 минут — с запасом на медленный
# старт Chrome и на паузу между spawn и первым запросом.
DEFAULT_GRACE_SECONDS = 600.0

# Бюджет одного прогона. Синхронный HTTP-запрос не должен держать соединение
# дольше, чем UI готов ждать; на практике инвентарь укладывается в секунды,
# а лимит срабатывает только на деградировавшей машине.
DEFAULT_TIME_BUDGET_SECONDS = 8.0

# Погрешность сравнения «прошло ли N дней с последнего прогона». Цель расписания
# вычисляется с точностью до секунды, а нить просыпается чуть позже: без этой
# минуты копеечная задержка пробуждения вычиталась бы из интервала и переносила
# запуск ровно на следующие сутки. Ручной запуск и тем более запуск час назад
# погрешностью не являются — интервал всё равно не прошёл.
CLEANUP_INTERVAL_SLACK_SECONDS = 60.0

# --- шаблоны имён -------------------------------------------------------------
#
# Имена не выдуманы: profile_NNNN создаёт webdriver.py (randint(1000, 9999)),
# proxy_auth_plugin_<8> — tempfile.mkdtemp (алфавит a-z0-9_, ровно 8 символов),
# <browser_id>_<epoch>.png — search_controller._captcha_screenshot.
UC_PROFILES_DIRNAME = "uc_profiles"
SB_PROFILES_DIRNAME = "sb_profiles"
PROXY_PLUGIN_DIRNAME_PREFIX = "proxy_auth_plugin_"
SCREENSHOTS_DIRNAME = Path("engine/screenshots")

PROFILE_DIR_RE = re.compile(r"profile_\d{4}")
PLUGIN_DIR_RE = re.compile(rf"{re.escape(PROXY_PLUGIN_DIRNAME_PREFIX)}[a-z0-9_]{{8}}")
SCREENSHOT_FILE_RE = re.compile(r".+_\d{10}\.png")

# --- сообщения в лог (категория cleanup) --------------------------------------
#
# Тексты — контракт для тестов и для поиска по логам из UI.
MSG_REMOVED = "cleanup item removed"
MSG_SKIPPED = "cleanup item skipped"
MSG_FAILED = "cleanup item failed"
MSG_FINISHED = "cleanup finished"
MSG_BUDGET_EXHAUSTED = "cleanup time budget exhausted"
MSG_STATUS_UNREADABLE = "cleanup status unreadable"


class OutsideRootError(Exception):
    """Путь оказался за пределами whitelisted-корня (защита от TOCTOU)."""


@dataclass(frozen=True)
class CleanupTarget:
    """Одна категория мусора: корень-whitelist, шаблон имени и тип записи."""

    kind: str
    root: Path
    pattern: re.Pattern[str]
    is_dir: bool


@dataclass(frozen=True)
class _Candidate:
    path: Path
    target: CleanupTarget


def default_targets(
    temp_root: str | Path | None = None,
    screenshots_root: str | Path | None = None,
) -> list[CleanupTarget]:
    """Цели фазы 10.

    ``temp_root``/``screenshots_root`` — для тестов: боевой вызов берёт
    системный tempdir и ``engine/screenshots`` относительно текущего каталога
    (так же каталог создаёт ``search_controller``). Корни вычисляются на
    каждый вызов, а не при импорте: cwd теста меняется, а каталог скриншотов
    относительный.
    """
    temp = Path(tempfile.gettempdir()) if temp_root is None else Path(temp_root)
    screenshots = (
        Path.cwd() / SCREENSHOTS_DIRNAME
        if screenshots_root is None
        else Path(screenshots_root)
    )
    return [
        CleanupTarget("uc_profile", temp / UC_PROFILES_DIRNAME, PROFILE_DIR_RE, True),
        CleanupTarget("sb_profile", temp / SB_PROFILES_DIRNAME, PROFILE_DIR_RE, True),
        CleanupTarget("proxy_plugin", temp, PLUGIN_DIR_RE, True),
        CleanupTarget("screenshot", screenshots, SCREENSHOT_FILE_RE, False),
    ]


def _validate_interval_days(interval_days: int) -> None:
    if not isinstance(interval_days, int) or isinstance(interval_days, bool):
        raise ValueError(f"период очистки обязан быть целым числом, получено {interval_days!r}")
    if not MIN_CLEANUP_INTERVAL_DAYS <= interval_days <= MAX_CLEANUP_INTERVAL_DAYS:
        raise ValueError(
            f"период очистки обязан быть в пределах "
            f"{MIN_CLEANUP_INTERVAL_DAYS}..{MAX_CLEANUP_INTERVAL_DAYS}, "
            f"получено {interval_days!r}"
        )


def _parse_cleanup_time(cleanup_time: str) -> tuple[int, int]:
    if not isinstance(cleanup_time, str):
        raise ValueError(f"время очистки обязано быть строкой ЧЧ:ММ, получено {cleanup_time!r}")
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", cleanup_time)
    if match is None:
        raise ValueError(
            f"время очистки обязано быть ЧЧ:ММ в пределах 00:00-23:59, "
            f"получено {cleanup_time!r}"
        )
    return int(match.group(1)), int(match.group(2))


def seconds_until_cleanup(
    now: float,
    cleanup_time: str,
    interval_days: int = DEFAULT_CLEANUP_INTERVAL_DAYS,
    last_ts: float | None = None,
) -> float:
    """Секунд до ближайшего разрешённого запуска очистки — всегда строго > 0.

    Расписание повторяет закрытие дня (``seconds_until_day_close``): цель —
    ближайшие локальные ``cleanup_time``, ровно в них и далее — следующие, иначе
    нить, проснувшаяся в 04:00:00, ждала бы ещё сутки. Отличие — учёт
    ``interval_days``: пока с ``last_ts`` не прошло полных суток (с поправкой на
    ``CLEANUP_INTERVAL_SLACK_SECONDS``), ранние цели пропускаются и цель
    отодвигается до первой, наступающей после интервала. Поэтому ручной запуск
    в 12:00 честно сдвигает следующий плановый запуск, а не подрывается им.

    ``ValueError`` на мусорном времени/периоде: конфиг проходит валидацию до
    этого места, но расписание не имеет права молча считать не то.
    """
    hour, minute = _parse_cleanup_time(cleanup_time)
    _validate_interval_days(interval_days)

    base = now if last_ts is None else max(now, last_ts + interval_days * 86400 - CLEANUP_INTERVAL_SLACK_SECONDS)
    start = date.fromtimestamp(base)
    for offset in range(0, 8):
        day = start + timedelta(days=offset)
        candidate = time.mktime((day.year, day.month, day.day, hour, minute, 0, 0, 0, -1))
        if candidate > now and candidate >= base:
            return candidate - now
    # Недостижимо: цели перебираются от ``base`` и первая же наступает не позже
    # следующих суток. Ошибка, а не тихий ноль — ноль превратил бы цикл в
    # горячий.
    raise ValueError(f"не удалось вычислить расписание очистки для {cleanup_time!r}")


def is_cleanup_due(now: float, last_ts: float | None, interval_days: int) -> bool:
    """Разрешён ли запуск прямо сейчас: интервал с последнего прогона прошёл.

    Та же погрешность ``CLEANUP_INTERVAL_SLACK_SECONDS``, что и в
    ``seconds_until_cleanup``: цель и проверка обязаны считать интервал одинаково,
    иначе нить, проснувшаяся ровно по цели, увидит «интервал не прошёл» и
    пропустит запуск.
    """
    _validate_interval_days(interval_days)
    if last_ts is None:
        return True
    return (now - last_ts) >= (interval_days * 86400 - CLEANUP_INTERVAL_SLACK_SECONDS)


def last_run_ts(store: Any) -> float | None:
    """Метка времени последнего реального прогона из kv; None — прогона не было."""
    raw = store.get_flag(CLEANUP_LAST_TS_KEY)
    if raw is None or not str(raw).strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def is_within_root(root: str | Path, path: str | Path) -> bool:
    """Путь (после разрешения симлинков) находится внутри корня или равен ему.

    Резолвятся обе стороны: так ловятся и ``..``, и подмена компонента пути
    симлинком наружу. Ошибка разрешения — тоже «не внутри»: сомнительный путь
    не удаляется.
    """
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return False
    return True


def _tree_size(path: Path, is_dir: bool) -> int:
    """Размер кандидата в байтах для отчёта.

    Симлинки не преследуются (``followlinks=False`` + ``lstat``): размер
    чужого файла, на который указывает ссылка внутри профиля, — не размер
    того, что мы удаляем.
    """
    if not is_dir:
        try:
            return path.lstat().st_size
        except OSError:
            return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def _held_pids(paths: Iterable[Path]) -> dict[Path, int]:
    """Кандидаты, которые держат живые процессы: ``путь -> pid``.

    Два источника, оба из живого процесса:

    * ``cmdline`` — точная связь «каталог ↔ воркер»: Chrome получает
      ``--user-data-dir=<каталог>``, расширение прокси лежит в argv ровно в
      своём каталоге. Сравнение подстрокой по обеим формам пути (как задан и
      как разрешён) ловит и абсолютный, и резолвнутый вид.
    * ``open_files`` — держатель без упоминания в argv: файл внутри каталога
      открыт дескриптором (cookies/Singleton* у профиля, manifest расширения).

    Завершившийся процесс между итерациями, ``AccessDenied`` и зомби — это не
    отказ очистки, а отсутствие информации о конкретном PID, поэтому процесс
    просто пропускается.
    """
    wanted: dict[str, Path] = {}
    for path in paths:
        wanted.setdefault(str(path), path)
        try:
            wanted.setdefault(str(path.resolve()), path)
        except OSError:
            continue
    holders: dict[Path, int] = {}
    if not wanted:
        return holders

    def _mark(candidate: str) -> None:
        for text, path in wanted.items():
            if candidate == text or candidate.startswith(text + os.sep) or text in candidate:
                holders.setdefault(path, proc.pid)

    for proc in psutil.process_iter():
        try:
            cmdline = proc.cmdline()
            open_files = proc.open_files()
        except (psutil.Error, OSError):
            continue
        for argument in cmdline:
            if argument:
                _mark(argument)
        for handle in open_files:
            _mark(handle.path)
    return holders


def _remove_path(path: Path, root: Path) -> None:
    """Удалить кандидата, повторно проверив, что он всё ещё внутри корня.

    Повторная проверка нужна из-за TOCTOU: между инвентарём и удалением путь
    могут подменить симлинком. ``is_symlink`` отдельно — ``rmtree`` сам
    бросает ``OSError`` на симлинке, но причина в логе должна быть явной.
    """
    if path.is_symlink() or not is_within_root(root, path):
        raise OutsideRootError(str(path))
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


class CleanupService:
    """Инвентарь целей, безопасное удаление, отчёт и статус в kv.

    Один экземпляр на демон: им пользуются и плановая нить, и HTTP-хендлеры,
    поэтому здесь же лежит замок, сериализующий одновременные прогоны, и
    запись последнего отчёта в kv — отчёт обязан появиться в базе раньше, чем
    ``GET /control/cleanup/status`` на него посмотрит.
    """

    def __init__(
        self,
        store: Any,
        *,
        targets: list[CleanupTarget] | None = None,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
    ):
        self.store = store
        self._targets = targets
        self.grace_seconds = grace_seconds
        self.time_budget_seconds = time_budget_seconds
        self._lock = threading.Lock()

    # --- публичное API ------------------------------------------------------

    def run(self, config: Any, *, dry_run: bool = False) -> dict[str, Any]:
        """Один прогон очистки. Возвращает отчёт в формате контракта.

        ``config`` читается заново на каждом прогоне (``log_retention_days`` —
        поле config.json, меняется из UI без рестарта).
        """
        with self._lock:
            started = time.monotonic()
            deadline = started + self.time_budget_seconds
            now = time.time()
            cutoff = now - int(config.get("behavior.log_retention_days")) * 86400

            targets = self._targets if self._targets is not None else default_targets()
            candidates, inventory_errors = self._inventory(targets)
            holders = _held_pids([candidate.path for candidate in candidates])

            removed = 0
            removed_bytes = 0
            skipped_active = 0
            errors = inventory_errors
            processed = 0

            for candidate in candidates:
                if time.monotonic() >= deadline:
                    self.store.log(
                        "WARNING",
                        "cleanup",
                        MSG_BUDGET_EXHAUSTED,
                        {"remaining": len(candidates) - processed},
                    )
                    break
                processed += 1
                outcome, size = self._process(
                    candidate, holders=holders, cutoff=cutoff, dry_run=dry_run, now=now
                )
                if outcome == "removed":
                    removed += 1
                    removed_bytes += size
                elif outcome == "skipped_active":
                    skipped_active += 1
                elif outcome == "error":
                    errors += 1

            report = {
                "removed": removed,
                "removed_bytes": removed_bytes,
                "skipped_active": skipped_active,
                "errors": errors,
                "duration_ms": int(round((time.monotonic() - started) * 1000)),
                "dry_run": bool(dry_run),
            }
            self.store.log("INFO", "cleanup", MSG_FINISHED, dict(report))
            if not dry_run:
                # dry_run — не прогон: метка последнего запуска остаётся
                # прежней, иначе пробный запуск подавил бы ближайший плановый.
                # Отчёт пишется раньше метки: читатель, увидевший
                # CLEANUP_LAST_TS, обязан найти и отчёт — иначе status мелькал
                # бы пустым ``last`` в микросекунды между двумя записями.
                self.store.set_flag(
                    CLEANUP_LAST_REPORT_KEY,
                    json.dumps(report, ensure_ascii=False, sort_keys=True),
                )
                self.store.set_flag(CLEANUP_LAST_TS_KEY, str(time.time()))
            return report

    def status(self, config: Any) -> dict[str, Any]:
        """Статус для ``GET /control/cleanup/status``.

        ``next_run`` читается из kv — его пишет нить расписания (и ручной
        запуск). Ключа ещё нет только до первого старта демона: тогда status
        честно показывает ближайшее время по расписанию из конфига, а не null.
        Пустое значение — job выключен (так пишет демон при выключенном
        расписании).
        """
        return {"last": self._last(), "next_run": self._next_run(config)}

    def set_next_run(self, epoch: float | None) -> None:
        """Записать ближайший запуск в kv; None — job выключен."""
        self.store.set_flag(CLEANUP_NEXT_RUN_KEY, "" if epoch is None else str(epoch))

    def last_run_ts(self) -> float | None:
        """Метка последнего реального прогона (см. модульную ``last_run_ts``)."""
        return last_run_ts(self.store)

    # --- внутреннее ---------------------------------------------------------

    def _last(self) -> dict[str, Any] | None:
        ts = last_run_ts(self.store)
        if ts is None:
            return None
        raw = self.store.get_flag(CLEANUP_LAST_REPORT_KEY)
        if not raw:
            return None
        try:
            report = json.loads(raw)
        except json.JSONDecodeError:
            self.store.log(
                "WARNING", "cleanup", MSG_STATUS_UNREADABLE, {"key": CLEANUP_LAST_REPORT_KEY}
            )
            return None
        if not isinstance(report, dict):
            self.store.log(
                "WARNING", "cleanup", MSG_STATUS_UNREADABLE, {"key": CLEANUP_LAST_REPORT_KEY}
            )
            return None
        return {"ts": ts, "report": report}

    def _next_run(self, config: Any) -> float | None:
        raw = self.store.get_flag(CLEANUP_NEXT_RUN_KEY)
        if raw is not None:
            if not raw.strip():
                return None
            try:
                return float(raw)
            except ValueError:
                self.store.log(
                    "WARNING", "cleanup", MSG_STATUS_UNREADABLE, {"key": CLEANUP_NEXT_RUN_KEY}
                )
                return None
        try:
            return time.time() + seconds_until_cleanup(
                time.time(),
                str(config.get("behavior.cleanup_time")),
                int(config.get("behavior.cleanup_interval_days")),
                last_run_ts(self.store),
            )
        except (ValueError, KeyError):
            return None

    def _inventory(self, targets: list[CleanupTarget]) -> tuple[list[_Candidate], int]:
        """Все записи в корнях, совпавшие с шаблоном имени. Плюс ошибки корня.

        Перечисляется только whitelist-корень: одноимённый каталог рядом с ним
        не виден вовсе, поэтому и не может быть удалён по недосмотру.
        """
        candidates: list[_Candidate] = []
        errors = 0
        for target in targets:
            root = Path(target.root)
            if not root.is_dir():
                # Каталога нет — установка без скриншотов/профилей, не ошибка.
                continue
            try:
                entries = list(os.scandir(root))
            except OSError as exc:
                errors += 1
                self.store.log(
                    "ERROR",
                    "cleanup",
                    MSG_FAILED,
                    {
                        "path": str(root),
                        "bytes": 0,
                        "result": "error",
                        "reason": type(exc).__name__,
                        "target": target.kind,
                    },
                )
                continue
            for entry in entries:
                if target.pattern.fullmatch(entry.name):
                    candidates.append(_Candidate(Path(entry.path), target))
        candidates.sort(key=lambda candidate: str(candidate.path))
        return candidates, errors

    def _process(
        self,
        candidate: _Candidate,
        *,
        holders: dict[Path, int],
        cutoff: float,
        dry_run: bool,
        now: float,
    ) -> tuple[str, int]:
        """Решение по одному кандидату. Возвращает (итог, байты)."""
        path, target = candidate.path, candidate.target

        # Симлинк не преследуем ни в каком виде: и наружу, и внутрь корня.
        if path.is_symlink():
            self._log_skip(path, target, "symlink", level="WARNING")
            return "skipped", 0
        if not is_within_root(target.root, path):
            # До сюда доходим только при TOCTOU или кривом корне: имя совпало,
            # а путь увёл за границу whitelist.
            self._log_skip(path, target, "outside_root", level="WARNING")
            return "skipped", 0
        if target.is_dir and not path.is_dir():
            self._log_skip(path, target, "not_a_directory", level="WARNING")
            return "skipped", 0
        if not target.is_dir and not path.is_file():
            self._log_skip(path, target, "not_a_file", level="WARNING")
            return "skipped", 0

        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            self.store.log(
                "ERROR",
                "cleanup",
                MSG_FAILED,
                {
                    "path": str(path),
                    "bytes": 0,
                    "result": "error",
                    "reason": type(exc).__name__,
                    "target": target.kind,
                },
            )
            return "error", 0

        # Скриншоты: срок жизни задаёт log_retention_days (см. докстринг модуля).
        if not target.is_dir and mtime >= cutoff:
            self._log_skip(path, target, "within_retention")
            return "skipped", 0

        holder_pid = holders.get(path)
        if holder_pid is not None:
            # Держатель — WARNING: живой профиль, которого коснулись, обязан
            # быть виден оператору, а не спрятан в INFO-потоке.
            self._log_skip(path, target, "active_process", pid=holder_pid, level="WARNING")
            return "skipped_active", 0

        if now - mtime < self.grace_seconds:
            self._log_skip(path, target, "recent_mtime")
            return "skipped_active", 0

        size = _tree_size(path, target.is_dir)
        if dry_run:
            self._log_skip(path, target, "dry_run", size=size)
            # Отчёт dry_run описывает найденные кандидаты (поле dry_run=true
            # предупреждает, что ничего не удалено), в лог уходит пропуск.
            return "removed", size

        try:
            _remove_path(path, target.root)
        except OutsideRootError:
            self._log_skip(path, target, "outside_root", level="WARNING")
            return "skipped", 0
        except Exception as exc:  # noqa: BLE001 - одна операция не роняет прогон
            self.store.log(
                "ERROR",
                "cleanup",
                MSG_FAILED,
                {
                    "path": str(path),
                    "bytes": size,
                    "result": "error",
                    "reason": type(exc).__name__,
                    "target": target.kind,
                },
            )
            return "error", size

        self.store.log(
            "INFO",
            "cleanup",
            MSG_REMOVED,
            {
                "path": str(path),
                "bytes": size,
                "result": "removed",
                "target": target.kind,
            },
        )
        return "removed", size

    def _log_skip(
        self,
        path: Path,
        target: CleanupTarget,
        reason: str,
        *,
        size: int = 0,
        pid: int | None = None,
        level: str = "INFO",
    ) -> None:
        """Один пропуск в лог: ``reason`` — машиночитаемая причина."""
        fields: dict[str, Any] = {
            "path": str(path),
            "bytes": size,
            "result": "skipped",
            "reason": reason,
            "target": target.kind,
        }
        if pid is not None:
            fields["pid"] = pid
        self.store.log(level, "cleanup", MSG_SKIPPED, fields)
