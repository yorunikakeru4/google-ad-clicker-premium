import { describe, expect, it } from "vitest";
import {
  collectAllRows,
  cursorOf,
  fetchNewRows,
  mergeLogRows,
  oldestCursor,
  type LogCursor,
  type LogPageFetch,
  type LogRow,
} from "./logMerge";

/** Строка с id и ts, совпадающими по порядку: больше id — новее. */
function row(id: number, ts: number = id): LogRow {
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

/** БД в порядке `ts DESC, id DESC` — так же, как её отдаёт list_logs_page. */
function sortedDesc(rows: LogRow[]): LogRow[] {
  return [...rows].sort((a, b) => b.ts - a.ts || b.id - a.id);
}

/**
 * Фейковая БД: та же семантика курсора, что у `list_logs_page`
 * (`ts < ? OR (ts = ? AND id < ?)`), плюс счётчик вызовов.
 */
function pager(db: LogRow[]): { fetch: LogPageFetch; calls: LogCursor[] } {
  const calls: LogCursor[] = [];
  const ordered = sortedDesc(db);
  const fetch: LogPageFetch = (cursor, limit) => {
    if (cursor) calls.push(cursor);
    else calls.push({ ts: Number.NaN, id: Number.NaN });
    const from = cursor
      ? ordered.filter(
          (r) =>
            r.ts < cursor.ts || (r.ts === cursor.ts && r.id < cursor.id),
        )
      : ordered;
    return Promise.resolve(from.slice(0, limit));
  };
  return { fetch, calls };
}

describe("mergeLogRows", () => {
  it("склеивает страницы без дублей по id и держит порядок ts DESC", () => {
    const existing = [row(3), row(2), row(1)];
    const incoming = [row(5), row(4), row(3)];

    const merged = mergeLogRows(existing, incoming);

    expect(merged.map((r) => r.id)).toEqual([5, 4, 3, 2, 1]);
  });

  it("равные ts разрываются по id DESC", () => {
    const existing = [row(2, 10), row(1, 10)];
    const incoming = [row(3, 10), row(1, 10)];

    const merged = mergeLogRows(existing, incoming);

    expect(merged.map((r) => r.id)).toEqual([3, 2, 1]);
  });

  it("свежая копия строки вытесняет старую версию", () => {
    const stale = { ...row(1), message: "до обновления" };
    const fresh = { ...row(1), message: "после обновления" };

    const merged = mergeLogRows([stale], [fresh]);

    expect(merged).toHaveLength(1);
    expect(merged[0].message).toBe("после обновления");
  });

  it("лимит вытесняет самые старые строки", () => {
    const existing = [row(4), row(3)];
    const incoming = [row(6), row(5)];

    const merged = mergeLogRows(existing, incoming, 3);

    expect(merged.map((r) => r.id), "вытеснен id 3").toEqual([6, 5, 4]);
  });

  it("курсор берётся от самой старой строки", () => {
    expect(oldestCursor([row(5), row(3), row(4)])).toEqual({ ts: 3, id: 3 });
    expect(cursorOf(row(7, 42))).toEqual({ ts: 42, id: 7 });
    expect(oldestCursor([])).toBeNull();
  });
});

describe("fetchNewRows", () => {
  const db: LogRow[] = Array.from({ length: 20 }, (_, i) => row(i + 1));
  /** Позиция самого нового известного ряда (id 10, ts 10). */
  const newestKnown: LogCursor = { ts: 10, id: 10 };

  it("догоняет все новые строки через страницы: без потерь и дублей", async () => {
    const { fetch, calls } = pager(db);

    const fresh = await fetchNewRows(fetch, newestKnown, 3, 100);

    expect(fresh.map((r) => r.id)).toEqual([20, 19, 18, 17, 16, 15, 14, 13, 12, 11]);
    expect(calls, "шёл до страницы, пересекшей известные").toHaveLength(4);
  });

  it("новых строк нет — одна страница и пустой результат", async () => {
    const { fetch, calls } = pager(db);

    const fresh = await fetchNewRows(fetch, { ts: 20, id: 20 }, 5, 100);

    expect(fresh).toEqual([]);
    expect(calls, "пересечение найдено на первой странице").toHaveLength(1);
  });

  it("без известных строк (первая загрузка) отдаёт одну страницу", async () => {
    const { fetch, calls } = pager(db);

    const fresh = await fetchNewRows(fetch, null, 4, 100);

    expect(fresh.map((r) => r.id)).toEqual([20, 19, 18, 17]);
    expect(calls, "обход вглубь не нужен").toHaveLength(1);
  });

  it("короткая страница означает конец базы", async () => {
    const { fetch, calls } = pager(db);

    const fresh = await fetchNewRows(fetch, null, 50, 100);

    expect(fresh).toHaveLength(20);
    expect(calls, "вторая страница не запрашивается").toHaveLength(1);
  });

  it("лимит сканирования останавливает обход даже без пересечения", async () => {
    const { fetch, calls } = pager(db);

    const fresh = await fetchNewRows(fetch, { ts: 0, id: 0 }, 3, 6);

    expect(fresh, "взяты ровно строки в пределах лимита").toHaveLength(6);
    expect(calls, "больше лимита страниц не читается").toHaveLength(2);
  });
});

describe("collectAllRows", () => {
  it("читает все страницы до короткой, сохраняя порядок и дедуплируя", async () => {
    const db = [row(6), row(5), row(4), row(3), row(2), row(1)];
    const { fetch } = pager(db);
    // Страница 2 намеренно дублирует хвост страницы 1.
    let page = 0;
    const flakyFetch: LogPageFetch = (cursor, limit) => {
      page += 1;
      if (page === 2) {
        return Promise.resolve([row(4), row(3), row(2)]);
      }
      return fetch(cursor, limit);
    };

    const rows = await collectAllRows(flakyFetch, 3, 100);

    expect(rows.map((r) => r.id)).toEqual([6, 5, 4, 3, 2, 1]);
  });

  it("останавливается на короткой странице", async () => {
    const db = [row(4), row(3), row(2), row(1)];
    let pages = 0;
    const countingFetch: LogPageFetch = (cursor, limit) => {
      pages += 1;
      return pager(db).fetch(cursor, limit);
    };

    const rows = await collectAllRows(countingFetch, 3, 100);

    expect(rows).toHaveLength(4);
    expect(pages, "короткая вторая страница — стоп, без третьего запроса").toBe(2);
  });

  it("лимит строк ограничивает выгрузку", async () => {
    const db = Array.from({ length: 10 }, (_, i) => row(i + 1));
    const { fetch } = pager(db);

    const rows = await collectAllRows(fetch, 3, 4);

    expect(rows.map((r) => r.id), "свежие, не старые").toEqual([10, 9, 8, 7]);
  });

  it("пустая база — пустой список без лишних запросов", async () => {
    const { fetch, calls } = pager([]);

    const rows = await collectAllRows(fetch, 10, 100);

    expect(rows).toEqual([]);
    expect(calls).toHaveLength(1);
  });
});
