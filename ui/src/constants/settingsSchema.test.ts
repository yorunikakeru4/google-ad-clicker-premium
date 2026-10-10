// Полнота settingsSchema против _SCHEMA движка (план §5, фаза 2:
// «перенос всех параметров config.json с типом и подсказками»).
//
// Эталон — JSON-фикстура секций ниже, копия
// engine/control_plane/config.py::_SCHEMA: состав секций, порядок ключей,
// типы и значения по умолчанию. Она меняется только в паре с самим _SCHEMA
// (и, если поле нужно в UI, — со settingsSchema.ts). Тест проверяет оба
// направления: поле, которое есть в движке, но потерялось в форме, и
// мёртвое поле формы, которого движок не знает.
//
// Пределы числовых полей и список секретов лежат отдельными фикстурами:
// их источник — _numeric_limits и _SECRET_FIELDS того же модуля.

import { describe, expect, it } from "vitest";
import {
  settingsSections,
  type SettingFieldDef,
  type SettingSection,
  type SettingType,
} from "./settingsSchema";

interface EngineField {
  type: "bool" | "int" | "float" | "str";
  default: string | number | boolean;
}

/** Копия _SCHEMA: секция -> ключ -> (тип, default). */
const ENGINE_SCHEMA: Record<string, Record<string, EngineField>> = {
  paths: {
    query_file: { type: "str", default: "" },
    proxy_file: { type: "str", default: "" },
    user_agents: { type: "str", default: "user_agents.txt" },
    filtered_domains: { type: "str", default: "domains.txt" },
  },
  webdriver: {
    proxy: { type: "str", default: "" },
    auth: { type: "bool", default: true },
    incognito: { type: "bool", default: false },
    country_domain: { type: "bool", default: false },
    language_from_proxy: { type: "bool", default: true },
    ss_on_exception: { type: "bool", default: false },
    window_size: { type: "str", default: "" },
    shift_windows: { type: "bool", default: false },
    use_seleniumbase: { type: "bool", default: false },
    proxy_transport: { type: "str", default: "cdp_auth" },
  },
  behavior: {
    query: { type: "str", default: "" },
    ad_page_min_wait: { type: "int", default: 10 },
    ad_page_max_wait: { type: "int", default: 15 },
    nonad_page_min_wait: { type: "int", default: 15 },
    nonad_page_max_wait: { type: "int", default: 20 },
    max_scroll_limit: { type: "int", default: 0 },
    check_shopping_ads: { type: "bool", default: true },
    excludes: { type: "str", default: "" },
    own_domain: { type: "str", default: "" },
    random_mouse: { type: "bool", default: false },
    custom_cookies: { type: "bool", default: false },
    click_order: { type: "int", default: 5 },
    browser_count: { type: "int", default: 2 },
    multiprocess_style: { type: "int", default: 1 },
    loop_wait_time: { type: "int", default: 60 },
    wait_factor: { type: "float", default: 1.0 },
    running_interval_start: { type: "str", default: "" },
    running_interval_end: { type: "str", default: "" },
    "2captcha_apikey": { type: "str", default: "" },
    hooks_enabled: { type: "bool", default: false },
    telegram_enabled: { type: "bool", default: false },
    send_to_android: { type: "bool", default: false },
    request_boost: { type: "bool", default: false },
    captcha_policy: { type: "str", default: "stop" },
    captcha_threshold_percent: { type: "float", default: 5.0 },
    captcha_threshold_action: { type: "str", default: "warn" },
    log_retention_days: { type: "int", default: 30 },
    log_file_level: { type: "str", default: "INFO" },
    db_size_limit_mb: { type: "int", default: 0 },
    cleanup_time: { type: "str", default: "04:00" },
    cleanup_interval_days: { type: "int", default: 1 },
  },
  export: {
    enabled: { type: "bool", default: false },
    host: { type: "str", default: "127.0.0.1" },
    port: { type: "int", default: 5432 },
    dbname: { type: "str", default: "adclicker_export" },
    user: { type: "str", default: "adclicker" },
    password: { type: "str", default: "" },
    sslmode: { type: "str", default: "prefer" },
    batch_size: { type: "int", default: 500 },
  },
};

/** Копия _SECRET_FIELDS: поля, которые движок отдаёт замаскированными. */
const ENGINE_SECRETS = ["behavior.2captcha_apikey", "webdriver.proxy", "export.password"];

/** Копия _numeric_limits: поля, у которых есть границы в движке. */
const ENGINE_LIMITS: Record<string, { min: number; max: number }> = {
  "behavior.browser_count": { min: 1, max: 8 },
  "behavior.ad_page_min_wait": { min: 0, max: 3600 },
  "behavior.ad_page_max_wait": { min: 0, max: 3600 },
  "behavior.nonad_page_min_wait": { min: 0, max: 3600 },
  "behavior.nonad_page_max_wait": { min: 0, max: 3600 },
  "behavior.click_order": { min: 0, max: 1000 },
  "behavior.loop_wait_time": { min: 0, max: 86400 },
  "behavior.wait_factor": { min: 0.01, max: 100 },
  "behavior.max_scroll_limit": { min: 0, max: 3600 },
  "behavior.captcha_threshold_percent": { min: 0, max: 100 },
  "behavior.log_retention_days": { min: 1, max: 3650 },
  "behavior.db_size_limit_mb": { min: 0, max: 102400 },
  "behavior.cleanup_interval_days": { min: 1, max: 30 },
  "export.port": { min: 1, max: 65535 },
  "export.batch_size": { min: 50, max: 5000 },
};

/** Какие типы формы допустимы для типа движка (enum — тоже свой тип). */
const ALLOWED_FORM_TYPES: Record<EngineField["type"], SettingType[]> = {
  bool: ["bool"],
  int: ["int", "enum"],
  float: ["float"],
  str: ["string", "path", "enum"],
};

function sectionOf(key: string): SettingSection | undefined {
  return settingsSections.find((section) => section.key === key);
}

function fieldKeys(sectionKey: string): string[] {
  return sectionOf(sectionKey)?.fields.map((field) => field.key) ?? [];
}

function allFields(): { path: string; field: SettingFieldDef }[] {
  const rows: { path: string; field: SettingFieldDef }[] = [];
  for (const section of settingsSections) {
    for (const field of section.fields) {
      rows.push({ path: `${section.key}.${field.key}`, field });
    }
  }
  return rows;
}

describe("settingsSchema: секции и поля против _SCHEMA", () => {
  it("секции совпадают: ни лишней, ни потерянной", () => {
    expect(settingsSections.map((section) => section.key)).toEqual(
      Object.keys(ENGINE_SCHEMA),
    );
  });

  it("поля каждой секции совпадают по составу и порядку", () => {
    for (const [sectionKey, engineFields] of Object.entries(ENGINE_SCHEMA)) {
      expect(fieldKeys(sectionKey), `секция ${sectionKey}`).toEqual(
        Object.keys(engineFields),
      );
    }
  });

  it("каждое поле _SCHEMA есть в форме и наоборот", () => {
    const formPaths = new Set(allFields().map((row) => row.path));
    const enginePaths = new Set(
      Object.entries(ENGINE_SCHEMA).flatMap(([sectionKey, fields]) =>
        Object.keys(fields).map((key) => `${sectionKey}.${key}`),
      ),
    );

    expect([...formPaths].filter((path) => !enginePaths.has(path))).toEqual([]);
    expect([...enginePaths].filter((path) => !formPaths.has(path))).toEqual([]);
  });

  // Счётчик — смотри на сумму ключей _SCHEMA, а не на него самого: он ловит
  // потерю поля и дубль пути разом. Меняется только вместе с _SCHEMA
  // (36 полей до политики CAPTCHA + 3 под неё + 3 про хранение логов = 42
  // + 2 про очистку профилей = 44 + 1 про наш домен = 45
  // + 8 полей экспорта в PostgreSQL = 53).
  it("в форме ровно 53 поля, без повторов пути", () => {
    const paths = allFields().map((row) => row.path);
    expect(new Set(paths).size, "пути полей повторяются").toBe(paths.length);
    expect(paths).toHaveLength(53);
  });

  it("типы формы соответствуют типам движка", () => {
    for (const [sectionKey, engineFields] of Object.entries(ENGINE_SCHEMA)) {
      for (const [key, engineField] of Object.entries(engineFields)) {
        const field = sectionOf(sectionKey)?.fields.find((item) => item.key === key);
        expect(field, `${sectionKey}.${key} отсутствует в форме`).toBeDefined();
        expect(
          ALLOWED_FORM_TYPES[engineField.type],
          `${sectionKey}.${key}: форма ${field?.type}, движок ждёт ${engineField.type}`,
        ).toContain(field?.type);
      }
    }
  });

  it("значения по умолчанию совпадают с _SCHEMA", () => {
    for (const { path, field } of allFields()) {
      const [sectionKey, key] = path.split(".");
      const engineField = ENGINE_SCHEMA[sectionKey]?.[key];
      expect(engineField, `${path} отсутствует в _SCHEMA`).toBeDefined();
      expect(field.default, path).toEqual(engineField?.default);
    }
  });
});

describe("settingsSchema: описания полей", () => {
  it("у каждого поля есть непустая подсказка", () => {
    for (const { path, field } of allFields()) {
      expect(field.hint.trim().length, path).toBeGreaterThan(0);
    }
  });

  it("каждое enum-поле имеет непустой список вариантов своего типа", () => {
    const enumFields = allFields().filter((row) => row.field.type === "enum");
    expect(enumFields.length).toBeGreaterThan(0);

    for (const { path, field } of enumFields) {
      const [sectionKey, key] = path.split(".");
      const engineType = ENGINE_SCHEMA[sectionKey]?.[key]?.type;
      expect(field.options, path).toBeDefined();
      expect(field.options?.length, path).toBeGreaterThan(0);

      for (const option of field.options ?? []) {
        const expected =
          engineType === "int" ? "number" : engineType === "str" ? "string" : "boolean";
        expect(typeof option.value, `${path}: вариант ${option.title}`).toBe(expected);
        expect(option.title.trim().length, `${path}: пустой заголовок`).toBeGreaterThan(0);
      }

      const values = (field.options ?? []).map((option) => option.value);
      expect(new Set(values).size, `${path}: варианты повторяются`).toBe(values.length);
      expect(values, `${path}: default не входит в варианты`).toContain(field.default);
    }
  });

  it("границы совпадают с пределами движка, у числовых полей есть min и max", () => {
    for (const [path, limits] of Object.entries(ENGINE_LIMITS)) {
      const [sectionKey, key] = path.split(".");
      const field = sectionOf(sectionKey)?.fields.find((item) => item.key === key);
      expect(field, `${path} отсутствует в форме`).toBeDefined();
      expect(field?.min, `${path}.min`).toBe(limits.min);
      expect(field?.max, `${path}.max`).toBe(limits.max);
    }

    // Числовой ввод без границ — это когда «пусто» и «мусор» уходят в
    // демон; каждое int/float-поле обязано иметь min и max.
    for (const { path, field } of allFields()) {
      if (field.type !== "int" && field.type !== "float") continue;
      expect(field.min, `${path}: нет min`).toBeDefined();
      expect(field.max, `${path}: нет max`).toBeDefined();
    }
  });

  it("секреты отмечены ровно теми полями, что маскирует движок", () => {
    const secrets = allFields()
      .filter((row) => row.field.secret)
      .map((row) => row.path);

    expect(secrets.sort()).toEqual([...ENGINE_SECRETS].sort());
  });
});
