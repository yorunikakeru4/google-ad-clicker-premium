// API-слой над Tauri-командами читалки БД (src-tauri/src/commands.rs).
//
// Имена аргументов здесь — camelCase: Tauri-макрос по умолчанию переименовывает
// snake_case-параметры команд под JavaScript-сторону (rename_all = camelCase).
// Ошибки команд не глотаются: DbError приходит сериализованным объектом
// {kind, message}, dbErrorMessage превращает его в текст для UI.

import { invoke } from "@tauri-apps/api/core";
import type { CaptchaEvent } from "./captcha";
import type { DiagnosticSnapshot } from "./diagnostics";
import type { LogQueryFilters } from "./logFilters";
import type { LogCursor, LogRow } from "./logMerge";

/** Подменяемый invoke: боевой — @tauri-apps/api, в тестах — фейк. */
export type DbInvoke = (
  command: string,
  args: Record<string, unknown>,
) => Promise<unknown>;

export interface ListLogsPageParams {
  query: LogQueryFilters;
  limit: number;
  /** Курсор прошлой страницы; null — самые новые строки. */
  cursor?: LogCursor | null;
}

/** Сводка запусков — ответ runs_summary. */
export interface RunsSummary {
  succeeded: number;
  failed: number;
  other: number;
  last_error: string | null;
}

/** Часовой бакет кликов — ответ clicks_per_hour. */
export interface HourlyClicks {
  bucket: number;
  count: number;
}

/**
 * Часовой бакет событий CAPTCHA — ответ captchas_per_hour.
 *
 * Отдельный тип при том же виде, что у [`HourlyClicks`]: у команд разные
 * контракты, и путаница «клики/капчи» в коде UI стоит дороже совпадения
 * полей.
 */
export interface HourlyCaptchas {
  bucket: number;
  count: number;
}

export interface BrowserRequests {
  browser_id: string | null;
  count: number;
}

/** Скользящий час запросов плюс нагрузка по воркерам. */
export interface RequestsLastHour {
  total: number;
  per_browser: BrowserRequests[];
}

export interface ActiveWorker {
  browser_id: string;
  pid: number | null;
  status: string;
  started_at: number | null;
  heartbeat_at: number;
  restart_count: number;
  last_error: string | null;
}

/**
 * Uptime-доля за окно в часах — ответ команды `uptime_summary`.
 *
 * `ratio: null` — в окне нет ни одной часовой строки (замер идёт с первого
 * запуска демона), не «0%». `window_seconds` — знаменатель доли:
 * 3600 × число слотов окна, `buckets` — сколько строк в окно попало.
 */
export interface UptimeSummary {
  ratio: number | null;
  uptime_seconds: number;
  window_seconds: number;
  buckets: number;
}

/**
 * Размер файлов открытой БД — ответ команды `db_size`.
 * `bytes` — основной файл, `wal_bytes` — `-wal`; сумма ровно то, что
 * сравнивает с лимитом автозащита демона.
 */
export interface DbSize {
  path: string;
  bytes: number;
  wal_bytes: number;
}

export interface DbApi {
  /** Открыть БД без аргументов: env ADCLICKER_DB → adclicker.db решает Rust. */
  open(): Promise<string>;
  listLogsPage(params: ListLogsPageParams): Promise<LogRow[]>;
  countLogs(query: LogQueryFilters): Promise<number>;
  runsSummary(since: number): Promise<RunsSummary>;
  clicksPerHour(since: number, buckets: number): Promise<HourlyClicks[]>;
  /** События CAPTCHA по часам — график «CAPTCHA по часам» на Dashboard. */
  captchasPerHour(since: number, buckets: number): Promise<HourlyCaptchas[]>;
  requestsLastHour(now: number): Promise<RequestsLastHour>;
  captchaShare(since: number): Promise<number | null>;
  activeWorkers(now: number, thresholdSecs: number): Promise<ActiveWorker[]>;
  /** Uptime-доля за окно в часах — карточка Uptime на Dashboard. */
  uptimeSummary(sinceHours: number): Promise<UptimeSummary>;
  /** Последний снимок диагностики на каждый воркер — экран Diagnostics. */
  listDiagnostics(): Promise<DiagnosticSnapshot[]>;
  /** Последние события CAPTCHA — лента и подсветка на Dashboard. */
  listCaptchaEvents(limit: number): Promise<CaptchaEvent[]>;
  /** Размер файлов открытой БД — индикатор «БД: X МБ» в тулбаре Logs. */
  dbSize(): Promise<DbSize>;
}

/** Поле из `message`-объекта сериализованного DbError. */
function errorField(message: unknown, key: string): string | null {
  if (typeof message !== "object" || message === null) return null;
  const value = (message as Record<string, unknown>)[key];
  return typeof value === "string" ? value : null;
}

/**
 * Текст ошибки команды для UI. Копии видов согласованы с `Display` для
 * `DbError` в src-tauri/src/db.rs — на стороне фронта живёт только то, что
 * видит пользователь; структура ({kind, message}) берётся из serde-контракта.
 */
export function dbErrorMessage(error: unknown): string {
  if (typeof error === "string") return error;
  if (error instanceof Error) return error.message;

  if (typeof error === "object" && error !== null) {
    const kind = (error as { kind?: unknown }).kind;
    if (typeof kind === "string") {
      const message = (error as { message?: unknown }).message;
      switch (kind) {
        case "DatabaseNotFound":
          return (
            `База данных не найдена: ${errorField(message, "path") ?? "путь неизвестен"} — ` +
            "файл создаёт демон при первом запуске, а он стартует вместе с приложением. " +
            "Если файла нет и через минуту, причина в баннере «нет связи»."
          );
        case "OpenFailed":
          return `Не удалось открыть базу ${errorField(message, "path") ?? ""}: ${errorField(message, "reason") ?? "неизвестная причина"}`;
        case "ReadFailed":
          return `Ошибка чтения из базы: ${errorField(message, "reason") ?? "неизвестная причина"}`;
        case "NotOpen":
          return "База данных не открыта: сначала вызовите db_open с путём к файлу.";
        default:
          break;
      }
    }
    // Не наш формат — отдаём JSON, а не [object Object].
    try {
      return JSON.stringify(error);
    } catch {
      return String(error);
    }
  }
  return String(error);
}

function queryArgs(query: LogQueryFilters): Record<string, unknown> {
  return {
    level: query.level,
    category: query.category,
    browserId: query.browserId,
    since: query.since,
    until: query.until,
  };
}

export function createDbApi(call: DbInvoke = invoke): DbApi {
  return {
    open: () => call("db_open", {}) as Promise<string>,

    listLogsPage: ({ query, limit, cursor }) =>
      call("list_logs_page", {
        limit,
        ...queryArgs(query),
        beforeTs: cursor?.ts ?? null,
        beforeId: cursor?.id ?? null,
      }) as Promise<LogRow[]>,

    countLogs: (query) => call("count_logs", queryArgs(query)) as Promise<number>,

    runsSummary: (since) =>
      call("runs_summary", { since }) as Promise<RunsSummary>,

    clicksPerHour: (since, buckets) =>
      call("clicks_per_hour", { since, buckets }) as Promise<HourlyClicks[]>,

    captchasPerHour: (since, buckets) =>
      call("captchas_per_hour", { since, buckets }) as Promise<HourlyCaptchas[]>,

    requestsLastHour: (now) =>
      call("requests_last_hour", { now }) as Promise<RequestsLastHour>,

    captchaShare: (since) =>
      call("captcha_share", { since }) as Promise<number | null>,

    activeWorkers: (now, thresholdSecs) =>
      call("active_workers", { now, thresholdSecs }) as Promise<ActiveWorker[]>,

    uptimeSummary: (sinceHours) =>
      call("uptime_summary", { sinceHours }) as Promise<UptimeSummary>,

    listDiagnostics: () =>
      call("list_diagnostics", {}) as Promise<DiagnosticSnapshot[]>,

    listCaptchaEvents: (limit) =>
      call("list_captcha_events", { limit }) as Promise<CaptchaEvent[]>,

    dbSize: () => call("db_size", {}) as Promise<DbSize>,
  };
}

/** Боевой экземпляр: один на приложение, как и вызов invoke. */
export const dbApi: DbApi = createDbApi();
