import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createDaemonStatus } from "./useDaemonStatus";
import type { DaemonApi } from "../lib/daemonApi";
import type { Health, StateSnapshot } from "../lib/types";

const HEALTH_BODY = JSON.stringify({
  status: "ok",
  version: 1,
  state: "running",
  supervisor_alive: true,
  workers_total: 2,
  workers_alive: 2,
  workers_failed: 0,
  circuits_open: [],
  paused: false,
});

const STATE_BODY = JSON.stringify({
  state: "running",
  paused: false,
  workers: [
    {
      browser_id: "br-1",
      pid: 4242,
      status: "running",
      restart_count: 1,
      started_at: 1_000_000,
      heartbeat_at: 1_000_005,
      last_error: null,
    },
  ],
  worker_count: 1,
  alive_count: 1,
  updated_at: 1_000_010,
});

interface PendingCall {
  path: string;
  resolve: (payload: unknown) => void;
  reject: (error: Error) => void;
}

/** Управляемый фейк API: запросы висят, пока тест сам их не разрешит. */
function fakeApi() {
  const pending: PendingCall[] = [];
  const calls: string[] = [];
  const api: DaemonApi = {
    health: () => {
      calls.push("/health");
      return new Promise((resolve, reject) =>
        pending.push({
          path: "/health",
          resolve: (payload) => resolve(payload as Health),
          reject,
        }),
      );
    },
    state: () => {
      calls.push("/state");
      return new Promise((resolve, reject) =>
        pending.push({
          path: "/state",
          resolve: (payload) => resolve(payload as StateSnapshot),
          reject,
        }),
      );
    },
    control: vi.fn(async () => '{"state":"running"}'),
  };

  return {
    api,
    calls,
    get pending() {
      return pending;
    },
    /** Завершает все висящие запросы: /health → healthBody, /state → stateBody. */
    settle(
      healthBody: string = HEALTH_BODY,
      stateBody: string = STATE_BODY,
    ) {
      for (const call of pending.splice(0)) {
        call.resolve(JSON.parse(call.path === "/health" ? healthBody : stateBody));
      }
    },
    failAll(message: string) {
      for (const call of pending.splice(0)) call.reject(new Error(message));
    },
    failPath(path: string, message: string) {
      for (let i = pending.length - 1; i >= 0; i--) {
        if (pending[i].path === path) {
          pending[i].reject(new Error(message));
          pending.splice(i, 1);
        }
      }
    },
  };
}

describe("createDaemonStatus: тик опроса", () => {
  it("успешный тик переводит в online и отдаёт воркеров", async () => {
    const fake = fakeApi();
    const status = createDaemonStatus({ api: fake.api });

    const tick = status.tick();
    fake.settle();
    await tick;

    expect(status.state.value.phase).toBe("online");
    expect(status.state.value.health?.workers_alive).toBe(2);
    expect(status.state.value.snapshot?.workers[0].browser_id).toBe("br-1");
    expect(status.view.value.workersTotal).toBe(2);
    expect(status.state.value.lastOkAt).not.toBeNull();

    status.stop();
  });

  it("ошибка тика переводит в offline с читаемым текстом", async () => {
    const fake = fakeApi();
    const status = createDaemonStatus({ api: fake.api });

    const tick = status.tick();
    fake.failAll("не удалось подключиться к демону на 127.0.0.1:8787");
    await tick;

    expect(status.state.value.phase).toBe("offline");
    expect(status.state.value.lastError).toContain("не удалось подключиться");
    expect(status.state.value.health).toBeNull();
    expect(status.view.value.online).toBe(false);

    status.stop();
  });

  it("частичный ответ (жив health, упал state) — всё равно offline", async () => {
    const fake = fakeApi();
    const status = createDaemonStatus({ api: fake.api });

    const tick = status.tick();
    fake.failPath("/state", "таймаут ответа демона");
    fake.settle();
    await tick;

    expect(status.state.value.phase).toBe("offline");
    expect(status.state.value.health).toBeNull();

    status.stop();
  });

  it("параллельный тик не дублирует запросы", async () => {
    const fake = fakeApi();
    const status = createDaemonStatus({ api: fake.api });

    const first = status.tick();
    const second = status.tick();
    await second;

    // второй тик не должен ничего запросить, пока первый в полёте
    expect(fake.calls).toEqual(["/health", "/state"]);

    fake.settle();
    await first;

    expect(fake.calls).toEqual(["/health", "/state"]);

    // после завершения новый тик снова ходит в демону
    const third = status.tick();
    fake.settle();
    await third;
    expect(fake.calls).toEqual(["/health", "/state", "/health", "/state"]);

    status.stop();
  });

  it("восстановление после ошибки возвращает online", async () => {
    const fake = fakeApi();
    const status = createDaemonStatus({ api: fake.api });

    const failing = status.tick();
    fake.failAll("connection refused");
    await failing;
    expect(status.state.value.phase).toBe("offline");

    const recovering = status.tick();
    fake.settle();
    await recovering;

    expect(status.state.value.phase).toBe("online");
    expect(status.state.value.lastError).toBeNull();

    status.stop();
  });
});

describe("createDaemonStatus: интервал", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("startPolling дёргает тик раз в секунду", async () => {
    const fake = fakeApi();
    const status = createDaemonStatus({ api: fake.api });

    status.startPolling();
    expect(fake.calls).toEqual(["/health", "/state"]);

    fake.settle();
    await vi.advanceTimersByTimeAsync(1000);
    expect(fake.calls).toEqual(["/health", "/state", "/health", "/state"]);

    fake.settle();
    await vi.advanceTimersByTimeAsync(1000);
    expect(fake.calls).toHaveLength(6);

    status.stop();
    fake.settle();
    await vi.advanceTimersByTimeAsync(5000);
    expect(fake.calls).toHaveLength(6);
  });

  it("startPolling повторно не плодит интервалы", async () => {
    const fake = fakeApi();
    const status = createDaemonStatus({ api: fake.api });

    status.startPolling();
    status.startPolling();
    fake.settle();
    await vi.advanceTimersByTimeAsync(1000);

    // один интервал: первый тик + ровно один по таймеру
    expect(fake.calls).toHaveLength(4);

    status.stop();
  });

  it("незавершённый тик не плодит параллельные запросы по таймеру", async () => {
    const fake = fakeApi();
    const status = createDaemonStatus({ api: fake.api });

    status.startPolling();
    // тела не приходят: тики остаются в полёте
    await vi.advanceTimersByTimeAsync(3000);

    expect(fake.calls).toEqual(["/health", "/state"]);

    status.stop();
  });
});

describe("createDaemonStatus: control", () => {
  it("успешная команда чистит прошлую ошибку и обновляет состояние", async () => {
    const fake = fakeApi();
    const status = createDaemonStatus({ api: fake.api });
    status.controlError.value = "старая ошибка";

    await status.send("start");

    expect(fake.api.control).toHaveBeenCalledWith("start");
    expect(status.controlError.value).toBeNull();
    // состояние после команды свежее: тик ушёл сразу, не дожидаясь интервала
    expect(fake.calls).toEqual(["/health", "/state"]);

    status.stop();
  });

  it("ошибка демона видна пользователю и не роняет опрос", async () => {
    const fake = fakeApi();
    const status = createDaemonStatus({ api: fake.api });
    vi.mocked(fake.api.control).mockRejectedValueOnce(
      new Error("воркеры уже запущены"),
    );

    const ok = await status.send("start");

    expect(ok).toBe(false);
    expect(status.controlError.value).toBe("воркеры уже запущены");

    status.stop();
  });

  it("busy блокирует кнопки на время команды", async () => {
    const fake = fakeApi();
    const status = createDaemonStatus({ api: fake.api });
    let release: () => void = () => {};
    vi.mocked(fake.api.control).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = () => resolve("{}");
        }),
    );

    const sending = status.send("start");
    expect(status.busy.value).toBe(true);
    expect(status.view.value.busy).toBe(true);

    release();
    await sending;
    expect(status.busy.value).toBe(false);

    status.stop();
  });
});
