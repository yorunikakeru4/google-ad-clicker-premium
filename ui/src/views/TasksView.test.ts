// Экран Tasks: паузы управляются теми же disabled-правилами, что и шапка
// (lib/control.disabledReason), расписание и источник запросов показываются
// из конфига демона.
//
// Рендер — SSR без mounted: конфиг в тесте подкладывается в синглтон
// useTasks напрямую, состояние демона — в синглтон useDaemonStatus.

import { beforeEach, describe, expect, it } from "vitest";
import { createSSRApp, h } from "vue";
import { renderToString } from "vue/server-renderer";
import vuetify from "../plugins/vuetify";
import { useTasks } from "../composables/useTasks";
import { useDaemonStatus } from "../composables/useDaemonStatus";
import { createPollState } from "../lib/poll";
import type { TasksConfig } from "../lib/tasks";
import type { Health, StateSnapshot } from "../lib/types";
import TasksView from "./TasksView.vue";

const CONFIG: TasksConfig = {
  queryFile: "queries.txt",
  query: "",
  intervalStart: "23:00",
  intervalEnd: "06:00",
  adPageMinWait: 10,
  adPageMaxWait: 15,
  nonadPageMinWait: 15,
  nonadPageMaxWait: 20,
  loopWaitTime: 60,
};

function healthOf(overrides: Partial<Health> = {}): Health {
  return {
    status: "ok",
    version: 1,
    state: "running",
    supervisor_alive: true,
    workers_total: 3,
    workers_alive: 3,
    workers_failed: 0,
    circuits_open: [],
    paused: false,
    ...overrides,
  };
}

function snapshotOf(overrides: Partial<StateSnapshot> = {}): StateSnapshot {
  return {
    state: "running",
    paused: false,
    workers: [],
    worker_count: 3,
    alive_count: 2,
    updated_at: 1_000_010,
    ...overrides,
  };
}

interface DaemonFixtures {
  online?: boolean;
  busy?: boolean;
  health?: Health | null;
  snapshot?: StateSnapshot | null;
  controlError?: string | null;
}

/** Ставит синглтону useDaemonStatus заданный снимок (AppShell в тесте нет). */
function setDaemon(fixtures: DaemonFixtures = {}): void {
  const daemon = useDaemonStatus();
  daemon.state.value = {
    ...createPollState(),
    phase: (fixtures.online ?? true) ? "online" : "offline",
    health: fixtures.health === undefined ? healthOf() : fixtures.health,
    snapshot: fixtures.snapshot === undefined ? snapshotOf() : fixtures.snapshot,
  };
  daemon.busy.value = fixtures.busy ?? false;
  daemon.controlError.value = fixtures.controlError ?? null;
}

async function renderTasks(): Promise<string> {
  const app = createSSRApp({ render: () => h(TasksView) });
  app.use(vuetify);
  return renderToString(app);
}

/** Открывающий тег элемента с data-test: по нему проверяются disabled/title. */
function tagOf(html: string, hook: string): string {
  const at = html.indexOf(`data-test="${hook}"`);
  expect(at, `нет data-test="${hook}"`).toBeGreaterThanOrEqual(0);
  const start = html.lastIndexOf("<", at);
  const end = html.indexOf(">", at);
  return html.slice(start, end + 1);
}

function isDisabled(tag: string): boolean {
  return /\sdisabled(\s|=|>)/.test(tag);
}

beforeEach(() => {
  const tasks = useTasks();
  tasks.config.value = null;
  tasks.loading.value = false;
  tasks.error.value = null;
  setDaemon();
});

describe("Tasks: пауза и продолжить", () => {
  it("работающий пул: «Пауза» доступна, «Продолжить» заблокирована (паузы нет)", async () => {
    setDaemon({ health: healthOf(), snapshot: snapshotOf() });
    const html = await renderTasks();

    expect(isDisabled(tagOf(html, "tasks-pause-btn"))).toBe(false);
    const resume = tagOf(html, "tasks-resume-btn");
    expect(isDisabled(resume)).toBe(true);
    expect(resume).toContain("пауза не установлена");
  });

  it("пауза активна: «Пауза» заблокирована как уже установленная, «Продолжить» доступна", async () => {
    setDaemon({
      health: healthOf({ paused: true, state: "paused" }),
      snapshot: snapshotOf({ paused: true, state: "paused" }),
    });
    const html = await renderTasks();

    const pause = tagOf(html, "tasks-pause-btn");
    expect(isDisabled(pause)).toBe(true);
    expect(pause).toContain("уже на паузе");
    expect(isDisabled(tagOf(html, "tasks-resume-btn"))).toBe(false);
    expect(html).toContain("Пауза активна");
  });

  it("пустой пул: «Пауза» заблокирована — паузировать нечего", async () => {
    setDaemon({
      health: healthOf({ workers_total: 0, workers_alive: 0 }),
      snapshot: snapshotOf({ worker_count: 0, alive_count: 0 }),
    });
    const html = await renderTasks();

    const pause = tagOf(html, "tasks-pause-btn");
    expect(isDisabled(pause)).toBe(true);
    expect(pause).toContain("нет запущенных воркеров");
  });

  it("демон недоступен: обе кнопки заблокированы с одной причиной", async () => {
    setDaemon({ online: false, health: null, snapshot: null });
    const html = await renderTasks();

    const pause = tagOf(html, "tasks-pause-btn");
    const resume = tagOf(html, "tasks-resume-btn");
    expect(isDisabled(pause)).toBe(true);
    expect(isDisabled(resume)).toBe(true);
    expect(pause).toContain("демон недоступен");
    expect(resume).toContain("демон недоступен");
  });

  it("команда в полёте: обе кнопки заблокированы (busy)", async () => {
    setDaemon({ busy: true });
    const html = await renderTasks();

    expect(isDisabled(tagOf(html, "tasks-pause-btn"))).toBe(true);
    expect(isDisabled(tagOf(html, "tasks-resume-btn"))).toBe(true);
    expect(tagOf(html, "tasks-pause-btn")).toContain("идёт выполнение команды");
  });
});

describe("Tasks: состояние и ошибки", () => {
  it("показывает воркеров кратко: активны N из M из /state", async () => {
    setDaemon({ snapshot: snapshotOf({ worker_count: 3, alive_count: 2 }) });
    const html = await renderTasks();

    expect(html).toContain('data-test="tasks-workers"');
    expect(html).toContain("2 из 3");
    // полная таблица воркеров живёт на Dashboard, здесь — только сводка
    expect(html).not.toContain("workers-table");
  });

  it("ошибка команды демона видна на экране", async () => {
    setDaemon({ controlError: "воркеры уже запущены" });
    const html = await renderTasks();

    expect(html).toContain('data-test="tasks-control-error"');
    expect(html).toContain("воркеры уже запущены");
  });

  it("ошибка чтения конфига видна на экране", async () => {
    const tasks = useTasks();
    tasks.error.value = "неверный токен";
    const html = await renderTasks();

    expect(html).toContain('data-test="tasks-config-error"');
    expect(html).toContain("неверный токен");
  });
});

describe("Tasks: расписание и источник запросов", () => {
  it("ночное окно показано как ночное, а не как ошибка", async () => {
    useTasks().config.value = { ...CONFIG };
    const html = await renderTasks();

    expect(html).toContain('data-test="tasks-schedule-label"');
    expect(html).toContain("23:00");
    expect(html).toContain("06:00");
    expect(html).toContain('data-test="tasks-schedule-detail"');
    expect(html).toMatch(/полноч/);
    // деталь окна — не класс ошибки
    expect(html).not.toContain("tasks-schedule-error");
  });

  it("источник запросов: активен файл, exclusivity-подсказка на месте", async () => {
    useTasks().config.value = { ...CONFIG };
    const html = await renderTasks();

    expect(html).toContain('data-test="tasks-source-label"');
    expect(html).toContain("queries.txt");
    expect(html).toContain('data-test="tasks-source-hint"');
    expect(html).toContain("paths.query_file");
    expect(html).toContain("behavior.query");
  });

  it("конфиг ещё не загружен — карточки честно ждут, а не показывают пустые значения", async () => {
    const html = await renderTasks();

    expect(html).toContain('data-test="tasks-config-pending"');
    expect(html).not.toContain("tasks-config-error");
  });
});
