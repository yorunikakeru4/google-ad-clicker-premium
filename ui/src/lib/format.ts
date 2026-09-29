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
