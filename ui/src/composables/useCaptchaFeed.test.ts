// Лента CAPTCHA на Dashboard: опрос событий, всплывающие уведомления о
// НОВЫХ событиях (без дублей на повторных тиках) и производная подсветка
// воркеров с нерешённой капчей.
//
// API и opener подменяются целиком: здесь важен порядок вызовов, состояние
// ref'ов и то, как фильтруются уже показанные события.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CAPTCHA_FEED_POLL_MS,
  createCaptchaFeed,
  type CaptchaFeedOptions,
} from "./useCaptchaFeed";
import {
  CAPTCHA_FEED_LIMIT,
  CAPTCHA_UNSOLVED_WINDOW_SECONDS,
  type CaptchaEvent,
} from "../lib/captcha";
import type { DbApi } from "../lib/dbApi";

const NOW = 1_760_000_000;

function event(
  id: number,
  overrides: Partial<CaptchaEvent> = {},
): CaptchaEvent {
  return {
    ts: NOW - 5,
    browser_id: "br-1",
    proxy_id: null,
    page_url: null,
    sitekey: null,
    screenshot_path: null,
    solved: false,
    solver: null,
    elapsed_ms: null,
    ...overrides,
  };
}

/** Фейк читалки: список живёт в state, ошибка — по желанию. */
function fakeApi(rows: CaptchaEvent[] = []) {
  const state = {
    rows: rows.map((row) => ({ ...row })),
    error: null as Error | null,
  };
  const api = {
    listCaptchaEvents: vi.fn(async (_limit: number) => {
      if (state.error) throw state.error;
      return state.rows.map((row) => ({ ...row }));
    }),
  };
  return { api, state };
}

function options(extra: Partial<CaptchaFeedOptions> = {}): CaptchaFeedOptions {
  return { isReady: () => true, now: () => NOW, pollMs: 1000, ...extra };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("createCaptchaFeed: опрос событий", () => {
  it("интервал живёт в диапазоне 3–5 с — БД не долбят", () => {
    expect(CAPTCHA_FEED_POLL_MS).toBeGreaterThanOrEqual(3000);
    expect(CAPTCHA_FEED_POLL_MS).toBeLessThanOrEqual(5000);
  });

  it("start читает события и обновляет их раз в интервал, stop гасит тики", async () => {
    const fake = fakeApi([event(1)]);
    const feed = createCaptchaFeed(fake.api as Pick<DbApi, "listCaptchaEvents">, options());

    await feed.start();

    expect(fake.api.listCaptchaEvents).toHaveBeenCalledTimes(1);
    expect(fake.api.listCaptchaEvents).toHaveBeenCalledWith(CAPTCHA_FEED_LIMIT);
    expect(feed.events.value.map((row) => row.id)).toEqual([1]);
    expect(feed.error.value).toBeNull();

    await vi.advanceTimersByTimeAsync(1000);
    expect(fake.api.listCaptchaEvents).toHaveBeenCalledTimes(2);

    feed.stop();
    await vi.advanceTimersByTimeAsync(3000);
    expect(fake.api.listCaptchaEvents, "после stop тики не уходят").toHaveBeenCalledTimes(2);
  });

  it("без открытой БД тики не ходят в читалку и не пугают ошибкой", async () => {
    const fake = fakeApi([event(1)]);
    let ready = false;
    const feed = createCaptchaFeed(
      fake.api as Pick<DbApi, "listCaptchaEvents">,
      options({ isReady: () => ready }),
    );

    await feed.start();
    await vi.advanceTimersByTimeAsync(4000);

    expect(fake.api.listCaptchaEvents).not.toHaveBeenCalled();
    expect(feed.error.value).toBeNull();

    ready = true;
    await feed.tick();

    expect(fake.api.listCaptchaEvents).toHaveBeenCalledTimes(1);
    expect(feed.events.value).toHaveLength(1);
    feed.stop();
  });

  it("ошибка чтения видна, следующий успешный тик её снимает", async () => {
    const fake = fakeApi([event(1)]);
    const feed = createCaptchaFeed(
      fake.api as Pick<DbApi, "listCaptchaEvents">,
      options(),
    );
    fake.state.error = new Error("Ошибка чтения из базы: SQLITE_BUSY");

    await feed.start();
    expect(feed.error.value).toContain("SQLITE_BUSY");
    expect(feed.events.value).toEqual([]);

    fake.state.error = null;
    await feed.tick();

    expect(feed.error.value).toBeNull();
    expect(feed.events.value).toHaveLength(1);
    feed.stop();
  });

  it("параллельный тик возвращает тот же промис, а не новый запрос", async () => {
    const fake = fakeApi();
    let release!: (rows: CaptchaEvent[]) => void;
    fake.api.listCaptchaEvents = vi.fn(
      () => new Promise<CaptchaEvent[]>((resolve) => (release = resolve)),
    );
    const feed = createCaptchaFeed(
      fake.api as Pick<DbApi, "listCaptchaEvents">,
      options(),
    );

    const first = feed.tick();
    const second = feed.tick();
    expect(fake.api.listCaptchaEvents).toHaveBeenCalledTimes(1);

    release([]);
    await Promise.all([first, second]);
    expect(fake.api.listCaptchaEvents).toHaveBeenCalledTimes(1);
  });
});

describe("createCaptchaFeed: уведомления без дублей", () => {
  it("первый тик — базовая линия: уже накопленные события не всплывают", async () => {
    const fake = fakeApi([event(1), event(2)]);
    const feed = createCaptchaFeed(
      fake.api as Pick<DbApi, "listCaptchaEvents">,
      options(),
    );

    await feed.start();
    expect(feed.events.value).toHaveLength(2);
    expect(feed.notices.value, "история не должна сыпать уведомлениями").toEqual([]);
    expect(feed.notice.value).toBeNull();

    // Повторные тики тех же событий — тоже без уведомлений.
    await vi.advanceTimersByTimeAsync(3000);
    expect(feed.notices.value).toEqual([]);
    feed.stop();
  });

  it("новое событие даёт ровно одно уведомление и не дублируется на тиках", async () => {
    const fake = fakeApi([event(1)]);
    const feed = createCaptchaFeed(
      fake.api as Pick<DbApi, "listCaptchaEvents">,
      options(),
    );
    await feed.start();

    // Свежее событие появилось в БД между тиками.
    fake.state.rows = [event(2, { browser_id: "br-2" }), event(1)];
    await vi.advanceTimersByTimeAsync(1000);
    expect(feed.notices.value.map((notice) => notice.id)).toEqual([2]);
    expect(feed.notice.value).toMatchObject({ id: 2, browserId: "br-2" });

    // Три повторных тика с тем же списком — ни одной копии.
    await vi.advanceTimersByTimeAsync(3000);
    expect(feed.notices.value.map((notice) => notice.id)).toEqual([2]);

    // Ещё одно новое событие — второе уведомление в очередь, не замена.
    fake.state.rows = [event(3, { browser_id: "br-3" }), event(2), event(1)];
    await vi.advanceTimersByTimeAsync(1000);
    expect(feed.notices.value.map((notice) => notice.id)).toEqual([2, 3]);
    feed.stop();
  });

  it("событие, ушедшее из ленты и вернувшееся, повторно не всплывает", async () => {
    const fake = fakeApi([event(1)]);
    const feed = createCaptchaFeed(
      fake.api as Pick<DbApi, "listCaptchaEvents">,
      options(),
    );
    await feed.start();

    fake.state.rows = [event(2)];
    await vi.advanceTimersByTimeAsync(1000);
    expect(feed.notices.value.map((notice) => notice.id)).toEqual([2]);

    // Старая строка выпала из лимита и вернулась — id уже показан.
    fake.state.rows = [event(2), event(1)];
    await vi.advanceTimersByTimeAsync(1000);
    expect(feed.notices.value.map((notice) => notice.id)).toEqual([2]);
    feed.stop();
  });

  it("dismiss снимает текущее уведомление и открывает следующее из очереди", async () => {
    const fake = fakeApi([event(1)]);
    const feed = createCaptchaFeed(
      fake.api as Pick<DbApi, "listCaptchaEvents">,
      options(),
    );
    await feed.start();

    fake.state.rows = [event(3), event(2), event(1)];
    await vi.advanceTimersByTimeAsync(1000);
    expect(feed.notice.value?.id).toBe(3);

    feed.dismiss();
    expect(feed.notice.value?.id).toBe(2);

    feed.dismiss();
    expect(feed.notice.value).toBeNull();
    expect(feed.notices.value).toEqual([]);

    // Пустая очередь — no-op, а не ошибка.
    feed.dismiss();
    expect(feed.notice.value).toBeNull();
    feed.stop();
  });

  it("провал первой загрузки не считается базовой линией: уведомления не теряются", async () => {
    const fake = fakeApi([event(1)]);
    const feed = createCaptchaFeed(
      fake.api as Pick<DbApi, "listCaptchaEvents">,
      options(),
    );
    fake.state.error = new Error("База данных не открыта");

    await feed.start();
    expect(feed.notices.value).toEqual([]);

    // Первая удачная загрузка — базовая линия, а не всплеск уведомлений.
    fake.state.error = null;
    await feed.tick();
    expect(feed.notices.value).toEqual([]);

    fake.state.rows = [event(2), event(1)];
    await vi.advanceTimersByTimeAsync(1000);
    expect(feed.notices.value.map((notice) => notice.id)).toEqual([2]);
    feed.stop();
  });
});

describe("createCaptchaFeed: подсветка и скриншот", () => {
  it("alertIds — воркеры с нерешённым событием в окне 30 минут", async () => {
    const fake = fakeApi([
      event(1, { browser_id: "br-1", ts: NOW - 60, solved: false }),
      event(2, { browser_id: "br-2", ts: NOW - 60, solved: true }),
      event(3, {
        browser_id: "br-3",
        ts: NOW - CAPTCHA_UNSOLVED_WINDOW_SECONDS - 5,
        solved: false,
      }),
    ]);
    const feed = createCaptchaFeed(
      fake.api as Pick<DbApi, "listCaptchaEvents">,
      options(),
    );

    await feed.start();

    expect(feed.alertIds.value).toEqual(["br-1"]);
    feed.stop();
  });

  it("подсветка — производная от уже загруженных событий: читалка зовётся один раз", async () => {
    const fake = fakeApi([event(1, { browser_id: "br-1" })]);
    const feed = createCaptchaFeed(
      fake.api as Pick<DbApi, "listCaptchaEvents">,
      options(),
    );

    await feed.start();
    expect(feed.alertIds.value).toEqual(["br-1"]);
    expect(fake.api.listCaptchaEvents).toHaveBeenCalledTimes(1);
    feed.stop();
  });

  it("openShot зовёт opener с путём и запоминает отказ", async () => {
    const fake = fakeApi();
    const opener = vi.fn(async () => {});
    const feed = createCaptchaFeed(
      fake.api as Pick<DbApi, "listCaptchaEvents">,
      options({ opener }),
    );

    await feed.openShot("/tmp/shots/br-1.png");

    expect(opener).toHaveBeenCalledWith("/tmp/shots/br-1.png");
    expect(feed.screenshotError.value).toBeNull();

    opener.mockRejectedValueOnce(new Error("нет программы по умолчанию"));
    await feed.openShot("/tmp/shots/gone.png");
    expect(feed.screenshotError.value).toContain("нет программы");

    // Успех чистит прошлую ошибку.
    await feed.openShot("/tmp/shots/br-2.png");
    expect(feed.screenshotError.value).toBeNull();
    feed.stop();
  });

  it("путь к скриншоту отсутствует — opener не дёргается", async () => {
    const fake = fakeApi();
    const opener = vi.fn(async () => {});
    const feed = createCaptchaFeed(
      fake.api as Pick<DbApi, "listCaptchaEvents">,
      options({ opener }),
    );

    await feed.openShot(null);

    expect(opener).not.toHaveBeenCalled();
    expect(feed.screenshotError.value).toBeTruthy();
    feed.stop();
  });
});
