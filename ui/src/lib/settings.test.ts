// Поведение слоя Settings: патч только изменённых полей, разбор 400 с
// problems и контракт GET/POST /control/config.
//
// Ключевая опасность экрана — маска `********`: если она уедет в патч или
// пустая строка затрёт секрет, ключ потеряется в config.json. Поэтому
// маска и пустые значения проверяются здесь отдельными тестами, а не
// «попутно».

import { describe, expect, it } from "vitest";
import type { Transport } from "./daemonApi";
import {
  SECRET_MASK,
  SettingsValidationError,
  buildPatch,
  createSettingsApi,
  fieldPath,
  flattenConfig,
  normalizeConfig,
  parseProblems,
  pluralFields,
  splitProblems,
  type SettingsConfig,
  type SettingValue,
} from "./settings";

/** Конфиг как его отдаёт демон: три секции, все ключи на месте. */
function daemonConfig(overrides: Partial<Record<string, Record<string, SettingValue>>> = {}): SettingsConfig {
  return normalizeConfig(overrides);
}

function transportOf(
  replies: { status: number; body: string } | ((request: { path: string; method: string; body?: string }) => { status: number; body: string }),
): { transport: Transport; calls: { path: string; method: string; body?: string }[] } {
  const calls: { path: string; method: string; body?: string }[] = [];
  const transport: Transport = async (request) => {
    calls.push({ path: request.path, method: request.method, body: request.body });
    return typeof replies === "function" ? replies(request) : replies;
  };
  return { transport, calls };
}

function okBody(config: SettingsConfig): string {
  return JSON.stringify({ config });
}

describe("fieldPath / normalizeConfig / flattenConfig", () => {
  it("поле склеивается в section.key, значения плоско по тем же ключам", () => {
    expect(fieldPath("behavior", "browser_count")).toBe("behavior.browser_count");

    const flat = flattenConfig(daemonConfig({ behavior: { browser_count: 4 } }));
    expect(flat["behavior.browser_count"]).toBe(4);
    expect(flat["webdriver.proxy"]).toBe("");
    expect(Object.keys(flat)).toContain("paths.query_file");
  });

  it("normalize дополняет недостающие ключи дефолтами и отбрасывает чужие", () => {
    const normalized = normalizeConfig({
      paths: { query_file: "q.txt", bogus: "x" },
      nonsense: { a: 1 },
    });

    expect(normalized.paths.query_file).toBe("q.txt");
    expect(normalized.paths).not.toHaveProperty("bogus");
    expect(normalized).not.toHaveProperty("nonsense");
    expect(normalized.behavior.browser_count).toBe(2);
    expect(normalized.webdriver.proxy_transport).toBe("cdp_auth");
  });

  it("normalize переживает null и пустой ответ — значения по умолчанию", () => {
    expect(normalizeConfig(null).behavior.click_order).toBe(5);
    expect(normalizeConfig(undefined).paths.user_agents).toBe("user_agents.txt");
  });
});

describe("buildPatch: только изменённые поля", () => {
  it("без правок патч пуст — POST не нужен", () => {
    const snapshot = daemonConfig({ behavior: { browser_count: 3 } });
    const values = flattenConfig(snapshot);

    expect(buildPatch(snapshot, values)).toEqual({});
  });

  it("в патче только правленые поля, по секциям, остальное не тронуто", () => {
    const snapshot = daemonConfig({
      behavior: { browser_count: 3, click_order: 5 },
      webdriver: { window_size: "" },
    });
    const values = { ...flattenConfig(snapshot), "behavior.browser_count": 7, "webdriver.window_size": "1280x800" };

    expect(buildPatch(snapshot, values)).toEqual({
      behavior: { browser_count: 7 },
      webdriver: { window_size: "1280x800" },
    });
  });

  it("пустое незасекреченное поле уходит пустым, а нетронутая маска — нет", () => {
    const snapshot = daemonConfig({
      behavior: { query: "shoes", "2captcha_apikey": SECRET_MASK },
      webdriver: { proxy: SECRET_MASK },
    });
    const values = { ...flattenConfig(snapshot), "behavior.query": "" };

    const patch = buildPatch(snapshot, values);

    expect(patch.behavior.query).toBe("");
    expect(patch.behavior).not.toHaveProperty("2captcha_apikey");
    expect(patch).not.toHaveProperty("webdriver");
  });

  it("заменённая и очищенная маска уходят в патч как обычные значения", () => {
    const snapshot = daemonConfig({
      behavior: { "2captcha_apikey": SECRET_MASK },
      webdriver: { proxy: SECRET_MASK },
    });

    const replaced = buildPatch(snapshot, {
      ...flattenConfig(snapshot),
      "behavior.2captcha_apikey": "NEW-KEY",
    });
    expect(replaced).toEqual({ behavior: { "2captcha_apikey": "NEW-KEY" } });

    const cleared = buildPatch(snapshot, {
      ...flattenConfig(snapshot),
      "webdriver.proxy": "",
    });
    expect(cleared).toEqual({ webdriver: { proxy: "" } });
  });

  it("поля, которых нет в схеме формы, в патч не попадают", () => {
    const snapshot = daemonConfig();
    const values: Record<string, SettingValue> = {
      ...flattenConfig(snapshot),
      "behavior.bogus": "x",
    };

    expect(buildPatch(snapshot, values)).toEqual({});
  });
});

describe("parseProblems / splitProblems: 400 invalid_config", () => {
  const body = JSON.stringify({
    error: {
      code: "invalid_config",
      message: "behavior.click_order: значение больше максимума 1000",
      problems: [
        { field: "behavior.click_order", message: "значение больше максимума 1000" },
        { field: "behavior.ad_page_min_wait", message: "должно быть не больше behavior.ad_page_max_wait (15)" },
        { field: "behavior.ad_page_max_wait", message: "должно быть не меньше behavior.ad_page_min_wait (20)" },
        { field: "config", message: "ожидается объект конфигурации" },
      ],
    },
  });

  it("problems разбираются из тела ответа", () => {
    expect(parseProblems(body)).toEqual([
      { field: "behavior.click_order", message: "значение больше максимума 1000" },
      { field: "behavior.ad_page_min_wait", message: "должно быть не больше behavior.ad_page_max_wait (15)" },
      { field: "behavior.ad_page_max_wait", message: "должно быть не меньше behavior.ad_page_min_wait (20)" },
      { field: "config", message: "ожидается объект конфигурации" },
    ]);
  });

  it("не invalid_config и не-JSON — problems нет, а не падение", () => {
    expect(parseProblems(JSON.stringify({ error: { code: "invalid_json", message: "мусор" } }))).toBeNull();
    expect(parseProblems("<html>oops</html>")).toBeNull();
    expect(parseProblems(JSON.stringify({ error: { code: "invalid_config", message: "x", problems: [] } }))).toBeNull();
  });

  it("ошибки складываются по полям, чужие поля уходят отдельным списком", () => {
    const { byField, unattributed } = splitProblems(parseProblems(body) ?? []);

    expect(Object.keys(byField).sort()).toEqual([
      "behavior.ad_page_max_wait",
      "behavior.ad_page_min_wait",
      "behavior.click_order",
    ]);
    expect(byField["behavior.click_order"]).toBe("значение больше максимума 1000");
    // перекрёстное правило: обе стороны получают текст рядом со своим полем
    expect(byField["behavior.ad_page_min_wait"]).toContain("ad_page_max_wait");
    expect(byField["behavior.ad_page_max_wait"]).toContain("ad_page_min_wait");
    expect(unattributed).toEqual(["config: ожидается объект конфигурации"]);
  });

  it("две проблемы одного поля склеиваются в одну подпись", () => {
    const { byField } = splitProblems([
      { field: "paths.query_file", message: "файл не найден: q.txt" },
      { field: "paths.query_file", message: "ожидается строка" },
    ]);

    expect(byField["paths.query_file"]).toBe("файл не найден: q.txt; ожидается строка");
  });

  it("pluralFields считает по-русски", () => {
    expect(pluralFields(1)).toBe("поле");
    expect(pluralFields(2)).toBe("поля");
    expect(pluralFields(5)).toBe("полей");
    expect(pluralFields(11)).toBe("полей");
    expect(pluralFields(21)).toBe("поле");
  });
});

describe("createSettingsApi: GET/POST /control/config", () => {
  it("GET отдаёт конфиг из {config}, маску — как есть", async () => {
    const config = daemonConfig({
      behavior: { browser_count: 6, "2captcha_apikey": SECRET_MASK },
      webdriver: { proxy: SECRET_MASK },
    });
    const { transport, calls } = transportOf({ status: 200, body: okBody(config) });
    const api = createSettingsApi(transport);

    const loaded = await api.load();

    expect(calls).toEqual([{ path: "/control/config", method: "GET", body: undefined }]);
    expect(loaded.behavior.browser_count).toBe(6);
    // маска доезжает до формы как есть, а не превращается в пустую строку:
    // «пусто» значило бы «секрета нет» и предложило бы затереть ключ
    expect(loaded.behavior["2captcha_apikey"]).toBe(SECRET_MASK);
    expect(loaded.webdriver.proxy).toBe(SECRET_MASK);
  });

  it("GET не-200 — читаемый текст ошибки демона", async () => {
    const { transport } = transportOf({
      status: 401,
      body: JSON.stringify({ error: { code: "unauthorized", message: "неверный токен" } }),
    });
    const api = createSettingsApi(transport);

    await expect(api.load()).rejects.toThrow("неверный токен");
  });

  it("ответ без секции конфига — ошибка, а не тихие дефолты", async () => {
    const { transport } = transportOf({ status: 200, body: JSON.stringify({ config: { paths: {} } }) });
    const api = createSettingsApi(transport);

    await expect(api.load()).rejects.toThrow(/секции/);
  });

  it("POST уходит патчем и возвращает новый конфиг", async () => {
    const config = daemonConfig({ behavior: { browser_count: 3 } });
    const { transport, calls } = transportOf({ status: 200, body: okBody(config) });
    const api = createSettingsApi(transport);

    const patch = { behavior: { browser_count: 5 } };
    const saved = await api.save(patch);

    expect(calls).toHaveLength(1);
    expect(calls[0].path).toBe("/control/config");
    expect(calls[0].method).toBe("POST");
    expect(JSON.parse(calls[0].body ?? "")).toEqual(patch);
    expect(saved.behavior.browser_count).toBe(3);
  });

  it("400 invalid_config → SettingsValidationError с problems", async () => {
    const body = JSON.stringify({
      error: {
        code: "invalid_config",
        message: "behavior.click_order: значение больше максимума 1000",
        problems: [{ field: "behavior.click_order", message: "значение больше максимума 1000" }],
      },
    });
    const { transport } = transportOf({ status: 400, body });
    const api = createSettingsApi(transport);

    const raised = await api.save({ behavior: { click_order: 5000 } }).catch((error: unknown) => error);

    expect(raised).toBeInstanceOf(SettingsValidationError);
    const validation = raised as SettingsValidationError;
    expect(validation.problems).toEqual([
      { field: "behavior.click_order", message: "значение больше максимума 1000" },
    ]);
    expect(validation.message).toContain("значение больше максимума 1000");
  });

  it("400 без problems (например, битое тело) — обычная ошибка с текстом демона", async () => {
    const { transport } = transportOf({
      status: 400,
      body: JSON.stringify({ error: { code: "invalid_json", message: "тело не JSON" } }),
    });
    const api = createSettingsApi(transport);

    const raised = await api.save({}).catch((error: unknown) => error);

    expect(raised).not.toBeInstanceOf(SettingsValidationError);
    expect((raised as Error).message).toBe("тело не JSON");
  });

  it("обрыв транспорта доходит до вызывающего как сообщение", async () => {
    const transport: Transport = async () => {
      throw new Error("не удалось подключиться к демону на 127.0.0.1:8787");
    };
    const api = createSettingsApi(transport);

    await expect(api.load()).rejects.toThrow("не удалось подключиться");
  });
});
