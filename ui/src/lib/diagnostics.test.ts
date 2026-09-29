// Контракт экрана Diagnostics: разбор снимка (headers/suspicion_flags — сырой
// JSON из Rust-читалки), карточки воркеров, сбор через control API и экспорт
// снимка в CSV/JSON.
//
// Транспорт и читалка подменяются: тесты видят ровно то, что уйдёт в
// control_request/list_diagnostics, и ровно то, что попадёт в файл экспорта.

import { describe, expect, it, vi } from "vitest";
import {
  DIAGNOSTIC_CSV_COLUMNS,
  DIAGNOSTICS_WORKER_STALE_SECS,
  buildDiagnosticCards,
  createDiagnosticsApi,
  diagnosticToCsv,
  diagnosticToJson,
  diagnosticsApiError,
  flagSeverity,
  parseHeaders,
  parseSuspicionFlags,
  rawParams,
  sessionParams,
  type DiagnosticSnapshot,
} from "./diagnostics";
import type { ActiveWorker, DbApi } from "./dbApi";
import type { Transport } from "./daemonApi";

function snapshot(overrides: Partial<DiagnosticSnapshot> = {}): DiagnosticSnapshot {
  return {
    ts: 1_760_000_000,
    browser_id: "br-1",
    proxy_id: 7,
    ip: "203.0.113.7",
    country: "DE",
    user_agent: "Mozilla/5.0",
    accept_language: "de-DE",
    timezone_id: "Europe/Berlin",
    screen_w: 1920,
    screen_h: 1080,
    platform: "Linux x86_64",
    webgl_vendor: "Google Inc.",
    webgl_renderer: "ANGLE (LLVM 15)",
    browser_version: "153.0.0",
    headers: '{"User-Agent":"Mozilla/5.0"}',
    suspicion_flags: "[]",
    ...overrides,
  };
}

function worker(browser_id: string): ActiveWorker {
  return {
    browser_id,
    pid: 100,
    status: "running",
    started_at: 1.0,
    heartbeat_at: 2.0,
    restart_count: 0,
    last_error: null,
  };
}

/** Читалка с управляемыми ответами: две команды, которые дергает экран. */
function fakeDb() {
  const listDiagnostics = vi.fn(async (): Promise<DiagnosticSnapshot[]> => []);
  const activeWorkers = vi.fn(async (): Promise<ActiveWorker[]> => []);
  const db: Pick<DbApi, "listDiagnostics" | "activeWorkers"> = {
    listDiagnostics,
    activeWorkers,
  };
  return { db, listDiagnostics, activeWorkers };
}

function transportOf(reply: { status: number; body: string }) {
  const calls: { path: string; method: string; body?: string }[] = [];
  const transport: Transport = async (request) => {
    calls.push({
      path: request.path,
      method: request.method,
      body: request.body,
    });
    return reply;
  };
  return { transport, calls };
}

function errorBody(code: string, message?: string): string {
  return JSON.stringify({ error: { code, message } });
}

describe("parseSuspicionFlags", () => {
  it("массив строк разбирается как есть", () => {
    expect(parseSuspicionFlags('["language_mismatch","ua_old"]')).toEqual([
      "language_mismatch",
      "ua_old",
    ]);
  });

  it("NULL и пустая строка — флагов нет, а не ошибка разбора", () => {
    expect(parseSuspicionFlags(null)).toEqual([]);
    expect(parseSuspicionFlags("")).toEqual([]);
    expect(parseSuspicionFlags("   ")).toEqual([]);
  });

  it("битый JSON не роняет экран: null, а не исключение", () => {
    expect(parseSuspicionFlags("{broken")).toBeNull();
    expect(parseSuspicionFlags("не JSON вовсе")).toBeNull();
  });

  it("не-массив — тоже не разобран, а из массива остаются строки", () => {
    expect(parseSuspicionFlags('{"flag":true}')).toBeNull();
    expect(parseSuspicionFlags('"language_mismatch"')).toBeNull();
    expect(parseSuspicionFlags("[1,null,\"ok\",true]")).toEqual(["ok"]);
  });
});

describe("parseHeaders", () => {
  it("объект заголовков разбирается с сохранением значений", () => {
    expect(
      parseHeaders('{"User-Agent":"Mozilla/5.0","Sec-CH-UA":"Chromium"}'),
    ).toEqual({ "User-Agent": "Mozilla/5.0", "Sec-CH-UA": "Chromium" });
  });

  it("NULL, битый JSON и не-объект — null без исключений", () => {
    expect(parseHeaders(null)).toBeNull();
    expect(parseHeaders("")).toBeNull();
    expect(parseHeaders("{broken")).toBeNull();
    expect(parseHeaders("[1,2]")).toBeNull();
    expect(parseHeaders("5")).toBeNull();
    expect(parseHeaders("null")).toBeNull();
  });
});

describe("flagSeverity", () => {
  it("жёсткие несоответствия — красные", () => {
    expect(flagSeverity("language_mismatch")).toBe("error");
    expect(flagSeverity("ua_os_mismatch")).toBe("error");
    expect(flagSeverity("webdriver_detected")).toBe("error");
    expect(flagSeverity("TIMEZONE_INCONSISTENT")).toBe("error");
  });

  it("всё остальное — жёлтое предупреждение, включая неизвестные флаги", () => {
    expect(flagSeverity("no_country_hint")).toBe("warn");
    expect(flagSeverity("screen_likely_spoofed")).toBe("warn");
    expect(flagSeverity("будущий_флаг_движка")).toBe("warn");
  });
});

describe("sessionParams: параметры сессии для карточки", () => {
  it("контрактные поля уходят готовыми к показу строками", () => {
    const params = sessionParams(snapshot());
    const byLabel = new Map(params.map((param) => [param.label, param.value]));

    expect(byLabel.get("Внешний IP")).toBe("203.0.113.7");
    expect(byLabel.get("Страна")).toBe("DE");
    expect(byLabel.get("User-Agent")).toBe("Mozilla/5.0");
    expect(byLabel.get("Accept-Language")).toBe("de-DE");
    expect(byLabel.get("Timezone")).toBe("Europe/Berlin");
    expect(byLabel.get("Разрешение экрана")).toBe("1920×1080");
    expect(byLabel.get("Платформа")).toBe("Linux x86_64");
    expect(byLabel.get("WebGL vendor")).toBe("Google Inc.");
    expect(byLabel.get("WebGL renderer")).toBe("ANGLE (LLVM 15)");
    expect(byLabel.get("Версия браузера")).toBe("153.0.0");
    // Полей контракта в таблице ещё нет — экран честно ждёт их появления.
    expect(byLabel.get("Ядер (hardwareConcurrency)")).toBeNull();
    expect(byLabel.get("Память (deviceMemory)")).toBeNull();
    expect(byLabel.get("Внутренний IP")).toBeNull();
  });

  it("незаполненный снимок — все значения null, экран покажет «нет данных»", () => {
    const params = sessionParams(
      snapshot({
        ip: null,
        country: null,
        user_agent: null,
        accept_language: null,
        timezone_id: null,
        screen_w: null,
        screen_h: null,
        platform: null,
        webgl_vendor: null,
        webgl_renderer: null,
        browser_version: null,
      }),
    );

    expect(params.length).toBeGreaterThan(0);
    for (const param of params) {
      expect(param.value, `${param.label} должен быть пустым`).toBeNull();
    }
  });

  it("полумытое разрешение экрана не выдаётся за готовое", () => {
    const params = sessionParams(snapshot({ screen_h: null }));
    const screen = params.find((param) => param.label === "Разрешение экрана");
    expect(screen?.value).toBeNull();
  });
});

describe("rawParams: режим «как сайт видит сессию»", () => {
  it("все колонки уходят сырыми, без интерпретации", () => {
    const raw = rawParams(snapshot());
    const byKey = new Map(raw.map((param) => [param.key, param.value]));

    expect(byKey.get("ts")).toBe(1_760_000_000);
    expect(byKey.get("browser_id")).toBe("br-1");
    expect(byKey.get("proxy_id")).toBe(7);
    expect(byKey.get("ip")).toBe("203.0.113.7");
    expect(byKey.get("suspicion_flags")).toBe("[]");
    expect(raw.map((param) => param.key)).toEqual([
      "ts",
      "browser_id",
      "proxy_id",
      "ip",
      "country",
      "user_agent",
      "accept_language",
      "timezone_id",
      "screen_w",
      "screen_h",
      "platform",
      "webgl_vendor",
      "webgl_renderer",
      "browser_version",
      "headers",
      "suspicion_flags",
    ]);
  });

  it("headers — pretty JSON, но битый остаётся исходной строкой", () => {
    const pretty = rawParams(snapshot({ headers: '{"a":1,"b":[2,3]}' })).find(
      (param) => param.key === "headers",
    );
    expect(pretty?.value).toBe('{\n  "a": 1,\n  "b": [\n    2,\n    3\n  ]\n}');

    const broken = rawParams(snapshot({ headers: "{broken" })).find(
      (param) => param.key === "headers",
    );
    expect(broken?.value).toBe("{broken");
  });

  it("отсутствующее поле — null, экран покажет «нет данных»", () => {
    const raw = rawParams(snapshot({ country: null, headers: null }));
    const byKey = new Map(raw.map((param) => [param.key, param.value]));
    expect(byKey.get("country")).toBeNull();
    expect(byKey.get("headers")).toBeNull();
  });
});

describe("buildDiagnosticCards", () => {
  it("карточка на каждый воркер: со снимком и без него", () => {
    const cards = buildDiagnosticCards(
      [snapshot({ browser_id: "br-2" })],
      ["br-1", "br-2"],
    );

    expect(cards).toEqual([
      { browserId: "br-2", snapshot: expect.objectContaining({ browser_id: "br-2" }) },
      { browserId: "br-1", snapshot: null },
    ]);
  });

  it("снимок воркера, которого больше нет в живых, не выбрасывается", () => {
    const cards = buildDiagnosticCards(
      [snapshot({ browser_id: "br-9" })],
      ["br-1"],
    );

    expect(cards.map((card) => card.browserId)).toEqual(["br-9", "br-1"]);
  });

  it("дубли живых воркеров схлопываются, порядок стабилен", () => {
    const cards = buildDiagnosticCards([], ["br-2", "br-1", "br-2"]);
    expect(cards.map((card) => card.browserId)).toEqual(["br-2", "br-1"]);
    expect(cards.every((card) => card.snapshot === null)).toBe(true);
  });

  it("без снимков и без живых воркеров карточек нет", () => {
    expect(buildDiagnosticCards([], [])).toEqual([]);
  });
});

describe("createDiagnosticsApi: чтение", () => {
  it("list идёт в читалку, workerIds — из активных воркеров", async () => {
    const fake = fakeDb();
    fake.listDiagnostics.mockResolvedValue([snapshot()]);
    fake.activeWorkers.mockResolvedValue([worker("br-1"), worker("br-3")]);
    const api = createDiagnosticsApi(fake.db, transportOf({ status: 200, body: "{}" }).transport);

    expect(await api.list()).toHaveLength(1);
    expect(await api.liveWorkerIds()).toEqual(["br-1", "br-3"]);
    expect(fake.listDiagnostics).toHaveBeenCalledTimes(1);
    expect(fake.activeWorkers).toHaveBeenCalledTimes(1);

    const [now, threshold] = fake.activeWorkers.mock.calls[0];
    expect(now).toBeGreaterThan(1_700_000_000, "now — текущая эпоха в секундах");
    expect(threshold).toBe(DIAGNOSTICS_WORKER_STALE_SECS);
    expect(DIAGNOSTICS_WORKER_STALE_SECS).toBeGreaterThan(0);
    expect(DIAGNOSTICS_WORKER_STALE_SECS).toBeLessThanOrEqual(600);
  });

  it("ошибка читалки проходит наружу без поглощения", async () => {
    const fake = fakeDb();
    fake.listDiagnostics.mockRejectedValue({ kind: "NotOpen", message: "нет базы" });
    const api = createDiagnosticsApi(fake.db, transportOf({ status: 200, body: "{}" }).transport);

    await expect(api.list()).rejects.toEqual({ kind: "NotOpen", message: "нет базы" });
  });
});

describe("createDiagnosticsApi: сбор снимка", () => {
  it("для воркера — контрактный body, из ответа берётся requested", async () => {
    const { transport, calls } = transportOf({
      status: 200,
      body: '{"requested":3}',
    });
    const api = createDiagnosticsApi(fakeDb().db, transport);

    expect(await api.collect("br-1")).toBe(3);
    expect(calls).toEqual([
      {
        path: "/control/diagnostics/collect",
        method: "POST",
        body: '{"browser_id":"br-1"}',
      },
    ]);
  });

  it("collectAll — тот же endpoint с флагом all", async () => {
    const { transport, calls } = transportOf({
      status: 200,
      body: '{"requested":5}',
    });
    const api = createDiagnosticsApi(fakeDb().db, transport);

    expect(await api.collect(null)).toBe(5);
    expect(calls[0].body).toBe('{"all":true}');
  });

  it("ответ без requested — ошибка, а не молчаливый undefined", async () => {
    const { transport } = transportOf({ status: 200, body: "{}" });
    const api = createDiagnosticsApi(fakeDb().db, transport);

    await expect(api.collect("br-1")).rejects.toThrow(/requested/);
  });

  it("не-JSON ответ демона — ошибка с телом для диагностики", async () => {
    const { transport } = transportOf({ status: 200, body: "<html>ошибка</html>" });
    const api = createDiagnosticsApi(fakeDb().db, transport);

    await expect(api.collect(null)).rejects.toThrow(/<html>/);
  });

  it("400 invalid_request без message читается локальным текстом", async () => {
    const { transport } = transportOf({ status: 400, body: errorBody("invalid_request") });
    const api = createDiagnosticsApi(fakeDb().db, transport);

    await expect(api.collect("br-1")).rejects.toThrow(/invalid_request/);
    await expect(api.collect("br-1")).rejects.toThrow(/browser_id/);
  });

  it("message демона сильнее локального текста", async () => {
    const { transport } = transportOf({
      status: 400,
      body: errorBody("invalid_request", "нет воркера br-9"),
    });
    const api = createDiagnosticsApi(fakeDb().db, transport);

    await expect(api.collect("br-9")).rejects.toThrow(/нет воркера br-9/);
  });

  it("не-JSON тело ошибки — HTTP-код и начало тела", async () => {
    const { transport } = transportOf({ status: 503, body: "демон занят" });
    const api = createDiagnosticsApi(fakeDb().db, transport);

    await expect(api.collect(null)).rejects.toThrow(/HTTP 503/);
  });
});

describe("diagnosticsApiError", () => {
  it("известный код без message получает локальный текст", () => {
    expect(diagnosticsApiError(400, errorBody("invalid_request"))).toMatch(
      /invalid_request/,
    );
  });

  it("неизвестный код и не-JSON падают в apiErrorMessage", () => {
    expect(diagnosticsApiError(500, errorBody("internal"))).toBe(
      "HTTP 500: " + errorBody("internal"),
    );
    expect(diagnosticsApiError(502, "плохой прокси")).toBe(
      "HTTP 502: плохой прокси",
    );
  });
});

describe("diagnosticToCsv: экспорт снимка", () => {
  it("одна строка данных с RFC-4180 экранированием", () => {
    const csv = diagnosticToCsv(
      snapshot({
        ts: 1.5,
        ip: null,
        headers: null,
        user_agent: 'Mozilla/5.0, "Beta"',
        suspicion_flags: '["a","b"]',
      }),
    );

    const lines = csv.split("\n");
    expect(lines).toHaveLength(2);
    expect(lines[0]).toBe(DIAGNOSTIC_CSV_COLUMNS.join(","));
    expect(lines[1]).toBe(
      '1.5,br-1,7,,"DE","Mozilla/5.0, ""Beta""",de-DE,Europe/Berlin,1920,1080,' +
        'Linux x86_64,Google Inc.,ANGLE (LLVM 15),153.0.0,,"[""a"",""b""]"',
    );
  });

  it("перенос строки внутри значения остаётся внутри кавычек", () => {
    const csv = diagnosticToCsv(snapshot({ user_agent: "line1\nline2" }));
    expect(csv).toContain('"line1\nline2"');
  });

  it("NULL — пустая ячейка, а не строка «null»", () => {
    const csv = diagnosticToCsv(
      snapshot({ country: null, proxy_id: null, screen_w: null }),
    );
    const cells = csv.split("\n")[1].split(",");
    expect(cells).toContain("");
    expect(csv).not.toContain("null");
  });
});

describe("diagnosticToJson: экспорт снимка", () => {
  it("headers и suspicion_flags уходят разобранными, остальное — как есть", () => {
    const row = snapshot({
      headers: '{"User-Agent":"Mozilla/5.0"}',
      suspicion_flags: '["language_mismatch"]',
    });

    const parsed: Record<string, unknown> = JSON.parse(diagnosticToJson(row));

    expect(parsed.headers).toEqual({ "User-Agent": "Mozilla/5.0" });
    expect(parsed.suspicion_flags).toEqual(["language_mismatch"]);
    expect(parsed.ts).toBe(row.ts);
    expect(parsed.browser_id).toBe("br-1");
    expect(parsed.ip).toBe("203.0.113.7");
    expect(parsed.user_agent).toBe("Mozilla/5.0");
  });

  it("битый JSON не теряется: в экспорт уходит исходная строка", () => {
    const parsed: Record<string, unknown> = JSON.parse(
      diagnosticToJson(snapshot({ headers: "{broken", suspicion_flags: "nope" })),
    );

    expect(parsed.headers).toBe("{broken");
    expect(parsed.suspicion_flags).toBe("nope");
  });

  it("отсутствующие поля — null, а не пропущенные ключи", () => {
    const parsed: Record<string, unknown> = JSON.parse(
      diagnosticToJson(snapshot({ headers: null, suspicion_flags: null })),
    );

    expect(parsed.headers).toBeNull();
    expect(parsed.suspicion_flags).toBeNull();
    for (const column of DIAGNOSTIC_CSV_COLUMNS) {
      expect(parsed).toHaveProperty(column);
    }
  });
});
