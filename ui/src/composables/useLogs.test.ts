import { describe, expect, it } from "vitest";
import { createLogs, MAX_LOGS_ROWS } from "./useLogs";
import {
  FILTERS_STORAGE_KEY,
  type LogQueryFilters,
  type StorageLike,
} from "../lib/logFilters";
import type { LogCursor, LogRow } from "../lib/logMerge";

function fakeStorage(initial?: Record<string, string>): StorageLike & {
  data: Map<string, string>;
} {
  const data = new Map<string, string>(Object.entries(initial ?? {}));
  return {
    data,
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => void data.set(key, value),
  };
}

/**
 * Память вместо SQLite: те же правила, что у list_logs_page/count_logs, —
 * фильтры точного равенства, включительное окно времени, курсор строже
 * позиции, порядок ts DESC, id DESC.
 */
class FakeDb {
  rows: LogRow[] = [];
  pageCalls: { cursor: LogCursor | null; limit: number; query: LogQueryFilters }[] = [];
  countCalls = 0;
  failWith: unknown = null;

  private nextId = 1;

  add(ts: number, level = "INFO", category: string | null = null): LogRow {
    const row: LogRow = {
      id: this.nextId++,
      ts,
      level,
      browser_id: null,
      category,
      message: `строка ${ts}`,
      fields: null,
    };
    this.rows.push(row);
    return row;
  }

  private match(row: LogRow, query: LogQueryFilters): boolean {
    if (query.level !== null && row.level !== query.level) return false;
    if (query.category !== null && row.category !== query.category) return false;
    if (query.browserId !== null && row.browser_id !== query.browserId) {
      return false;
    }
    if (query.since !== null && row.ts < query.since) return false;
    if (query.until !== null && row.ts > query.until) return false;
    return true;
  }

  private ordered(query: LogQueryFilters): LogRow[] {
    return this.rows
      .filter((row) => this.match(row, query))
      .sort((a, b) => b.ts - a.ts || b.id - a.id);
  }

  async listLogsPage(params: {
    query: LogQueryFilters;
    limit: number;
    cursor?: LogCursor | null;
  }): Promise<LogRow[]> {
    if (this.failWith !== null) throw this.failWith;
    this.pageCalls.push({
      cursor: params.cursor ?? null,
      limit: params.limit,
      query: params.query,
    });
    let rows = this.ordered(params.query);
    const cursor = params.cursor ?? null;
    if (cursor) {
      rows = rows.filter(
        (row) =>
          row.ts < cursor.ts || (row.ts === cursor.ts && row.id < cursor.id),
      );
    }
    return rows.slice(0, params.limit);
  }

  async countLogs(query: LogQueryFilters): Promise<number> {
    this.countCalls += 1;
    return this.ordered(query).length;
  }
}

const PAGE = 3;

/** Прокачивает микрозадачи висящего тика (toggleLive запускает его сам). */
function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function setup(db: FakeDb, storage = fakeStorage()) {
  let ready = true;
  const logs = createLogs({
    api: {
      listLogsPage: (params) => db.listLogsPage(params),
      countLogs: (query) => db.countLogs(query),
    },
    storage,
    isReady: () => ready,
    pageSize: PAGE,
  });
  return { logs, storage, setReady: (value: boolean) => (ready = value) };
}

describe("createLogs: первая загрузка", () => {
  it("читает страницу и счётчик, курсор первой страницы — null", async () => {
    const db = new FakeDb();
    db.add(1);
    db.add(2);
    db.add(3);
    const { logs } = setup(db);

    await logs.start();

    expect(logs.rows.value.map((row) => row.id)).toEqual([3, 2, 1]);
    expect(logs.total.value).toBe(3);
    expect(db.pageCalls[0].cursor).toBeNull();
    expect(db.pageCalls[0].limit).toBe(PAGE);
    logs.stop();
  });

  it("фильтр из localStorage применяется к запросу", async () => {
    const db = new FakeDb();
    db.add(1, "ERROR");
    db.add(2, "INFO");
    db.add(3, "ERROR");
    const storage = fakeStorage({
      [FILTERS_STORAGE_KEY]: JSON.stringify({
        level: "ERROR",
        category: "",
        browserId: "",
        since: null,
        until: null,
      }),
    });
    const { logs } = setup(db, storage);

    await logs.start();

    expect(logs.rows.value.map((row) => row.id)).toEqual([3, 1]);
    expect(db.pageCalls[0].query.level).toBe("ERROR");
    expect(logs.total.value).toBe(2);
    logs.stop();
  });

  it("база не открыта — команды не зовутся", async () => {
    const db = new FakeDb();
    db.add(1);
    const { logs, setReady } = setup(db);
    setReady(false);

    await logs.start();

    expect(db.pageCalls).toHaveLength(0);
    expect(db.countCalls).toBe(0);
    expect(logs.rows.value).toEqual([]);
    logs.stop();
  });
});

describe("createLogs: живой режим", () => {
  it("тик привозит новые строки без дублей и в порядке ts DESC", async () => {
    const db = new FakeDb();
    db.add(1);
    db.add(2);
    db.add(3);
    const { logs } = setup(db);
    await logs.start();

    db.add(4);
    db.add(5);
    await logs.tick();

    expect(logs.rows.value.map((row) => row.id)).toEqual([5, 4, 3, 2, 1]);
    expect(new Set(logs.rows.value.map((row) => row.id)).size).toBe(5);
    expect(
      db.pageCalls,
      "тику хватило страницы: первая строка «не новее» — стоп",
    ).toHaveLength(2);
    expect(db.pageCalls[1].cursor).toBeNull();
    expect(logs.total.value).toBe(5);
    logs.stop();
  });

  it("без новых строк тик не дублирует строки и не пересчитывает count", async () => {
    const db = new FakeDb();
    db.add(1);
    db.add(2);
    const { logs } = setup(db);
    await logs.start();
    const countCalls = db.countCalls;

    await logs.tick();
    await logs.tick();

    expect(logs.rows.value.map((row) => row.id)).toEqual([2, 1]);
    expect(db.countCalls, "count обновляется только с новым").toBe(countCalls);
    logs.stop();
  });

  it("пауза не читает базу, продолжение догоняет пропущенное", async () => {
    const db = new FakeDb();
    db.add(1);
    const { logs } = setup(db);
    await logs.start();
    const callsWhileLive = db.pageCalls.length;

    logs.toggleLive();
    db.add(2);
    db.add(3);
    await logs.tick();
    expect(db.pageCalls, "на паузе тик — no-op").toHaveLength(callsWhileLive);
    expect(logs.rows.value.map((row) => row.id)).toEqual([1]);

    logs.toggleLive(); // продолжение сам запускает тик
    await flush();
    expect(logs.rows.value.map((row) => row.id)).toEqual([3, 2, 1]);
    logs.stop();
  });

  it("догоняющий тик после паузы не теряет строки за несколькими страницами", async () => {
    const db = new FakeDb();
    db.add(1);
    const { logs } = setup(db);
    await logs.start();

    logs.toggleLive(); // пауза
    for (let ts = 2; ts <= 9; ts += 1) db.add(ts);
    logs.toggleLive(); // продолжение сам запускает тик
    await flush();

    expect(logs.rows.value.map((row) => row.id)).toEqual([
      9, 8, 7, 6, 5, 4, 3, 2, 1,
    ]);
    expect(new Set(logs.rows.value.map((row) => row.id)).size).toBe(9);
    expect(
      db.pageCalls.map((call) => call.cursor),
      "догоняющий обход шёл курсором через страницы, пока не упёрся в known",
    ).toEqual([null, null, { ts: 7, id: 7 }, { ts: 4, id: 4 }]);
    logs.stop();
  });

  it("живой режим ограничен MAX_LOGS_ROWS без потери свежих строк", async () => {
    const db = new FakeDb();
    db.add(1);
    const { logs } = setup(db);
    await logs.start();

    logs.toggleLive(); // пауза
    for (let ts = 2; ts <= MAX_LOGS_ROWS + 50; ts += 1) db.add(ts);
    logs.toggleLive(); // продолжение сам запускает тик
    await flush();

    expect(logs.rows.value).toHaveLength(MAX_LOGS_ROWS);
    expect(logs.rows.value[0].id, "самые новые сверху").toBe(
      MAX_LOGS_ROWS + 50,
    );
    expect(
      logs.rows.value[logs.rows.value.length - 1].id,
      "вытеснены самые старые, новые не потеряны",
    ).toBe(51);
    expect(new Set(logs.rows.value.map((row) => row.id)).size, "без дублей").toBe(
      MAX_LOGS_ROWS,
    );
    logs.stop();
  });
});

describe("createLogs: фильтры", () => {
  it("смена фильтра перечитывает список и сохраняет его в хранилище", async () => {
    const db = new FakeDb();
    db.add(1, "ERROR");
    db.add(2, "INFO");
    db.add(3, "ERROR");
    const { logs, storage } = setup(db);
    await logs.start();

    await logs.updateFilters({ level: "ERROR" });

    expect(logs.rows.value.map((row) => row.id)).toEqual([3, 1]);
    expect(logs.total.value).toBe(2);
    const saved = JSON.parse(
      storage.data.get(FILTERS_STORAGE_KEY) ?? "{}",
    ) as { level?: string };
    expect(saved.level).toBe("ERROR");
    logs.stop();
  });

  it("сброс возвращает пустые фильтры и перечитывает", async () => {
    const db = new FakeDb();
    db.add(1, "ERROR");
    db.add(2, "INFO");
    const { logs } = setup(db);
    await logs.start();
    await logs.updateFilters({ level: "ERROR" });
    expect(logs.total.value).toBe(1);

    await logs.resetFilters();

    expect(logs.filters.value.level).toBe("");
    expect(logs.rows.value).toHaveLength(2);
    expect(logs.total.value).toBe(2);
    logs.stop();
  });

  it("невалидное значение фильтра отбрасывается, а не уходит в запрос", async () => {
    const db = new FakeDb();
    db.add(1);
    const { logs } = setup(db);
    await logs.start();

    await logs.updateFilters({ level: "TRACE" as string });

    expect(logs.filters.value.level).toBe("");
    expect(db.pageCalls[db.pageCalls.length - 1].query.level).toBeNull();
    logs.stop();
  });
});

describe("createLogs: подгрузка старых", () => {
  it("подгрузка идёт курсором от самой старой строки и заканчивается", async () => {
    const db = new FakeDb();
    for (let ts = 1; ts <= 7; ts += 1) db.add(ts);
    const { logs } = setup(db);
    await logs.start();
    expect(logs.rows.value.map((row) => row.id)).toEqual([7, 6, 5]);

    await logs.loadOlder();
    expect(logs.rows.value.map((row) => row.id)).toEqual([7, 6, 5, 4, 3, 2]);
    expect(db.pageCalls[1].cursor).toEqual({ ts: 5, id: 5 });
    expect(logs.exhausted.value).toBe(false);

    await logs.loadOlder();
    expect(logs.rows.value.map((row) => row.id)).toEqual([7, 6, 5, 4, 3, 2, 1]);
    expect(logs.exhausted.value, "короткая страница — конец").toBe(true);

    const calls = db.pageCalls.length;
    await logs.loadOlder();
    expect(db.pageCalls, "после exhausted запросов нет").toHaveLength(calls);
    logs.stop();
  });

  it("подгрузка не трогает счётчик live-строк (автоскролл не дёргается)", async () => {
    const db = new FakeDb();
    for (let ts = 1; ts <= 6; ts += 1) db.add(ts);
    const { logs } = setup(db);
    await logs.start();
    const liveAdded = logs.liveAdded.value;

    await logs.loadOlder();

    expect(logs.liveAdded.value).toBe(liveAdded);
    logs.stop();
  });
});

describe("createLogs: ошибки", () => {
  it("ошибка чтения становится текстом, а не роняет экран", async () => {
    const db = new FakeDb();
    db.failWith = {
      kind: "ReadFailed",
      message: { reason: "SQLITE_BUSY" },
    };
    const { logs } = setup(db);

    await logs.start();

    expect(logs.error.value).toContain("SQLITE_BUSY");
    expect(logs.rows.value).toEqual([]);
    expect(logs.loading.value).toBe(false);
    logs.stop();
  });
});
