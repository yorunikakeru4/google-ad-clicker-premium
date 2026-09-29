// Поведение экрана Diagnostics: опрос снимков, карточки воркеров и сбор через
// control API.
//
// API подменяется целиком (транспорт и читалка мокается в
// lib/diagnostics.test.ts): здесь важен порядок вызовов, состояние ref'ов и
// читаемость ошибок.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createDiagnostics, DIAGNOSTICS_POLL_MS } from "./useDiagnostics";
import type { DiagnosticSnapshot, DiagnosticsApi } from "../lib/diagnostics";

function snapshot(
  browser_id: string,
  overrides: Partial<DiagnosticSnapshot> = {},
): DiagnosticSnapshot {
  return {
    ts: 1_760_000_000,
    browser_id,
    proxy_id: null,
    ip: "203.0.113.7",
    country: "DE",
    user_agent: "Mozilla/5.0",
    accept_language: "de-DE",
    timezone_id: "Europe/Berlin",
    screen_w: 1920,
    screen_h: 1080,
    platform: "Linux x86_64",
    webgl_vendor: null,
    webgl_renderer: null,
    browser_version: null,
    headers: null,
    suspicion_flags: null,
    ...overrides,
  };
}

/** Управляемый фейк API: каждая функция падает по своей ошибке из state. */
function fakeApi(snapshots: DiagnosticSnapshot[] = []) {
  const state = {
    snapshots,
    workerIds: [] as string[],
    listError: null as Error | null,
    workersError: null as Error | null,
    requested: 2,
    collectError: null as Error | null,
  };

  const api: DiagnosticsApi = {
    list: vi.fn(async () => {
      if (state.listError) throw state.listError;
      return state.snapshots.map((row) => ({ ...row }));
    }),
    liveWorkerIds: vi.fn(async () => {
      if (state.workersError) throw state.workersError;
      return [...state.workerIds];
    }),
    collect: vi.fn(async () => {
      if (state.collectError) throw state.collectError;
      return state.requested;
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

describe("createDiagnostics: снимки и опрос", () => {
  it("start читает снимки и воркеров, обновляет раз в DIAGNOSTICS_POLL_MS", async () => {
    const fake = fakeApi([snapshot("br-1")]);
    fake.state.workerIds = ["br-1", "br-2"];
    const diagnostics = createDiagnostics(fake.api);

    await diagnostics.start();

    expect(fake.api.list).toHaveBeenCalledTimes(1);
    expect(fake.api.liveWorkerIds).toHaveBeenCalledTimes(1);
    expect(diagnostics.snapshots.value.map((row) => row.browser_id)).toEqual([
      "br-1",
    ]);
    expect(diagnostics.loading.value).toBe(false);
    expect(diagnostics.error.value).toBeNull();

    await vi.advanceTimersByTimeAsync(DIAGNOSTICS_POLL_MS);
    expect(fake.api.list).toHaveBeenCalledTimes(2);

    diagnostics.stop();
    await vi.advanceTimersByTimeAsync(DIAGNOSTICS_POLL_MS * 3);
    expect(fake.api.list, "после stop тики не уходят").toHaveBeenCalledTimes(2);
  });

  it("интервал живёт в диапазоне 3–5 с — БД не долбят", () => {
    expect(DIAGNOSTICS_POLL_MS).toBeGreaterThanOrEqual(3000);
    expect(DIAGNOSTICS_POLL_MS).toBeLessThanOrEqual(5000);
  });

  it("первый тик держит loading, повторные тики экран не прячут", async () => {
    const gate = deferred<DiagnosticSnapshot[]>();
    const fake = fakeApi();
    fake.api.list = vi.fn(() => gate.promise);
    const diagnostics = createDiagnostics(fake.api);

    const starting = diagnostics.start();
    expect(diagnostics.loading.value, "скелетон до первого ответа").toBe(true);

    gate.resolve([snapshot("br-1")]);
    await starting;
    expect(diagnostics.loading.value).toBe(false);
    expect(diagnostics.snapshots.value).toHaveLength(1);

    const refreshing = diagnostics.tick();
    expect(diagnostics.loading.value, "обновление — тихое").toBe(false);
    await refreshing;
    diagnostics.stop();
  });

  it("параллельный тик возвращает тот же промис, а не новый запрос", async () => {
    const gate = deferred<DiagnosticSnapshot[]>();
    const fake = fakeApi();
    fake.api.list = vi.fn(() => gate.promise);
    const diagnostics = createDiagnostics(fake.api);

    const first = diagnostics.tick();
    const second = diagnostics.tick();
    expect(fake.api.list).toHaveBeenCalledTimes(1);

    gate.resolve([]);
    await Promise.all([first, second]);
    expect(fake.api.list).toHaveBeenCalledTimes(1);
  });

  it("ошибка чтения видна, следующий успешный тик её снимает", async () => {
    const fake = fakeApi([snapshot("br-1")]);
    const diagnostics = createDiagnostics(fake.api);
    fake.state.listError = new Error("Ошибка чтения из базы: нет таблицы");

    await diagnostics.start();

    expect(diagnostics.error.value).toContain("нет таблицы");
    expect(diagnostics.snapshots.value).toEqual([]);

    fake.state.listError = null;
    await diagnostics.tick();

    expect(diagnostics.error.value).toBeNull();
    expect(diagnostics.snapshots.value).toHaveLength(1);
    diagnostics.stop();
  });

  it("падение списка живых воркеров видно в error, а не глушится молча", async () => {
    const fake = fakeApi([snapshot("br-1")]);
    fake.state.workersError = new Error("База данных не открыта");
    const diagnostics = createDiagnostics(fake.api);

    await diagnostics.start();

    expect(diagnostics.error.value).toContain("не открыта");
    diagnostics.stop();
  });

  it("без открытой БД тики не ходят в читалку и не пугают ошибкой", async () => {
    const fake = fakeApi([snapshot("br-1")]);
    let ready = false;
    const diagnostics = createDiagnostics(fake.api, { isReady: () => ready });

    await diagnostics.start();
    await vi.advanceTimersByTimeAsync(DIAGNOSTICS_POLL_MS * 2);

    expect(fake.api.list).not.toHaveBeenCalled();
    expect(diagnostics.error.value).toBeNull();
    expect(diagnostics.loading.value).toBe(false);

    ready = true;
    await diagnostics.tick();

    expect(fake.api.list).toHaveBeenCalledTimes(1);
    expect(diagnostics.snapshots.value).toHaveLength(1);
    diagnostics.stop();
  });

  it("cards: карточка на воркера со снимком и без него", async () => {
    const fake = fakeApi([snapshot("br-2")]);
    fake.state.workerIds = ["br-1", "br-2"];
    const diagnostics = createDiagnostics(fake.api);

    await diagnostics.start();

    expect(
      diagnostics.cards.value.map((card) => [
        card.browserId,
        card.snapshot !== null,
      ]),
    ).toEqual([
      ["br-2", true],
      ["br-1", false],
    ]);
    diagnostics.stop();
  });
});

describe("createDiagnostics: сбор снимка", () => {
  it("сбор для воркера: requested виден, ошибка чистится, список перечитан", async () => {
    const fake = fakeApi();
    const diagnostics = createDiagnostics(fake.api);
    await diagnostics.start();
    fake.state.requested = 3;
    diagnostics.actionError.value = "старая ошибка";

    const ok = await diagnostics.collect("br-1");

    expect(ok).toBe(true);
    expect(fake.api.collect).toHaveBeenCalledWith("br-1");
    expect(diagnostics.collectResult.value).toEqual({ requested: 3 });
    expect(diagnostics.actionError.value).toBeNull();
    expect(fake.api.list).toHaveBeenCalledTimes(2);
    diagnostics.stop();
  });

  it("сбор для всех уходит collect(null) со своим итогом", async () => {
    const fake = fakeApi();
    const diagnostics = createDiagnostics(fake.api);
    await diagnostics.start();
    fake.state.requested = 5;

    const ok = await diagnostics.collectAll();

    expect(ok).toBe(true);
    expect(fake.api.collect).toHaveBeenCalledWith(null);
    expect(diagnostics.collectResult.value).toEqual({ requested: 5 });
    diagnostics.stop();
  });

  it("ошибка сбора читаемо в actionError, итог не появляется", async () => {
    const fake = fakeApi();
    const diagnostics = createDiagnostics(fake.api);
    await diagnostics.start();
    fake.state.collectError = new Error(
      "Демон отклонил запрос сбора диагностики (invalid_request): укажите browser_id воркера или all.",
    );

    const ok = await diagnostics.collect("br-1");

    expect(ok).toBe(false);
    expect(diagnostics.actionError.value).toContain("invalid_request");
    expect(diagnostics.collectResult.value).toBeNull();
    diagnostics.stop();
  });

  it("действия не выполняются параллельно", async () => {
    const fake = fakeApi();
    const diagnostics = createDiagnostics(fake.api);
    await diagnostics.start();
    const gate = deferred<number>();
    vi.mocked(fake.api.collect).mockImplementationOnce(() => gate.promise);

    const first = diagnostics.collect("br-1");
    const second = diagnostics.collect("br-2");

    expect(await second, "второе действие отклоняется без запроса").toBe(false);
    expect(fake.api.collect).toHaveBeenCalledTimes(1);

    gate.resolve(1);
    expect(await first).toBe(true);
    diagnostics.stop();
  });

  it("успех очищает прошлый итог только на новом действии", async () => {
    const fake = fakeApi();
    const diagnostics = createDiagnostics(fake.api);
    await diagnostics.start();

    await diagnostics.collect("br-1");
    expect(diagnostics.collectResult.value).toEqual({ requested: 2 });

    // Тик опроса не трогает итог: он отвечает на последнее действие.
    await diagnostics.tick();
    expect(diagnostics.collectResult.value).toEqual({ requested: 2 });
    diagnostics.stop();
  });
});
