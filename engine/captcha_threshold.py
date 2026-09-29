"""Порог CAPTCHA: формула доли, действия политики и edge-триггер.

Доля CAPTCHA за скользящий час — ``count(captcha_events) / count(network_requests)``
по строкам с ``ts >= since``. Это ровно то, что считает ``captcha_share`` из
``ui/src-tauri/src/metrics.rs``, поэтому дашборд и политика обязаны показывать
одно и то же число, а не два близких. Обе таблицы режутся одним ``since``:
иначе в окно свежих запросов попадала бы капча часовой давности.

Знаменатель 0 — ``None``, а не 0% и не деление: «запросов не было» не является
ни здоровьем, ни превышением, и ложное 0% выглядело бы как отсутствие проблемы.

**Edge, а не level.** Действие по порогу выполняется один раз на переход
«доля была ниже порога → стала не меньше», а не на каждом тике с превышающей
долёй. Состояние сбрасывается, как только доля опустилась ниже порога — и
также по ``None``: отсутствие данных это не «превышение держится», а отсутствие
данных, иначе после простоя политика залипла бы и следующее превышение не
сработало бы. Простая гистерезис-семантика: порог один, отдельной полосы
возврата нет, важен только факт перехода.

Модуль обязан оставаться лёгким: его импортирует
``engine.control_plane.config`` (там живут дефолты и enum полей), поэтому
здесь только стандартная библиотека и ``engine.db.migrations`` — ни логгера,
ни ``StoreWriter``, ни открытия БД на уровне импорта.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from engine.db import migrations

if TYPE_CHECKING:
    from pathlib import Path

    from engine.control_plane.state import StateStore

__all__ = [
    "CAPTCHA_ACTION_PAUSE",
    "CAPTCHA_ACTION_ROTATE",
    "CAPTCHA_ACTION_WARN",
    "CAPTCHA_ROTATE_REASON",
    "CAPTCHA_SHARE_WINDOW_SECONDS",
    "CAPTCHA_THRESHOLD_ACTIONS",
    "DEFAULT_CAPTCHA_THRESHOLD_ACTION",
    "DEFAULT_CAPTCHA_THRESHOLD_PERCENT",
    "CaptchaThresholdPolicy",
    "captcha_share",
]

# Скользящее окно доли, секунды. Час — то же окно, что у метрики
# «запросы/час» и у captcha_share дашборда: граница нижняя включительно
# (``ts >= since``), верхняя — «сейчас», как в Rust-реализации. Час достаточно
# широк, чтобы единичные капчи не дёргали политику, и достаточно узок, чтобы
# всплеск был замечен в первые же минуты.
CAPTCHA_SHARE_WINDOW_SECONDS = 3600.0

# Действия политики. Значения — единственное место правды: их же читает
# _ENUM_FIELDS конфигурации, поэтому четвёртое действие нельзя добавить,
# забыв валидацию.
CAPTCHA_ACTION_WARN = "warn"
CAPTCHA_ACTION_PAUSE = "pause"
CAPTCHA_ACTION_ROTATE = "rotate"

CAPTCHA_THRESHOLD_ACTIONS = frozenset(
    {CAPTCHA_ACTION_WARN, CAPTCHA_ACTION_PAUSE, CAPTCHA_ACTION_ROTATE}
)

# Дефолты политики: порог 5% (план §5, фаза 8) и безобидное действие warn.
# 0 в конфиге — допустимое значение и значит «срабатывать на любой доле»,
# включая нулевую, поэтому выбирать его стоит осознанно.
DEFAULT_CAPTCHA_THRESHOLD_PERCENT = 5.0
DEFAULT_CAPTCHA_THRESHOLD_ACTION = CAPTCHA_ACTION_WARN

# Причина ротации, которую супервизор кладёт в лог/пропуск: читается в
# logs/proxy_usage при разборе инцидента, откуда вообще взялась подмена.
CAPTCHA_ROTATE_REASON = "captcha_threshold"


def captcha_share(db_path: str | Path, since: float) -> float | None:
    """Доля CAPTCHA в окне ``since..now``: ``captcha_events / network_requests``.

    ``None`` — знаменатель равен нулю (запросов в окне нет): деление здесь
    не выполняется вовсе. Граница окна нижняя включительно, верхняя не
    ограничивается — как и в ``captcha_share`` дашборда, чтобы два места не
    разъезжались на полуинтервалах.

    Читает обе таблицы одним запросом: два отдельных соединения могли бы
    увидеть разные снимки и дать долю, которой не было ни в одном из них.
    """
    conn = migrations.connect(db_path)
    try:
        row = conn.execute(
            "SELECT "
            "  (SELECT COUNT(*) FROM captcha_events WHERE ts >= ?), "
            "  (SELECT COUNT(*) FROM network_requests WHERE ts >= ?)",
            (since, since),
        ).fetchone()
    finally:
        conn.close()
    captchas, requests = int(row[0]), int(row[1])
    if requests == 0:
        return None
    return captchas / requests


class ThresholdActions(Protocol):
    """То, что политике нужно от супервизора ради действий pause и rotate."""

    def pause(self) -> None: ...

    def rotate_all(self, *, reason: str) -> int: ...


class CaptchaThresholdPolicy:
    """Edge-триггер по доле CAPTCHA: одно превышение — одно действие.

    ``check`` вызывается периодическим job'ом демона и возвращает действие,
    которое выполнила (``None`` — нечего делать). Состояние перехода живёт в
    памяти, как и у остальных решений демона: оно нужно ровно между тиками и
    не является пользовательским состоянием.

    Ошибка формулы (БД недоступна) не перехватывается здесь — она уходит
    вызывающему (job логирует её и продолжает). А вот сбой самого действия
    (например, ``pause`` при остановленном пуле) гасится и логируется: превышение
    уже зафиксировано, и повторять упавшее действие на каждом следующем тике
    значило бы превратить одну ошибку в поток.
    """

    def __init__(
        self,
        store: StateStore,
        supervisor: ThresholdActions,
        *,
        share_fn: Callable[[str | Path, float], float | None] = captcha_share,
        clock: Callable[[], float] = time.time,
        window: float = CAPTCHA_SHARE_WINDOW_SECONDS,
    ):
        self.store = store
        self.supervisor = supervisor
        self._share_fn = share_fn
        self._clock = clock
        self._window = window
        self._exceeded = False

    @property
    def exceeded(self) -> bool:
        """Сейчас доля держится на пороге или выше (состояние перехода)."""
        return self._exceeded

    def share(self) -> float | None:
        """Доля за окно, отсчитанное от часов политики, а не от time.time()."""
        return self._share_fn(self.store.db_path, self._clock() - self._window)

    def check(self, threshold_percent: float, action: str) -> str | None:
        """Один тик: считает долю, обновляет состояние перехода, действует.

        Порог сравнивается в процентах (``доля * 100``), граница
        включительно: доля ровно на пороге — уже превышение. ``None`` не
        триггерит и не считается превышением.
        """
        share = self.share()
        if share is None:
            self._exceeded = False
            return None
        if share * 100.0 < threshold_percent:
            self._exceeded = False
            return None
        if self._exceeded:
            # Тот же переход, что и на прошлом тике: уровень, а не событие.
            return None
        self._exceeded = True
        self.store.log(
            "WARNING",
            "captcha",
            "captcha threshold exceeded",
            {
                "share": share,
                "threshold_percent": threshold_percent,
                "action": action,
            },
        )
        try:
            self._apply(action)
        except Exception as exc:  # noqa: BLE001 - превышение уже записано, job не должен падать
            self.store.log(
                "ERROR",
                "captcha",
                "captcha threshold action failed",
                {"action": action, "error": type(exc).__name__},
            )
        return action

    def _apply(self, action: str) -> None:
        """Действие по ``action``; неизвестное значение — ошибка, а не молчание."""
        if action == CAPTCHA_ACTION_WARN:
            # Лог WARNING выше и есть действие: больше ничего не требуется.
            return
        if action == CAPTCHA_ACTION_PAUSE:
            # Тот же kv-флаг, что ставит кнопка Pause в UI: воркеры дорабатывают
            # текущий сценарий и не берут новый.
            self.supervisor.pause()
            return
        if action == CAPTCHA_ACTION_ROTATE:
            # Принудительная ротация живых воркеров тем же путём, что и
            # degraded: профиль сохраняется, restart_count не растёт.
            self.supervisor.rotate_all(reason=CAPTCHA_ROTATE_REASON)
            return
        raise ValueError(f"неизвестное действие порога CAPTCHA: {action!r}")
