// Рендер карточки ленты CAPTCHA в node: строки из событий, время/страница/
// исход решения, кнопка скриншота и состояния без данных. Рендер без
// браузера — как в views/screens.test.ts.

import { describe, expect, it } from "vitest";
import { createSSRApp, h, type Component } from "vue";
import { renderToString } from "vue/server-renderer";
import vuetify from "../../plugins/vuetify";
import CaptchaFeedCard from "./CaptchaFeedCard.vue";
import type { CaptchaEvent } from "../../lib/captcha";

async function render(
  component: Component,
  props: Record<string, unknown>,
): Promise<string> {
  const app = createSSRApp({ render: () => h(component, props) });
  app.use(vuetify);
  return renderToString(app);
}

const AT = new Date(2026, 8, 30, 7, 5, 9).getTime() / 1000;

function event(
  id: number,
  overrides: Partial<CaptchaEvent> = {},
): CaptchaEvent {
  return {
    id,
    ts: AT,
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

/** Кусок строки события: от data-test до конца строки таблицы. */
function rowOf(html: string, id: number): string {
  const at = html.indexOf(`data-test="captcha-event-${id}"`);
  if (at === -1) return "";
  const end = html.indexOf("</tr>", at);
  return end === -1 ? html.slice(at) : html.slice(at, end);
}

/**
 * Видимый текст ячейки: от `>` с data-test до ближайшего closeTag — так
 * атрибуты (title с полным адресом) не попадают в «текст» строки.
 */
function textOf(html: string, test: string, closeTag = "</span>"): string {
  const at = html.indexOf(`data-test="${test}"`);
  if (at === -1) return "";
  const open = html.indexOf(">", at);
  if (open === -1) return "";
  const start = open + 1;
  const end = html.indexOf(closeTag, start);
  const chunk = end === -1 ? html.slice(start) : html.slice(start, end);
  return chunk
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function card(events: CaptchaEvent[], extra: Record<string, unknown> = {}) {
  return render(CaptchaFeedCard, { events, ...extra });
}

describe("CaptchaFeedCard: строки событий", () => {
  it("каждая строка несёт время, воркера, страницу, исход и длительность", async () => {
    const html = await card([
      event(1, {
        browser_id: "br-1",
        page_url: "https://www.google.com/search?q=ads",
        solved: false,
        elapsed_ms: 4200,
      }),
      event(2, {
        browser_id: null,
        solved: true,
        elapsed_ms: 420,
        screenshot_path: "/tmp/engine/screenshots/br-2.png",
      }),
    ]);

    expect(html).toContain('data-test="captcha-feed"');
    expect(html).toContain('data-test="captcha-events"');

    const first = rowOf(html, 1);
    expect(first).toContain("07:05:09");
    expect(first).toContain("br-1");
    expect(first).toContain("не решена");
    expect(first).toContain("4.2 с");

    const second = rowOf(html, 2);
    expect(second).toContain("решена");
    expect(second, "воркер неизвестен — тире, а не undefined").toContain("—");
    expect(second).toContain("420 мс");
  });

  it("длинная страница усекается, полный адрес остаётся в title", async () => {
    const url = "https://www.google.com/search?q=captcha+threshold+policy+demo";
    const html = await card([event(1, { page_url: url })]);

    // Полный адрес доступен глазами через title ячейки...
    expect(html).toContain(`title="${url}"`);

    // ...а видимый текст — усечённая подпись, а не вся строка ссылки.
    const label = textOf(html, "captcha-page-1");
    expect(label).not.toBe(url);
    expect(label.endsWith("…")).toBe(true);
    expect(label.length).toBeLessThanOrEqual(60);
    expect(html).toContain('data-test="captcha-browser-1"');
  });

  it("без страницы в ячейке тире, а не пустая строка", async () => {
    const html = await card([event(1, { page_url: null })]);

    expect(rowOf(html, 1)).toContain("—");
  });

  it("кнопка скриншота активна только при сохранённом пути", async () => {
    const html = await card([
      event(1, { screenshot_path: "/tmp/engine/screenshots/br-1.png" }),
      event(2, { browser_id: "br-2", screenshot_path: null }),
    ]);

    const withShot = rowOf(html, 1);
    const withoutShot = rowOf(html, 2);
    expect(withShot).toContain('data-test="captcha-shot-1"');
    expect(withShot).not.toContain("disabled");
    expect(withoutShot).toContain('data-test="captcha-shot-2"');
    expect(withoutShot).toContain("disabled");
  });
});

describe("CaptchaFeedCard: состояния", () => {
  it("первая загрузка — индикатор, а не «событий ещё не было»", async () => {
    const html = await card([], { loading: true });

    expect(html).toContain('data-test="captcha-feed-loading"');
    expect(html).not.toContain("captcha-feed-empty");
  });

  it("событий нет — пустое состояние, а не пустая таблица", async () => {
    const html = await card([]);

    expect(html).toContain('data-test="captcha-feed-empty"');
    expect(html).not.toContain("captcha-events");
    expect(html).not.toContain("captcha-shot-");
  });

  it("ошибка чтения показывается в карточке и не глушится пустым списком", async () => {
    const html = await card([], { error: "Ошибка чтения из базы: SQLITE_BUSY" });

    expect(html).toContain('data-test="captcha-feed-error"');
    expect(html).toContain("SQLITE_BUSY");
    expect(html).toContain('data-test="captcha-feed-empty"');
  });

  it("отказ opener виден отдельной строкой, а не теряется", async () => {
    const html = await card([], {
      screenshotError: "Скриншот для этого события не сохранён",
    });

    expect(html).toContain('data-test="captcha-shot-error"');
    expect(html).toContain("не сохранён");
  });
});
