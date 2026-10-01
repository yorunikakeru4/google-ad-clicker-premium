// API-слой и клиентская модель экрана Diagnostics.
//
// Снимки читает Rust-читалка (`list_diagnostics`): последний снимок на
// воркера, `headers`/`suspicion_flags` — сырые строки JSON. Их разбор —
// обязанность этой стороны: читалка не должна падать на битом JSON, но и
// экран не должен падать — поэтому парсинг защитный и возвращает
// «не разобрано», а не исключение.
//
// Сбор идёт в control API демона (`POST /control/diagnostics/collect`), и
// строка появляется в БД асинхронно — экран просто поллит читалку.
//
// Экспорт клиентский: CSV по RFC-4180 (общий `csvCell` с Logs) и JSON с уже
// разобранными headers/flags — файлы соответствуют тому, что видит юзер.

import {
  apiErrorMessage,
  diagnosticsRequest,
  errorPayload,
  type DiagnosticsAction,
} from "./control";
import { csvCell } from "./csv";
import { tauriTransport, type Transport } from "./daemonApi";
import type { DbApi } from "./dbApi";

/**
 * Колонки таблицы `diagnostics` в порядке схемы (`engine/db/schema.sql`) —
 * каркас и для CSV-экспорта, и для raw-режима «как сайт видит сессию».
 */
export const DIAGNOSTIC_CSV_COLUMNS = [
  "ts",
  "browser_id",
  "proxy_id",
  "ip",
  "country",
  "user_agent",
  "accept_language",
  "timezone_id",
  "screen_w",
  "screen_h",
  "platform",
  "webgl_vendor",
  "webgl_renderer",
  "browser_version",
  "headers",
  "suspicion_flags",
] as const;

/** Последний снимок сессии воркера — контракт `list_diagnostics`. */
export interface DiagnosticSnapshot {
  ts: number;
  browser_id: string | null;
  proxy_id: number | null;
  ip: string | null;
  country: string | null;
  user_agent: string | null;
  accept_language: string | null;
  timezone_id: string | null;
  screen_w: number | null;
  screen_h: number | null;
  platform: string | null;
  webgl_vendor: string | null;
  webgl_renderer: string | null;
  browser_version: string | null;
  /** Сырой JSON-объект фактических заголовков запроса. */
  headers: string | null;
  /** Сырой JSON-массив флагов подозрений. */
  suspicion_flags: string | null;
  /**
   * Поля из плана §5 (фаза 7), для которых в таблице `diagnostics` пока нет
   * колонок: читалка их не отдаёт, поэтому экран показывает «нет данных» и
   * заполняется сам, как только движок начнёт их собирать.
   */
  local_ip?: string | null;
  hardware_concurrency?: number | null;
  device_memory?: number | null;
}

/** Порог «живого» воркера для карточек без снимка: heartbeat идёт каждые 5 с. */
export const DIAGNOSTICS_WORKER_STALE_SECS = 60;

// --- защитный разбор JSON -------------------------------------------------

/**
 * Список флагов подозрений: массив строк, либо `null`, если JSON битый или
 * это не массив. `null` — сигнал показать «не разобрано», а не «всё ок».
 *
 * NULL/пустая колонка — пустой список: движок просто не писал флаги.
 */
export function parseSuspicionFlags(raw: string | null): string[] | null {
  if (raw === null || raw.trim() === "") return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!Array.isArray(parsed)) return null;
  // Нестроки в массиве — не флаги: показывать чип «[object Object]» нельзя.
  return parsed.filter((value): value is string => typeof value === "string");
}

/**
 * Объект заголовков: `null` — колонки нет, JSON битый или это не объект.
 * Массив/число/`null` внутри JSON — тоже `null`: это не заголовки.
 */
export function parseHeaders(raw: string | null): Record<string, unknown> | null {
  if (raw === null || raw.trim() === "") return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return null;
  }
  return parsed as Record<string, unknown>;
}

/** Жёсткое несоответствие — красное, остальное — жёлтое предупреждение. */
export type FlagSeverity = "error" | "warn";

/**
 * Словаря флагов у UI нет: их пишет движок. Классифицируем по смыслу —
 * расхождения и детекты (mismatch/inconsistent/detected/…) красные, всё
 * прочее, включая будущие неизвестные флаги, — жёлтое: новое не должно
 * теряться, но и не должно кричать без причины.
 */
const ERROR_FLAG_PATTERNS = [
  "mismatch",
  "inconsistent",
  "conflict",
  "detect",
  "blocked",
  "webdriver",
  "forbidden",
  "denied",
  "invalid",
  "failed",
];

export function flagSeverity(flag: string): FlagSeverity {
  const lower = flag.toLowerCase();
  return ERROR_FLAG_PATTERNS.some((pattern) => lower.includes(pattern))
    ? "error"
    : "warn";
}

// --- представления карточки ----------------------------------------------

/** Строка параметров сессии: готовое значение либо null («нет данных»). */
export interface SessionParam {
  label: string;
  value: string | number | null;
}

/**
 * Параметры сессии для карточки: контрактные колонки плюс поля, которых в
 * схеме ещё нет (см. [`DiagnosticSnapshot`]) — экран рисует их одинаково и
 * не исчезает, когда движок начнёт их собирать.
 */
export function sessionParams(row: DiagnosticSnapshot): SessionParam[] {
  const screen =
    typeof row.screen_w === "number" && typeof row.screen_h === "number"
      ? `${row.screen_w}×${row.screen_h}`
      : null;
  return [
    { label: "Внешний IP", value: row.ip ?? null },
    { label: "Внутренний IP", value: row.local_ip ?? null },
    { label: "Страна", value: row.country ?? null },
    { label: "User-Agent", value: row.user_agent ?? null },
    { label: "Accept-Language", value: row.accept_language ?? null },
    { label: "Timezone", value: row.timezone_id ?? null },
    { label: "Разрешение экрана", value: screen },
    { label: "Платформа", value: row.platform ?? null },
    { label: "WebGL vendor", value: row.webgl_vendor ?? null },
    { label: "WebGL renderer", value: row.webgl_renderer ?? null },
    { label: "Версия браузера", value: row.browser_version ?? null },
    {
      label: "Ядер (hardwareConcurrency)",
      value: row.hardware_concurrency ?? null,
    },
    { label: "Память (deviceMemory)", value: row.device_memory ?? null },
  ];
}

/** Строка raw-режима: ключ колонки и её значение без интерпретации. */
export interface RawParam {
  key: string;
  value: string | number | null;
}

/**
 * Pretty JSON для raw-режима: объект читается глазами, битый остаётся
 * исходной строкой — режим обещает показать ровно то, что в базе.
 */
function prettyJson(raw: string | null): string | null {
  if (raw === null) return null;
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

/** «Как сайт видит сессию»: все 16 колонок как есть, без форматирования. */
export function rawParams(row: DiagnosticSnapshot): RawParam[] {
  return [
    { key: "ts", value: row.ts },
    { key: "browser_id", value: row.browser_id },
    { key: "proxy_id", value: row.proxy_id },
    { key: "ip", value: row.ip },
    { key: "country", value: row.country },
    { key: "user_agent", value: row.user_agent },
    { key: "accept_language", value: row.accept_language },
    { key: "timezone_id", value: row.timezone_id },
    { key: "screen_w", value: row.screen_w },
    { key: "screen_h", value: row.screen_h },
    { key: "platform", value: row.platform },
    { key: "webgl_vendor", value: row.webgl_vendor },
    { key: "webgl_renderer", value: row.webgl_renderer },
    { key: "browser_version", value: row.browser_version },
    { key: "headers", value: prettyJson(row.headers) },
    { key: "suspicion_flags", value: prettyJson(row.suspicion_flags) },
  ];
}

/** Карточка экрана: воркер и его последний снимок (или его отсутствие). */
export interface DiagnosticCard {
  browserId: string | null;
  snapshot: DiagnosticSnapshot | null;
}

/** Ключ режима «как сайт видит сессию» в localStorage. */
export const RAW_MODES_STORAGE_KEY = "adclicker:diagnostics-raw";

/** Минимальный контракт localStorage — в тестах подменяется картой. */
export interface RawModesStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/**
 * Сохранённые режимы карточек.
 *
 * Состояние живёт в компоненте, но переход на другой экран его уничтожает —
 * без хранилища переключатель «терялся» при каждом уходе со страницы.
 * Битый или чужой JSON, недоступное хранилище (приватный режим) и
 * небулевы значения — не ошибка: режим просто сбрасывается.
 */
export function loadRawModes(storage: RawModesStorage): Record<string, boolean> {
  try {
    const raw = storage.getItem(RAW_MODES_STORAGE_KEY);
    if (raw === null) return {};
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return {};
    return Object.fromEntries(
      Object.entries(parsed as Record<string, unknown>).filter(
        (entry): entry is [string, boolean] => typeof entry[1] === "boolean",
      ),
    );
  } catch {
    return {};
  }
}

/** Новое состояние после переключения одной карточки. */
export function withRawMode(
  modes: Record<string, boolean>,
  key: string,
  value: boolean,
): Record<string, boolean> {
  return { ...modes, [key]: value };
}

/** Пишет режимы; сбой хранилища не должен ронять экран. */
export function saveRawModes(storage: RawModesStorage, modes: Record<string, boolean>): void {
  try {
    storage.setItem(RAW_MODES_STORAGE_KEY, JSON.stringify(modes));
  } catch {
    // нет хранилища: режим действует до перезагрузки страницы
  }
}

/**
 * Карточки = снимки из читалки ∪ живые воркеры.
 *
 * Снимки идут первыми (читалка уже отдала последний на воркера), дальше —
 * живые воркеры без снимка с `snapshot: null` (экран покажет «нет данных»).
 * Дубли живых воркеров схлопываются; снимок воркера, которого больше нет
 * среди живых, не выбрасывается — его тоже нужно увидеть.
 */
export function buildDiagnosticCards(
  snapshots: readonly DiagnosticSnapshot[],
  workerIds: readonly string[],
): DiagnosticCard[] {
  const cards: DiagnosticCard[] = snapshots.map((snapshot) => ({
    browserId: snapshot.browser_id,
    snapshot,
  }));
  const known = new Set(snapshots.map((snapshot) => snapshot.browser_id));
  for (const browserId of workerIds) {
    if (known.has(browserId)) continue;
    known.add(browserId);
    cards.push({ browserId, snapshot: null });
  }
  return cards;
}

// --- API ------------------------------------------------------------------

export interface DiagnosticsApi {
  /** Последние снимки из read-only читалки. */
  list(): Promise<DiagnosticSnapshot[]>;
  /** browser_id воркеров с живым heartbeat — для карточек «нет данных». */
  liveWorkerIds(): Promise<string[]>;
  /**
   * Запрос сбора снимка; `null` — для всех воркеров. Возвращает `requested`
   * из ответа демона: сколько воркеров взято в работу.
   */
  collect(browserId: string | null): Promise<number>;
}

/**
 * Локальные тексты для 400, когда демон прислал только код: «invalid_request»
 * человеку ничего не говорит. message демона (если прислал) всегда сильнее.
 */
const COLLECT_MESSAGES: Record<string, string> = {
  invalid_request:
    "Демон отклонил запрос сбора диагностики (invalid_request): укажите browser_id воркера или all.",
};

/** Читаемый текст ошибки ответа демона на сбор снимка. */
export function diagnosticsApiError(status: number, body: string): string {
  const payload = errorPayload(body);
  if (payload?.message) return payload.message;
  const local = payload?.code ? COLLECT_MESSAGES[payload.code] : undefined;
  if (local) return local;
  return apiErrorMessage(status, body);
}

/** `requested` из ответа сбора; отсутствие — ошибка, а не молчаливый undefined. */
function pickRequested(body: string, path: string): number {
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    throw new Error(`не-JSON ответ демона на ${path}: ${body.slice(0, 200)}`);
  }
  const requested =
    typeof parsed === "object" && parsed !== null
      ? (parsed as { requested?: unknown }).requested
      : undefined;
  if (typeof requested !== "number") {
    throw new Error(
      `ответ демона без requested на ${path}: ${body.slice(0, 200)}`,
    );
  }
  return requested;
}

export function createDiagnosticsApi(
  db: Pick<DbApi, "listDiagnostics" | "activeWorkers">,
  transport: Transport = tauriTransport,
): DiagnosticsApi {
  const actionFor = (browserId: string | null): DiagnosticsAction =>
    browserId === null
      ? { kind: "collectAll" }
      : { kind: "collect", browserId };

  return {
    list: () => db.listDiagnostics(),

    async liveWorkerIds() {
      const workers = await db.activeWorkers(
        Date.now() / 1000,
        DIAGNOSTICS_WORKER_STALE_SECS,
      );
      return workers.map((worker) => worker.browser_id);
    },

    async collect(browserId) {
      const request = diagnosticsRequest(actionFor(browserId));
      const reply = await transport({
        path: request.path,
        method: request.method,
        body: request.body,
      });
      if (reply.status < 200 || reply.status >= 300) {
        throw new Error(diagnosticsApiError(reply.status, reply.body));
      }
      return pickRequested(reply.body, request.path);
    },
  };
}

// --- экспорт ---------------------------------------------------------------

/** Ячейка CSV: колонка строкой, всё нестроковое — пустая ячейка. */
function csvValue(
  row: DiagnosticSnapshot,
  column: string,
): string | number | null {
  const value = (row as unknown as Record<string, unknown>)[column];
  return typeof value === "string" || typeof value === "number" ? value : null;
}

/**
 * Снимок в CSV: строка заголовков плюс одна строка данных. Экранирование —
 * общее `csvCell` (RFC 4180): запятые, кавычки и переводы строки безопасны.
 * JSON-колонки уходят сырыми строками — CSV плоский, парсинг не нужен.
 */
export function diagnosticToCsv(row: DiagnosticSnapshot): string {
  const lines: string[] = [DIAGNOSTIC_CSV_COLUMNS.join(",")];
  lines.push(
    DIAGNOSTIC_CSV_COLUMNS.map((column) => csvCell(csvValue(row, column))).join(
      ",",
    ),
  );
  return lines.join("\n");
}

/**
 * Снимок для JSON-экспорта: объект целиком, но `headers` и
 * `suspicion_flags` уже разобраны. Битый JSON не теряется — в экспорт уходит
 * исходная строка, чтобы файл не «чинил» данные молча.
 */
export function exportableSnapshot(
  row: DiagnosticSnapshot,
): Record<string, unknown> {
  return {
    ...row,
    headers:
      row.headers === null ? null : (parseHeaders(row.headers) ?? row.headers),
    suspicion_flags:
      row.suspicion_flags === null
        ? null
        : (parseSuspicionFlags(row.suspicion_flags) ?? row.suspicion_flags),
  };
}

/** Снимок в JSON: pretty-print, чтобы файл читался глазами. */
export function diagnosticToJson(row: DiagnosticSnapshot): string {
  return JSON.stringify(exportableSnapshot(row), null, 2);
}
