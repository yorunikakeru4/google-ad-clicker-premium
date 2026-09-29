// Фильтры экрана Logs: форма, валидация и персистентность в localStorage.
//
// Значения формы — сырые строки: пустая строка значит «фильтр выключен»,
// время хранится как значение datetime-local («YYYY-MM-DDTHH:mm», локальный
// пояс). Хранилище не считается доверенным: всё прочитанное проходит ту же
// валидацию, что и пользовательский ввод, поэтому битый или чужой JSON не
// превращается в фильтр, который молча ничего не находит.

/** Уровни — зеркало `LEVELS` из engine/log.py. */
export const LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"] as const;

/** Категории — зеркало `CATEGORIES` из engine/log.py. */
export const LOG_CATEGORIES = [
  "proxy",
  "browser",
  "captcha",
  "click",
  "cleanup",
  "scheduler",
] as const;

/** Ключ хранилища: версия в имени — при смене формы старый JSON отбрасывается. */
export const FILTERS_STORAGE_KEY = "adclicker.logs.filters.v1";

/** Длиннее — не browser_id, а мусор: значение отбрасывается целиком. */
const BROWSER_ID_MAX_LENGTH = 128;

/**
 * Верхняя граница окна включает выбранную минуту целиком: `ts` в БД — REAL
 * с долями секунды, а datetime-local отдаёт только минуту.
 */
const UNTIL_TAIL_SECONDS = 59.999;

export interface LogFilterValues {
  /** Уровень из [LOG_LEVELS] или "" — фильтр не применяется. */
  level: string;
  /** Категория из [LOG_CATEGORIES] или "". */
  category: string;
  /** browser_id или "". */
  browserId: string;
  /** Начало окна, «YYYY-MM-DDTHH:mm», или null. */
  since: string | null;
  /** Конец окна, «YYYY-MM-DDTHH:mm», или null. */
  until: string | null;
}

export const EMPTY_LOG_FILTERS: LogFilterValues = {
  level: "",
  category: "",
  browserId: "",
  since: null,
  until: null,
};

/** Минимальный контракт localStorage — в тестах подменяется картой. */
export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** Форма datetime-local: секунды опциональны, их может добавить гонка. */
const DATETIME_LOCAL_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/;

export function normalizeLevel(value: unknown): string {
  if (typeof value !== "string") return "";
  const candidate = value.trim().toUpperCase();
  return (LOG_LEVELS as readonly string[]).includes(candidate)
    ? candidate
    : "";
}

export function normalizeCategory(value: unknown): string {
  if (typeof value !== "string") return "";
  const candidate = value.trim().toLowerCase();
  return (LOG_CATEGORIES as readonly string[]).includes(candidate)
    ? candidate
    : "";
}

export function normalizeBrowserId(value: unknown): string {
  if (typeof value !== "string") return "";
  const trimmed = value.trim();
  return trimmed.length <= BROWSER_ID_MAX_LENGTH ? trimmed : "";
}

/** Валидное значение datetime-local → оно же; всё остальное → null. */
export function normalizeDatetime(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (trimmed === "" || !DATETIME_LOCAL_RE.test(trimmed)) return null;
  return Number.isNaN(new Date(trimmed).getTime()) ? null : trimmed;
}

/** Секунды из datetime-local локального пояса; невалидный ввод не доходит сюда. */
function datetimeToSeconds(value: string): number {
  return new Date(value).getTime() / 1000;
}

/** Любой прочитанный объект → безопасные значения фильтров. */
export function sanitizeLogFilters(raw: unknown): LogFilterValues {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return { ...EMPTY_LOG_FILTERS };
  }
  const input = raw as Record<string, unknown>;
  const since = normalizeDatetime(input.since);
  const until = normalizeDatetime(input.until);
  // Инвертированное окно дало бы пустую выборку и вечное «ничего не найдено»:
  // честнее сбросить обе границы, чем оставлять ловушку.
  const inverted =
    since !== null && until !== null && datetimeToSeconds(since) > datetimeToSeconds(until);

  return {
    level: normalizeLevel(input.level),
    category: normalizeCategory(input.category),
    browserId: normalizeBrowserId(input.browserId),
    since: inverted ? null : since,
    until: inverted ? null : until,
  };
}

/** Фильтры из хранилища; любая ошибка чтения — значения по умолчанию. */
export function loadLogFilters(
  storage: StorageLike,
  key: string = FILTERS_STORAGE_KEY,
): LogFilterValues {
  let raw: string | null;
  try {
    raw = storage.getItem(key);
  } catch {
    return { ...EMPTY_LOG_FILTERS };
  }
  if (raw === null) return { ...EMPTY_LOG_FILTERS };
  try {
    return sanitizeLogFilters(JSON.parse(raw));
  } catch {
    return { ...EMPTY_LOG_FILTERS };
  }
}

/**
 * Сохранить фильтры. Ошибка хранилища (quota, приватный режим) гасится
 * осознанно: неудачная запись не должна ломать экран логов — фильтры просто
 * не переживут перезагрузку.
 */
export function saveLogFilters(
  storage: StorageLike,
  values: LogFilterValues,
  key: string = FILTERS_STORAGE_KEY,
): void {
  try {
    storage.setItem(key, JSON.stringify(values));
  } catch {
    // См. докстринг: персистентность — удобство, не контракт.
  }
}

export interface LogQueryFilters {
  level: string | null;
  category: string | null;
  browserId: string | null;
  /** unix-секунды, как ts в БД. */
  since: number | null;
  /** unix-секунды; конец выбранной минуты включительно. */
  until: number | null;
}

/** Значения формы → аргументы команд list_logs_page/count_logs. */
export function toQueryFilters(values: LogFilterValues): LogQueryFilters {
  return {
    level: values.level === "" ? null : values.level,
    category: values.category === "" ? null : values.category,
    browserId: values.browserId === "" ? null : values.browserId,
    since: values.since === null ? null : datetimeToSeconds(values.since),
    until:
      values.until === null
        ? null
        : datetimeToSeconds(values.until) + UNTIL_TAIL_SECONDS,
  };
}
