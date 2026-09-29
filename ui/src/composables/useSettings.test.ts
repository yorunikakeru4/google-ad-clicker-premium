// Поведение экрана Settings: загрузка, dirty-состояние, патч изменённых
// полей, ошибки валидации по-польно и сохранность маски секретов.
//
// API подменяется целиком: здесь важен порядок вызовов, состояние ref'ов
// и то, что именно уходит в POST /control/config.

import { describe, expect, it, vi } from "vitest";
import { createSettings } from "./useSettings";
import { SECRET_MASK, SettingsValidationError, normalizeConfig, type SettingsApi, type SettingsConfig } from "../lib/settings";

function clone(config: SettingsConfig): SettingsConfig {
  return JSON.parse(JSON.stringify(config)) as SettingsConfig;
}

/** Слияние патча так же, как его делает демон: секции сливаются по ключам. */
function deepMerge(base: SettingsConfig, patch: SettingsConfig): SettingsConfig {
  const merged = clone(base);
  for (const [section, values] of Object.entries(patch)) {
    merged[section] = { ...(merged[section] ?? {}), ...values };
  }
  return merged;
}

function fakeApi(start: SettingsConfig = normalizeConfig({})) {
  const state = {
    config: clone(start),
    loadError: null as Error | null,
    saveError: null as Error | null,
    patches: [] as SettingsConfig[],
  };

  const api: SettingsApi = {
    load: vi.fn(async () => {
      if (state.loadError) throw state.loadError;
      return clone(state.config);
    }),
    save: vi.fn(async (patch: SettingsConfig) => {
      if (state.saveError) throw state.saveError;
      state.patches.push(clone(patch));
      state.config = deepMerge(state.config, patch);
      return clone(state.config);
    }),
  };

  return { api, state };
}

describe("createSettings: загрузка", () => {
  it("GET при монтировании заполняет форму и снимает dirty", async () => {
    const fake = fakeApi(normalizeConfig({ behavior: { browser_count: 4 } }));
    const settings = createSettings(fake.api);

    await settings.load();

    expect(fake.api.load).toHaveBeenCalledTimes(1);
    expect(settings.values.value["behavior.browser_count"]).toBe(4);
    expect(settings.loaded.value).toBe(true);
    expect(settings.dirty.value).toBe(false);
    expect(settings.loadError.value).toBeNull();
    expect(settings.canSave.value).toBe(false);
  });

  it("ошибка загрузки читаема, форма не считается заготовленной", async () => {
    const fake = fakeApi();
    fake.state.loadError = new Error("нет соединения с демоном на 127.0.0.1:8787");
    const settings = createSettings(fake.api);

    await settings.load();

    expect(settings.loadError.value).toContain("нет соединения");
    expect(settings.loaded.value).toBe(false);
    // без снапшота править нечего: save заблокирован, ложного dirty нет
    expect(settings.dirty.value).toBe(false);
    expect(settings.canSave.value).toBe(false);
  });

  it("повторный load идёт в кэш — правки пользователя не затираются", async () => {
    const fake = fakeApi();
    const settings = createSettings(fake.api);
    await settings.load();
    settings.setValue("behavior.browser_count", 9);

    await settings.load();

    expect(fake.api.load).toHaveBeenCalledTimes(1);
    expect(settings.values.value["behavior.browser_count"]).toBe(9);
    expect(settings.dirty.value).toBe(true);
  });
});

describe("createSettings: dirty и патч", () => {
  it("dirty растёт от правки и гаснет после сохранения", async () => {
    const fake = fakeApi();
    const settings = createSettings(fake.api);
    await settings.load();

    settings.setValue("behavior.browser_count", 5);
    settings.setValue("webdriver.incognito", true);

    expect(settings.dirty.value).toBe(true);
    // порядок — как в схеме формы: секции сверху вниз
    expect(settings.dirtyKeys.value).toEqual([
      "webdriver.incognito",
      "behavior.browser_count",
    ]);
    expect(settings.canSave.value).toBe(true);

    expect(await settings.save()).toBe(true);
    expect(settings.dirty.value).toBe(false);
    expect(settings.dirtyKeys.value).toEqual([]);
    expect(settings.canSave.value).toBe(false);
  });

  it("сохранение уходит патчем только изменённых полей", async () => {
    const fake = fakeApi(normalizeConfig({ behavior: { browser_count: 3, click_order: 5 } }));
    const settings = createSettings(fake.api);
    await settings.load();
    settings.setValue("behavior.browser_count", 6);

    expect(await settings.save()).toBe(true);

    expect(fake.api.save).toHaveBeenCalledTimes(1);
    expect(fake.state.patches).toEqual([{ behavior: { browser_count: 6 } }]);
  });

  it("сохранение без правок не шлёт пустой POST", async () => {
    const fake = fakeApi();
    const settings = createSettings(fake.api);
    await settings.load();

    expect(await settings.save()).toBe(true);

    expect(fake.api.save).not.toHaveBeenCalled();
    expect(settings.success.value).toBeNull();
  });

  it("ответ демона становится новым снапшотом", async () => {
    const fake = fakeApi(normalizeConfig({ behavior: { browser_count: 3 } }));
    const settings = createSettings(fake.api);
    await settings.load();
    settings.setValue("behavior.browser_count", 6);

    await settings.save();

    expect(fake.state.config.behavior.browser_count).toBe(6);
    expect(settings.values.value["behavior.browser_count"]).toBe(6);
    // возврат к прежнему значению снова dirty — сравнение идёт с новым снапшотом
    settings.setValue("behavior.browser_count", 3);
    expect(settings.dirty.value).toBe(true);
  });

  it("успех виден и чистится первой же правкой", async () => {
    const fake = fakeApi();
    const settings = createSettings(fake.api);
    await settings.load();
    settings.setValue("behavior.click_order", 3);

    await settings.save();
    expect(settings.success.value).toContain("сохранены");

    settings.setValue("behavior.click_order", 4);
    expect(settings.success.value).toBeNull();
  });
});

describe("createSettings: ошибки валидации по-польно", () => {
  const problems = [
    { field: "behavior.click_order", message: "значение больше максимума 1000" },
    { field: "behavior.ad_page_min_wait", message: "должно быть не больше behavior.ad_page_max_wait (15)" },
    { field: "behavior.ad_page_max_wait", message: "должно быть не меньше behavior.ad_page_min_wait (20)" },
  ];

  it("400 → подсветка полей, значения остаются, снапшот не меняется", async () => {
    const fake = fakeApi(normalizeConfig({ behavior: { browser_count: 3 } }));
    const settings = createSettings(fake.api);
    await settings.load();
    settings.setValue("behavior.click_order", 5000);

    fake.state.saveError = new SettingsValidationError(
      "behavior.click_order: значение больше максимума 1000",
      problems,
    );

    expect(await settings.save()).toBe(false);

    expect(settings.fieldErrors.value).toEqual({
      "behavior.click_order": "значение больше максимума 1000",
      "behavior.ad_page_min_wait": "должно быть не больше behavior.ad_page_max_wait (15)",
      "behavior.ad_page_max_wait": "должно быть не меньше behavior.ad_page_min_wait (20)",
    });
    // введённое значение не откатывается: пользователь правит, а не начинает заново
    expect(settings.values.value["behavior.click_order"]).toBe(5000);
    expect(settings.dirty.value).toBe(true);
    expect(settings.success.value).toBeNull();
    // снапшот не изменился — следующая попытка снова уйдёт в демон
    expect(settings.values.value["behavior.browser_count"]).toBe(3);
    // сводка над формой: видно, что сохранение не прошло и сколько полей
    expect(settings.saveError.value).toContain("исправьте");
    expect(settings.saveError.value).toContain("3");
  });

  it("ошибка, которую нельзя отнести к полю, не теряется", async () => {
    const fake = fakeApi();
    const settings = createSettings(fake.api);
    await settings.load();
    settings.setValue("behavior.query", "shoes");

    fake.state.saveError = new SettingsValidationError("конфиг: ожидается объект", [
      { field: "config", message: "ожидается объект конфигурации" },
    ]);

    expect(await settings.save()).toBe(false);

    expect(settings.fieldErrors.value).toEqual({});
    expect(settings.saveError.value).toContain("config: ожидается объект конфигурации");
  });

  it("правка поля гасит только его ошибку", async () => {
    const fake = fakeApi();
    const settings = createSettings(fake.api);
    await settings.load();
    settings.setValue("behavior.click_order", 5000);
    fake.state.saveError = new SettingsValidationError("x", problems);
    await settings.save();

    settings.setValue("behavior.click_order", 7);

    expect(settings.fieldErrors.value).not.toHaveProperty("behavior.click_order");
    expect(settings.fieldErrors.value).toHaveProperty("behavior.ad_page_min_wait");
  });

  it("ошибка сети читаема, значения остаются, форма по-прежнему dirty", async () => {
    const fake = fakeApi();
    const settings = createSettings(fake.api);
    await settings.load();
    settings.setValue("behavior.query", "shoes");
    fake.state.saveError = new Error("HTTP 500: внутренняя ошибка демона");

    expect(await settings.save()).toBe(false);

    expect(settings.saveError.value).toBe("HTTP 500: внутренняя ошибка демона");
    expect(settings.fieldErrors.value).toEqual({});
    expect(settings.values.value["behavior.query"]).toBe("shoes");
    expect(settings.dirty.value).toBe(true);
  });

  it("после неудачи следующий save снова уходит в демон", async () => {
    const fake = fakeApi();
    const settings = createSettings(fake.api);
    await settings.load();
    settings.setValue("behavior.click_order", 5000);
    fake.state.saveError = new SettingsValidationError("x", problems);
    await settings.save();

    fake.state.saveError = null;
    settings.setValue("behavior.click_order", 10);

    expect(await settings.save()).toBe(true);
    expect(fake.api.save).toHaveBeenCalledTimes(2);
    expect(settings.fieldErrors.value).toEqual({});
    expect(settings.saveError.value).toBeNull();
  });
});

describe("createSettings: маска секретов и пустые значения", () => {
  it("нетронутые маски не уезжают в патч", async () => {
    const fake = fakeApi(
      normalizeConfig({
        behavior: { "2captcha_apikey": SECRET_MASK, query: "shoes" },
        webdriver: { proxy: SECRET_MASK },
      }),
    );
    const settings = createSettings(fake.api);
    await settings.load();
    settings.setValue("behavior.browser_count", 5);

    expect(await settings.save()).toBe(true);

    expect(fake.state.patches).toEqual([{ behavior: { browser_count: 5 } }]);
    const sent = fake.state.patches[0];
    expect(JSON.stringify(sent)).not.toContain(SECRET_MASK);
    expect(sent.behavior).not.toHaveProperty("2captcha_apikey");
    expect(sent).not.toHaveProperty("webdriver");
  });

  it("правка маски уходит новым значением, очистка — пустой строкой", async () => {
    const fake = fakeApi(normalizeConfig({ behavior: { "2captcha_apikey": SECRET_MASK } }));
    const settings = createSettings(fake.api);
    await settings.load();
    const lastPatch = () => fake.state.patches[fake.state.patches.length - 1];

    settings.setValue("behavior.2captcha_apikey", "NEW-KEY");
    expect(await settings.save()).toBe(true);
    expect(lastPatch()).toEqual({ behavior: { "2captcha_apikey": "NEW-KEY" } });

    settings.setValue("behavior.2captcha_apikey", "");
    expect(await settings.save()).toBe(true);
    expect(lastPatch()).toEqual({ behavior: { "2captcha_apikey": "" } });
    // очистка — легальный способ сказать «секрета больше нет»
    expect(fake.state.config.behavior["2captcha_apikey"]).toBe("");
  });

  it("пустое незасекреченное поле отправляется пустым, а не выпадает из патча", async () => {
    const fake = fakeApi(normalizeConfig({ paths: { query_file: "queries.txt" } }));
    const settings = createSettings(fake.api);
    await settings.load();
    settings.setValue("paths.query_file", "");

    expect(await settings.save()).toBe(true);

    expect(fake.state.patches).toEqual([{ paths: { query_file: "" } }]);
  });
});

describe("createSettings: отмена правок", () => {
  it("reset возвращает снапшот и чистит все ошибки", async () => {
    const fake = fakeApi(normalizeConfig({ behavior: { browser_count: 3 } }));
    const settings = createSettings(fake.api);
    await settings.load();
    settings.setValue("behavior.click_order", 5000);
    fake.state.saveError = new SettingsValidationError("x", [
      { field: "behavior.click_order", message: "значение больше максимума 1000" },
    ]);
    await settings.save();
    settings.setValue("behavior.query", "shoes");

    settings.reset();

    expect(settings.values.value["behavior.click_order"]).toBe(5);
    expect(settings.values.value["behavior.query"]).toBe("");
    expect(settings.values.value["behavior.browser_count"]).toBe(3);
    expect(settings.fieldErrors.value).toEqual({});
    expect(settings.saveError.value).toBeNull();
    expect(settings.success.value).toBeNull();
    expect(settings.dirty.value).toBe(false);
  });
});
