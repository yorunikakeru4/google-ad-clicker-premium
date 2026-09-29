// Клиентская модель ленты CAPTCHA: разметка времени/страницы/длительности,
// производная подсветка воркеров и открытие скриншота через opener.
//
// opener подменяется аргументом: тест не должен трогать ни Tauri-плагин, ни
// файловую систему — проверяется только «что ушло в opener и что вернулось».

import { describe, expect, it, vi } from "vitest";
import {
  CAPTCHA_UNSOLVED_WINDOW_SECONDS,
  captchaNoticeText,
  formatElapsed,
  formatEventTime,
  openScreenshot,
  pageUrlLabel,
  toNotice,
  unsolvedBrowserIds,
  type CaptchaEvent,
} from "./captcha";

function event(overrides: Partial<CaptchaEvent> & { id: number }): CaptchaEvent {
  return {
    ts: 1_760_000_000,
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

describe("formatEventTime и formatElapsed", () => {
  it("время события — локальные HH:MM:SS из эпохи в секундах", () => {
    const ts = new Date(2026, 8, 30, 7, 5, 9).getTime() / 1000;
    expect(formatEventTime(ts)).toBe("07:05:09");
  });

  it("длительность: миллисекунды, секунды и отсутствие значения", () => {
    expect(formatElapsed(420)).toBe("420 мс");
    expect(formatElapsed(4200)).toBe("4.2 с");
    expect(formatElapsed(0)).toBe("0 мс");
    expect(formatElapsed(null)).toBe("—");
    expect(formatElapsed(undefined)).toBe("—");
  });
});

describe("pageUrlLabel", () => {
  it("без страницы — тире, а не пустая ячейка", () => {
    expect(pageUrlLabel(null)).toBe("—");
    expect(pageUrlLabel("")).toBe("—");
  });

  it("короткая ссылка остаётся целиком", () => {
    expect(pageUrlLabel("https://a.test/x")).toBe("https://a.test/x");
  });

  it("длинная ссылка усекается с многоточием и не растет за предел", () => {
    const url = `https://www.google.com/search?q=${"x".repeat(200)}`;
    const label = pageUrlLabel(url, 40);

    expect(label.length).toBeLessThanOrEqual(40);
    expect(label.endsWith("…")).toBe(true);
    expect(url.startsWith(label.slice(0, -1))).toBe(true);
  });

  it("ссылка ровно в предел не усекается", () => {
    const url = "y".repeat(40);
    expect(pageUrlLabel(url, 40)).toBe(url);
  });
});

describe("unsolvedBrowserIds: подсветка воркеров", () => {
  const NOW = 1_760_000_000;
  const FRESH = NOW - CAPTCHA_UNSOLVED_WINDOW_SECONDS;
  const STALE = NOW - CAPTCHA_UNSOLVED_WINDOW_SECONDS - 1;

  it("только нерешённые события в окне 30 минут", () => {
    const ids = unsolvedBrowserIds(
      [
        event({ id: 1, browser_id: "br-1", ts: FRESH, solved: false }),
        event({ id: 2, browser_id: "br-2", ts: FRESH, solved: true }),
        event({ id: 3, browser_id: "br-3", ts: STALE, solved: false }),
        event({ id: 4, browser_id: "br-4", ts: NOW, solved: false }),
      ],
      NOW,
    );

    expect(ids).toEqual(["br-1", "br-4"]);
  });

  it("граница окна включается: событие ровно в 30 минут назад подсвечивает", () => {
    expect(
      unsolvedBrowserIds([event({ id: 1, browser_id: "br-1", ts: FRESH })], NOW),
    ).toEqual(["br-1"]);
  });

  it("воркер без browser_id и повторы схлопываются", () => {
    const ids = unsolvedBrowserIds(
      [
        event({ id: 1, browser_id: null, ts: NOW }),
        event({ id: 2, browser_id: "br-1", ts: NOW }),
        event({ id: 3, browser_id: "br-1", ts: NOW - 10 }),
      ],
      NOW,
    );

    expect(ids).toEqual(["br-1"]);
  });

  it("пустая лента — пустая подсветка", () => {
    expect(unsolvedBrowserIds([], NOW)).toEqual([]);
  });
});

describe("уведомление о событии", () => {
  it("текст несёт воркера и исход решения", () => {
    expect(
      captchaNoticeText(toNotice(event({ id: 1, browser_id: "br-7", solved: false }))),
    ).toBe("CAPTCHA · br-7 — не решена");
    expect(
      captchaNoticeText(toNotice(event({ id: 2, browser_id: "br-7", solved: true }))),
    ).toBe("CAPTCHA · br-7 — решена");
  });

  it("событие без воркера не роняет текст в «undefined»", () => {
    expect(
      captchaNoticeText(toNotice(event({ id: 3, browser_id: null, solved: false }))),
    ).toBe("CAPTCHA · неизвестный воркер — не решена");
  });

  it("ключ уведомления — id, чтобы повторный тик не плодил копии", () => {
    const row = event({ id: 42, ts: 100, browser_id: "br-1", solved: true });
    expect(toNotice(row)).toEqual({
      id: 42,
      ts: 100,
      browserId: "br-1",
      solved: true,
    });
  });
});

describe("openScreenshot", () => {
  it("путь уходит в opener как есть, успех — без ошибки", async () => {
    const opener = vi.fn(async () => {});

    const error = await openScreenshot("/tmp/shots/br-1.png", opener);

    expect(error).toBeNull();
    expect(opener).toHaveBeenCalledWith("/tmp/shots/br-1.png");
  });

  it("без сохранённого скриншота opener не вызывается, ошибка объясняет почему", async () => {
    const opener = vi.fn(async () => {});

    for (const path of [null, undefined, ""]) {
      const error = await openScreenshot(path, opener);
      expect(error).toBeTruthy();
    }

    expect(opener).not.toHaveBeenCalled();
  });

  it("отказ opener не уходит в консоль молча — возвращается текст", async () => {
    const opener = vi.fn(async () => {
      throw new Error("нет программы по умолчанию");
    });

    expect(await openScreenshot("/tmp/a.png", opener)).toContain(
      "нет программы по умолчанию",
    );
  });
});
