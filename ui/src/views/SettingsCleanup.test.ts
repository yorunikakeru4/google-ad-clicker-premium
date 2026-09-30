// Секция «Очистка профилей» на Settings: статус расписания, ручной запуск,
// превью с dry_run и отчёт.
//
// Рендер — vue/server-renderer в node: onMounted не вызывается, поэтому
// состояние useCleanup засеивается здесь явно (singleton свежий на файл).
// Тесты проверяют и разметку, и то, что in-flight блокирует обе кнопки.

import { beforeAll, beforeEach, describe, expect, it } from "vitest";
import { createSSRApp, h } from "vue";
import { renderToString } from "vue/server-renderer";
import vuetify from "../plugins/vuetify";
import SettingsView from "./SettingsView.vue";
import { useCleanup } from "../composables/useCleanup";
import type { CleanupReport } from "../lib/cleanup";

// useSettings читает localStorage в момент создания — до первого рендера.
const storage = new Map<string, string>();

beforeAll(() => {
  (globalThis as { window?: unknown }).window = {
    localStorage: {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => void storage.set(key, value),
    },
  };
});

beforeEach(() => {
  const cleanup = useCleanup();
  cleanup.status.value = null;
  cleanup.loaded.value = false;
  cleanup.loading.value = false;
  cleanup.statusError.value = null;
  cleanup.actionError.value = null;
  cleanup.report.value = null;
  cleanup.pending.value = null;
});

async function render(): Promise<string> {
  const app = createSSRApp({ render: () => h(SettingsView) });
  app.use(vuetify);
  return renderToString(app);
}

/** Открывающий тег элемента с данным data-test — для атрибутов и классов. */
function tagOf(html: string, test: string): string {
  const at = html.indexOf(`data-test="${test}"`);
  expect(at, `нет data-test="${test}"`).toBeGreaterThanOrEqual(0);
  const start = html.lastIndexOf("<", at);
  const end = html.indexOf(">", at);
  return html.slice(start, end + 1);
}

function report(overrides: Partial<CleanupReport> = {}): CleanupReport {
  return {
    removed: 3,
    removed_bytes: 1_572_864,
    skipped_active: 1,
    errors: 0,
    duration_ms: 1200,
    dry_run: false,
    ...overrides,
  };
}

function seedStatus(cleanup: ReturnType<typeof useCleanup>): void {
  cleanup.status.value = {
    last: {
      ts: new Date(2026, 9, 30, 4, 0, 0).getTime() / 1000,
      report: report(),
    },
    next_run: new Date(2026, 10, 1, 4, 0, 0).getTime() / 1000,
  };
  cleanup.loaded.value = true;
}

describe("Settings: секция «Очистка профилей»", () => {
  it("до загрузки: заглушки статуса, кнопки есть, отчёта и ошибок нет", async () => {
    const html = await render();

    for (const hook of [
      "settings-cleanup",
      "cleanup-last",
      "cleanup-next",
      "cleanup-refresh",
      "cleanup-run",
      "cleanup-preview",
    ]) {
      expect(html, hook).toContain(`data-test="${hook}"`);
    }

    const cleanup = useCleanup();
    expect(cleanup.lastLine.value).toBe("—");
    expect(cleanup.nextLine.value).toBe("—");
    expect(html).not.toContain("cleanup-report");
    expect(html).not.toContain("cleanup-action-error");
    expect(html).not.toContain("cleanup-status-error");

    // кнопки активны: в полёте ничего нет
    expect(tagOf(html, "cleanup-run")).not.toContain("disabled");
    expect(tagOf(html, "cleanup-preview")).not.toContain("disabled");
    expect(tagOf(html, "cleanup-refresh")).not.toContain("disabled");
  });

  it("статус: дата последнего прогона со счётчиками и ближайший запуск", async () => {
    seedStatus(useCleanup());

    const html = await render();

    expect(html).toContain(
      "Последняя очистка: 2026-10-30 04:00 (удалено 3, пропущено активных 1, ошибок 0)",
    );
    expect(html).toContain("Следующая очистка: 2026-11-01 04:00");
  });

  it("прогона не было, job выключен — честные формулировки", async () => {
    const cleanup = useCleanup();
    cleanup.status.value = { last: null, next_run: null };
    cleanup.loaded.value = true;

    const html = await render();

    expect(html).toContain("Последняя очистка: не выполнялась");
    expect(html).toContain("Следующая очистка: по расписанию выключено");
  });

  it("ошибка статуса — свой алерт с текстом демона", async () => {
    const cleanup = useCleanup();
    cleanup.statusError.value = "нет соединения с демоном";

    const html = await render();

    expect(html).toContain('data-test="cleanup-status-error"');
    expect(html).toContain("нет соединения с демоном");
  });

  it("in-flight: обе кнопки запуска заблокированы, у идущей — лоадер", async () => {
    const cleanup = useCleanup();
    cleanup.pending.value = "run";

    const html = await render();

    expect(tagOf(html, "cleanup-run")).toContain("disabled");
    expect(tagOf(html, "cleanup-run")).toContain("v-btn--loading");
    expect(tagOf(html, "cleanup-preview")).toContain("disabled");
    expect(tagOf(html, "cleanup-refresh")).toContain("disabled");
  });
});

describe("Settings: отчёт очистки", () => {
  it("реальный прогон: счётчики, байты и длительность, без пометки превью", async () => {
    const cleanup = useCleanup();
    cleanup.report.value = report();

    const html = await render();

    expect(html).toContain('data-test="cleanup-report"');
    expect(tagOf(html, "cleanup-report")).toContain("text-success");
    expect(html).not.toContain("cleanup-report-preview");
    expect(html).toContain("Удалено: 3 (1.5 МБ)");
    expect(html).toContain("Пропущено активных: 1");
    expect(html).toContain("Ошибок: 0");
    expect(html).toContain("Длительность: 1.2 с");
  });

  it("dry_run явно помечен: превью, ничего не удалено, счётчики — кандидаты", async () => {
    const cleanup = useCleanup();
    cleanup.report.value = report({
      removed: 7,
      removed_bytes: 2_097_152,
      skipped_active: 2,
      duration_ms: 400,
      dry_run: true,
    });

    const html = await render();

    expect(html).toContain('data-test="cleanup-report-preview"');
    expect(html).toContain("ничего не удалено");
    expect(html).toContain("Кандидатов: 7 (2.0 МБ)");
    expect(html).toContain("Пропущено активных: 2");
    expect(html).toContain("Длительность: 0.4 с");
    expect(html).not.toContain("Удалено:");
  });

  it("ошибки больше нуля — жёлтый алерт", async () => {
    const cleanup = useCleanup();
    cleanup.report.value = report({ errors: 2 });

    const html = await render();

    expect(tagOf(html, "cleanup-report")).toContain("text-warning");
    expect(html).toContain("Ошибок: 2");
  });

  it("ошибка запуска — свой алерт, отчёт прошлого прогона не показывается", async () => {
    const cleanup = useCleanup();
    cleanup.actionError.value = "HTTP 500: внутренняя ошибка демона";

    const html = await render();

    expect(html).toContain('data-test="cleanup-action-error"');
    expect(html).toContain("HTTP 500");
    expect(html).not.toContain("cleanup-report");
  });
});
