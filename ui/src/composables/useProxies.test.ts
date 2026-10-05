// Поведение экрана Proxies: опрос списка, действия через control API и
// прогресс проверки по last_checked_at.
//
// API подменяется целиком (транспорт мокается в lib/proxies.test.ts):
// здесь важен порядок вызовов, состояние ref'ов и читаемость ошибок.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createProxies, PROXIES_POLL_MS } from "./useProxies";
import type {
  ProxiesApi,
  ProxyChangeResult,
  ProxyFilePath,
  ProxyRow,
} from "../lib/proxies";

function proxyRow(overrides: Partial<ProxyRow> & { id: number }): ProxyRow {
  return {
    label: null,
    scheme: "http",
    host: `host-${overrides.id}.example`,
    port: 8080,
    country: null,
    latency_ms: null,
    is_alive: true,
    fail_count: 0,
    last_checked_at: null,
    last_error: null,
    assigned_browser_id: null,
    usage_count: 0,
    ...overrides,
  };
}

/** Управляемый фейк API: каждая функция падает по своей ошибке из state. */
function fakeApi(initial: ProxyRow[] = []) {
  const state = {
    rows: initial.map((row) => ({ ...row })),
    listError: null as Error | null,
    addResult: { added: 0, skipped: 0, problems: [] } as ProxyChangeResult,
    addError: null as Error | null,
    importResult: { added: 0, skipped: 0, problems: [] } as ProxyChangeResult,
    importError: null as Error | null,
    removed: 1,
    removeError: null as Error | null,
    checkError: null as Error | null,
    file: { path: "/data/proxies.txt", exists: true } as ProxyFilePath,
    fileError: null as Error | null,
  };

  const api: ProxiesApi = {
    list: vi.fn(async () => {
      if (state.listError) throw state.listError;
      return state.rows.map((row) => ({ ...row }));
    }),
    add: vi.fn(async () => {
      if (state.addError) throw state.addError;
      return state.addResult;
    }),
    importFile: vi.fn(async () => {
      if (state.importError) throw state.importError;
      return state.importResult;
    }),
    remove: vi.fn(async () => {
      if (state.removeError) throw state.removeError;
      return state.removed;
    }),
    check: vi.fn(async () => {
      if (state.checkError) throw state.checkError;
    }),
    filePath: vi.fn(async () => {
      if (state.fileError) throw state.fileError;
      return { ...state.file };
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

describe("createProxies: список и опрос", () => {
  it("start читает список и обновляет его раз в PROXIES_POLL_MS", async () => {
    const fake = fakeApi([proxyRow({ id: 1 })]);
    const proxies = createProxies(fake.api);

    await proxies.start();

    expect(fake.api.list).toHaveBeenCalledTimes(1);
    expect(proxies.rows.value.map((row) => row.id)).toEqual([1]);
    expect(proxies.loading.value).toBe(false);
    expect(proxies.error.value).toBeNull();

    await vi.advanceTimersByTimeAsync(PROXIES_POLL_MS);
    expect(fake.api.list).toHaveBeenCalledTimes(2);

    proxies.stop();
    await vi.advanceTimersByTimeAsync(PROXIES_POLL_MS * 3);
    expect(fake.api.list, "после stop тики не уходят").toHaveBeenCalledTimes(2);
  });

  it("интервал живёт в диапазоне 2–5 с — БД не долбят", () => {
    expect(PROXIES_POLL_MS).toBeGreaterThanOrEqual(2000);
    expect(PROXIES_POLL_MS).toBeLessThanOrEqual(5000);
  });

  it("первый тик держит loading, повторные тики таблицу не прячут", async () => {
    const gate = deferred<ProxyRow[]>();
    const fake = fakeApi();
    fake.api.list = vi.fn(() => gate.promise);
    const proxies = createProxies(fake.api);

    const starting = proxies.start();
    expect(proxies.loading.value, "скелетон до первого ответа").toBe(true);

    gate.resolve([proxyRow({ id: 1 })]);
    await starting;
    expect(proxies.loading.value).toBe(false);
    expect(proxies.rows.value).toHaveLength(1);

    const refreshing = proxies.tick();
    expect(proxies.loading.value, "обновление — тихое").toBe(false);
    await refreshing;
    proxies.stop();
  });

  it("параллельный тик возвращает тот же промис, а не новый запрос", async () => {
    const gate = deferred<ProxyRow[]>();
    const fake = fakeApi();
    fake.api.list = vi.fn(() => gate.promise);
    const proxies = createProxies(fake.api);

    const first = proxies.tick();
    const second = proxies.tick();
    expect(fake.api.list).toHaveBeenCalledTimes(1);

    gate.resolve([]);
    await Promise.all([first, second]);
    expect(fake.api.list).toHaveBeenCalledTimes(1);
  });

  it("ошибка списка видна, следующий успешный тик её снимает", async () => {
    const fake = fakeApi([proxyRow({ id: 1 })]);
    const proxies = createProxies(fake.api);
    fake.state.listError = new Error(
      "нет соединения с демоном на 127.0.0.1:8787",
    );

    await proxies.start();

    expect(proxies.error.value).toContain("нет соединения");
    expect(proxies.rows.value).toEqual([]);

    fake.state.listError = null;
    await proxies.tick();

    expect(proxies.error.value).toBeNull();
    expect(proxies.rows.value).toHaveLength(1);
    proxies.stop();
  });

  it("API, бросающий синхронно, не залипает в полёте — следующий тик идёт в сеть", async () => {
    const fake = fakeApi([proxyRow({ id: 2 })]);
    let synchronous = true;
    fake.api.list = vi.fn(() => {
      if (synchronous) throw new Error("синхронный сбой списка");
      return Promise.resolve([proxyRow({ id: 2 })]);
    });
    const proxies = createProxies(fake.api);

    await proxies.tick();
    expect(proxies.error.value).toContain("синхронный сбой");

    synchronous = false;
    await proxies.tick();

    expect(fake.api.list).toHaveBeenCalledTimes(2);
    expect(
      proxies.rows.value.map((row) => row.id),
      "второй тик обязан пойти в API, а не вернуть старый промис",
    ).toEqual([2]);
    expect(proxies.error.value).toBeNull();
  });
});

describe("createProxies: добавление", () => {
  it("успех: строки уходят в API, итог виден, список перечитан", async () => {
    const fake = fakeApi();
    const proxies = createProxies(fake.api);
    await proxies.start();
    fake.state.addResult = { added: 2, skipped: 1, problems: ["дубликат"] };

    const ok = await proxies.add(["http://a:1", "b:2"]);

    expect(ok).toBe(true);
    expect(fake.api.add).toHaveBeenCalledWith(["http://a:1", "b:2"]);
    expect(proxies.addResult.value).toEqual({
      added: 2,
      skipped: 1,
      problems: ["дубликат"],
    });
    expect(proxies.actionError.value).toBeNull();
    expect(fake.api.list).toHaveBeenCalledTimes(2);
    proxies.stop();
  });

  it("ошибка API читаемо показывается, итог не появляется", async () => {
    const fake = fakeApi();
    const proxies = createProxies(fake.api);
    await proxies.start();
    fake.state.addError = new Error("HTTP 400: демон отклонил список");

    const ok = await proxies.add(["a:1"]);

    expect(ok).toBe(false);
    expect(proxies.actionError.value).toContain("HTTP 400");
    expect(proxies.addResult.value).toBeNull();
    proxies.stop();
  });

  it("действия не выполняются параллельно", async () => {
    const fake = fakeApi();
    const proxies = createProxies(fake.api);
    await proxies.start();
    const gate = deferred<ProxyChangeResult>();
    vi.mocked(fake.api.add).mockImplementationOnce(() => gate.promise);

    const first = proxies.add(["a:1"]);
    const second = proxies.add(["b:2"]);

    expect(await second, "второе действие отклоняется без запроса").toBe(false);
    expect(fake.api.add).toHaveBeenCalledTimes(1);

    gate.resolve({ added: 1, skipped: 0, problems: [] });
    expect(await first).toBe(true);
    proxies.stop();
  });
});

describe("createProxies: импорт и удаление", () => {
  it("импорт показывает added/skipped/problems", async () => {
    const fake = fakeApi();
    const proxies = createProxies(fake.api);
    await proxies.start();
    fake.state.importResult = { added: 5, skipped: 2, problems: ["строка 4"] };

    const ok = await proxies.importFile();

    expect(ok).toBe(true);
    expect(fake.api.importFile).toHaveBeenCalledTimes(1);
    expect(proxies.importResult.value).toEqual({
      added: 5,
      skipped: 2,
      problems: ["строка 4"],
    });
    expect(proxies.actionError.value).toBeNull();
    proxies.stop();
  });

  it("ошибка импорта попадает в actionError", async () => {
    const fake = fakeApi();
    const proxies = createProxies(fake.api);
    await proxies.start();
    fake.state.importError = new Error("proxies.txt не найден");

    const ok = await proxies.importFile();

    expect(ok).toBe(false);
    expect(proxies.actionError.value).toContain("proxies.txt");
    expect(proxies.importResult.value).toBeNull();
    proxies.stop();
  });

  it("удаление перечитывает список и чистит прошлую ошибку", async () => {
    const fake = fakeApi([proxyRow({ id: 1 })]);
    const proxies = createProxies(fake.api);
    await proxies.start();
    fake.state.rows = [];
    proxies.actionError.value = "старая ошибка";

    const ok = await proxies.remove(1);

    expect(ok).toBe(true);
    expect(fake.api.remove).toHaveBeenCalledWith(1);
    expect(proxies.actionError.value).toBeNull();
    expect(proxies.rows.value).toEqual([]);
    proxies.stop();
  });

  it("409 «прокси назначен потоку» доходит до пользователя текстом", async () => {
    const fake = fakeApi([proxyRow({ id: 1 })]);
    const proxies = createProxies(fake.api);
    await proxies.start();
    fake.state.removeError = new Error(
      "Прокси назначен потоку — сначала снимите назначение.",
    );

    const ok = await proxies.remove(1);

    expect(ok).toBe(false);
    expect(proxies.actionError.value).toContain("назначен потоку");
    expect(proxies.rows.value, "неудача не чистит список").toHaveLength(1);
    proxies.stop();
  });
});

describe("createProxies: проверка", () => {
  it("проверка идёт, пока все last_checked_at не обновятся", async () => {
    const fake = fakeApi([
      proxyRow({ id: 1, last_checked_at: 100 }),
      proxyRow({ id: 2, last_checked_at: 200 }),
    ]);
    const proxies = createProxies(fake.api);
    await proxies.start();

    const ok = await proxies.runCheck();

    expect(ok).toBe(true);
    expect(fake.api.check).toHaveBeenCalledTimes(1);
    expect(proxies.checking.value).toBe(true);
    expect(proxies.checkProgress.value).toEqual({ done: 0, total: 2 });

    fake.state.rows[0] = { ...fake.state.rows[0], last_checked_at: 300 };
    await proxies.tick();
    expect(proxies.checkProgress.value).toEqual({ done: 1, total: 2 });
    expect(proxies.checking.value).toBe(true);

    fake.state.rows[1] = { ...fake.state.rows[1], last_checked_at: 400 };
    await proxies.tick();
    expect(proxies.checkProgress.value).toEqual({ done: 2, total: 2 });
    expect(proxies.checking.value).toBe(false);
    proxies.stop();
  });

  it("удалённая во время проверки строка не залипает в прогрессе", async () => {
    const fake = fakeApi([proxyRow({ id: 1, last_checked_at: 100 })]);
    const proxies = createProxies(fake.api);
    await proxies.start();
    await proxies.runCheck();
    expect(proxies.checking.value).toBe(true);

    fake.state.rows = [];
    await proxies.tick();

    expect(proxies.checking.value).toBe(false);
    proxies.stop();
  });

  it("пустой список — проверять нечего, индикатор не включается", async () => {
    const fake = fakeApi();
    const proxies = createProxies(fake.api);
    await proxies.start();

    await proxies.runCheck();

    expect(proxies.checking.value).toBe(false);
    expect(proxies.checkProgress.value).toEqual({ done: 0, total: 0 });
    proxies.stop();
  });

  it("409 «проверка уже идёт» — сообщение, а не индикатор", async () => {
    const fake = fakeApi([proxyRow({ id: 1, last_checked_at: 100 })]);
    const proxies = createProxies(fake.api);
    await proxies.start();
    fake.state.checkError = new Error(
      "Проверка уже идёт — дождитесь её завершения.",
    );

    const ok = await proxies.runCheck();

    expect(ok).toBe(false);
    expect(proxies.actionError.value).toContain("Проверка уже идёт");
    expect(proxies.checking.value).toBe(false);
    proxies.stop();
  });
});

describe("createProxies: открытие proxies.txt", () => {
  it("путь из демона уходит в opener, прошлая ошибка снимается", async () => {
    const fake = fakeApi();
    const opener = vi.fn(async () => {});
    const proxies = createProxies(fake.api, PROXIES_POLL_MS, opener);
    await proxies.start();
    proxies.actionError.value = "старая ошибка";

    const ok = await proxies.openFile();

    expect(ok).toBe(true);
    expect(fake.api.filePath).toHaveBeenCalledTimes(1);
    expect(opener).toHaveBeenCalledWith("/data/proxies.txt");
    expect(proxies.actionError.value).toBeNull();
    expect(proxies.pending.value).toBeNull();
    proxies.stop();
  });

  it("файла нет на диске — своя причина, opener не вызывается", async () => {
    const fake = fakeApi();
    fake.state.file = { path: "/data/proxies.txt", exists: false };
    const opener = vi.fn(async () => {});
    const proxies = createProxies(fake.api, PROXIES_POLL_MS, opener);
    await proxies.start();

    const ok = await proxies.openFile();

    expect(ok).toBe(false);
    expect(opener).not.toHaveBeenCalled();
    expect(proxies.actionError.value).toContain("не найден");
    expect(proxies.actionError.value).toContain("/data/proxies.txt");
    proxies.stop();
  });

  it("отказ opener доходит в actionError, а не валит экран", async () => {
    const fake = fakeApi();
    const opener = vi.fn(async () => {
      throw new Error("нет приложения по умолчанию");
    });
    const proxies = createProxies(fake.api, PROXIES_POLL_MS, opener);
    await proxies.start();

    const ok = await proxies.openFile();

    expect(ok).toBe(false);
    expect(proxies.actionError.value).toContain("нет приложения по умолчанию");
    proxies.stop();
  });

  it("повторный вызов во время первого не идёт параллельно", async () => {
    const fake = fakeApi();
    let release!: () => void;
    fake.api.filePath = vi.fn(
      () =>
        new Promise<ProxyFilePath>((resolve) => {
          release = () => resolve({ path: "/data/proxies.txt", exists: true });
        }),
    );
    const opener = vi.fn(async () => {});
    const proxies = createProxies(fake.api, PROXIES_POLL_MS, opener);
    await proxies.start();

    const first = proxies.openFile();
    const second = await proxies.openFile();

    expect(second).toBe(false);
    release();
    expect(await first).toBe(true);
    expect(fake.api.filePath).toHaveBeenCalledTimes(1);
    proxies.stop();
  });
});
