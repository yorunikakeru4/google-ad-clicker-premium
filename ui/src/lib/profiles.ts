// API-слой экрана Profiles поверх HTTP-прокси демона.
//
// Мутации (добавить/импорт/удалить/назначить/сбросить/статус) идут только
// сюда, в control API: ридер БД остаётся read-only, а списком владеет демон.
// Пути и тела собирает lib/control::profilesRequest — здесь остаётся разбор
// ответов и читаемость ошибок.
//
// Список прокси (`listProxies`) тянется тем же транспортом: колонка «Прокси»
// резолвит `proxy_id` профиля в label/адрес на стороне клиента, потому что
// контракт GET /control/profiles отдаёт только `proxy_id`.
//
// Кредов здесь нет по построению: строка списка собирается поле в поле
// ([`pickProfile`]), поэтому даже ответ демона с лишними ключами не
// превратится в данные, которые UI может показать или залогировать.

import {
  apiErrorMessage,
  profilesRequest,
  type NewProfile,
  type ProfilesAction,
  type WritableProfileStatus,
} from "./control";
import { tauriTransport, type Transport } from "./daemonApi";
import { formatProblems } from "./format";
import { createProxiesApi, type ProxyRow } from "./proxies";

export type { NewProfile, WritableProfileStatus } from "./control";

/** Строка списка профилей — контракт GET /control/profiles без кредов. */
export interface ProfileRow {
  id: number;
  name: string;
  key_ref: string | null;
  proxy_id: number | null;
  user_agent: string | null;
  locale: string | null;
  timezone: string | null;
  status: string;
  last_used_at: number | null;
  /** Сырой JSON: демон может отдать строку, объект или null. */
  fields: string | null;
  assigned_browser_id: string | null;
}

/** Итог добавления/импорта: added/skipped/problems из ответа демона. */
export interface ProfileChangeResult {
  added: number;
  skipped: number;
  problems: string[];
}

/**
 * Итог батчевого удаления — ответ `{"ids": [...]}` / `{"all": true}` на
 * /control/profiles/delete.
 *
 * Форма намеренно совпадает с импортом и с `ProxyDeleteResult` из
 * proxies.ts (счётчик + причины): демон отвечает best-effort, и оператору
 * нужны обе половины — сколько удалилось и почему остальное осталось
 * (занят воркером, не найден).
 */
export interface ProfileDeleteResult {
  deleted: number;
  skipped: number;
  /** Текст причины по каждому пропущенному id: занят воркером, не найден. */
  problems: string[];
}

/** Итог назначения диапазона: сколько назначено и сколько свободно осталось. */
export interface AssignResult {
  assigned: number;
  available: number;
}

/** Итог сброса назначений: сколько профилей освобождено. */
export interface UnassignResult {
  released: number;
}

export interface ProfilesApi {
  list(): Promise<ProfileRow[]>;
  /** Список прокси для колонки «Прокси»: тот же демон, тот же транспорт. */
  listProxies(): Promise<ProxyRow[]>;
  add(profiles: NewProfile[]): Promise<ProfileChangeResult>;
  importLines(lines: string[]): Promise<ProfileChangeResult>;
  /** Импорт из user_agents.txt: путь демон берёт из конфига (paths.user_agents). */
  importFile(): Promise<ProfileChangeResult>;
  /** Возвращает `deleted` из ответа демона. */
  remove(id: number): Promise<number>;
  /** Батч best-effort: занятые и не найденные идут в skipped/problems. */
  removeMany(ids: number[]): Promise<ProfileDeleteResult>;
  /** Весь пул; занятые живым воркером — в skipped/problems. */
  removeAll(): Promise<ProfileDeleteResult>;
  assign(startId: number, endId: number): Promise<AssignResult>;
  unassign(): Promise<UnassignResult>;
  setStatus(id: number, status: WritableProfileStatus): Promise<void>;
}

/**
 * Локальные тексты для ситуаций, когда демон прислал только код.
 *
 * Контрактная пара 409 `profile_in_use` — гарантия читаемости; message
 * демона (если прислал) всегда сильнее.
 */
const CONFLICT_MESSAGES: Record<string, string> = {
  profile_in_use: "Профиль назначен потоку — сначала снимите назначение.",
};

/** Текст для 400 без message: эндпоинт знает причину лучше HTTP-кода. */
const BAD_REQUEST_FALLBACKS: Record<string, string> = {
  assign: "Некорректный диапазон: проверьте start_id и end_id.",
  status: "Недопустимый статус: доступны free, blocked, error.",
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

/**
 * Читаемый текст ошибки ответа демона.
 *
 * Приоритет: message демона → локальный текст для известного кода (409) →
 * переданный fallback для 400 без сообщения → `apiErrorMessage`. Не-JSON
 * тело не роняется: остаётся код и начало тела для диагностики.
 */
export function profileApiError(
  status: number,
  body: string,
  fallback400?: string,
): string {
  const payload = errorPayload(body);
  if (payload?.message) return payload.message;
  if (status === 409 && payload?.code && CONFLICT_MESSAGES[payload.code]) {
    return CONFLICT_MESSAGES[payload.code];
  }
  if (status === 400 && fallback400) return fallback400;
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
 * `fields` в едином виде: сырой JSON-строкой.
 *
 * Демон может отдать и строку (как ридер БД), и уже разобранный объект —
 * фронт разбирает сам, а тип остаётся один, чтобы в UI не ветвиться.
 */
function asRawFields(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (typeof value === "object" && value !== null) {
    return JSON.stringify(value);
  }
  return null;
}

/**
 * Строка списка из ответа демона.
 *
 * Объект собирается поле в поле (а не спредом ответа): список — и есть
 * маскирование. Ключ `username`/`password` в [`ProfileRow`] нет, поэтому
 * лишние поля демона сюда попасть не могут — по построению.
 */
function pickProfile(raw: unknown): ProfileRow {
  if (typeof raw !== "object" || raw === null) {
    throw new Error("нестрока профиля в ответе демона");
  }
  const source = raw as Record<string, unknown>;
  if (typeof source.id !== "number" || typeof source.name !== "string") {
    throw new Error("неполная строка профиля в ответе демона (нет id/name)");
  }
  return {
    id: source.id,
    name: source.name,
    key_ref: asStringOrNull(source.key_ref),
    proxy_id: asNumberOrNull(source.proxy_id),
    user_agent: asStringOrNull(source.user_agent),
    locale: asStringOrNull(source.locale),
    timezone: asStringOrNull(source.timezone),
    status: typeof source.status === "string" ? source.status : "free",
    last_used_at: asNumberOrNull(source.last_used_at),
    fields: asRawFields(source.fields),
    assigned_browser_id: asStringOrNull(source.assigned_browser_id),
  };
}

function pickChangeResult(raw: unknown, path: string): ProfileChangeResult {
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
    // Демон шлёт причины объектами ({line_index, message} у импорта,
    // {index, message} у add): без нормализации в строки UI терял бы их
    // («пропущено 3» без объяснения, план §1 п.4).
    problems: formatProblems(problems),
  };
}

/**
 * Разбор итога батчевого удаления.
 *
 * Причины нормализуются тем же [`formatProblems`], что и у импорта: строки
 * проходят как есть (так их и шлёт демон для delete), а объекты, если
 * демон когда-нибудь начнёт их слать, дошли бы до алерта текстом, а не
 * `[object Object]`.
 */
function pickDeleteResult(raw: unknown, path: string): ProfileDeleteResult {
  if (typeof raw !== "object" || raw === null) {
    throw new Error(`не-JSON ответ демона на ${path}`);
  }
  const source = raw as Record<string, unknown>;
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
    problems: formatProblems(problems),
  };
}

function pickNumberField(raw: unknown, field: string, path: string): number {
  if (typeof raw !== "object" || raw === null) {
    throw new Error(`не-JSON ответ демона на ${path}`);
  }
  const value = (raw as Record<string, unknown>)[field];
  if (typeof value !== "number") {
    throw new Error(`ответ демона без ${field} на ${path}`);
  }
  return value;
}

function pickPair(
  raw: unknown,
  first: string,
  second: string,
  path: string,
): { [key: string]: number } {
  if (typeof raw !== "object" || raw === null) {
    throw new Error(`не-JSON ответ демона на ${path}`);
  }
  const source = raw as Record<string, unknown>;
  if (typeof source[first] !== "number" || typeof source[second] !== "number") {
    throw new Error(`ответ демона без ${first}/${second} на ${path}`);
  }
  return { [first]: source[first], [second]: source[second] };
}

export function createProfilesApi(transport: Transport): ProfilesApi {
  const proxies = createProxiesApi(transport);

  async function send(
    action: ProfilesAction,
    fallback400?: string,
  ): Promise<unknown> {
    const request = profilesRequest(action);
    const reply = await transport({
      path: request.path,
      method: request.method,
      body: request.body,
    });
    if (reply.status < 200 || reply.status >= 300) {
      throw new Error(profileApiError(reply.status, reply.body, fallback400));
    }
    return parseJson(reply.body, request.path);
  }

  return {
    async list() {
      const payload = await send({ kind: "list" });
      if (
        typeof payload !== "object" ||
        payload === null ||
        !Array.isArray((payload as { profiles?: unknown }).profiles)
      ) {
        throw new Error("ответ демона без списка профилей на /control/profiles");
      }
      return (payload as { profiles: unknown[] }).profiles.map(pickProfile);
    },

    listProxies: () => proxies.list(),

    async add(profiles) {
      return pickChangeResult(
        await send({ kind: "add", profiles }),
        "/control/profiles",
      );
    },

    async importLines(lines) {
      return pickChangeResult(
        await send({ kind: "import", lines }),
        "/control/profiles/import",
      );
    },

    async importFile() {
      // Тот же endpoint, что и импорт строк, но с флагом file: демон сам
      // читает paths.user_agents из конфига.
      return pickChangeResult(
        await send({ kind: "importFile" }),
        "/control/profiles/import",
      );
    },

    async remove(id) {
      return pickNumberField(
        await send({ kind: "delete", id }),
        "deleted",
        "/control/profiles/delete",
      );
    },

    async removeMany(ids) {
      return pickDeleteResult(
        await send({ kind: "deleteMany", ids }),
        "/control/profiles/delete",
      );
    },

    async removeAll() {
      return pickDeleteResult(
        await send({ kind: "deleteAll" }),
        "/control/profiles/delete",
      );
    },

    async assign(startId, endId) {
      const payload = await send(
        { kind: "assign", start_id: startId, end_id: endId },
        BAD_REQUEST_FALLBACKS.assign,
      );
      const pair = pickPair(payload, "assigned", "available", "/control/profiles/assign");
      return { assigned: pair.assigned, available: pair.available };
    },

    async unassign() {
      const payload = await send({ kind: "unassign" });
      return {
        released: pickNumberField(payload, "released", "/control/profiles/unassign"),
      };
    },

    async setStatus(id, status) {
      await send(
        { kind: "status", id, status },
        BAD_REQUEST_FALLBACKS.status,
      );
    },
  };
}

/** Боевой экземпляр: транспорт — Rust-команда control_request. */
export const profilesApi: ProfilesApi = createProfilesApi(tauriTransport);

/**
 * Разбор textarea импорта: одна непустая строка = один `key_ref`.
 *
 * Пустые строки и `#`-комментарии отбрасываются — подсказка в диалоге
 * обещает ровно это поведение. Строгая валидация ключей на клиенте не нужна:
 * авторитетна сторона демона.
 */
export function parseProfileImportLines(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line !== "" && !line.startsWith("#"));
}

/** Целое id от 1 иначе null: 0, дроби и мусор — не id. */
export function parseProfileId(raw: string): number | null {
  const text = raw.trim();
  if (!/^\d+$/.test(text)) return null;
  const value = Number(text);
  return Number.isSafeInteger(value) && value >= 1 ? value : null;
}

/** Результат клиентской проверки диапазона назначения. */
export type AssignRangeCheck =
  | { ok: true; start: number; end: number }
  | { ok: false; error: string };

/**
 * Валидация start_id/end_id до ухода в демон.
 *
 * Демон и так ответит 400, но пустое поле и перевёрнутый диапазон лучше
 * показать до отправки — это чистая клиентская экономия одного запроса.
 */
export function checkAssignRange(
  startRaw: string,
  endRaw: string,
): AssignRangeCheck {
  const start = parseProfileId(startRaw);
  const end = parseProfileId(endRaw);
  if (start === null || end === null) {
    return { ok: false, error: "start_id и end_id — целые числа от 1" };
  }
  if (start > end) {
    return { ok: false, error: "start_id не может быть больше end_id" };
  }
  return { ok: true, start, end };
}

/**
 * Разбор `fields` для показа: объект или null.
 *
 * Битый JSON не роняет строку: вызывающий видит null и показывает сырой
 * текст — данные профиля ценнее аккуратного «—».
 */
export function parseProfileFields(raw: string | null): Record<string, unknown> | null {
  if (raw === null || raw.trim() === "") return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return null;
    }
    return parsed as Record<string, unknown>;
  } catch {
    return null;
  }
}

/** Строка таблицы экрана: готовые к показу значения, прочерки вместо NULL. */
export interface ProfileTableRow {
  id: number;
  name: string;
  /** Ссылка на ключ, а не сам ключ: показывается как обычный текст. */
  key_ref: string;
  /** label прокси, иначе host:port, иначе прочерк. */
  proxy: string;
  user_agent: string;
  status: string;
  assigned_browser_id: string;
  /** Эпоха в секундах (REAL в SQLite) — сортируется числом, а не строкой. */
  last_used_at: number | null;
}

/**
 * Готовая строка таблицы.
 *
 * `proxiesById` — join на стороне клиента: контракт профилей отдаёт только
 * `proxy_id`. Если прокси пропал из списка, но профиль на него ссылается,
 * показываем честный `#id`, а не прочерк — строка не должна врать.
 */
export function toProfileTableRow(
  row: ProfileRow,
  proxiesById: Map<number, ProxyRow>,
): ProfileTableRow {
  let proxy = "—";
  if (row.proxy_id !== null) {
    const found = proxiesById.get(row.proxy_id);
    if (found) {
      const label = found.label?.trim();
      proxy = label ? label : `${found.host}:${found.port}`;
    } else {
      proxy = `#${row.proxy_id}`;
    }
  }
  return {
    id: row.id,
    name: row.name,
    key_ref: row.key_ref ?? "—",
    proxy,
    user_agent: row.user_agent ?? "—",
    status: row.status,
    assigned_browser_id: row.assigned_browser_id ?? "—",
    last_used_at: row.last_used_at,
  };
}

/** Значения клиентских фильтров экрана. */
export interface ProfileFilterValues {
  /** Пустая строка — фильтр по статусу не применять. */
  status: string;
  /** Пустая строка — поиск по name не применять. */
  name: string;
}

/**
 * Клиентский фильтр списка: статус точным совпадением, имя — подстрокой
 * без учёта регистра. 300 строк — не проблема для фильтра на каждой
 * перерисовке, сеть здесь не участвует.
 */
export function filterProfiles(
  rows: ProfileRow[],
  filters: ProfileFilterValues,
): ProfileRow[] {
  const query = filters.name.trim().toLowerCase();
  return rows.filter((row) => {
    if (filters.status !== "" && row.status !== filters.status) return false;
    if (query !== "" && !row.name.toLowerCase().includes(query)) return false;
    return true;
  });
}
