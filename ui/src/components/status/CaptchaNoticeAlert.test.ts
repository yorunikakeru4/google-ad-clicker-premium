// Рендер всплывающего уведомления о событии CAPTCHA в node: текст с
// воркером и исходом решения есть только когда уведомление реально есть —
// закрытое не должно висеть в DOM.

import { describe, expect, it } from "vitest";
import { createSSRApp, h, type Component } from "vue";
import { renderToString } from "vue/server-renderer";
import vuetify from "../../plugins/vuetify";
import CaptchaNoticeAlert from "./CaptchaNoticeAlert.vue";
import type { CaptchaNotice } from "../../lib/captcha";

async function render(
  component: Component,
  props: Record<string, unknown>,
): Promise<string> {
  const app = createSSRApp({ render: () => h(component, props) });
  app.use(vuetify);
  return renderToString(app);
}

function notice(overrides: Partial<CaptchaNotice> = {}): CaptchaNotice {
  return {
    id: 7,
    ts: 1_760_000_000,
    browserId: "br-7",
    solved: false,
    ...overrides,
  };
}

describe("CaptchaNoticeAlert", () => {
  it("уведомление показывает воркера и исход решения", async () => {
    const html = await render(CaptchaNoticeAlert, {
      notice: notice(),
    });

    expect(html).toContain('data-test="captcha-notice"');
    expect(html).toContain('data-test="captcha-notice-text"');
    expect(html).toContain("CAPTCHA · br-7 — не решена");
    expect(html).toContain('data-test="captcha-notice-close"');
    expect(html).toContain("Закрыть");
  });

  it("решённая капча подписывается иначе, а не той же строкой", async () => {
    const html = await render(CaptchaNoticeAlert, {
      notice: notice({ solved: true }),
    });

    expect(html).toContain("CAPTCHA · br-7 — решена");
    expect(html).not.toContain("не решена");
  });

  it("уведомления нет — всплывашки нет в разметке вовсе", async () => {
    const html = await render(CaptchaNoticeAlert, { notice: null });

    expect(html).not.toContain("captcha-notice");
    expect(html).not.toContain("CAPTCHA ·");
  });
});
