// API-слой экрана Proxies поверх HTTP-прокси демона.
//
// Мутации (добавить/импорт/удалить/проверить) идут только сюда, в control
// API: ридер БД остаётся read-only, а списком владеет демон. Пути и тела
// собирает lib/control::proxiesRequest — здесь остаётся разбор ответов и
// читаемость ошибок.
//
// Креды: ни одна функция не возвращает `username`/`password`. Строки списка
// собираются полей за полем ([PROXY_FIELDS]), поэтому даже ответ демона с
// лишними ключами не превратится в данные, которые UI может показать или
// залогировать.

import { apiErrorMessage, proxiesRequest, type ProxiesAction } from "./control";
import { tauriTransport, type Transport } from "./daemonApi";

/** Строка списка прокси — контракт GET /control/proxies без кредов. */
export interface ProxyRow {
  id: number;
  label: string | null;
  scheme: string;
  host: string;
  port: number;
  country: string | null;
  latency_ms: number | null;
  /** Демон может отдать и 0/1: UI нормализует через Boolean(). */
  is_alive: boolean | number;
  fail_count: number;
  last_checked_at: number | null;
  last_error: string | null;
  assigned_browser_id: string | null;
  usage_count: number;
}

/**
 * Итог добавления/импорта: added/skipped/problems из ответа демона.
 */
export interface ProxyChangeResult {
  added: number;
  skipped: number;
  problems: string[];
}

/**
 * Путь к файлу прокси — ответ GET /control/proxies/file.
 *
 * `path` уже абсолютный (его резолвит демон от своего каталога, тот же,
 * от которого читает импорт), `exists` — лежит ли файл на диске: его может
 * ещё не быть, и тогда opener получил бы путь в никуда.
 */
export interface ProxyFilePath {
  path: string;
  exists: boolean;
}

/** Открыватель файла в системной программе; подменяется в тестах. */
export type FileOpener = (path: string) => Promise<void>;

export interface ProxiesApi {
  list(): Promise<ProxyRow[]>;
  add(lines: string[]): Promise<ProxyChangeResult>;
  importFile(): Promise<ProxyChangeResult>;
  /** Возвращает `deleted` из ответа демона. */
  remove(id: number): Promise<number>;
  check(): Promise<void>;
  /** Абсолютный путь к proxies.txt для кнопки «открыть в системе». */
  filePath(): Promise<ProxyFilePath>;
}

/**
 * Читаемый текст конфликта 409.
 *
 * Код из контракта — гарантия, message демона — его собственные слова
 * (если прислал). Локальный текст нужен, когда демон отдал только код:
 * «proxy_in_use» человеку ничего не говорит.
 */
const CONFLICT_MESSAGES: Record<string, string> = {
  proxy_in_use: "Прокси назначен потоку — сначала снимите назначение.",
  check_in_progress: "Проверка уже идёт — дождитесь её завершения.",
};

function errorPayload(
  body: string,
): { code: string | null; message: string | null } | null {
  try {
    const parsed: unknown = JSON.parse(body);
    if (typeof parsed !== "object" || parsed === null) return null;
    const error = (parsed as { error?: unknown }).error;
    if (typeof error !== "object" || error === null) return null;
    const code = (error as { code?: unknown }).code;
    const message = (error as { message?: unknown }).message;
    return {
      code: typeof code === "string" ? code : null,
      message:
        typeof message === "string" && message.trim() !== "" ? message : null,
    };
  } catch {
    return null;
  }
}

export function proxyApiError(status: number, body: string): string {
  const payload = errorPayload(body);
  const fallback =
    status === 409 && payload?.code ? CONFLICT_MESSAGES[payload.code] : undefined;
  if (fallback) return payload?.message ?? fallback;
  return apiErrorMessage(status, body);
}

function parseJson(body: string, path: string): unknown {
  try {
    return JSON.parse(body);
  } catch {
    throw new Error(`не-JSON ответ демона на ${path}: ${body.slice(0, 200)}`);
  }
}

function asStringOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function asNumberOrNull(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

/**
 * Строка списка из ответа демона.
 *
 * Объект собирается поле в поле (а не спредом ответа): список — и есть
 * маскирование. Ключ `username`/`password` в [`ProxyRow`] нет, поэтому
 * лишние поля демона сюда попасть не могут — по построению.
 */
function pickProxy(raw: unknown): ProxyRow {
  if (typeof raw !== "object" || raw === null) {
    throw new Error("нестрока прокси в ответе демона");
  }
  const source = raw as Record<string, unknown>;
  if (
    typeof source.id !== "number" ||
    typeof source.host !== "string" ||
    typeof source.port !== "number"
  ) {
    throw new Error("неполная строка прокси в ответе демона (нет id/host/port)");
  }
  return {
    id: source.id,
    label: asStringOrNull(source.label),
    scheme: typeof source.scheme === "string" ? source.scheme : "http",
    host: source.host,
    port: source.port,
    country: asStringOrNull(source.country),
    latency_ms: asNumberOrNull(source.latency_ms),
    is_alive:
      typeof source.is_alive === "boolean" || typeof source.is_alive === "number"
        ? source.is_alive
        : true,
    fail_count: typeof source.fail_count === "number" ? source.fail_count : 0,
    last_checked_at: asNumberOrNull(source.last_checked_at),
    last_error: asStringOrNull(source.last_error),
    assigned_browser_id: asStringOrNull(source.assigned_browser_id),
    usage_count: typeof source.usage_count === "number" ? source.usage_count : 0,
  };
}

function pickChangeResult(raw: unknown, path: string): ProxyChangeResult {
  if (typeof raw !== "object" || raw === null) {
    throw new Error(`не-JSON ответ демона на ${path}`);
  }
  const source = raw as Record<string, unknown>;
  const { added, skipped, problems } = source;
  if (
    typeof added !== "number" ||
    typeof skipped !== "number" ||
    !Array.isArray(problems)
  ) {
    throw new Error(`ответ демона без added/skipped/problems на ${path}`);
  }
  return {
    added,
    skipped,
    problems: problems.filter((problem): problem is string => typeof problem === "string"),
  };
}

export function createProxiesApi(transport: Transport): ProxiesApi {
  async function send(action: ProxiesAction): Promise<unknown> {
    const request = proxiesRequest(action);
    const reply = await transport({
      path: request.path,
      method: request.method,
      body: request.body,
    });
    if (reply.status < 200 || reply.status >= 300) {
      throw new Error(proxyApiError(reply.status, reply.body));
    }
    return parseJson(reply.body, request.path);
  }

  return {
    async list() {
      const payload = await send({ kind: "list" });
      if (
        typeof payload !== "object" ||
        payload === null ||
        !Array.isArray((payload as { proxies?: unknown }).proxies)
      ) {
        throw new Error("ответ демона без списка прокси на /control/proxies");
      }
      return (payload as { proxies: unknown[] }).proxies.map(pickProxy);
    },

    async add(lines) {
      return pickChangeResult(await send({ kind: "add", lines }), "/control/proxies");
    },

    async importFile() {
      return pickChangeResult(
        await send({ kind: "import" }),
        "/control/proxies/import",
      );
    },

    async remove(id) {
      const payload = await send({ kind: "delete", id });
      if (
        typeof payload !== "object" ||
        payload === null ||
        typeof (payload as { deleted?: unknown }).deleted !== "number"
      ) {
        throw new Error("ответ демона без deleted на /control/proxies/delete");
      }
      return (payload as { deleted: number }).deleted;
    },

    async check() {
      await send({ kind: "check" });
    },

    async filePath() {
      const payload = await send({ kind: "file" });
      if (typeof payload !== "object" || payload === null) {
        throw new Error("не-JSON ответ демона на /control/proxies/file");
      }
      const source = payload as { path?: unknown; exists?: unknown };
      if (typeof source.path !== "string" || source.path === "") {
        throw new Error("ответ демона без path на /control/proxies/file");
      }
      return { path: source.path, exists: source.exists === true };
    },
  };
}

/** Боевой экземпляр: транспорт — Rust-команда control_request. */
export const proxiesApi: ProxiesApi = createProxiesApi(tauriTransport);

/** Результат клиентского разбора строк для диалога добавления. */
export interface ParsedProxyLines {
  /** Непустые строки без комментариев — их и отправляет UI: сервер решает. */
  lines: string[];
  /** Подсказки UX по подозрительным строкам; блокируют отправку они не должны. */
  problems: string[];
}

/** Первая причина, по которой строка похожа на ошибку, либо null. */
function proxyLineProblem(line: string): string | null {
  const withoutScheme = line.replace(/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//, "");
  const hostAndPort = withoutScheme.slice(withoutScheme.lastIndexOf("@") + 1);
  const colon = hostAndPort.lastIndexOf(":");
  if (colon < 0) return "нет порта — ожидается host:port";
  const host = hostAndPort.slice(0, colon);
  const port = hostAndPort.slice(colon + 1);
  if (host === "") return "нет хоста";
  if (!/^\d+$/.test(port)) return `порт «${port}» — не число`;
  const value = Number(port);
  if (value < 1 || value > 65535) return `порт ${value} вне диапазона 1–65535`;
  return null;
}

/**
 * Построчный разбор текста диалога добавления.
 *
 * Проверка — только для UX: авторитетна сторона демона, поэтому строки с
 * подсказками остаются в `lines` и уходят в запрос как есть.
 */
export function parseProxyLines(text: string): ParsedProxyLines {
  const lines: string[] = [];
  const problems: string[] = [];
  text.split(/\r?\n/).forEach((raw, index) => {
    const line = raw.trim();
    if (line === "" || line.startsWith("#")) return;
    lines.push(line);
    const problem = proxyLineProblem(line);
    if (problem !== null) problems.push(`строка ${index + 1}: ${problem}`);
  });
  return { lines, problems };
}

/** Строка таблицы экрана: готовые к показу значения, прочерки вместо NULL. */
export interface ProxyTableRow {
  id: number;
  /** host:port — адрес в том виде, в каком его показывает список. */
  address: string;
  label: string;
  country: string;
  latency_ms: number | null;
  status: "ok" | "error";
  fail_count: number;
  last_error: string | null;
  /** browser_id назначенного потока либо «—». */
  assigned_browser_id: string;
  usage_count: number;
}

export function toProxyTableRow(row: ProxyRow): ProxyTableRow {
  const label = row.label?.trim();
  const country = row.country?.trim();
  return {
    id: row.id,
    address: `${row.host}:${row.port}`,
    label: label ? label : "—",
    country: country ? country : "—",
    latency_ms: row.latency_ms,
    status: row.is_alive ? "ok" : "error",
    fail_count: row.fail_count,
    last_error: row.last_error,
    assigned_browser_id: row.assigned_browser_id ?? "—",
    usage_count: row.usage_count,
  };
}
