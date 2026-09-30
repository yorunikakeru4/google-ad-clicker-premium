// Форматирование для таблицы воркеров и heartbeat-индикатора.
// Чистые функции: время передаётся снаружи, чтобы тесты не зависели от часов.

const EM_DASH = "—";

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/**
 * Аптайм воркера как `HH:MM:SS` с суточным префиксом `Nd`.
 *
 * `startedAt` — эпоха в секундах (REAL в SQLite), `nowSeconds` — тоже секунды.
 * Начало в будущем (гонка часов, свежий перевыпуск) клампится в ноль, а не
 * уходит в отрицательное время.
 */
export function formatUptime(
  startedAt: number | null | undefined,
  nowSeconds: number,
): string {
  if (startedAt == null) return EM_DASH;

  const total = Math.max(0, Math.floor(nowSeconds - startedAt));
  const days = Math.floor(total / 86_400);
  const hours = Math.floor((total % 86_400) / 3_600);
  const minutes = Math.floor((total % 3_600) / 60);
  const seconds = total % 60;

  const clock = `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`;
  return days > 0 ? `${days}д ${clock}` : clock;
}

/** PID воркера: после остановки он обнулён в БД — показываем тире. */
export function formatPid(pid: number | null | undefined): string {
  return pid == null ? EM_DASH : String(pid);
}

/** Последняя ошибка воркера: пустая — тире, а не пустая ячейка. */
export function formatLastError(error: string | null | undefined): string {
  return error == null || error === "" ? EM_DASH : error;
}

/** Время последнего ответа демона: локальные HH:MM:SS. */
export function formatClockTime(epochMs: number | null | undefined): string {
  if (epochMs == null) return EM_DASH;
  const date = new Date(epochMs);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/**
 * Дата и время из эпохи секунд: `YYYY-MM-DD HH:MM` локального пояса.
 *
 * `epochSeconds` — эпоха в секундах (REAL в SQLite, как `last_used_at`, и
 * значения kv-флагов расписания), в отличие от [`formatClockTime`], которому
 * нужны миллисекунды. Дата обязательна: очистка и профиль могли не
 * выполняться неделю, и одного времени суток для «когда» было бы мало.
 */
export function formatLocalDateTime(epochSeconds: number | null | undefined): string {
  if (epochSeconds == null || !Number.isFinite(epochSeconds)) return EM_DASH;
  const date = new Date(Math.floor(epochSeconds) * 1000);
  const year = date.getFullYear();
  const month = pad(date.getMonth() + 1);
  const day = pad(date.getDate());
  return `${year}-${month}-${day} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** Последнее использование профиля: та же локальная дата, что и у статусов. */
export function formatLastUsed(epochSeconds: number | null | undefined): string {
  return formatLocalDateTime(epochSeconds);
}

/**
 * Размер в человекочитаемый вид: байты меньше килобайта, дальше КБ и МБ
 * с одной цифрой после запятой.
 *
 * `removed_bytes` из отчёта очистки приходит целым числом байт; мусор
 * (NaN, отрицательное) показывается тире, а не «NaN Б».
 */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return EM_DASH;
  if (bytes < 1024) return `${Math.round(bytes)} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`;
}

/**
 * Длительность в секундах с десятой: `duration_ms` из отчёта очистки → `1.2 с`.
 *
 * Сотые доли секунды в отчёте нет смысла показывать: прогон и так измеряется
 * секундами, а лишняя точка только шумит строку.
 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return EM_DASH;
  return `${(ms / 1000).toFixed(1)} с`;
}
