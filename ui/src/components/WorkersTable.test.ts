// Рендер таблицы воркеров (план §5, фаза 4) в node: колонки и значения из
// снимка /state, стабильный порядок строк, пустое состояние, офлайн демона
// и строки с pid = null. Рендер без браузера — как в views/screens.test.ts.

import { describe, expect, it } from "vitest";
import { createSSRApp, h, type Component } from "vue";
import { renderToString } from "vue/server-renderer";
import vuetify from "../plugins/vuetify";
import WorkersTable from "./WorkersTable.vue";
import type { PollPhase } from "../lib/poll";
import type { StateSnapshot, WorkerRow } from "../lib/types";

async function render(
  component: Component,
  props: Record<string, unknown>,
): Promise<string> {
  const app = createSSRApp({ render: () => h(component, props) });
  app.use(vuetify);
  return renderToString(app);
}

/**
 * Текст от data-test до ближайшего closeTag, без тегов и лишних пробелов.
 * Так проверяются значения ячеек, не ломаясь на вложенных чипах и переводах.
 */
function textOf(html: string, test: string, closeTag = "</td>"): string {
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

/** Снимок /state; аптайм в тестах меряется от updated_at до started_at. */
const SNAPSHOT_AT = 1_000_010;

function worker(overrides: Partial<WorkerRow> = {}): WorkerRow {
  return {
    browser_id: "br-1",
    pid: 4242,
    status: "running",
    restart_count: 1,
    started_at: 1_000_000,
    heartbeat_at: 1_000_005,
    last_error: null,
    ...overrides,
  };
}

function snapshot(workers: WorkerRow[]): StateSnapshot {
  return {
    state: "running",
    paused: false,
    workers,
    worker_count: workers.length,
    alive_count: workers.filter((row) => row.status === "running").length,
    updated_at: SNAPSHOT_AT,
  };
}

function table(props: {
  phase: PollPhase;
  snapshot: StateSnapshot | null;
  captchaAlertIds?: string[];
}): Promise<string> {
  return render(WorkersTable, props);
}

describe("WorkersTable: строки из /state", () => {
  it("колонки и значения приходят из снимка, аптайм — от started_at до updated_at", async () => {
    const html = await table({
      phase: "online",
      snapshot: snapshot([
        worker(),
        worker({
          browser_id: "br-2",
          pid: null,
          status: "backoff",
          restart_count: 3,
          last_error: "chromium crashed",
        }),
      ]),
    });

    expect(html).toContain('data-test="workers-table"');
    for (const header of [
      "browser_id",
      "статус",
      "PID",
      "uptime",
      "рестарты",
      "последняя ошибка",
    ]) {
      expect(html).toContain(header);
    }

    const first = textOf(html, "worker-row-br-1", "</tr>");
    expect(first).toContain("br-1");
    expect(textOf(html, "worker-status-br-1")).toBe("running");
    expect(textOf(html, "worker-pid-br-1")).toBe("4242");
    expect(textOf(html, "worker-uptime-br-1")).toBe("00:00:10");
    expect(textOf(html, "worker-restarts-br-1")).toBe("1");
    expect(textOf(html, "worker-error-br-1")).toBe("—");

    expect(textOf(html, "worker-pid-br-2")).toBe("—");
    expect(textOf(html, "worker-restarts-br-2")).toBe("3");
    expect(textOf(html, "worker-error-br-2")).toBe("chromium crashed");
  });

  it("порядок строк — как приходит из /state, без пересортировки на тике", async () => {
    const html = await table({
      phase: "online",
      snapshot: snapshot([
        worker({ browser_id: "br-7" }),
        worker({ browser_id: "br-2" }),
      ]),
    });

    expect(html.indexOf('data-test="worker-row-br-7"')).toBeLessThan(
      html.indexOf('data-test="worker-row-br-2"'),
    );
  });

  it("строка с pid, started_at и last_error = null остаётся целой: тире вместо NaN", async () => {
    const html = await table({
      phase: "online",
      snapshot: snapshot([worker({ pid: null, started_at: null, last_error: null })]),
    });

    const row = textOf(html, "worker-row-br-1", "</tr>");
    expect(row).toContain("br-1");
    expect(textOf(html, "worker-pid-br-1")).toBe("—");
    expect(textOf(html, "worker-uptime-br-1")).toBe("—");
    expect(textOf(html, "worker-error-br-1")).toBe("—");
    expect(html).not.toContain("NaN");
  });
});

describe("WorkersTable: подсветка CAPTCHA", () => {
  it("воркер из captchaAlertIds получает warning-индикатор, остальные — нет", async () => {
    const html = await table({
      phase: "online",
      snapshot: snapshot([worker(), worker({ browser_id: "br-2" })]),
      captchaAlertIds: ["br-1"],
    });

    expect(html).toContain('data-test="worker-captcha-br-1"');
    expect(html).toContain("Нерешённая CAPTCHA");
    expect(html).not.toContain('data-test="worker-captcha-br-2"');
  });

  it("без prop подсветки ни одна строка не помечена", async () => {
    const html = await table({
      phase: "online",
      snapshot: snapshot([worker()]),
    });

    expect(html).not.toContain("worker-captcha-");
  });

  it("событие чужого воркера не подсвечивает лишние строки", async () => {
    const html = await table({
      phase: "online",
      snapshot: snapshot([worker(), worker({ browser_id: "br-2" })]),
      captchaAlertIds: ["br-9"],
    });

    expect(html).not.toContain("worker-captcha-");
  });
});

describe("WorkersTable: состояния без строк", () => {
  it("демон онлайн, но пул пуст — «воркеров нет», не ошибка", async () => {
    const html = await table({ phase: "online", snapshot: snapshot([]) });

    expect(html).toContain('data-test="workers-empty"');
    expect(html).toContain("Воркеров нет");
    expect(html).not.toContain("workers-table");
  });

  it("демон офлайн — дружелюбный текст и никакого NaN в аптайме", async () => {
    const html = await table({ phase: "offline", snapshot: null });

    expect(html).toContain('data-test="workers-empty"');
    expect(html).toContain("Демон недоступен");
    expect(html).not.toContain("workers-table");
    expect(html).not.toContain("NaN");
  });

  it("первый ответ ещё не пришёл — ожидание, а не «воркеров нет»", async () => {
    // «в полёте» теперь не фаза: пока тик идёт, phase остаётся idle —
    // таблица показывает ожидание, а не мигает между состояниями.
    const html = await table({ phase: "idle", snapshot: null });

    expect(html).toContain('data-test="workers-empty"');
    expect(html).toContain("Ожидание первого ответа демона");
    expect(html).not.toContain("Воркеров нет");
    expect(html).not.toContain("NaN");
  });
});
