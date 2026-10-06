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
import SettingField from "../components/forms/SettingField.vue";
import DashboardView from "./DashboardView.vue";
import DiagnosticsView from "./DiagnosticsView.vue";
import DomainsView from "./DomainsView.vue";
import KeyWordsView from "./KeyWordsView.vue";
import LogsView from "./LogsView.vue";
import ProfilesView from "./ProfilesView.vue";
import ProxiesView from "./ProxiesView.vue";
import SettingsView from "./SettingsView.vue";

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

  it("SettingField: контроль по типу, маска и ошибка рядом с полем", async () => {
    const booleanField = await render(SettingField, {
      name: "flag",
      type: "bool",
      modelValue: true,
    });
    expect(booleanField).toContain('type="checkbox"');

    const numberField = await render(SettingField, {
      name: "browser_count",
      type: "int",
      modelValue: 5,
      min: 1,
      max: 8,
    });
    expect(numberField).toContain('type="number"');
    expect(numberField).toContain('min="1"');
    expect(numberField).toContain('max="8"');

    const enumField = await render(SettingField, {
      name: "proxy_transport",
      type: "enum",
      modelValue: "cdp_auth",
      options: [{ title: "CDP-авторизация (по умолчанию)", value: "cdp_auth" }],
    });
    expect(enumField).toContain("CDP-авторизация (по умолчанию)");

    const maskedField = await render(SettingField, {
      name: "proxy",
      type: "string",
      modelValue: "********",
      secret: true,
    });
    expect(maskedField).toContain("********");

    const invalidField = await render(SettingField, {
      name: "click_order",
      type: "int",
      modelValue: 5000,
      error: "значение больше максимума 1000",
    });
    expect(invalidField).toContain("значение больше максимума 1000");
    expect(invalidField).toContain('role="alert"');
  });
});

describe("экраны на шаблоне", () => {
  it("Dashboard: PageLayout, пять метрик, графики, без панели управления", async () => {
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
      "card-uptime-ratio",
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
      "uptime-ratio-value",
      "uptime-claim",
      "captcha-feed",
      "captcha-feed-empty",
      "chart-Клики/час",
    ]) {
      expect(html).toContain(`data-test="${hook}"`);
    }

    // данных ещё нет: утверждения честно говорят об этом, а не молчат
    expect(html).toContain("нет данных");
    expect(html).toContain("нет данных за окно — н/д");

    // Uptime до первой выборки: idle-статус объясняет, почему не 0% и не 100%
    expect(html).toContain("нет данных — замер с первого запуска демона");
    expect(html).toContain("за 24 ч");

    // управление демоном — только в AppShell
    expect(html).not.toContain("control-panel");
    expect(html).not.toContain("kill-dialog");

    // таблица воркеров из /state — секция под метриками и до графиков
    const workersAt = html.indexOf('data-test="workers-card"');
    expect(workersAt).toBeGreaterThan(html.indexOf('data-test="card-captcha"'));
    expect(workersAt).toBeLessThan(html.indexOf('data-test="chart-Клики/час"'));
    // лента CAPTCHA — после воркеров и до графиков: подсветка и события рядом
    const feedAt = html.indexOf('data-test="captcha-feed"');
    expect(feedAt).toBeGreaterThan(workersAt);
    expect(feedAt).toBeLessThan(html.indexOf('data-test="chart-Клики/час"'));
    // ни уведомлений, ни строк событий — их порождает только новый тик
    expect(html).not.toContain("captcha-notice");
    expect(html).not.toContain("captcha-events");
    // до первого ответа демона экран честно ждёт, а не показывает пустой пул
    expect(html).toContain('data-test="workers-empty"');
    expect(html).toContain("Ожидание первого ответа демона");
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
      "export-open",
      "live-toggle",
      "live-status",
      "logs-count",
      "logs-scroll",
      "db-size",
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
    // диалог экспорта закрыт, лимит БД не загружен — не выдумываем значения
    expect(html).not.toContain("export-dialog");
    expect(html).toContain("не загружен");
  });

  it("Proxies: PageLayout, действия и пустое состояние без данных", async () => {
    const html = await render(ProxiesView);

    expect(html).toContain("Proxies");
    for (const hook of [
      "proxies-add",
      "proxies-import",
      "proxies-check",
      "proxies-open-file",
      "proxies-delete-failed",
      "proxies-delete-all",
      "proxies-empty",
    ]) {
      expect(html).toContain(`data-test="${hook}"`);
    }

    // ни ошибок чтения, ни действий, ни диалогов — экран только открылся
    expect(html).not.toContain("proxies-error");
    expect(html).not.toContain("proxies-action-error");
    expect(html).not.toContain("proxies-add-dialog");
    expect(html).not.toContain("proxies-delete-dialog");
    expect(html).not.toContain("proxies-bulk-delete-dialog");
    expect(html).not.toContain("proxies-delete-result");
    expect(html).not.toContain("proxies-check-progress");
    // кнопка батч-удаления есть всегда: без строк со статусом она на нуле
    expect(html).toContain("Удалить с ошибкой");
  });

  it("Profiles: PageLayout, фильтры, массовые действия и пустое состояние", async () => {
    const html = await render(ProfilesView);

    expect(html).toContain("Profiles");
    for (const hook of [
      "profiles-add",
      "profiles-import",
      "profiles-assign",
      "profiles-unassign",
      "profiles-filter-status",
      "profiles-filter-name",
      "profiles-empty",
    ]) {
      expect(html).toContain(`data-test="${hook}"`);
    }

    // ни ошибок, ни итогов, ни открытых диалогов — экран только открылся
    expect(html).not.toContain("profiles-action-error");
    expect(html).not.toContain("profiles-add-dialog");
    expect(html).not.toContain("profiles-import-dialog");
    expect(html).not.toContain("profiles-assign-dialog");
    expect(html).not.toContain("profiles-delete-dialog");
    expect(html).not.toContain("profiles-unassign-dialog");
    // без данных таблица не показывает статусы и прочерки строк
    expect(html).not.toContain("profiles-status-free");
  });

  it("Diagnostics: тулбар со сбором, пустое состояние без снимков", async () => {
    const html = await render(DiagnosticsView);

    expect(html).toContain("Diagnostics");
    for (const hook of ["diagnostics-collect-all", "diagnostics-empty"]) {
      expect(html).toContain(`data-test="${hook}"`);
    }

    // снимков нет: ни карточек, ни ошибок, ни итогов сбора
    expect(html).not.toContain("diagnostics-card-");
    expect(html).not.toContain("diagnostics-read-error");
    expect(html).not.toContain("diagnostics-action-error");
    expect(html).not.toContain("diagnostics-collect-result");
  });

  it("Settings: тулбар, три секции, поля из схемы и секция очистки", async () => {
    const html = await render(SettingsView);

    expect(html).toContain("Settings");
    for (const hook of [
      "settings-save",
      "settings-reset",
      "settings-section-paths",
      "settings-section-webdriver",
      "settings-section-behavior",
      "settings-cleanup",
      "cleanup-last",
      "cleanup-next",
      "cleanup-run",
      "cleanup-preview",
      "cleanup-refresh",
    ]) {
      expect(html).toContain(`data-test="${hook}"`);
    }

    // до первого ответа демона: ни ошибок, ни успеха, ни пометки «изменено»
    expect(html).not.toContain("settings-load-error");
    expect(html).not.toContain("settings-save-error");
    expect(html).not.toContain("settings-success");
    expect(html).not.toContain("settings-dirty");

    // статус очистки ещё не читался: заглушки, а не выдуманный прогон
    expect(html).not.toContain("не выполнялась");
    expect(html).not.toContain("cleanup-report");
    expect(html).not.toContain("cleanup-action-error");

    // поля схемы и подсказки дошли в разметку, включая поздно добавленные
    expect(html).toContain("proxy_transport");
    expect(html).toContain("running_interval_start");
    expect(html).toContain("2captcha_apikey");
    expect(html).toContain("cleanup_time");
    expect(html).toContain("cleanup_interval_days");
    expect(html).toContain("Нижняя граница случайной паузы на странице с рекламой");
    expect(html).toContain("Время ежедневной очистки");
    expect(html).toContain("Периодичность очистки профилей");
  });

  it("Key Words: карточка источника, таблица и пустое состояние без данных", async () => {
    const html = await render(KeyWordsView);

    expect(html).toContain("Key Words");
    for (const hook of [
      "keywords-add",
      "keywords-delete-all",
      "keywords-open-file",
      "keywords-empty",
    ]) {
      expect(html).toContain(`data-test="${hook}"`);
    }

    // до первого ответа демона: ни ошибок, ни итогов, ни источника
    expect(html).not.toContain("keywords-action-error");
    expect(html).not.toContain("keywords-add-result");
    expect(html).not.toContain("keywords-delete-result");
    expect(html).not.toContain("keywords-source-single");
    expect(html).not.toContain("keywords-source-file");
    expect(html).not.toContain("Списком здесь управлять нельзя");
    // диалоги закрыты: v-dialog не рисует содержимое до открытия
    expect(html).not.toContain("keywords-add-dialog");
    expect(html).not.toContain("keywords-delete-dialog");
    expect(html).not.toContain("keywords-bulk-delete-dialog");
  });

  it("Domains: таблица, карточка настроек и пустое состояние без данных", async () => {
    const html = await render(DomainsView);

    expect(html).toContain("Domains");
    for (const hook of [
      "domains-add",
      "domains-delete-all",
      "domains-open-file",
      "domains-empty",
      "domains-settings",
      "domains-settings-save",
    ]) {
      expect(html).toContain(`data-test="${hook}"`);
    }

    // ни ошибок, ни итогов, ни открытых диалогов — экран только открылся
    expect(html).not.toContain("domains-action-error");
    expect(html).not.toContain("domains-add-result");
    expect(html).not.toContain("domains-delete-result");
    expect(html).not.toContain("domains-settings-error");
    expect(html).not.toContain("domains-settings-success");
    expect(html).not.toContain("domains-add-dialog");
    expect(html).not.toContain("domains-delete-dialog");
    expect(html).not.toContain("domains-bulk-delete-dialog");
    // подсказка о семантике чёрного списка есть всегда
    expect(html).toContain("Наш домен блокируется автоматически");
  });
});
