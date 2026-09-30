// Контракт очистки профилей: POST /control/cleanup/run и GET
// /control/cleanup/status (engine/control_plane/api.py, engine/cleanup.py).
//
// Разбор — строгий по числам и структуре: кривой ответ не должен превратиться
// в отчёт с NaN, который UI покажет как «удалено NaN». Транспорт подменяется,
// поэтому здесь видно ровно то, что уйдёт в control_request.

import { describe, expect, it, vi } from "vitest";
import {
  cleanupReportTone,
  createCleanupApi,
  parseCleanupReport,
  parseCleanupStatus,
} from "./cleanup";
import type { Transport, TransportRequest } from "./daemonApi";

const REPORT = {
  removed: 3,
  removed_bytes: 1_572_864,
  skipped_active: 1,
  errors: 0,
  duration_ms: 1200,
  dry_run: false,
};

/** Транспорт, отдающий готовое тело; запросы копируются для проверок. */
function transportOf(
  handler: (request: TransportRequest) => { status: number; body: string },
) {
  const calls: TransportRequest[] = [];
  const transport: Transport = vi.fn(async (request) => {
    calls.push(request);
    return handler(request);
  });
  return { calls, transport };
}

describe("parseCleanupReport", () => {
  it("полный отчёт разбирается по полям контракта", () => {
    expect(parseCleanupReport(REPORT, "/control/cleanup/run")).toEqual(REPORT);
  });

  it("dry_run без поля — false, а не падение", () => {
    const { dry_run: _dropped, ...withoutFlag } = REPORT;
    expect(parseCleanupReport(withoutFlag, "/control/cleanup/run").dry_run).toBe(false);
  });

  it("dry_run: true доходит как есть — превью помечается", () => {
    expect(
      parseCleanupReport({ ...REPORT, dry_run: true }, "/control/cleanup/run").dry_run,
    ).toBe(true);
  });

  it("нечисловой счётчик — ошибка с путём, а не NaN в отчёте", () => {
    expect(() =>
      parseCleanupReport({ ...REPORT, removed: "3" }, "/control/cleanup/run"),
    ).toThrowError(/control\/cleanup\/run/);
    expect(() =>
      parseCleanupReport({ ...REPORT, removed_bytes: null }, "/control/cleanup/run"),
    ).toThrowError(/removed_bytes/);
    expect(() =>
      parseCleanupReport({ ...REPORT, duration_ms: Number.NaN }, "/control/cleanup/run"),
    ).toThrowError(/duration_ms/);
  });

  it("не-объект вместо отчёта — ошибка, а не тихий ноль", () => {
    expect(() => parseCleanupReport([1, 2], "/control/cleanup/run")).toThrowError(
      /control\/cleanup\/run/,
    );
    expect(() => parseCleanupReport('"ok"', "/control/cleanup/run")).toThrowError(
      /control\/cleanup\/run/,
    );
  });
});

describe("parseCleanupStatus", () => {
  it("последний прогон и ближайший запуск разбирааются", () => {
    expect(
      parseCleanupStatus(
        { last: { ts: 1_700_000_000, report: REPORT }, next_run: 1_700_086_400 },
        "/control/cleanup/status",
      ),
    ).toEqual({ last: { ts: 1_700_000_000, report: REPORT }, next_run: 1_700_086_400 });
  });

  it("прогона не было и job выключен — оба значения null", () => {
    expect(
      parseCleanupStatus({ last: null, next_run: null }, "/control/cleanup/status"),
    ).toEqual({ last: null, next_run: null });
  });

  it("метки без значения — ошибка контракта", () => {
    expect(() =>
      parseCleanupStatus({ next_run: null }, "/control/cleanup/status"),
    ).toThrowError(/last/);
    expect(() =>
      parseCleanupStatus({ last: null }, "/control/cleanup/status"),
    ).toThrowError(/next_run/);
    expect(() =>
      parseCleanupStatus({ last: null, next_run: "soon" }, "/control/cleanup/status"),
    ).toThrowError(/next_run/);
  });

  it("битые внутренности last — ошибка, а не прогон без отчёта", () => {
    expect(() =>
      parseCleanupStatus(
        { last: { ts: "now", report: REPORT }, next_run: null },
        "/control/cleanup/status",
      ),
    ).toThrowError(/ts/);
    expect(() =>
      parseCleanupStatus(
        { last: { ts: 1_700_000_000, report: { removed: 1 } }, next_run: null },
        "/control/cleanup/status",
      ),
    ).toThrowError(/removed_bytes/);
    expect(() =>
      parseCleanupStatus({ last: {}, next_run: null }, "/control/cleanup/status"),
    ).toThrowError(/ts/);
  });

  it("не-объект вместо статуса — ошибка", () => {
    expect(() => parseCleanupStatus(null, "/control/cleanup/status")).toThrowError(
      /control\/cleanup\/status/,
    );
  });
});

describe("cleanupReportTone", () => {
  it("ошибок нет — успех", () => {
    expect(cleanupReportTone(REPORT)).toBe("success");
    expect(cleanupReportTone({ ...REPORT, removed: 0, skipped_active: 5 })).toBe(
      "success",
    );
  });

  it("хотя бы одна ошибка — предупреждение", () => {
    expect(cleanupReportTone({ ...REPORT, errors: 1 })).toBe("warning");
    expect(cleanupReportTone({ ...REPORT, errors: 12 })).toBe("warning");
  });
});

describe("createCleanupApi", () => {
  it("run(false) уходит пустым объектом и возвращает отчёт", async () => {
    const { calls, transport } = transportOf(() => ({
      status: 200,
      body: JSON.stringify({ report: REPORT }),
    }));
    const api = createCleanupApi(transport);

    await expect(api.run(false)).resolves.toEqual(REPORT);
    expect(calls).toEqual([
      { path: "/control/cleanup/run", method: "POST", body: "{}" },
    ]);
  });

  it("run(true) несёт dry_run в теле", async () => {
    const { calls, transport } = transportOf(() => ({
      status: 200,
      body: JSON.stringify({ report: { ...REPORT, dry_run: true } }),
    }));
    const api = createCleanupApi(transport);

    const report = await api.run(true);

    expect(report.dry_run).toBe(true);
    expect(calls[0]?.body).toBe('{"dry_run":true}');
  });

  it("ответ run без report — ошибка контракта", async () => {
    const { transport } = transportOf(() => ({ status: 200, body: "{}" }));
    const api = createCleanupApi(transport);

    await expect(api.run(false)).rejects.toThrowError(/report/);
  });

  it("status уходит GETом и разбирает ответ", async () => {
    const { calls, transport } = transportOf(() => ({
      status: 200,
      body: JSON.stringify({ last: null, next_run: 1_700_086_400 }),
    }));
    const api = createCleanupApi(transport);

    await expect(api.status()).resolves.toEqual({
      last: null,
      next_run: 1_700_086_400,
    });
    expect(calls).toEqual([{ path: "/control/cleanup/status", method: "GET" }]);
    expect(calls[0]?.body).toBeUndefined();
  });

  it("не-200 — читаемый текст ошибки демона", async () => {
    const { transport } = transportOf(() => ({
      status: 400,
      body: JSON.stringify({ error: { code: "bad", message: "тело должно быть объектом" } }),
    }));
    const api = createCleanupApi(transport);

    await expect(api.run(false)).rejects.toThrowError("тело должно быть объектом");
    await expect(api.status()).rejects.toThrowError("тело должно быть объектом");
  });

  it("обрыв транспорта доходит сообщением, а не TypeError", async () => {
    const transport: Transport = vi.fn(async () => {
      throw new Error("нет соединения с демоном");
    });
    const api = createCleanupApi(transport);

    await expect(api.run(true)).rejects.toThrowError("нет соединения с демоном");
  });
});
