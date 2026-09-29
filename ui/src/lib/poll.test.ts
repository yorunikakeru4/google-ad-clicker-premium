import { describe, expect, it } from "vitest";
import { createPollState, reducePoll, type PollState } from "./poll";
import type { Health, StateSnapshot } from "./types";

function health(overrides: Partial<Health> = {}): Health {
  return {
    status: "ok",
    version: 1,
    state: "running",
    supervisor_alive: true,
    workers_total: 2,
    workers_alive: 2,
    workers_failed: 0,
    circuits_open: [],
    paused: false,
    ...overrides,
  };
}

function snapshot(overrides: Partial<StateSnapshot> = {}): StateSnapshot {
  return {
    state: "running",
    paused: false,
    workers: [
      {
        browser_id: "br-1",
        pid: 101,
        status: "running",
        restart_count: 0,
        started_at: 1_000_000,
        heartbeat_at: 1_000_005,
        last_error: null,
      },
    ],
    worker_count: 1,
    alive_count: 1,
    updated_at: 1_000_010,
    ...overrides,
  };
}

describe("createPollState", () => {
  it("стартует в idle без данных", () => {
    const state = createPollState();

    expect(state.phase).toBe("idle");
    expect(state.seq).toBe(0);
    expect(state.lastOkAt).toBeNull();
    expect(state.lastError).toBeNull();
    expect(state.health).toBeNull();
    expect(state.snapshot).toBeNull();
  });
});

describe("reducePoll: begin", () => {
  it("первый begin переводит в inflight и нумерует тик", () => {
    const state = reducePoll(createPollState(), { kind: "begin" });

    expect(state.phase).toBe("inflight");
    expect(state.seq).toBe(1);
  });

  it("повторный begin не запускает дублирующийся тик", () => {
    const first = reducePoll(createPollState(), { kind: "begin" });
    const second = reducePoll(first, { kind: "begin" });

    expect(second).toBe(first);
    expect(second.seq).toBe(1);
  });

  it("после завершения тика следующий begin получает новый seq", () => {
    const started = reducePoll(createPollState(), { kind: "begin" });
    const done = reducePoll(started, {
      kind: "success",
      seq: 1,
      health: health(),
      snapshot: snapshot(),
      at: 10,
    });

    const next = reducePoll(done, { kind: "begin" });

    expect(next.phase).toBe("inflight");
    expect(next.seq).toBe(2);
  });
});

describe("reducePoll: success", () => {
  it("успех переводит в online и сохраняет ответы", () => {
    const started = reducePoll(createPollState(), { kind: "begin" });
    const state = reducePoll(started, {
      kind: "success",
      seq: 1,
      health: health({ workers_alive: 2 }),
      snapshot: snapshot(),
      at: 1234,
    });

    expect(state.phase).toBe("online");
    expect(state.lastOkAt).toBe(1234);
    expect(state.lastError).toBeNull();
    expect(state.health?.workers_alive).toBe(2);
    expect(state.snapshot?.workers).toHaveLength(1);
  });

  it("успех с чужим seq (опоздавший ответ) игнорируется", () => {
    const stale: PollState = reducePoll(
      reducePoll(createPollState(), { kind: "begin" }),
      {
        kind: "success",
        seq: 1,
        health: health(),
        snapshot: snapshot(),
        at: 10,
      },
    );
    const started = reducePoll(stale, { kind: "begin" });
    const late = reducePoll(started, {
      kind: "success",
      seq: 1,
      health: health({ workers_total: 99 }),
      snapshot: snapshot(),
      at: 20,
    });

    expect(late).toBe(started);
    expect(late.phase).toBe("inflight");
    expect(late.health?.workers_total).toBe(2);
  });
});

describe("reducePoll: failure", () => {
  it("ошибка переводит в offline и чистит данные", () => {
    const online = reducePoll(
      reducePoll(createPollState(), { kind: "begin" }),
      {
        kind: "success",
        seq: 1,
        health: health(),
        snapshot: snapshot(),
        at: 10,
      },
    );
    const started = reducePoll(online, { kind: "begin" });
    const offline = reducePoll(started, {
      kind: "failure",
      seq: 2,
      error: "connection refused",
      at: 20,
    });

    expect(offline.phase).toBe("offline");
    expect(offline.lastError).toBe("connection refused");
    expect(offline.health).toBeNull();
    expect(offline.snapshot).toBeNull();
    // время последнего ответа сохраняется — по нему живёт индикатор heartbeat
    expect(offline.lastOkAt).toBe(10);
  });

  it("опоздавшая ошибка с чужим seq не трогает текущий тик", () => {
    const started = reducePoll(createPollState(), { kind: "begin" });
    const lateFailure = reducePoll(started, {
      kind: "failure",
      seq: 7,
      error: "stale",
      at: 20,
    });

    expect(lateFailure).toBe(started);
    expect(lateFailure.phase).toBe("inflight");
  });

  it("после ошибки опрос продолжается", () => {
    const offline = reducePoll(
      reducePoll(createPollState(), { kind: "begin" }),
      { kind: "failure", seq: 1, error: "boom", at: 10 },
    );
    const next = reducePoll(offline, { kind: "begin" });

    expect(next.phase).toBe("inflight");
    expect(next.seq).toBe(2);
  });
});
