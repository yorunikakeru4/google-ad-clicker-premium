import { describe, expect, it } from "vitest";
import {
  apiErrorMessage,
  controlRequest,
  disabledReason,
  errorMessage,
  proxiesRequest,
  toStatusView,
  type StatusView,
} from "./control";

const offline: StatusView = {
  online: false,
  busy: false,
  state: null,
  paused: false,
  workersTotal: 0,
  workersAlive: 0,
};

const idle: StatusView = {
  online: true,
  busy: false,
  state: "stopped",
  paused: false,
  workersTotal: 0,
  workersAlive: 0,
};

const running: StatusView = {
  online: true,
  busy: false,
  state: "running",
  paused: false,
  workersTotal: 3,
  workersAlive: 3,
};

const paused: StatusView = {
  online: true,
  busy: false,
  state: "paused",
  paused: true,
  workersTotal: 2,
  workersAlive: 2,
};

const stopping: StatusView = {
  online: true,
  busy: false,
  state: "stopping",
  paused: false,
  workersTotal: 1,
  workersAlive: 1,
};

// Пул пуст, а в БД остался флаг running (демон перезапускался).
const staleRunning: StatusView = {
  online: true,
  busy: false,
  state: "running",
  paused: false,
  workersTotal: 0,
  workersAlive: 0,
};

const ALL = ["start", "pause", "resume", "kill", "restart"] as const;

describe("controlRequest", () => {
  it("каждая команда уходит на свой endpoint демона", () => {
    expect(controlRequest("start")).toEqual({
      method: "POST",
      path: "/control/start",
    });
    expect(controlRequest("pause")).toEqual({
      method: "POST",
      path: "/control/pause",
    });
    expect(controlRequest("resume")).toEqual({
      method: "POST",
      path: "/control/resume",
    });
    // Kill в UI — это POST /control/stop в API демона.
    expect(controlRequest("kill")).toEqual({
      method: "POST",
      path: "/control/stop",
    });
    expect(controlRequest("restart")).toEqual({
      method: "POST",
      path: "/control/restart",
    });
  });
});

describe("proxiesRequest", () => {
  it("каждое действие прокси уходит на свой endpoint с телом контракта", () => {
    expect(proxiesRequest({ kind: "list" })).toEqual({
      method: "GET",
      path: "/control/proxies",
    });
    expect(
      proxiesRequest({ kind: "add", lines: ["http://a.example:8080", "b:3128"] }),
    ).toEqual({
      method: "POST",
      path: "/control/proxies",
      body: JSON.stringify({ lines: ["http://a.example:8080", "b:3128"] }),
    });
    expect(proxiesRequest({ kind: "import" })).toEqual({
      method: "POST",
      path: "/control/proxies/import",
      body: "{}",
    });
    expect(proxiesRequest({ kind: "delete", id: 7 })).toEqual({
      method: "POST",
      path: "/control/proxies/delete",
      body: '{"id":7}',
    });
    expect(proxiesRequest({ kind: "check" })).toEqual({
      method: "POST",
      path: "/control/proxies/check",
      body: "{}",
    });
  });

  it("тело собирается из payload, а не из глобального состояния", () => {
    const first = proxiesRequest({ kind: "add", lines: ["a:1"] });
    const second = proxiesRequest({ kind: "delete", id: 42 });

    expect(first.body).toBe(JSON.stringify({ lines: ["a:1"] }));
    expect(second.body).toBe('{"id":42}');
    expect(JSON.parse(second.body ?? "{}")).toEqual({ id: 42 });
  });
});

describe("disabledReason: демон недоступен", () => {
  it("оффлайн блокирует все команды", () => {
    for (const action of ALL) {
      expect(disabledReason(action, offline)).toMatch(/недоступен/);
    }
  });
});

describe("disabledReason: команда уже в полёте", () => {
  it("busy блокирует все команды", () => {
    const busy: StatusView = { ...running, busy: true };
    for (const action of ALL) {
      expect(disabledReason(action, busy)).toBeTruthy();
    }
  });
});

describe("disabledReason: пул остановлен", () => {
  it("start доступен, остальные нет", () => {
    expect(disabledReason("start", idle)).toBeNull();
    expect(disabledReason("pause", idle)).toMatch(/нет запущенных/);
    expect(disabledReason("resume", idle)).toMatch(/пауза не установлена/);
    expect(disabledReason("kill", idle)).toMatch(/остановлены/);
    expect(disabledReason("restart", idle)).toMatch(/нечего перезапускать/);
  });
});

describe("disabledReason: пул работает", () => {
  it("start блокирован (AlreadyRunning), pause/kill/restart доступны", () => {
    expect(disabledReason("start", running)).toMatch(/уже запущены/);
    expect(disabledReason("pause", running)).toBeNull();
    expect(disabledReason("resume", running)).toMatch(/пауза не установлена/);
    expect(disabledReason("kill", running)).toBeNull();
    expect(disabledReason("restart", running)).toBeNull();
  });
});

describe("disabledReason: пауза", () => {
  it("resume доступен, pause — уже на паузе", () => {
    expect(disabledReason("resume", paused)).toBeNull();
    expect(disabledReason("pause", paused)).toMatch(/паузе/);
    expect(disabledReason("start", paused)).toMatch(/уже запущены/);
    expect(disabledReason("kill", paused)).toBeNull();
    expect(disabledReason("restart", paused)).toBeNull();
  });
});

describe("disabledReason: остановка в полёте", () => {
  it("stopping закрывает все команды", () => {
    for (const action of ALL) {
      expect(disabledReason(action, stopping)).toBeTruthy();
    }
  });
});

describe("disabledReason: флаг running при пустом пуле", () => {
  it("start и kill доступны (снимают флаг), restart — нет", () => {
    expect(disabledReason("start", staleRunning)).toBeNull();
    expect(disabledReason("kill", staleRunning)).toBeNull();
    expect(disabledReason("restart", staleRunning)).toMatch(
      /нечего перезапускать/,
    );
    expect(disabledReason("pause", staleRunning)).toMatch(
      /нет запущенных/,
    );
  });
});

describe("toStatusView", () => {
  it("собирает вид из /health и /state", () => {
    const view = toStatusView({
      online: true,
      busy: false,
      health: {
        status: "ok",
        version: 1,
        state: "running",
        supervisor_alive: true,
        workers_total: 3,
        workers_alive: 2,
        workers_failed: 1,
        circuits_open: ["br-3"],
        paused: false,
      },
      state: {
        state: "running",
        paused: false,
        workers: [],
        worker_count: 3,
        alive_count: 2,
        updated_at: 1_000_000,
      },
    });

    expect(view).toEqual({
      online: true,
      busy: false,
      state: "running",
      paused: false,
      workersTotal: 3,
      workersAlive: 2,
    });
  });

  it("оффлайн без данных — всё по нулям", () => {
    const view = toStatusView({
      online: false,
      busy: false,
      health: null,
      state: null,
    });

    expect(view.online).toBe(false);
    expect(view.state).toBeNull();
    expect(view.workersTotal).toBe(0);
  });
});

describe("apiErrorMessage", () => {
  it("берёт сообщение демона из JSON-ошибки", () => {
    const body = JSON.stringify({
      error: { code: "already_running", message: "воркеры уже запущены" },
    });
    expect(apiErrorMessage(409, body)).toBe("воркеры уже запущены");
  });

  it("не-JSON тело — код и начало тела", () => {
    const message = apiErrorMessage(502, "<html>bad gateway</html>");
    expect(message).toContain("502");
    expect(message).toContain("bad gateway");
  });

  it("пустое тело — только HTTP-код", () => {
    expect(apiErrorMessage(500, "")).toBe("HTTP 500");
  });
});

describe("errorMessage", () => {
  it("строковая ошибка (Err из Rust) проходит как есть", () => {
    expect(errorMessage("ADCLICKER_CONTROL_TOKEN не задана")).toBe(
      "ADCLICKER_CONTROL_TOKEN не задана",
    );
  });

  it("Error — по message", () => {
    expect(errorMessage(new Error("connection refused"))).toBe(
      "connection refused",
    );
  });

  it("прочие значения не роняют форму", () => {
    expect(errorMessage(42)).toBe("42");
  });
});
