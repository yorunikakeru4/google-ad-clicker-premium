// API-слой экрана Settings поверх GET/POST /control/config.
//
// Контракт демона (engine/control_plane/api.py):
//   GET  /control/config → {"config": {paths, webdriver, behavior}}, секреты
//                          замаскированы SECRET_MASK;
//   POST /control/config → тело — патч (частичное обновление), 200 с новым
//                          конфигом; невалидный патч → 400
//                          {"error": {"code": "invalid_config", "message",
//                          "problems": [{field, message}]}}.
//
// Отсюда два правила формы:
//   1. в патч попадают только изменённые поля — так нетронутые секреты
//      (маска ********) вообще не уезжают на сервер и не могут затереться;
//   2. 400 с problems разбирается по-польно: каждая проблема становится
//      подписью своего поля, а то, что полю отнести нельзя (ошибка структуры
//      конфига), не теряется и уходит в сводку над формой.

import { apiErrorMessage } from "./control";
import { tauriTransport, type Transport } from "./daemonApi";
import { settingsSections } from "../constants/settingsSchema";

/** Замаскированное значение секрета — как его отдаёт демон. */
export const SECRET_MASK = "********";

export type SettingValue = string | number | boolean;

/** Конфиг демона: секция -> ключ -> значение. */
export type SettingsConfig = Record<string, Record<string, SettingValue>>;

/** Значения формы: "section.key" -> значение. */
export type SettingsValues = Record<string, SettingValue>;

/** Одна строка из `error.problems` ответа 400. */
export interface FieldProblem {
  field: string;
  message: string;
}

/** 400 invalid_config: проблемы уже собраны демоном, текст — его же. */
export class SettingsValidationError extends Error {
  readonly problems: FieldProblem[];

  constructor(message: string, problems: FieldProblem[]) {
    super(message);
    this.name = "SettingsValidationError";
    this.problems = problems;
  }
}

export function fieldPath(section: string, key: string): string {
  return `${section}.${key}`;
}

/** Пути всех полей формы в порядке секций схемы. */
export function schemaPaths(): string[] {
  return settingsSections.flatMap((section) =>
    section.fields.map((field) => fieldPath(section.key, field.key)),
  );
}

/**
 * Конфиг в форме: только поля схемы, недостающие — дефолт схемы.
 *
 * Чужие ключи отбрасываются: отправить их в патч нельзя — демон ответит
 * «неизвестный параметр», а пользователь их и не редактировал.
 */
export function normalizeConfig(raw: unknown): SettingsConfig {
  const source =
    typeof raw === "object" && raw !== null && !Array.isArray(raw)
      ? (raw as Record<string, unknown>)
      : {};
  const config: SettingsConfig = {};

  for (const section of settingsSections) {
    const provided = source[section.key];
    const values =
      typeof provided === "object" && provided !== null && !Array.isArray(provided)
        ? (provided as Record<string, unknown>)
        : {};

    config[section.key] = {};
    for (const field of section.fields) {
      const value = field.key in values ? values[field.key] : field.default;
      config[section.key][field.key] =
        typeof value === "string" || typeof value === "number" || typeof value === "boolean"
          ? value
          : field.default;
    }
  }

  return config;
}

/** Конфиг в плоские ключи "section.key" — так форма хранит значения. */
export function flattenConfig(config: SettingsConfig): SettingsValues {
  const values: SettingsValues = {};
  for (const section of settingsSections) {
    for (const field of section.fields) {
      const value = config[section.key]?.[field.key];
      values[fieldPath(section.key, field.key)] =
        value === undefined ? field.default : value;
    }
  }
  return values;
}

/**
 * Патч из изменившихся полей.
 *
 * Сравнение строгое (===): маска `********` равна сама себе и без правок в
 * патч не попадает, поэтому секрет на сервере остаётся нетронутым. Пустая
 * строка — обычное значение «не задано» и уходит пустой строкой, но только
 * если её реально ввели.
 */
export function buildPatch(snapshot: SettingsConfig, values: SettingsValues): SettingsConfig {
  const patch: SettingsConfig = {};

  for (const section of settingsSections) {
    for (const field of section.fields) {
      const path = fieldPath(section.key, field.key);
      if (!(path in values)) continue;
      if (values[path] === snapshot[section.key]?.[field.key]) continue;
      patch[section.key] = patch[section.key] ?? {};
      patch[section.key][field.key] = values[path];
    }
  }

  return patch;
}

/** Разбор тела ошибки демона; null — это не его формат. */
function errorPayload(
  body: string,
): { code: string | null; message: string | null; problems: FieldProblem[] | null } | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const error = (parsed as { error?: unknown }).error;
  if (typeof error !== "object" || error === null) return null;

  const source = error as { code?: unknown; message?: unknown; problems?: unknown };
  const rawProblems = source.problems;
  const problems =
    Array.isArray(rawProblems) && rawProblems.length > 0
      ? rawProblems
          .filter(
            (item): item is { field: unknown; message: unknown } =>
              typeof item === "object" && item !== null,
          )
          .map((item) => ({
            field: String((item as { field?: unknown }).field ?? ""),
            message: String((item as { message?: unknown }).message ?? ""),
          }))
          .filter((problem) => problem.field !== "")
      : null;

  return {
    code: typeof source.code === "string" ? source.code : null,
    message: typeof source.message === "string" && source.message !== "" ? source.message : null,
    problems,
  };
}

/**
 * Ошибка валидации из тела ответа; null — это не 400 invalid_config.
 *
 * Сообщение — слова демона (тот же текст он собирает в `error.message`),
 * проблемы — его же список `[{field, message}]`.
 */
function validationError(body: string): SettingsValidationError | null {
  const payload = errorPayload(body);
  if (
    payload === null ||
    payload.code !== "invalid_config" ||
    payload.problems === null ||
    payload.problems.length === 0
  ) {
    return null;
  }
  return new SettingsValidationError(
    payload.message ?? "демон отклонил настройки",
    payload.problems,
  );
}

/**
 * `error.problems` из 400 invalid_config; null — ошибки не по полям
 * (или тело вовсе не JSON).
 */
export function parseProblems(body: string): FieldProblem[] | null {
  return validationError(body)?.problems ?? null;
}

/**
 * Проблемы к форме: подписи по полям + то, что полю отнести нельзя.
 *
 * Перекрёстные правила демон отдаёт обеими сторонами (min и max), поэтому
 * обе стороны получают текст рядом со своим полем, а не только первая.
 */
export function splitProblems(problems: FieldProblem[]): {
  byField: Record<string, string>;
  unattributed: string[];
} {
  const known = new Set(schemaPaths());
  const byField: Record<string, string> = {};
  const unattributed: string[] = [];

  for (const problem of problems) {
    if (!known.has(problem.field)) {
      unattributed.push(`${problem.field}: ${problem.message}`);
      continue;
    }
    byField[problem.field] =
      problem.field in byField
        ? `${byField[problem.field]}; ${problem.message}`
        : problem.message;
  }

  return { byField, unattributed };
}

/** Склонение «поле/поля/полей» для сводки об ошибке. */
export function pluralFields(count: number): string {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return "поле";
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return "поля";
  return "полей";
}

export interface SettingsApi {
  /** Текущий конфиг демона, секреты — замаскированными. */
  load(): Promise<SettingsConfig>;
  /** Частичное обновление; возвращает конфиг после применения патча. */
  save(patch: SettingsConfig): Promise<SettingsConfig>;
}

/** Ответ демона `{config: ...}` в конфиг формы; кривой ответ — ошибка. */
function parseConfig(body: string, path: string): SettingsConfig {
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    throw new Error(`не-JSON ответ демона на ${path}: ${body.slice(0, 200)}`);
  }

  const config = (parsed as { config?: unknown } | null)?.config;
  if (typeof config !== "object" || config === null || Array.isArray(config)) {
    throw new Error(`ответ демона без config на ${path}`);
  }
  for (const section of settingsSections) {
    const value = (config as Record<string, unknown>)[section.key];
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      throw new Error(`ответ демона без секции ${section.key} на ${path}`);
    }
  }

  return normalizeConfig(config);
}

export function createSettingsApi(transport: Transport): SettingsApi {
  const path = "/control/config";

  async function send(method: "GET" | "POST", body?: string): Promise<SettingsConfig> {
    const reply = await transport({ path, method, body });

    if (reply.status >= 200 && reply.status < 300) {
      return parseConfig(reply.body, path);
    }

    const validation = validationError(reply.body);
    if (validation !== null) throw validation;
    throw new Error(apiErrorMessage(reply.status, reply.body));
  }

  return {
    load: () => send("GET"),
    save: (patch) => send("POST", JSON.stringify(patch)),
  };
}

/** Боевой экземпляр: транспорт — Rust-команда control_request. */
export const settingsApi: SettingsApi = createSettingsApi(tauriTransport);
