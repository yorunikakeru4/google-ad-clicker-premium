// Экспорт выборки логов в CSV по RFC 4180: разделитель — запятая, значения
// с запятыми, кавычками, переводами строки или кареткой берутся в кавычки,
// кавычка внутри удваивается. NULL из SQLite становится пустой ячейкой, а не
// строкой «null». BOM не добавляется: файл UTF-8, парсеры справляются.

/** Строка лога, как её отдаёт list_logs_page (без id — в CSV не нужен). */
export interface CsvLogRow {
  ts: number;
  level: string;
  browser_id: string | null;
  category: string | null;
  message: string;
  fields: string | null;
}

/** Порядок колонок в файле: заголовок и данные собираются из одного списка. */
export const CSV_COLUMNS = [
  "ts",
  "level",
  "browser_id",
  "category",
  "message",
  "fields",
] as const;

/** Символы, из-за которых значение обязано пойти в кавычки. */
const NEEDS_QUOTES = /[",\r\n]/;

/**
 * Ячейка CSV: `null`/`undefined` → пустая строка, безопасные значения — как
 * есть, опасные — в кавычках с удвоением внутренних.
 */
export function csvCell(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "";
  const text = String(value);
  if (!NEEDS_QUOTES.test(text)) return text;
  return `"${text.replace(/"/g, '""')}"`;
}

/**
 * Весь документ: строка заголовка плюс одна строка на запись, `\n` между
 * строками. Экранирование переносов внутри значения сохраняет запись
 * одной строкой RFC 4180, поэтому число строк файла = числу записей + 1.
 */
export function logsToCsv(rows: readonly CsvLogRow[]): string {
  const lines: string[] = [CSV_COLUMNS.join(",")];
  for (const row of rows) {
    lines.push(CSV_COLUMNS.map((column) => csvCell(row[column])).join(","));
  }
  return lines.join("\n");
}
