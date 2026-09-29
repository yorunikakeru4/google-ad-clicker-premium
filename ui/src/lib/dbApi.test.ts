import { describe, expect, it } from "vitest";
import { createDbApi, dbErrorMessage } from "./dbApi";
import type { LogQueryFilters } from "./logFilters";

type InvokeCall = { command: string; args: Record<string, unknown> };

/** Фейковый invoke: записывает вызовы и возвращает заготовленный ответ. */
function fakeInvoke(reply: unknown = null) {
  const calls: InvokeCall[] = [];
  const invoke = (command: string, args: Record<string, unknown>) => {
    calls.push({ command, args });
    return Promise.resolve(reply);
  };
  return { calls, invoke };
}

const EMPTY_QUERY: LogQueryFilters = {
  level: null,
  category: null,
  browserId: null,
  since: null,
  until: null,
};

describe("dbErrorMessage", () => {
  it("строковая ошибка проходит как есть", () => {
    expect(dbErrorMessage("База упала")).toBe("База упала");
    expect(dbErrorMessage(new Error("net down"))).toBe("net down");
  });

  it("DatabaseNotFound: виден путь и подсказка про движок", () => {
    const text = dbErrorMessage({
      kind: "DatabaseNotFound",
      message: { path: "/home/user/adclicker.db" },
    });

    expect(text).toContain("/home/user/adclicker.db");
    expect(text).toContain("не найдена");
  });

  it("OpenFailed: видны путь и причину", () => {
    const text = dbErrorMessage({
      kind: "OpenFailed",
      message: { path: "/tmp/x.db", reason: "в базе нет таблицы logs" },
    });

    expect(text).toContain("/tmp/x.db");
    expect(text).toContain("в базе нет таблицы logs");
  });

  it("ReadFailed: видна причину", () => {
    const text = dbErrorMessage({
      kind: "ReadFailed",
      message: { reason: "SQLITE_BUSY" },
    });

    expect(text).toContain("SQLITE_BUSY");
  });

  it("NotOpen: единичный вариант без message подсказывает про db_open", () => {
    expect(dbErrorMessage({ kind: "NotOpen" })).toContain("db_open");
  });

  it("неизвестный формат не превращается в [object Object]", () => {
    expect(dbErrorMessage(42)).toBe("42");
    expect(dbErrorMessage({ something: "else" })).toBe(
      '{"something":"else"}',
    );
    expect(dbErrorMessage(null)).toBe("null");
  });
});

describe("createDbApi", () => {
  it("open зовёт db_open без аргументов (env → adclicker.db решает Rust)", async () => {
    const { calls, invoke } = fakeInvoke("/data/adclicker.db");
    const api = createDbApi(invoke);

    const path = await api.open();

    expect(path).toBe("/data/adclicker.db");
    expect(calls).toEqual([{ command: "db_open", args: {} }]);
  });

  it("listLogsPage передаёт фильтры и курсор camelCase-именами", async () => {
    const { calls, invoke } = fakeInvoke([]);
    const api = createDbApi(invoke);

    await api.listLogsPage({
      query: { ...EMPTY_QUERY, level: "ERROR", since: 100, until: 200 },
      limit: 50,
      cursor: { ts: 9, id: 7 },
    });

    expect(calls).toEqual([
      {
        command: "list_logs_page",
        args: {
          limit: 50,
          level: "ERROR",
          category: null,
          browserId: null,
          since: 100,
          until: 200,
          beforeTs: 9,
          beforeId: 7,
        },
      },
    ]);
  });

  it("listLogsPage без курсора не шлёт before-ts/before-id", async () => {
    const { calls, invoke } = fakeInvoke([]);
    const api = createDbApi(invoke);

    await api.listLogsPage({ query: EMPTY_QUERY, limit: 10, cursor: null });

    expect(calls[0].args).toEqual({
      limit: 10,
      level: null,
      category: null,
      browserId: null,
      since: null,
      until: null,
      beforeTs: null,
      beforeId: null,
    });
  });

  it("countLogs и метрики уходят в свои команды", async () => {
    const { calls, invoke } = fakeInvoke(0);
    const api = createDbApi(invoke);

    await api.countLogs(EMPTY_QUERY);
    await api.runsSummary(1000);
    await api.clicksPerHour(1000, 24);
    await api.requestsLastHour(5000);
    await api.captchaShare(1000);
    await api.activeWorkers(5000, 15);

    expect(calls.map((call) => call.command)).toEqual([
      "count_logs",
      "runs_summary",
      "clicks_per_hour",
      "requests_last_hour",
      "captcha_share",
      "active_workers",
    ]);
    expect(calls[0].args).toEqual({ ...EMPTY_QUERY });
    expect(calls[1].args).toEqual({ since: 1000 });
    expect(calls[2].args).toEqual({ since: 1000, buckets: 24 });
    expect(calls[3].args).toEqual({ now: 5000 });
    expect(calls[4].args).toEqual({ since: 1000 });
    expect(calls[5].args).toEqual({ now: 5000, thresholdSecs: 15 });
  });

  it("ошибка invoke не глотается — её разбирает dbErrorMessage", async () => {
    const payload = {
      kind: "DatabaseNotFound",
      message: { path: "/data/adclicker.db" },
    };
    const api = createDbApi(() => Promise.reject(payload));

    await expect(api.open()).rejects.toEqual(payload);
    expect(dbErrorMessage(await api.open().catch((e: unknown) => e))).toContain(
      "/data/adclicker.db",
    );
  });
});
