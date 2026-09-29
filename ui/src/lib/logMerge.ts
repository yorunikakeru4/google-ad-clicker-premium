// Склейка страниц логов экрана Logs: курсор, дедупликация и порядок.
//
// Порядок экрана — новые сверху: ровно тот `ORDER BY ts DESC, id DESC`,
// который отдаёт `list_logs_page`, поэтому «подгрузить старше» продолжает
// список вниз, а автоскролл живого режима ведёт к верху списка.
//
// Живой тик читает страницы сверху вниз до первого пересечения с уже
// известными строками: так новое не теряется (обход идёт через курсор, пока
// страница не упрётся в известное) и не дублируется (склейка по id). После
// длинной паузы тик догоняет пропущенное тем же обходом.

/** Строка страницы логов, как её отдаёт list_logs_page. */
export interface LogRow {
  id: number;
  ts: number;
  level: string;
  browser_id: string | null;
  category: string | null;
  message: string;
  fields: string | null;
}

/** Курсор «строже позиции»: та же пара, что before_ts/before_id в SQL. */
export interface LogCursor {
  ts: number;
  id: number;
}

/** Читает одну страницу: cursor = null — самые новые строки. */
export type LogPageFetch = (
  cursor: LogCursor | null,
  limit: number,
) => Promise<LogRow[]>;

export function cursorOf(row: LogRow): LogCursor {
  return { ts: row.ts, id: row.id };
}

/** Порядок SQL: ts DESC, разрыв равных ts — id DESC. */
function compareDesc(a: LogRow, b: LogRow): number {
  if (a.ts !== b.ts) return b.ts - a.ts;
  return b.id - a.id;
}

/** «Строже курсора» — та же семантика, что `ts < ? OR (ts = ? AND id < ?)`. */
function isNewer(row: LogRow, cursor: LogCursor): boolean {
  return row.ts > cursor.ts || (row.ts === cursor.ts && row.id > cursor.id);
}

/**
 * Объединение загруженных страниц: дедупликация по id (свежая копия
 * вытесняет старую), порядок `ts DESC, id DESC`. `maxRows` — потолок списка;
 * лишние (самые старые) вытесняются, поэтому память ограничена, а экспорт в
 * CSV всегда ≤ лимита.
 */
export function mergeLogRows(
  existing: readonly LogRow[],
  incoming: readonly LogRow[],
  maxRows?: number,
): LogRow[] {
  const byId = new Map<number, LogRow>();
  for (const row of existing) byId.set(row.id, row);
  for (const row of incoming) byId.set(row.id, row);

  const merged = [...byId.values()].sort(compareDesc);
  if (maxRows === undefined) return merged;
  return merged.slice(0, Math.max(0, maxRows));
}

/** Курсор самой старой строки списка (вне зависимости от порядка входа). */
export function oldestCursor(rows: readonly LogRow[]): LogCursor | null {
  let oldest: LogCursor | null = null;
  for (const row of rows) {
    const cursor = cursorOf(row);
    if (
      oldest === null ||
      cursor.ts < oldest.ts ||
      (cursor.ts === oldest.ts && cursor.id < oldest.id)
    ) {
      oldest = cursor;
    }
  }
  return oldest;
}

/**
 * Новые строки: страницы читаются сверху вниз, пока каждая строка строже
 * позиции `newestKnown` (самой новой уже известной строки). Первая строка
 * «не новее» означает: дальше лежат только прочитанное — стоп, без потерь и
 * дублей. `newestKnown = null` — первая загрузка, одна страница без обхода.
 *
 * `maxScanRows` — потолок накопленных строк: даже если известные строки
 * недостижимы (например, странный откат clock), обход конечен.
 *
 * Догоняющий обход опирается на монотонность `(ts, id)` свежих записей:
 * движок пишет `ts = time.time()` в момент вставки, поэтому новый id идёт и
 * с более новым ts.
 */
export async function fetchNewRows(
  fetch: LogPageFetch,
  newestKnown: LogCursor | null,
  limit: number,
  maxScanRows: number,
): Promise<LogRow[]> {
  if (limit <= 0 || maxScanRows <= 0) return [];
  if (newestKnown === null) return fetch(null, limit);

  const fresh: LogRow[] = [];
  let cursor: LogCursor | null = null;
  while (fresh.length < maxScanRows) {
    const page = await fetch(cursor, limit);
    if (page.length === 0) break;

    for (const row of page) {
      if (!isNewer(row, newestKnown)) return fresh;
      fresh.push(row);
      if (fresh.length >= maxScanRows) return fresh;
    }
    if (page.length < limit) break; // короткая страница — конец базы
    cursor = cursorOf(page[page.length - 1]);
  }
  return fresh;
}

/**
 * Всё подряд от свежих к старым: обход курсором до короткой страницы или
 * `maxRows`. Курсор берётся от сырых строк страницы — от позиции, которую
 * реально вернул сервер, а не от результата склейки.
 */
export async function collectAllRows(
  fetch: LogPageFetch,
  limit: number,
  maxRows: number,
): Promise<LogRow[]> {
  if (limit <= 0 || maxRows <= 0) return [];

  let merged: LogRow[] = [];
  let cursor: LogCursor | null = null;
  while (merged.length < maxRows) {
    const page = await fetch(cursor, limit);
    if (page.length === 0) break;

    merged = mergeLogRows(merged, page, maxRows);
    if (page.length < limit) break;
    cursor = cursorOf(page[page.length - 1]);
  }
  return merged;
}
