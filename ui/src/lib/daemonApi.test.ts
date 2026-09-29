import { describe, expect, it } from "vitest";
import { createDaemonApi, type Transport } from "./daemonApi";
import type { Health, StateSnapshot } from "./types";

const HEALTH_BODY = JSON.stringify({
  status: "ok",
  version: 1,
  state: "running",
  supervisor_alive: true,
  workers_total: 2,
  workers_alive: 1,
  workers_failed: 1,
  circuits_open: ["br-2"],
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
      restart_count: 3,
      started_at: 1_000_000,
      heartbeat_at: 1_000_005,
      last_error: null,
    },
  ],
  worker_count: 1,
  alive_count: 1,
  updated_at: 1_000_010,
});

function transportOf(
  replies: Record<string, { status: number; body: string }>,
): { transport: Transport; calls: { path: string; method: string }[] } {
  const calls: { path: string; method: string }[] = [];
  const transport: Transport = async (request) => {
    calls.push({ path: request.path, method: request.method });
    const reply = replies[request.path];
    if (!reply) {
      throw new Error(`нет заглушки для ${request.path}`);
    }
    return reply;
  };
  return { transport, calls };
}

describe("createDaemonApi: health", () => {
  it("GET /health отдаёт разобранный payload", async () => {
    const { transport, calls } = transportOf({
      "/health": { status: 200, body: HEALTH_BODY },
    });
    const api = createDaemonApi(transport);

    const payload: Health = await api.health();

    expect(calls).toEqual([{ path: "/health", method: "GET" }]);
    expect(payload.workers_total).toBe(2);
    expect(payload.workers_alive).toBe(1);
    expect(payload.circuits_open).toEqual(["br-2"]);
  });

  it("не-200 ответ — ошибка с текстом демона", async () => {
    const { transport } = transportOf({
      "/health": {
        status: 401,
        body: JSON.stringify({
          error: { code: "unauthorized", message: "неверный токен" },
        }),
      },
    });
    const api = createDaemonApi(transport);

    await expect(api.health()).rejects.toThrow("неверный токен");
  });

  it("битый JSON — читаемая ошибка, а не исключение парсера", async () => {
    const { transport } = transportOf({
      "/health": { status: 200, body: "<html>oops</html>" },
    });
    const api = createDaemonApi(transport);

    await expect(api.health()).rejects.toThrow(/JSON/i);
  });

  it("отклонённый транспорт (нет соединения) доходит до вызывающего", async () => {
    const transport: Transport = async () => {
      throw new Error("не удалось подключиться к демону на 127.0.0.1:8787");
    };
    const api = createDaemonApi(transport);

    await expect(api.health()).rejects.toThrow("не удалось подключиться");
  });
});

describe("createDaemonApi: state", () => {
  it("GET /state отдаёт снимок воркеров", async () => {
    const { transport, calls } = transportOf({
      "/state": { status: 200, body: STATE_BODY },
    });
    const api = createDaemonApi(transport);

    const payload: StateSnapshot = await api.state();

    expect(calls).toEqual([{ path: "/state", method: "GET" }]);
    expect(payload.workers[0].browser_id).toBe("br-1");
    expect(payload.workers[0].pid).toBe(4242);
    expect(payload.workers[0].restart_count).toBe(3);
    expect(payload.alive_count).toBe(1);
  });
});

describe("createDaemonApi: control", () => {
  it("каждая команда уходит POST-ом на свой endpoint", async () => {
    const { transport, calls } = transportOf({
      "/control/start": { status: 200, body: '{"state":"running"}' },
      "/control/pause": { status: 200, body: '{"state":"paused"}' },
      "/control/resume": { status: 200, body: '{"state":"running"}' },
      "/control/stop": { status: 200, body: '{"state":"stopped"}' },
      "/control/restart": { status: 200, body: '{"state":"running"}' },
    });
    const api = createDaemonApi(transport);

    for (const action of ["start", "pause", "resume", "kill", "restart"] as const) {
      await api.control(action);
    }

    expect(calls).toEqual([
      { path: "/control/start", method: "POST" },
      { path: "/control/pause", method: "POST" },
      { path: "/control/resume", method: "POST" },
      { path: "/control/stop", method: "POST" },
      { path: "/control/restart", method: "POST" },
    ]);
  });

  it("409 от демона — ошибка с его сообщением (AlreadyRunning)", async () => {
    const { transport } = transportOf({
      "/control/start": {
        status: 409,
        body: JSON.stringify({
          error: { code: "already_running", message: "воркеры уже запущены" },
        }),
      },
    });
    const api = createDaemonApi(transport);

    await expect(api.control("start")).rejects.toThrow("воркеры уже запущены");
  });

  it("успешный ответ контрола возвращает тело", async () => {
    const { transport } = transportOf({
      "/control/stop": { status: 200, body: '{"state":"stopped"}' },
    });
    const api = createDaemonApi(transport);

    await expect(api.control("kill")).resolves.toBe('{"state":"stopped"}');
  });
});
