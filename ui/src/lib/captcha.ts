// Клиентская модель ленты CAPTCHA (план §5, фаза 8): контракт строк
// `captcha_events`, их представление в таблице и производная подсветка
// воркеров.
//
// Подсветка — производная от уже загруженных событий (`unsolvedBrowserIds`),
// а не отдельный запрос по воркерам: экран читает ленту и из неё же считает,
// у кого есть нерешённая капча в окне. Окно то же, что у пороговой политики
// движка, — 30 минут свежих событий.

import { openPath } from "@tauri-apps/plugin-opener";
import { errorMessage } from "./control";
import { formatLocalDateTime } from "./format";

/** Событие `captcha_events` — контракт команды `list_captcha_events`. */
export interface CaptchaEvent {
  id: number;
  /** Эпоха в секундах (REAL в SQLite). */
  ts: number;
  browser_id: string | null;
  proxy_id: number | null;
  page_url: string | null;
  sitekey: string | null;
  screenshot_path: string | null;
  /** Решена ли капча: 0/1 из колонки приходит булевым. */
  solved: boolean;
  solver: string | null;
  elapsed_ms: number | null;
}

/** Окно подсветки: нерешённое событие считается «живым» 30 минут. */
export const CAPTCHA_UNSOLVED_WINDOW_SECONDS = 30 * 60;

/** Сколько последних событий держит лента: хватает экрану, не раздувает память. */
export const CAPTCHA_FEED_LIMIT = 20;

/**
 * Окно ленты — сутки, как у всех блоков Dashboard.
 *
 * Без окна карточка показывала бы события прошлых дней как «последние»:
 * лента читает последние 20 строк без фильтра, и недельная давность была
 * видна только по времени, которого в ячейке и не было.
 */
export const CAPTCHA_FEED_WINDOW_SECONDS = 24 * 60 * 60;

/** Событие попадает в окно ленты относительно момента `nowSeconds`. */
export function inFeedWindow(ts: number, nowSeconds: number): boolean {
  return Number.isFinite(ts) && ts >= nowSeconds - CAPTCHA_FEED_WINDOW_SECONDS;
}

/** Предел подписи страницы: длинная ссылка не должна растягивать колонку. */
export const CAPTCHA_URL_LABEL_MAX = 60;

/**
 * Время события: локальные `YYYY-MM-DD HH:MM:SS` из эпохи в секундах.
 *
 * Дата обязательна: окно ленты — сутки, и одни часы вчерашнего дня
 * читались бы как «только что».
 */
export function formatEventTime(ts: number): string {
  if (!Number.isFinite(ts)) return "—";
  const seconds = String(new Date(ts * 1000).getSeconds()).padStart(2, "0");
  return `${formatLocalDateTime(ts)}:${seconds}`;
}

/**
 * Подпись страницы в ячейке: `—` вместо NULL, усечение с многоточием на
 * пределе. Полный адрес уходит в `title` — потеря строки невозможна.
 */
export function pageUrlLabel(
  url: string | null | undefined,
  max: number = CAPTCHA_URL_LABEL_MAX,
): string {
  if (url == null || url.trim() === "") return "—";
  if (url.length <= max) return url;
  return `${url.slice(0, max - 1)}…`;
}

/**
 * Длительность решения: до секунды — миллисекунды, дальше — секунды с
 * одним знаком. Нет значения — «—», а не 0 мс: 0 мс значило бы «решилось
 * мгновенно».
 */
export function formatElapsed(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)} мс`;
  return `${(ms / 1000).toFixed(1)} с`;
}

/**
 * Воркеры с нерешённой капчей за последние
 * [`CAPTCHA_UNSOLVED_WINDOW_SECONDS`] — то, чем подсвечивается таблица
 * воркеров. События без `browser_id` и решённые в подсветку не идут;
 * повторы воркера схлопываются, порядок — как в ленте.
 */
export function unsolvedBrowserIds(
  events: readonly CaptchaEvent[],
  nowSeconds: number,
): string[] {
  const since = nowSeconds - CAPTCHA_UNSOLVED_WINDOW_SECONDS;
  const seen = new Set<string>();
  const ids: string[] = [];

  for (const row of events) {
    if (row.solved || row.ts < since) continue;
    const browserId = row.browser_id;
    if (browserId == null || browserId === "" || seen.has(browserId)) continue;
    seen.add(browserId);
    ids.push(browserId);
  }

  return ids;
}

/**
 * Уведомление о новом событии. `id` — ключ дедупликации: один и тот же
 * показывается один раз, сколько бы ни приходило повторных тиков.
 */
export interface CaptchaNotice {
  id: number;
  ts: number;
  browserId: string | null;
  solved: boolean;
}

export function toNotice(event: CaptchaEvent): CaptchaNotice {
  return {
    id: event.id,
    ts: event.ts,
    browserId: event.browser_id,
    solved: event.solved,
  };
}

/** Текст уведомления: воркер и исход решения, без «undefined» в строке. */
export function captchaNoticeText(notice: CaptchaNotice): string {
  const who = notice.browserId ?? "неизвестный воркер";
  const outcome = notice.solved ? "решена" : "не решена";
  return `CAPTCHA · ${who} — ${outcome}`;
}

/** Открытие файла системным приложением — подменяется в тестах. */
export type ScreenshotOpener = (path: string) => Promise<void>;

/**
 * Открывает скриншот события. Возвращает текст ошибки или `null` (успех):
 * путь отсутствует — opener не вызывается, отказ программы по умолчанию
 * не уходит в консоль молча, а доходит до карточки отдельной строкой.
 */
export async function openScreenshot(
  path: string | null | undefined,
  opener: ScreenshotOpener = openPath,
): Promise<string | null> {
  if (!path) return "Скриншот для этого события не сохранён";
  try {
    await opener(path);
    return null;
  } catch (caught) {
    return errorMessage(caught);
  }
}
