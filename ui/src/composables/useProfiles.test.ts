// Поведение экрана Profiles: опрос списка профилей и прокси (join для
// колонки), действия через control API и читаемость ошибок.
//
// API подменяется целиком (транспорт мокается в lib/profiles.test.ts):
// здесь важен порядок вызовов, состояние ref'ов и читаемость ошибок.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createProfiles, PROFILES_POLL_MS } from "./useProfiles";
import type { ProfilesApi, ProfileChangeResult, ProfileRow } from "../lib/profiles";
import type { WritableProfileStatus } from "../lib/control";
import type { ProxyRow } from "../lib/proxies";

function profileRow(overrides: Partial<ProfileRow> & { id: number }): ProfileRow {
  return {
    name: `profile-${overrides.id}`,
    key_ref: null,
    proxy_id: null,
    user_agent: null,
    locale: null,
    timezone: null,
    status: "free",
    last_used_at: null,
    fields: null,
    assigned_browser_id: null,
    ...overrides,
  };
}

function proxyRow(id: number): ProxyRow {
  return {
    id,
    label: null,
    scheme: "http",
    host: `host-${id}.example`,
    port: 8080,
    latency_ms: null,
    is_alive: true,
    fail_count: 0,
    last_checked_at: null,
    last_error: null,
    assigned_browser_id: null,
    usage_count: 0,
  };
}

/** Управляемый фейк API: каждая функция падает по своей ошибке из state. */
function fakeApi(initial: ProfileRow[] = []) {
  const state = {
    rows: initial.map((row) => ({ ...row })),
    proxies: [proxyRow(7)],
    listError: null as Error | null,
    proxiesError: null as Error | null,
    addResult: { added: 0, skipped: 0, problems: [] } as ProfileChangeResult,
    addError: null as Error | null,
    importResult: { added: 0, skipped: 0, problems: [] } as ProfileChangeResult,
    importError: null as Error | null,
    removed: 1,
    removeError: null as Error | null,
    assignResult: { assigned: 0, available: 0 },
    assignError: null as Error | null,
    unassignResult: { released: 0 },
    unassignError: null as Error | null,
    statusError: null as Error | null,
  };

  const api: ProfilesApi = {
    list: vi.fn(async () => {
      if (state.listError) throw state.listError;
      return state.rows.map((row) => ({ ...row }));
    }),
    listProxies: vi.fn(async () => {
      if (state.proxiesError) throw state.proxiesError;
      return state.proxies.map((row) => ({ ...row }));
    }),
    add: vi.fn(async () => {
      if (state.addError) throw state.addError;
      return state.addResult;
    }),
    importLines: vi.fn(async () => {
      if (state.importError) throw state.importError;
      return state.importResult;
    }),
    remove: vi.fn(async () => {
      if (state.removeError) throw state.removeError;
      return state.removed;
    }),
    assign: vi.fn(async () => {
      if (state.assignError) throw state.assignError;
      return state.assignResult;
    }),
    unassign: vi.fn(async () => {
      if (state.unassignError) throw state.unassignError;
      return state.unassignResult;
    }),
    setStatus: vi.fn(async () => {
      if (state.statusError) throw state.statusError;
    }),
  };

  return { api, state };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("createProfiles: список и опрос", () => {
  it("start читает профили и прокси, обновляет их раз в PROFILES_POLL_MS", async () => {
    const fake = fakeApi([profileRow({ id: 1 })]);
    const profiles = createProfiles(fake.api);

    await profiles.start();

    expect(fake.api.list).toHaveBeenCalledTimes(1);
    expect(fake.api.listProxies).toHaveBeenCalledTimes(1);
    expect(profiles.rows.value.map((row) => row.id)).toEqual([1]);
    expect(profiles.proxies.value.map((row) => row.id)).toEqual([7]);
    expect(profiles.loading.value).toBe(false);
    expect(profiles.error.value).toBeNull();

    await vi.advanceTimersByTimeAsync(PROFILES_POLL_MS);
    expect(fake.api.list).toHaveBeenCalledTimes(2);
    expect(fake.api.listProxies).toHaveBeenCalledTimes(2);

    profiles.stop();
    await vi.advanceTimersByTimeAsync(PROFILES_POLL_MS * 3);
    expect(fake.api.list, "после stop тики не уходят").toHaveBeenCalledTimes(2);
  });

  it("интервал живёт в диапазоне 3–5 с — БД и демон не долбят", () => {
    expect(PROFILES_POLL_MS).toBeGreaterThanOrEqual(3000);
    expect(PROFILES_POLL_MS).toBeLessThanOrEqual(5000);
  });

  it("первый тик держит loading, повторные тики таблицу не прячут", async () => {
    const gate = deferred<ProfileRow[]>();
    const fake = fakeApi();
    fake.api.list = vi.fn(() => gate.promise);
    const profiles = createProfiles(fake.api);

    const starting = profiles.start();
    expect(profiles.loading.value, "скелетон до первого ответа").toBe(true);

    gate.resolve([profileRow({ id: 1 })]);
    await starting;
    expect(profiles.loading.value).toBe(false);
    expect(profiles.rows.value).toHaveLength(1);

    const refreshing = profiles.tick();
    expect(profiles.loading.value, "обновление — тихое").toBe(false);
    await refreshing;
    profiles.stop();
  });

  it("параллельный тик возвращает тот же промис, а не новые запросы", async () => {
    const gate = deferred<ProfileRow[]>();
    const fake = fakeApi();
    fake.api.list = vi.fn(() => gate.promise);
    const profiles = createProfiles(fake.api);

    const first = profiles.tick();
    const second = profiles.tick();
    expect(fake.api.list).toHaveBeenCalledTimes(1);

    gate.resolve([]);
    await Promise.all([first, second]);
    expect(fake.api.list).toHaveBeenCalledTimes(1);
    expect(fake.api.listProxies).toHaveBeenCalledTimes(1);
  });

  it("ошибка списка видна, следующий успешный тик её снимает", async () => {
    const fake = fakeApi([profileRow({ id: 1 })]);
    const profiles = createProfiles(fake.api);
    fake.state.listError = new Error(
      "нет соединения с демоном на 127.0.0.1:8787",
    );

    await profiles.start();

    expect(profiles.error.value).toContain("нет соединения");
    expect(profiles.rows.value).toEqual([]);

    fake.state.listError = null;
    await profiles.tick();

    expect(profiles.error.value).toBeNull();
    expect(profiles.rows.value).toHaveLength(1);
    profiles.stop();
  });

  it("падение списка прокси тоже видно: без него колонка прокси невозможна", async () => {
    const fake = fakeApi([profileRow({ id: 1 })]);
    const profiles = createProfiles(fake.api);
    fake.state.proxiesError = new Error("HTTP 500: демон упал");

    await profiles.start();

    expect(profiles.error.value).toContain("HTTP 500");
    profiles.stop();
  });

  it("API, бросающий синхронно, не залипает в полёте — следующий тик идёт в сеть", async () => {
    const fake = fakeApi([profileRow({ id: 2 })]);
    let synchronous = true;
    fake.api.list = vi.fn(() => {
      if (synchronous) throw new Error("синхронный сбой списка");
      return Promise.resolve([profileRow({ id: 2 })]);
    });
    const profiles = createProfiles(fake.api);

    await profiles.tick();
    expect(profiles.error.value).toContain("синхронный сбой");

    synchronous = false;
    await profiles.tick();

    expect(fake.api.list).toHaveBeenCalledTimes(2);
    expect(
      profiles.rows.value.map((row) => row.id),
      "второй тик обязан пойти в API, а не вернуть старый промис",
    ).toEqual([2]);
    expect(profiles.error.value).toBeNull();
  });
});

describe("createProfiles: добавление", () => {
  it("успех: профили уходят в API, итог виден, список перечитан", async () => {
    const fake = fakeApi();
    const profiles = createProfiles(fake.api);
    await profiles.start();
    fake.state.addResult = { added: 2, skipped: 1, problems: ["дубликат"] };

    const ok = await profiles.add([{ name: "alpha" }, { name: "beta" }]);

    expect(ok).toBe(true);
    expect(fake.api.add).toHaveBeenCalledWith([{ name: "alpha" }, { name: "beta" }]);
    expect(profiles.addResult.value).toEqual({
      added: 2,
      skipped: 1,
      problems: ["дубликат"],
    });
    expect(profiles.actionError.value).toBeNull();
    expect(fake.api.list).toHaveBeenCalledTimes(2);
    profiles.stop();
  });

  it("ошибка API читаемо показывается, итог не появляется", async () => {
    const fake = fakeApi();
    const profiles = createProfiles(fake.api);
    await profiles.start();
    fake.state.addError = new Error("HTTP 400: имя уже занято");

    const ok = await profiles.add([{ name: "alpha" }]);

    expect(ok).toBe(false);
    expect(profiles.actionError.value).toContain("HTTP 400");
    expect(profiles.addResult.value).toBeNull();
    profiles.stop();
  });

  it("действия не выполняются параллельно", async () => {
    const fake = fakeApi();
    const profiles = createProfiles(fake.api);
    await profiles.start();
    const gate = deferred<ProfileChangeResult>();
    vi.mocked(fake.api.add).mockImplementationOnce(() => gate.promise);

    const first = profiles.add([{ name: "a" }]);
    const second = profiles.add([{ name: "b" }]);

    expect(await second, "второе действие отклоняется без запроса").toBe(false);
    expect(fake.api.add).toHaveBeenCalledTimes(1);

    gate.resolve({ added: 1, skipped: 0, problems: [] });
    expect(await first).toBe(true);
    profiles.stop();
  });
});

describe("createProfiles: импорт и удаление", () => {
  it("импорт показывает added/skipped/problems", async () => {
    const fake = fakeApi();
    const profiles = createProfiles(fake.api);
    await profiles.start();
    fake.state.importResult = { added: 5, skipped: 2, problems: ["строка 4"] };

    const ok = await profiles.importLines(["k-1", "k-2"]);

    expect(ok).toBe(true);
    expect(fake.api.importLines).toHaveBeenCalledWith(["k-1", "k-2"]);
    expect(profiles.importResult.value).toEqual({
      added: 5,
      skipped: 2,
      problems: ["строка 4"],
    });
    expect(profiles.actionError.value).toBeNull();
    profiles.stop();
  });

  it("ошибка импорта попадает в actionError", async () => {
    const fake = fakeApi();
    const profiles = createProfiles(fake.api);
    await profiles.start();
    fake.state.importError = new Error("строка 7: пустой key_ref");

    const ok = await profiles.importLines([""]);

    expect(ok).toBe(false);
    expect(profiles.actionError.value).toContain("строка 7");
    expect(profiles.importResult.value).toBeNull();
    profiles.stop();
  });

  it("удаление перечитывает список и чистит прошлую ошибку", async () => {
    const fake = fakeApi([profileRow({ id: 1 })]);
    const profiles = createProfiles(fake.api);
    await profiles.start();
    fake.state.rows = [];
    profiles.actionError.value = "старая ошибка";

    const ok = await profiles.remove(1);

    expect(ok).toBe(true);
    expect(fake.api.remove).toHaveBeenCalledWith(1);
    expect(profiles.actionError.value).toBeNull();
    expect(profiles.rows.value).toEqual([]);
    profiles.stop();
  });

  it("409 «профиль назначен потоку» доходит до пользователя текстом", async () => {
    const fake = fakeApi([profileRow({ id: 1 })]);
    const profiles = createProfiles(fake.api);
    await profiles.start();
    fake.state.removeError = new Error(
      "Профиль назначен потоку — сначала снимите назначение.",
    );

    const ok = await profiles.remove(1);

    expect(ok).toBe(false);
    expect(profiles.actionError.value).toContain("назначен потоку");
    expect(profiles.rows.value, "неудача не чистит список").toHaveLength(1);
    profiles.stop();
  });
});

describe("createProfiles: назначение и статус", () => {
  it("назначение диапазона показывает assigned/available", async () => {
    const fake = fakeApi();
    const profiles = createProfiles(fake.api);
    await profiles.start();
    fake.state.assignResult = { assigned: 4, available: 6 };

    const ok = await profiles.assign(1, 4);

    expect(ok).toBe(true);
    expect(fake.api.assign).toHaveBeenCalledWith(1, 4);
    expect(profiles.assignResult.value).toEqual({ assigned: 4, available: 6 });
    expect(profiles.actionError.value).toBeNull();
    expect(fake.api.list).toHaveBeenCalledTimes(2);
    profiles.stop();
  });

  it("400 диапазона — читаемый текст, итог не появляется", async () => {
    const fake = fakeApi();
    const profiles = createProfiles(fake.api);
    await profiles.start();
    fake.state.assignError = new Error(
      "Некорректный диапазон: проверьте start_id и end_id.",
    );

    const ok = await profiles.assign(10, 1);

    expect(ok).toBe(false);
    expect(profiles.actionError.value).toContain("start_id и end_id");
    expect(profiles.assignResult.value).toBeNull();
    profiles.stop();
  });

  it("сброс назначений показывает released", async () => {
    const fake = fakeApi();
    const profiles = createProfiles(fake.api);
    await profiles.start();
    fake.state.unassignResult = { released: 7 };

    const ok = await profiles.unassign();

    expect(ok).toBe(true);
    expect(profiles.unassignResult.value).toEqual({ released: 7 });
    expect(profiles.actionError.value).toBeNull();
    profiles.stop();
  });

  it("ошибка сброса назначений видна, прошлый итог не остаётся", async () => {
    const fake = fakeApi();
    const profiles = createProfiles(fake.api);
    await profiles.start();
    profiles.unassignResult.value = { released: 7 };
    fake.state.unassignError = new Error("HTTP 500: демон упал");

    const ok = await profiles.unassign();

    expect(ok).toBe(false);
    expect(profiles.actionError.value).toContain("HTTP 500");
    expect(profiles.unassignResult.value).toBeNull();
    profiles.stop();
  });

  it("смена статуса перечитывает список", async () => {
    const fake = fakeApi([profileRow({ id: 3, status: "free" })]);
    const profiles = createProfiles(fake.api);
    await profiles.start();

    const ok = await profiles.setStatus(3, "blocked");

    expect(ok).toBe(true);
    expect(fake.api.setStatus).toHaveBeenCalledWith(3, "blocked");
    expect(profiles.actionError.value).toBeNull();
    expect(fake.api.list).toHaveBeenCalledTimes(2);
    profiles.stop();
  });

  it("400 статуса — читаемый текст про допустимые значения", async () => {
    const fake = fakeApi();
    const profiles = createProfiles(fake.api);
    await profiles.start();
    fake.state.statusError = new Error(
      "Недопустимый статус: доступны free, blocked, error.",
    );

    // "active" недопустим контрактом: отправляем осознанно, чтобы поймать 400.
    const ok = await profiles.setStatus(3, "active" as WritableProfileStatus);

    expect(ok).toBe(false);
    expect(profiles.actionError.value).toContain("free, blocked, error");
    profiles.stop();
  });
});
