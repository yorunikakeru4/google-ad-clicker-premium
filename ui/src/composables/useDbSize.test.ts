// Индикатор размера БД: дешёвое чтение при старте и по таймеру, пауза на
// stop. isReady держит команду под реальной фазой БД: вне открытого
// соединения db_size не зовётся и не шумит ошибкой NotOpen.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DB_SIZE_POLL_MS, createDbSize } from "./useDbSize";
import type { DbSize } from "../lib/dbApi";

function reply(overrides: Partial<DbSize> = {}): DbSize {
  return { path: "/data/adclicker.db", bytes: 1000, wal_bytes: 300, ...overrides };
}

function setup(options: { ready?: boolean; failWith?: unknown } = {}) {
  let ready = options.ready ?? true;
  const calls: number[] = [];
  const api = {
    dbSize: vi.fn(() => {
      calls.push(calls.length);
      if (options.failWith !== undefined) return Promise.reject(options.failWith);
      return Promise.resolve(reply());
    }),
  };
  const size = createDbSize({
    api,
    isReady: () => ready,
    pollMs: DB_SIZE_POLL_MS,
  });
  return { size, api, setReady: (value: boolean) => (ready = value) };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("createDbSize: чтение размера", () => {
  it("refresh суммирует основной файл и -wal и гасит прошлую ошибку", async () => {
    const { size, api } = setup();
    size.error.value = "старая ошибка";

    await size.refresh();

    expect(api.dbSize).toHaveBeenCalledTimes(1);
    expect(size.bytes.value).toBe(1300);
    expect(size.error.value).toBeNull();
  });

  it("база не открыта — команда не зовётся", async () => {
    const { size, api, setReady } = setup({ ready: false });
    setReady(false);

    await size.refresh();

    expect(api.dbSize).not.toHaveBeenCalled();
    expect(size.bytes.value).toBeNull();
  });

  it("ошибка чтения становится текстом, а прежний размер не выдумывается заново", async () => {
    const { size } = setup({
      failWith: { kind: "ReadFailed", message: { reason: "SQLITE_BUSY" } },
    });
    size.bytes.value = 700;

    await size.refresh();

    expect(size.error.value).toContain("SQLITE_BUSY");
    expect(size.bytes.value).toBe(700);
  });
});

describe("createDbSize: таймер", () => {
  it("start читает сразу и повторяет по таймеру, stop останавливает", async () => {
    const { size, api } = setup();

    size.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(api.dbSize).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(DB_SIZE_POLL_MS);
    expect(api.dbSize).toHaveBeenCalledTimes(2);

    size.stop();
    await vi.advanceTimersByTimeAsync(DB_SIZE_POLL_MS * 3);
    expect(api.dbSize, "после stop тики не уходят").toHaveBeenCalledTimes(2);
  });

  it("повторный start не плодит параллельные таймеры", async () => {
    const { size, api } = setup();

    size.start();
    size.start();
    await vi.advanceTimersByTimeAsync(DB_SIZE_POLL_MS);

    expect(api.dbSize).toHaveBeenCalledTimes(2);

    size.stop();
  });

  it("интервал обновления — в согласованном окне 30–60 секунд", () => {
    expect(DB_SIZE_POLL_MS).toBeGreaterThanOrEqual(30_000);
    expect(DB_SIZE_POLL_MS).toBeLessThanOrEqual(60_000);
  });
});
