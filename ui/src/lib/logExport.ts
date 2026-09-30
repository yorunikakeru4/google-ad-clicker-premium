// Экспорт из UI (план §5, фаза 9): сборка запроса диапазона поверх текущих
// фильтров экрана, курсорные страницы до исчерпания с защитным потолком и
// сериализация в CSV/JSON.
//
// Потолок нужен, чтобы «выгрузить год» не превращался в десятки мегабайт и
// десятки тысяч строк в памяти вкладки: выборка идёт страницами
// `list_logs_page` (как живой список), но не дольше EXPORT_ROW_CAP строк.
// Достижение потолка — не ошибка, а честное «усечено»: строки старше
// потолка в файл не попали, и вызывающий обязан сказать об этом пользователю.

import { logsToCsv, type CsvLogRow } from "./csv";
import {
  normalizeDatetime,
  toQueryFilters,
  type LogFilterValues,
  type LogQueryFilters,
} from "./logFilters";
import { cursorOf, type LogCursor, type LogPageFetch, type LogRow } from "./logMerge";

/** Потолок выгрузки: при достижении UI показывает предупреждение. */
export const EXPORT_ROW_CAP = 10_000;

/** Строк на страницу выборки; ниже потолка `MAX_LOGS_LIMIT` читалки. */
export const EXPORT_PAGE_SIZE = 500;

export type ExportFormat = "csv" | "json";

/** Окно времени диалога: значения datetime-local или null — без границы. */
export interface ExportRange {
  since: string | null;
  until: string | null;
}

export interface ExportFile {
  content: string;
  mime: string;
  /** Расширение без точки — для имени файла. */
  ext: string;
}

export interface ExportResult {
  rows: LogRow[];
  /** Потолок взят и в диапазоне остались строки — выгрузка усечена. */
  truncated: boolean;
}

/**
 * Текущие фильтры экрана плюс окно времени диалога: границы диалога
 * подменяют since/until фильтра (пустая граница снимает свою сторону),
 * level/category/browser_id остаются экранными — экспорт выгружает то, что
 * пользователь отфильтровал, но за выбранное окно.
 */
export function buildExportQuery(
  filters: LogFilterValues,
  range: ExportRange,
): LogQueryFilters {
  return toQueryFilters({
    ...filters,
    since: normalizeDatetime(range.since),
    until: normalizeDatetime(range.until),
  });
}

/**
 * Перевёрнутое окно — ошибка диалога, а не пустая выгрузка: начало позже
 * конца дало бы «ничего не найдено» без единого объяснения. Незакрытое окно
 * (граница одна или обе пусты) — не ошибка, границы опциональны.
 */
export function rangeProblem(range: ExportRange): string | null {
  const since = normalizeDatetime(range.since);
  const until = normalizeDatetime(range.until);
  if (since === null || until === null) return null;
  return new Date(since).getTime() > new Date(until).getTime()
    ? "Начало диапазона позже конца"
    : null;
}

/**
 * Выборка диапазона курсорными страницами: страницы идут, пока в них есть
 * строки и не взят потолок; короткая страница — конец диапазона.
 *
 * При взятом потолке делается один пробник (строка за последним курсором):
 * он отличает «в диапазоне ровно по потолку» (truncated = false, файл
 * полный) от «за потолком ещё есть строки» (truncated = true). Без пробника
 * индикатор врал бы половину случаёв.
 */
export async function collectExportRows(
  fetch: LogPageFetch,
  options: { pageSize?: number; cap?: number } = {},
): Promise<ExportResult> {
  const pageSize = Math.max(1, options.pageSize ?? EXPORT_PAGE_SIZE);
  const cap = Math.max(0, options.cap ?? EXPORT_ROW_CAP);

  const rows: LogRow[] = [];
  let cursor: LogCursor | null = null;

  while (rows.length < cap) {
    const limit = Math.min(pageSize, cap - rows.length);
    const page = await fetch(cursor, limit);
    if (page.length === 0) return { rows, truncated: false };

    const slice = page.slice(0, limit);
    rows.push(...slice);
    if (page.length < limit) return { rows, truncated: false };
    cursor = cursorOf(slice[slice.length - 1]);
  }

  const probe = await fetch(cursor, 1);
  return { rows, truncated: probe.length > 0 };
}

/**
 * Готовый файл выгрузки. CSV — тот же RFC 4180, что и экспорт экрана
 * (lib/csv), JSON — те же колонки объектами, без служебного `id`:
 * оба формата выгружают одни и те же данные, отличается только контейнер.
 */
export function exportFile(
  rows: readonly CsvLogRow[],
  format: ExportFormat,
): ExportFile {
  if (format === "json") {
    const data = rows.map((row) => ({
      ts: row.ts,
      level: row.level,
      browser_id: row.browser_id,
      category: row.category,
      message: row.message,
      fields: row.fields,
    }));
    return {
      content: JSON.stringify(data, null, 2),
      mime: "application/json;charset=utf-8",
      ext: "json",
    };
  }

  return {
    content: logsToCsv(rows),
    mime: "text/csv;charset=utf-8",
    ext: "csv",
  };
}

/** Имя файла: та же метка времени, что у экспортов экранов. */
export function exportFilename(format: ExportFormat, at: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  const stamp =
    `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}` +
    `-${pad(at.getHours())}${pad(at.getMinutes())}${pad(at.getSeconds())}`;
  return `adclicker-logs-${stamp}.${format}`;
}
