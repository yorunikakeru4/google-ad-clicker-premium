// Смоук-рендер экранов и компонентов шаблона в node (vue/server-renderer).
//
// vue-tsc не ругается на несуществующий проп в вызове компонента — молча
// превращает его в DOM-атрибут. Эти тесты ловят именно такой разрыв:
// пропсы/слоты/тест-хуки интегрированных экранов должны попасть в разметку.
//
// Рендер идёт без браузера и без mounted: циклы опроса (useDashboard,
// useLogs) стартуют в onMounted, поэтому здесь экран видит только начальные
// значения — «—», «нет данных» и пустую выборку.

import { beforeAll, describe, expect, it } from "vitest";
import { createSSRApp, h, type Component } from "vue";
import { renderToString } from "vue/server-renderer";
import vuetify from "../plugins/vuetify";
import FilterBar from "../components/data/FilterBar.vue";
import MetricCard from "../components/data/MetricCard.vue";
import DashboardView from "./DashboardView.vue";
import LogsView from "./LogsView.vue";

// useLogs читает localStorage в момент создания — до первого рендера Logs.
const storage = new Map<string, string>();

beforeAll(() => {
  (globalThis as { window?: unknown }).window = {
    localStorage: {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => void storage.set(key, value),
    },
  };
});

async function render(
  component: Component,
  props: Record<string, unknown> = {},
): Promise<string> {
  const app = createSSRApp({ render: () => h(component, props) });
  app.use(vuetify);
  return renderToString(app);
}

describe("компоненты шаблона", () => {
  it("MetricCard отдаёт hint, число и статус в свою разметку", async () => {
    const html = await render(MetricCard, {
      label: "Запросы/час",
      hint: "окно 24 часа",
      value: "77",
      valueTest: "requests-total",
      status: "error",
      statusLabel: "≥50/час: не выполнено (77)",
      statusTest: "requests-claim",
    });

    expect(html).toContain("Запросы/час");
    expect(html).toContain("· окно 24 часа");
    expect(html).toContain('data-test="requests-total"');
    expect(html).toContain('data-test="requests-claim"');
    expect(html).toContain("≥50/час: не выполнено (77)");
    expect(html).toContain("text-error");
  });

  it("FilterBar показывает чипы и «Сбросить» только при активных фильтрах", async () => {
    const active = await render(FilterBar, {
      chips: [{ key: "level", label: "Уровень", value: "ERROR" }],
    });
    expect(active).toContain("Уровень: ERROR");
    expect(active).toContain('data-test="filter-active"');
    expect(active).toContain('data-test="filter-reset"');

    const idle = await render(FilterBar, {});
    expect(idle).not.toContain("filter-active");
    expect(idle).not.toContain("filter-reset");
  });
});

describe("экраны на шаблоне", () => {
  it("Dashboard: PageLayout, четыре метрики, графики, без панели управления", async () => {
    const html = await render(DashboardView);

    expect(html).toContain("Dashboard");
    // алерты ошибки и загрузки — v-if, в покое их не должно быть
    expect(html).not.toContain("dashboard-error");
    expect(html).not.toContain("dashboard-loading");

    for (const hook of [
      "card-runs",
      "card-uptime",
      "card-requests",
      "card-captcha",
      "runs-total",
      "runs-succeeded",
      "runs-failed",
      "runs-other",
      "runs-last-error",
      "uptime-value",
      "active-workers",
      "requests-total",
      "requests-claim",
      "captcha-value",
      "captcha-claim",
      "chart-Клики/час",
    ]) {
      expect(html).toContain(`data-test="${hook}"`);
    }

    // данных ещё нет: утверждения честно говорят об этом, а не молчат
    expect(html).toContain("нет данных");
    expect(html).toContain("нет данных за окно — н/д");

    // управление демоном — только в AppShell
    expect(html).not.toContain("control-panel");
    expect(html).not.toContain("kill-dialog");
  });

  it("Logs: PageLayout, FilterBar с пятью контролами и тулбар действий", async () => {
    const html = await render(LogsView);

    expect(html).toContain("Logs");
    for (const hook of [
      "filter-level",
      "filter-category",
      "filter-browser",
      "filter-since",
      "filter-until",
      "load-older",
      "export-csv",
      "live-toggle",
      "live-status",
      "logs-count",
      "logs-scroll",
    ]) {
      expect(html).toContain(`data-test="${hook}"`);
    }

    // фильтры по умолчанию пусты — ни чипов, ни кнопки сброса
    expect(html).not.toContain("filter-active");
    expect(html).not.toContain("filter-reset");
    // БД не открыта и строк нет: таблица и empty-state не рисуются
    expect(html).not.toContain("logs-table");
    expect(html).not.toContain("logs-empty");
    expect(html).toContain("0 из");
  });
});
