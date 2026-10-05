import { describe, expect, it } from "vitest";
import {
  apiErrorMessage,
  cleanupRequest,
  configRequest,
  controlRequest,
  diagnosticsRequest,
  disabledReason,
  errorMessage,
  offlineBannerText,
  profilesRequest,
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

describe("configRequest", () => {
  it("чтение конфига — GET на /control/config без тела", () => {
    const request = configRequest();
    expect(request).toEqual({ method: "GET", path: "/control/config" });
    expect(request.body).toBeUndefined();
  });
});

describe("cleanupRequest", () => {
  it("ручной запуск — POST /control/cleanup/run с пустым объектом в теле", () => {
    expect(cleanupRequest({ kind: "run", dryRun: false })).toEqual({
      method: "POST",
      path: "/control/cleanup/run",
      body: "{}",
    });
  });

  it("dry_run — тот же endpoint с явным флагом в теле", () => {
    const request = cleanupRequest({ kind: "run", dryRun: true });
    expect(request).toEqual({
      method: "POST",
      path: "/control/cleanup/run",
      body: '{"dry_run":true}',
    });
    expect(JSON.parse(request.body ?? "")).toEqual({ dry_run: true });
  });

  it("статус — GET /control/cleanup/status без тела", () => {
    const request = cleanupRequest({ kind: "status" });
    expect(request).toEqual({ method: "GET", path: "/control/cleanup/status" });
    expect(request.body).toBeUndefined();
  });

  it("пути состоят из строчных сегментов — проходят allowlist control.rs", () => {
    for (const request of [
      cleanupRequest({ kind: "run", dryRun: false }),
      cleanupRequest({ kind: "run", dryRun: true }),
      cleanupRequest({ kind: "status" }),
    ]) {
      const tail = request.path.replace(/^\/control\//, "");
      expect(tail).not.toBe("");
      for (const segment of tail.split("/")) {
        expect(segment, request.path).toMatch(/^[a-z]+$/);
      }
    }
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
    expect(
      proxiesRequest({ kind: "deleteMany", ids: [3, 7, 9] }),
    ).toEqual({
      method: "POST",
      path: "/control/proxies/delete",
      body: '{"ids":[3,7,9]}',
    });
    expect(proxiesRequest({ kind: "deleteAll" })).toEqual({
      method: "POST",
      path: "/control/proxies/delete",
      body: '{"all":true}',
    });
    // Путь отдаёт демон: резолвится он от его каталога, у UI своего нет.
    expect(proxiesRequest({ kind: "file" })).toEqual({
      method: "GET",
      path: "/control/proxies/file",
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

describe("profilesRequest", () => {
  it("каждое действие профиля уходит на свой endpoint с телом контракта", () => {
    expect(profilesRequest({ kind: "list" })).toEqual({
      method: "GET",
      path: "/control/profiles",
    });
    expect(
      profilesRequest({
        kind: "add",
        profiles: [{ name: "alpha", key_ref: "k-1" }],
      }),
    ).toEqual({
      method: "POST",
      path: "/control/profiles",
      body: JSON.stringify({ profiles: [{ name: "alpha", key_ref: "k-1" }] }),
    });
    expect(profilesRequest({ kind: "import", lines: ["k-1", "k-2"] })).toEqual({
      method: "POST",
      path: "/control/profiles/import",
      body: JSON.stringify({ lines: ["k-1", "k-2"] }),
    });
    expect(profilesRequest({ kind: "delete", id: 7 })).toEqual({
      method: "POST",
      path: "/control/profiles/delete",
      body: '{"id":7}',
    });
    expect(profilesRequest({ kind: "assign", start_id: 1, end_id: 10 })).toEqual({
      method: "POST",
      path: "/control/profiles/assign",
      body: '{"start_id":1,"end_id":10}',
    });
    expect(profilesRequest({ kind: "unassign" })).toEqual({
      method: "POST",
      path: "/control/profiles/unassign",
      body: "{}",
    });
    expect(profilesRequest({ kind: "status", id: 3, status: "blocked" })).toEqual({
      method: "POST",
      path: "/control/profiles/status",
      body: '{"id":3,"status":"blocked"}',
    });
  });

  it("тело собирается из payload, а не из глобального состояния", () => {
    const first = profilesRequest({ kind: "add", profiles: [{ name: "a" }] });
    const second = profilesRequest({ kind: "assign", start_id: 5, end_id: 9 });

    expect(first.body).toBe(JSON.stringify({ profiles: [{ name: "a" }] }));
    expect(second.body).toBe('{"start_id":5,"end_id":9}');
    expect(JSON.parse(second.body ?? "{}")).toEqual({ start_id: 5, end_id: 9 });
  });

  it("GET-список не несёт тела", () => {
    expect(profilesRequest({ kind: "list" }).body).toBeUndefined();
  });
});

describe("diagnosticsRequest", () => {
  it("сбор для одного воркера — POST с browser_id по контракту", () => {
    expect(diagnosticsRequest({ kind: "collect", browserId: "br-1" })).toEqual({
      method: "POST",
      path: "/control/diagnostics/collect",
      body: JSON.stringify({ browser_id: "br-1" }),
    });
  });

  it("сбор для всех — тот же endpoint с флагом all", () => {
    expect(diagnosticsRequest({ kind: "collectAll" })).toEqual({
      method: "POST",
      path: "/control/diagnostics/collect",
      body: '{"all":true}',
    });
  });

  it("тело собирается из action, а не из глобального состояния", () => {
    const one = diagnosticsRequest({ kind: "collect", browserId: "br-9" });
    const all = diagnosticsRequest({ kind: "collectAll" });

    expect(JSON.parse(one.body)).toEqual({ browser_id: "br-9" });
    expect(JSON.parse(all.body)).toEqual({ all: true });
    expect(JSON.parse(one.body)).not.toHaveProperty("all");
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

  it("401 объясняет, что на порту демон с другим токеном", () => {
    const body = JSON.stringify({
      error: { code: "unauthorized", message: "неверный токен" },
    });

    const message = apiErrorMessage(401, body);

    expect(message).toContain("неверный токен");
    expect(message).toContain("другим ADCLICKER_CONTROL_TOKEN");
    expect(message).toContain("lsof -nP -iTCP:8787");
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

describe("offlineBannerText", () => {
  it("причина супервизора важнее симптома опроса", () => {
    const text = offlineBannerText(
      "на http://127.0.0.1:8787 уже работает демон с другим токеном",
      "неверный токен",
    );

    expect(text).toContain("уже работает демон с другим токеном");
    expect(text).not.toContain("неверный токен");
    expect(text).toContain("Данные на экранах могут быть устаревшими");
  });

  it("без причины супервизора показывается текст ошибки опроса", () => {
    expect(offlineBannerText(null, "нет соединения с демоном на 127.0.0.1:8787")).toContain(
      "Нет связи с демоном: нет соединения с демоном на 127.0.0.1:8787",
    );
  });

  it("без обеих причин остаётся базовый текст", () => {
    expect(offlineBannerText(null, null)).toBe(
      "Нет связи с демоном. Данные на экранах могут быть устаревшими.",
    );
  });

  it("точка в конце причины не складывается в двойную", () => {
    const text = offlineBannerText("не найден config.json.", null);

    expect(text).toBe(
      "Нет связи с демоном: не найден config.json. Данные на экранах могут быть устаревшими.",
    );
    expect(text).not.toContain("..");
  });
});
