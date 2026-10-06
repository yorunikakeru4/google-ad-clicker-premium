// API-слой списков: запросы (раздел Key Words) и домены.
//
// Оба списка живут в файлах (queries.txt / domains.txt) — source of truth
// для движка, поэтому мутации идут в control API демона (`/control/queries*`,
// `/control/domains*`), а не в БД: ридер БД к спискам отношения не имеет,
// владеет ими демон. Форматы ответов одинаковы, различаются только ключ тела
// удаления и состав снапшота — отсюда один фабричный [`createWordlistApi`].
//
// Настройки вокруг списков (behavior.own_domain, behavior.excludes) — поля
// config.json: их читает и правит lib/settings через POST /control/config,
// здесь они только приходят в снапшоте доменов для карточки настроек.

import {
  apiErrorMessage,
  domainsRequest,
  queriesRequest,
  type WordlistAction,
  type WordlistRequest,
} from "./control";
import { tauriTransport, type Transport } from "./daemonApi";

/** Итог добавления: счётчики и причины пропуска — как у прокси. */
export interface WordlistChangeResult {
  added: number;
  skipped: number;
  problems: string[];
}

/** Итог удаления: best-effort, ненайденные значения идут в problems. */
export interface WordlistDeleteResult {
  deleted: number;
  skipped: number;
  problems: string[];
}

/** Путь к файлу списка для кнопки «открыть в системе». */
export interface WordlistFilePath {
  path: string;
  exists: boolean;
}

/** Снапшот запросов: GET /control/queries. */
export interface QueriesSnapshot {
  queries: string[];
  /** "file" — активен paths.query_file, "single" — behavior.query. */
  source: "file" | "single";
  query_file: string;
  query: string;
}

/** Снапшот доменов: GET /control/domains. */
export interface DomainsSnapshot {
  domains: string[];
  filtered_domains: string;
  own_domain: string;
  excludes: string;
}

export interface WordlistApi<T> {
  list(): Promise<T>;
  add(lines: string[]): Promise<WordlistChangeResult>;
  remove(values: string[]): Promise<WordlistDeleteResult>;
  removeAll(): Promise<WordlistDeleteResult>;
  /** Абсолютный путь к файлу списка — его отдаёт демон, не UI. */
  filePath(): Promise<WordlistFilePath>;
}

/** Открыватель файла в системной программе; подменяется в тестах. */
export type FileOpener = (path: string) => Promise<void>;

function parseJson(body: string, path: string): unknown {
  try {
    return JSON.parse(body);
  } catch {
    throw new Error(`не-JSON ответ демона на ${path}: ${body.slice(0, 200)}`);
  }
}

function asStringArray(
  source: Record<string, unknown>,
  field: string,
  path: string,
): string[] {
  const value = source[field];
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) {
    throw new Error(`ответ демона без массива строк ${field} на ${path}`);
  }
  return value;
}

function asString(source: Record<string, unknown>, field: string, path: string): string {
  const value = source[field];
  if (typeof value !== "string") {
    throw new Error(`ответ демона без строки ${field} на ${path}`);
  }
  return value;
}

function asObject(payload: unknown, path: string): Record<string, unknown> {
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    throw new Error(`не-JSON ответ демона на ${path}`);
  }
  return payload as Record<string, unknown>;
}

/** Разбор ответа добавления. */
export function pickChangeResult(payload: unknown, path: string): WordlistChangeResult {
  const source = asObject(payload, path);
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

/** Разбор ответа удаления: причины уходят в алерт целиком. */
export function pickDeleteResult(payload: unknown, path: string): WordlistDeleteResult {
  const source = asObject(payload, path);
  const { deleted, skipped, problems } = source;
  if (
    typeof deleted !== "number" ||
    typeof skipped !== "number" ||
    !Array.isArray(problems)
  ) {
    throw new Error(`ответ демона без deleted/skipped/problems на ${path}`);
  }
  return {
    deleted,
    skipped,
    problems: problems.filter((problem): problem is string => typeof problem === "string"),
  };
}

function pickFilePath(payload: unknown, path: string): WordlistFilePath {
  const source = asObject(payload, path);
  if (typeof source.path !== "string" || typeof source.exists !== "boolean") {
    throw new Error(`ответ демона без path/exists на ${path}`);
  }
  return { path: source.path, exists: source.exists };
}

/** Снапшот запросов: список плюс активный источник. */
export function pickQueries(payload: unknown, path: string): QueriesSnapshot {
  const source = asObject(payload, path);
  const sourceKind = asString(source, "source", path);
  if (sourceKind !== "file" && sourceKind !== "single") {
    throw new Error(`неизвестный источник запросов "${sourceKind}" на ${path}`);
  }
  return {
    queries: asStringArray(source, "queries", path),
    source: sourceKind,
    query_file: asString(source, "query_file", path),
    query: asString(source, "query", path),
  };
}

/** Снапшот доменов: список плюс настройки чёрного списка из config.json. */
export function pickDomains(payload: unknown, path: string): DomainsSnapshot {
  const source = asObject(payload, path);
  return {
    domains: asStringArray(source, "domains", path),
    filtered_domains: asString(source, "filtered_domains", path),
    own_domain: asString(source, "own_domain", path),
    excludes: asString(source, "excludes", path),
  };
}

export function createWordlistApi<T>(
  request: (action: WordlistAction) => WordlistRequest,
  transport: Transport,
  pickSnapshot: (payload: unknown, path: string) => T,
): WordlistApi<T> {
  async function send(action: WordlistAction): Promise<unknown> {
    const { path, method, body } = request(action);
    const reply = await transport({ path, method, body });
    if (reply.status < 200 || reply.status >= 300) {
      throw new Error(apiErrorMessage(reply.status, reply.body));
    }
    return parseJson(reply.body, path);
  }

  return {
    list: async () => {
      const action: WordlistAction = { kind: "list" };
      return pickSnapshot(await send(action), request(action).path);
    },
    add: async (lines) =>
      pickChangeResult(
        await send({ kind: "add", lines }),
        request({ kind: "add", lines }).path,
      ),
    remove: async (values) =>
      pickDeleteResult(
        await send({ kind: "delete", values }),
        request({ kind: "delete", values }).path,
      ),
    removeAll: async () =>
      pickDeleteResult(
        await send({ kind: "deleteAll" }),
        request({ kind: "deleteAll" }).path,
      ),
    filePath: async () =>
      pickFilePath(await send({ kind: "file" }), request({ kind: "file" }).path),
  };
}

/** Боевые экземпляры: транспорт — Rust-команда control_request. */
export const queriesApi: WordlistApi<QueriesSnapshot> = createWordlistApi(
  queriesRequest,
  tauriTransport,
  pickQueries,
);

export const domainsApi: WordlistApi<DomainsSnapshot> = createWordlistApi(
  domainsRequest,
  tauriTransport,
  pickDomains,
);
