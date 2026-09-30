// Экспорт из UI (план §5, фаза 9): сборка запроса диапазона поверх текущих
// фильтров, курсорные страницы до исчерпания с защитным потолком и форматы
// файла (CSV как на экране, JSON на выбор).

import { describe, expect, it } from "vitest";
import {
  EXPORT_PAGE_SIZE,
  EXPORT_ROW_CAP,
  buildExportQuery,
  collectExportRows,
  exportFilename,
  exportFile,
  rangeProblem,
} from "./logExport";
import { EMPTY_LOG_FILTERS, type LogFilterValues } from "./logFilters";
import type { LogCursor, LogPageFetch, LogRow } from "./logMerge";

const FILTERS: LogFilterValues = {
  level: "ERROR",
  category: "click",
  browserId: "b1",
  since: "2026-01-01T00:00",
  until: "2026-01-02T00:00",
};

function row(id: number, ts: number): LogRow {
  return {
    id,
    ts,
    level: "INFO",
    browser_id: null,
    category: null,
    message: `строка ${id}`,
    fields: null,
  };
}

/** Потолки маленькие, чтобы тесты не плодили строки. */
const PAGE = 3;
const CAP = 5;

/** Страницы из готового списка: тот же курсор и порядок, что у list_logs_page. */
function pageFetch(rows: readonly LogRow[]): {
  fetch: LogPageFetch;
  calls: { cursor: LogCursor | null; limit: number }[];
} {
  const calls: { cursor: LogCursor | null; limit: number }[] = [];
  const fetch: LogPageFetch = (cursor, limit) => {
    calls.push({ cursor, limit });
    let rest = rows;
    if (cursor !== null) {
      rest = rows.filter(
        (item) =>
          item.ts < cursor.ts || (item.ts === cursor.ts && item.id < cursor.id),
      );
    }
    return Promise.resolve(rest.slice(0, limit));
  };
  return { fetch, calls };
}

describe("buildExportQuery: диапазон поверх текущих фильтров", () => {
  it("границы диалога подменяют since/until, остальные фильтры остаются", () => {
    const query = buildExportQuery(FILTERS, {
      since: "2026-03-01T10:30",
      until: "2026-03-01T11:00",
    });

    expect(query.level).toBe("ERROR");
    expect(query.category).toBe("click");
    expect(query.browserId).toBe("b1");
    expect(query.since).toBe(new Date("2026-03-01T10:30").getTime() / 1000);
    // верхняя граница включает выбранную минуту целиком — как у фильтров
    expect(query.until).toBeCloseTo(
      new Date("2026-03-01T11:00").getTime() / 1000 + 59.999,
      3,
    );
  });

  it("пустая граница диалога снимает окно времени фильтра", () => {
    const query = buildExportQuery(FILTERS, { since: "", until: null });

    expect(query.since).toBeNull();
    expect(query.until).toBeNull();
    expect(query.level, "время — не все фильтры").toBe("ERROR");
  });

  it("невалидная строка границы не доходит до запроса", () => {
    const query = buildExportQuery(EMPTY_LOG_FILTERS, {
      since: "не дата",
      until: undefined as unknown as string | null,
    });

    expect(query.since).toBeNull();
    expect(query.until).toBeNull();
  });
});

describe("rangeProblem: перевёрнутое окно", () => {
  it("начало позже конца — ошибка, ровная граница — нет", () => {
    expect(
      rangeProblem({ since: "2026-03-01T11:00", until: "2026-03-01T10:00" }),
    ).toContain("позже");
    expect(
      rangeProblem({ since: "2026-03-01T10:00", until: "2026-03-01T10:00" }),
    ).toBeNull();
  });

  it("незакрытое окно без ошибки: граница опциональна", () => {
    expect(rangeProblem({ since: null, until: null })).toBeNull();
    expect(rangeProblem({ since: "2026-03-01T10:00", until: null })).toBeNull();
    expect(rangeProblem({ since: null, until: "не дата" })).toBeNull();
  });
});

describe("collectExportRows: страницы и потолок", () => {
  it("читает курсорными страницами до короткой страницы", async () => {
    const rows = [row(7, 70), row(6, 60), row(5, 50), row(4, 40), row(3, 30)];
    const { fetch, calls } = pageFetch(rows);

    const result = await collectExportRows(fetch, { pageSize: PAGE, cap: 100 });

    expect(result.rows.map((item) => item.id)).toEqual([7, 6, 5, 4, 3]);
    expect(result.truncated, "диапазон исчерпан, а не усечён").toBe(false);
    expect(calls.map((call) => call.cursor)).toEqual([null, { ts: 50, id: 5 }]);
    expect(calls.every((call) => call.limit === PAGE)).toBe(true);
  });

  it("пустая база — пустая выгрузка без ошибки", async () => {
    const { fetch, calls } = pageFetch([]);

    const result = await collectExportRows(fetch, { pageSize: PAGE, cap: CAP });

    expect(result.rows).toEqual([]);
    expect(result.truncated).toBe(false);
    expect(calls).toHaveLength(1);
  });

  it("потолок останавливает выборку и честно помечает усечение", async () => {
    const rows = Array.from({ length: 20 }, (_, index) =>
      row(index + 1, (20 - index) * 10),
    );
    const { fetch, calls } = pageFetch(rows);

    const result = await collectExportRows(fetch, { pageSize: PAGE, cap: CAP });

    expect(result.rows).toHaveLength(CAP);
    expect(result.truncated, "за потолком строки есть — выгрузка усечена").toBe(
      true,
    );
    // страницы до потолка + пробник «а есть ли ещё?» на одну строку
    expect(calls).toHaveLength(Math.ceil(CAP / PAGE) + 1);
    expect(calls[calls.length - 1]).toEqual({
      cursor: { ts: rows[CAP - 1].ts, id: rows[CAP - 1].id },
      limit: 1,
    });
  });

  it("строк ровно по потолку — без лишнего усечения", async () => {
    const rows = Array.from({ length: CAP }, (_, index) =>
      row(index + 1, (CAP - index) * 10),
    );
    const { fetch } = pageFetch(rows);

    const result = await collectExportRows(fetch, { pageSize: PAGE, cap: CAP });

    expect(result.rows).toHaveLength(CAP);
    expect(result.truncated, "пробник вернул пусто — усечения нет").toBe(false);
  });

  it("константы потолка держат контракт с БД и разумный размер выгрузки", () => {
    expect(EXPORT_PAGE_SIZE).toBeGreaterThan(0);
    expect(EXPORT_PAGE_SIZE).toBeLessThanOrEqual(1000);
    expect(EXPORT_ROW_CAP).toBe(10_000);
  });
});

describe("exportFile: форматы", () => {
  const rows = [
    { ...row(2, 20), browser_id: "b1", category: "click", message: 'сказал "да", да' },
    { ...row(1, 10), level: "ERROR", message: "провал" },
  ];

  it("CSV — тот же RFC 4180, что у выгрузки экрана", () => {
    const file = exportFile(rows, "csv");

    expect(file.mime).toContain("text/csv");
    expect(file.ext).toBe("csv");
    expect(file.content.split("\n")).toHaveLength(3);
    expect(file.content.startsWith("ts,level,browser_id,category,message,fields")).toBe(
      true,
    );
    expect(file.content).toContain('"сказал ""да"", да"');
  });

  it("JSON — те же колонки, NULL остаётся null, а не строкой", () => {
    const file = exportFile(rows, "json");

    expect(file.mime).toContain("application/json");
    expect(file.ext).toBe("json");
    const parsed = JSON.parse(file.content) as Record<string, unknown>[];
    expect(parsed).toHaveLength(2);
    expect(Object.keys(parsed[0] ?? {})).toEqual([
      "ts",
      "level",
      "browser_id",
      "category",
      "message",
      "fields",
    ]);
    expect(parsed[0]?.browser_id).toBe("b1");
    expect(parsed[1]?.fields).toBeNull();
    expect(parsed[1]?.message).toBe("провал");
  });

  it("пустая выгрузка: CSV — заголовок, JSON — пустой массив", () => {
    expect(exportFile([], "csv").content.split("\n")).toHaveLength(1);
    expect(exportFile([], "json").content).toBe("[]");
  });

  it("имя файла содержит метку времени и расширение формата", () => {
    const at = new Date(2026, 2, 5, 4, 5, 6);
    expect(exportFilename("csv", at)).toBe("adclicker-logs-2026-03-05-040506.csv");
    expect(exportFilename("json", at)).toBe("adclicker-logs-2026-03-05-040506.json");
  });
});
