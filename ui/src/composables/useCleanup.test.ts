// Поведение секции очистки на Settings: статус по расписанию, ручной запуск
// и превью с dry_run.
//
// API подменяется целиком: здесь важен порядок вызовов, состояние ref'ов,
// in-flight защита (второй прогон не должен уйти в сеть) и читаемость ошибок.

import { describe, expect, it, vi } from "vitest";
import { createCleanup } from "./useCleanup";
import type { CleanupApi, CleanupReport, CleanupStatus } from "../lib/cleanup";

function report(overrides: Partial<CleanupReport> = {}): CleanupReport {
  return {
    removed: 3,
    removed_bytes: 1_572_864,
    skipped_active: 1,
    errors: 0,
    duration_ms: 1200,
    dry_run: false,
    ...overrides,
  };
}

function status(overrides: Partial<CleanupStatus> = {}): CleanupStatus {
  return {
    last: { ts: new Date(2026, 9, 30, 4, 0, 0).getTime() / 1000, report: report() },
    next_run: new Date(2026, 10, 1, 4, 0, 0).getTime() / 1000,
    ...overrides,
  };
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

/** Управляемый фейк API: каждая функция падает по своей ошибке из state. */
function fakeApi(initial: CleanupStatus = { last: null, next_run: null }) {
  const state = {
    status: initial as CleanupStatus | null,
    statusError: null as Error | null,
    runReport: report(),
    runError: null as Error | null,
  };

  const api: CleanupApi = {
    status: vi.fn(async () => {
      if (state.statusError) throw state.statusError;
      if (state.status === null) throw new Error("нет статуса");
      return state.status;
    }),
    // Как движок: dry_run в отчёте — это флаг запроса, а не состояние fake.
    run: vi.fn(async (dryRun: boolean) => {
      if (state.runError) throw state.runError;
      return { ...state.runReport, dry_run: dryRun };
    }),
  };

  return { api, state };
}

describe("createCleanup: статус", () => {
  it("refresh читает статус и раскладывает его по строкам", async () => {
    const fake = fakeApi(status());
    const cleanup = createCleanup(fake.api);

    await cleanup.refresh();

    expect(fake.api.status).toHaveBeenCalledTimes(1);
    expect(cleanup.loaded.value).toBe(true);
    expect(cleanup.statusError.value).toBeNull();
    expect(cleanup.lastLine.value).toBe(
      "Последняя очистка: 2026-10-30 04:00 (удалено 3, пропущено активных 1, ошибок 0)",
    );
    expect(cleanup.nextLine.value).toBe("Следующая очистка: 2026-11-01 04:00");
  });

  it("до первой загрузки строки — тире, а не выдуманный статус", async () => {
    const cleanup = createCleanup(fakeApi().api);

    expect(cleanup.loaded.value).toBe(false);
    expect(cleanup.lastLine.value).toBe("—");
    expect(cleanup.nextLine.value).toBe("—");
  });

  it("прогона не было — «не выполнялась», job выключен — «по расписанию выключено»", async () => {
    const fake = fakeApi({ last: null, next_run: null });
    const cleanup = createCleanup(fake.api);

    await cleanup.refresh();

    expect(cleanup.lastLine.value).toContain("не выполнялась");
    expect(cleanup.nextLine.value).toContain("по расписанию выключено");
  });

  it("параллельные refresh — один запрос, а не два", async () => {
    const fake = fakeApi(status());
    const gate = deferred<CleanupStatus>();
    vi.mocked(fake.api.status).mockImplementation(() => gate.promise);
    const cleanup = createCleanup(fake.api);

    const first = cleanup.refresh();
    const second = cleanup.refresh();
    expect(fake.api.status).toHaveBeenCalledTimes(1);

    gate.resolve(status());
    await Promise.all([first, second]);
    expect(fake.api.status).toHaveBeenCalledTimes(1);
    expect(cleanup.loaded.value).toBe(true);
  });

  it("ошибка статуса видна, следующий успешный refresh её снимает", async () => {
    const fake = fakeApi(status());
    const cleanup = createCleanup(fake.api);
    fake.state.statusError = new Error("нет соединения с демоном");

    await cleanup.refresh();
    expect(cleanup.statusError.value).toContain("нет соединения");
    expect(cleanup.loaded.value).toBe(false);

    fake.state.statusError = null;
    await cleanup.refresh();
    expect(cleanup.statusError.value).toBeNull();
    expect(cleanup.loaded.value).toBe(true);
  });

  it("API, бросающий синхронно, не залипает в полёте — следующий refresh идёт в сеть", async () => {
    const fake = fakeApi(status());
    let synchronous = true;
    vi.mocked(fake.api.status).mockImplementation(() => {
      if (synchronous) throw new Error("синхронный сбой статуса");
      return Promise.resolve(status());
    });
    const cleanup = createCleanup(fake.api);

    await cleanup.refresh();
    expect(cleanup.statusError.value).toContain("синхронный сбой");

    synchronous = false;
    await cleanup.refresh();
    expect(fake.api.status).toHaveBeenCalledTimes(2);
    expect(cleanup.statusError.value).toBeNull();
  });
});

describe("createCleanup: ручной запуск", () => {
  it("запуск показывает отчёт и перечитывает статус", async () => {
    const fake = fakeApi(status());
    const cleanup = createCleanup(fake.api);
    await cleanup.refresh();

    const ok = await cleanup.run(false);

    expect(ok).toBe(true);
    expect(fake.api.run).toHaveBeenCalledWith(false);
    expect(cleanup.report.value).toEqual(report());
    expect(cleanup.actionError.value).toBeNull();
    expect(cleanup.pending.value).toBeNull();
    expect(fake.api.status, "после запуска статус перечитывается").toHaveBeenCalledTimes(2);
  });

  it("превью: dry_run=true, отчёт остаётся с флагом превью", async () => {
    const fake = fakeApi(status());
    fake.state.runReport = report({ removed: 5, dry_run: true });
    const cleanup = createCleanup(fake.api);

    const ok = await cleanup.run(true);

    expect(ok).toBe(true);
    expect(fake.api.run).toHaveBeenCalledWith(true);
    expect(cleanup.report.value?.dry_run).toBe(true);
    expect(cleanup.report.value?.removed).toBe(5);
  });

  it("второй прогон во время первого не уходит в сеть", async () => {
    const fake = fakeApi(status());
    const gate = deferred<CleanupReport>();
    vi.mocked(fake.api.run).mockImplementation(() => gate.promise);
    const cleanup = createCleanup(fake.api);

    const first = cleanup.run(false);
    const second = cleanup.run(true);

    expect(await second, "второй запуск отклоняется без запроса").toBe(false);
    expect(fake.api.run).toHaveBeenCalledTimes(1);
    expect(cleanup.pending.value).toBe("run");

    gate.resolve(report());
    expect(await first).toBe(true);
    expect(cleanup.pending.value).toBeNull();
  });

  it("ошибка запуска читаема, отчёт прошлого прогона не показывается", async () => {
    const fake = fakeApi(status());
    const cleanup = createCleanup(fake.api);
    fake.state.runError = new Error("HTTP 400: демон отклонил прогон");

    const ok = await cleanup.run(false);

    expect(ok).toBe(false);
    expect(cleanup.actionError.value).toContain("HTTP 400");
    expect(cleanup.report.value).toBeNull();
    expect(cleanup.pending.value).toBeNull();
  });

  it("после неудачи следующий запуск снова уходит в сеть", async () => {
    const fake = fakeApi(status());
    const cleanup = createCleanup(fake.api);
    fake.state.runError = new Error("сбой");
    await cleanup.run(false);

    fake.state.runError = null;
    const ok = await cleanup.run(true);

    expect(ok).toBe(true);
    expect(fake.api.run).toHaveBeenCalledTimes(2);
    expect(cleanup.actionError.value).toBeNull();
    expect(cleanup.report.value?.dry_run).toBe(true);
  });

  it("неудачный запуск не считается обновлением статуса", async () => {
    const fake = fakeApi(status());
    const cleanup = createCleanup(fake.api);
    await cleanup.refresh();
    fake.state.runError = new Error("сбой");

    await cleanup.run(false);

    expect(fake.api.status).toHaveBeenCalledTimes(1);
  });
});
