// Экран Tasks: конфиг (/control/config) читается по требованию — при
// монтировании и по кнопке «Обновить», без собственного интервала опроса.

import { describe, expect, it, vi } from "vitest";
import { createTasks, type TasksApi } from "./useTasks";
import type { TasksConfig } from "../lib/tasks";

const CONFIG: TasksConfig = {
  queryFile: "queries.txt",
  query: "",
  intervalStart: "09:00",
  intervalEnd: "18:00",
  adPageMinWait: 10,
  adPageMaxWait: 15,
  nonadPageMinWait: 15,
  nonadPageMaxWait: 20,
  loopWaitTime: 60,
};

function fakeApi() {
  const state = { error: null as Error | null, config: CONFIG };
  const api: TasksApi = {
    loadConfig: vi.fn(async () => {
      if (state.error) throw state.error;
      return { ...state.config };
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

describe("createTasks: чтение конфига", () => {
  it("успех: конфиг положен, ошибка чистится", async () => {
    const fake = fakeApi();
    const tasks = createTasks(fake.api);
    tasks.error.value = "старая ошибка";

    const ok = await tasks.load();

    expect(ok).toBe(true);
    expect(fake.api.loadConfig).toHaveBeenCalledTimes(1);
    expect(tasks.config.value).toEqual(CONFIG);
    expect(tasks.error.value).toBeNull();
    expect(tasks.loading.value).toBe(false);
  });

  it("ошибка API видна читаемо, прошлый конфиг не затирается", async () => {
    const fake = fakeApi();
    const tasks = createTasks(fake.api);
    await tasks.load();
    fake.state.error = new Error("неверный токен");

    const ok = await tasks.load();

    expect(ok).toBe(false);
    expect(tasks.error.value).toContain("неверный токен");
    expect(tasks.config.value, "старые данные остаются до успешного чтения").toEqual(CONFIG);
    expect(tasks.loading.value).toBe(false);
  });

  it("параллельные load не плодят запросы к API", async () => {
    const gate = deferred<TasksConfig>();
    const loadConfig = vi.fn(() => gate.promise);
    const tasks = createTasks({ loadConfig });

    const first = tasks.load();
    const second = tasks.load();

    expect(loadConfig).toHaveBeenCalledTimes(1);

    gate.resolve(CONFIG);
    await Promise.all([first, second]);
    expect(loadConfig).toHaveBeenCalledTimes(1);
    expect(tasks.config.value).toEqual(CONFIG);
  });

  it("loading держится всё время запроса", async () => {
    const gate = deferred<TasksConfig>();
    const tasks = createTasks({ loadConfig: () => gate.promise });

    const loading = tasks.load();
    expect(tasks.loading.value).toBe(true);

    gate.resolve(CONFIG);
    await loading;
    expect(tasks.loading.value).toBe(false);
  });

  it("конфиг не опрашивается сам: повторный load только по кнопке", async () => {
    const fake = fakeApi();
    const tasks = createTasks(fake.api);

    await tasks.load();
    await tasks.load();

    // ровно два ручных вызова: монтирование и кнопка «Обновить», интервала нет
    expect(fake.api.loadConfig).toHaveBeenCalledTimes(2);
  });
});
