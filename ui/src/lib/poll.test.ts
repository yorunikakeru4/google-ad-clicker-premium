import { describe, expect, it } from "vitest";
import {
  createPollState,
  OFFLINE_AFTER_FAILURES,
  reducePoll,
  type PollState,
} from "./poll";
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
    expect(state.inflight).toBe(false);
    expect(state.seq).toBe(0);
    expect(state.lastOkAt).toBeNull();
    expect(state.lastError).toBeNull();
    expect(state.failures).toBe(0);
    expect(state.health).toBeNull();
    expect(state.snapshot).toBeNull();
  });
});

describe("reducePoll: begin", () => {
  it("первый begin поднимает inflight, не трогая фазу", () => {
    const state = reducePoll(createPollState(), { kind: "begin" });

    expect(state.inflight).toBe(true);
    expect(state.phase).toBe("idle");
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

    expect(next.inflight).toBe(true);
    expect(next.phase).toBe("online");
    expect(next.seq).toBe(2);
  });

  it("тик в полёте не меняет фазу: баннер и чип не моргают на каждом опросе", () => {
    // Регрессия: раньше «inflight» был фазой, и каждый успешный опрос на
    // время запроса выглядел как «нет связи» — баннер мигал раз в секунду.
    const online = reducePoll(reducePoll(createPollState(), { kind: "begin" }), {
      kind: "success",
      seq: 1,
      health: health(),
      snapshot: snapshot(),
      at: 10,
    });

    const begun = reducePoll(online, { kind: "begin" });
    expect(begun.inflight).toBe(true);
    expect(begun.phase).toBe("online");

    // И в офлайне повторная попытка не гасит баннер на время запроса.
    let offline = online;
    for (let seq = 2; seq <= 1 + OFFLINE_AFTER_FAILURES; seq += 1) {
      offline = reducePoll(reducePoll(offline, { kind: "begin" }), {
        kind: "failure",
        seq,
        error: "down",
        at: seq * 10,
      });
    }
    expect(offline.phase).toBe("offline");

    const retry = reducePoll(offline, { kind: "begin" });
    expect(retry.inflight).toBe(true);
    expect(retry.phase).toBe("offline");
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
    expect(late.inflight).toBe(true);
    expect(late.phase).toBe("online");
    expect(late.health?.workers_total).toBe(2);
  });
});

describe("reducePoll: failure", () => {
  /** Тик с ошибкой поверх уже онлайн-состояния. */
  function failOneTick(from: PollState, seq: number, error = "connection refused") {
    return reducePoll(reducePoll(from, { kind: "begin" }), {
      kind: "failure",
      seq,
      error,
      at: seq * 10,
    });
  }

  const onlineOnce = () =>
    reducePoll(reducePoll(createPollState(), { kind: "begin" }), {
      kind: "success",
      seq: 1,
      health: health(),
      snapshot: snapshot(),
      at: 10,
    });

  it("единичная ошибка не объявляет офлайн: баннер не моргает", () => {
    const failed = failOneTick(onlineOnce(), 2);

    expect(failed.phase).toBe("online");
    expect(failed.inflight).toBe(false);
    expect(failed.failures).toBe(1);
    // текст ошибки уже записан — он появится в баннере, если сбой продлится
    expect(failed.lastError).toBe("connection refused");
    // данные остаются: показывать их «живыми» до порога — осознанный выбор
    expect(failed.health?.workers_alive).toBe(2);
    expect(failed.snapshot?.workers).toHaveLength(1);
    expect(failed.lastOkAt).toBe(10);
  });

  it(`${OFFLINE_AFTER_FAILURES} неудачи подряд переводят в offline и чистят данные`, () => {
    let state = onlineOnce();
    state = failOneTick(state, 2, "boom 1");
    state = failOneTick(state, 3, "boom 2");
    expect(state.phase).toBe("online");

    state = failOneTick(state, 4, "boom 3");

    expect(state.phase).toBe("offline");
    expect(state.failures).toBe(OFFLINE_AFTER_FAILURES);
    expect(state.lastError).toBe("boom 3");
    expect(state.health).toBeNull();
    expect(state.snapshot).toBeNull();
    // время последнего ответа сохраняется — по нему живёт индикатор heartbeat
    expect(state.lastOkAt).toBe(10);
  });

  it("успех между сбоями сбрасывает счётчик", () => {
    let state = failOneTick(onlineOnce(), 2, "boom 1");
    state = failOneTick(state, 3, "boom 2");
    state = reducePoll(reducePoll(state, { kind: "begin" }), {
      kind: "success",
      seq: 4,
      health: health(),
      snapshot: snapshot(),
      at: 40,
    });

    expect(state.failures).toBe(0);
    expect(state.lastError).toBeNull();

    // после восстановления первый новый сбой снова не показывает баннер
    state = failOneTick(state, 5, "boom again");
    expect(state.phase).toBe("online");
    expect(state.failures).toBe(1);
  });

  it("первый тик без успешных ответов не рисует офлайн", () => {
    // До первого ответа lastOkAt === null, и баннер скрыт в любом случае;
    // фаза остаётся idle, чтобы «нет связи» не появлялось раньше первого
    // же измерения.
    const failed = failOneTick(createPollState(), 1);

    expect(failed.phase).toBe("idle");
    expect(failed.inflight).toBe(false);
    expect(failed.lastOkAt).toBeNull();
    expect(failed.failures).toBe(1);
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
    expect(lateFailure.inflight).toBe(true);
    expect(lateFailure.phase).toBe("idle");
  });

  it("после ошибки опрос продолжается", () => {
    const failed = failOneTick(createPollState(), 1, "boom");
    const next = reducePoll(failed, { kind: "begin" });

    expect(next.inflight).toBe(true);
    expect(next.seq).toBe(2);
  });
});
