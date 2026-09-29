// Логика экрана Tasks: расписание запуска, источник запросов, диапазоны пауз
// и чтение GET /control/config.
//
// Трактовка окна повторяет engine/scheduler.py (inside_running_interval):
// ночное окно через полночь легально, 00:00–00:00 = круглосуточно, частично
// заданное окно и окно короче 10 минут воркеры отвергают.

import { describe, expect, it } from "vitest";
import type { Transport } from "./daemonApi";
import {
  createTasksApi,
  describeSchedule,
  pickTasksConfig,
  querySource,
  waitRanges,
  type TasksConfig,
} from "./tasks";

const CONFIG: TasksConfig = {
  queryFile: "queries.txt",
  query: "",
  intervalStart: "09:00",
  intervalEnd: "18:00",
  adPageMinWait: 10,
  adPageMaxWait: 15,
  nonadPageMinWait: 15,
  nonadPageMaxWait: 20,
  loopWaitTime: 60,
};

/** Ответ GET /control/config: {"config": {paths, behavior, ...}}. */
function payload(
  behavior: Record<string, unknown> = {},
  paths: Record<string, unknown> = {},
): unknown {
  return {
    config: {
      paths: {
        query_file: "queries.txt",
        proxy_file: "",
        user_agents: "user_agents.txt",
        filtered_domains: "domains.txt",
        ...paths,
      },
      behavior: {
        query: "",
        running_interval_start: "09:00",
        running_interval_end: "18:00",
        ad_page_min_wait: 10,
        ad_page_max_wait: 15,
        nonad_page_min_wait: 15,
        nonad_page_max_wait: 20,
        loop_wait_time: 60,
        browser_count: 2,
        ...behavior,
      },
    },
  };
}

function transportOf(reply: { status: number; body: string } | Error) {
  const calls: { path: string; method: string; body?: string }[] = [];
  const transport: Transport = async (request) => {
    calls.push({ path: request.path, method: request.method, body: request.body });
    if (reply instanceof Error) throw reply;
    return reply;
  };
  return { transport, calls };
}

describe("describeSchedule: окно расписания", () => {
  it("пустое окно — ограничения нет, воркеры работают круглосуточно", () => {
    const info = describeSchedule("", "");
    expect(info.kind).toBe("always");
    expect(info.tone).toBe("ok");
    expect(info.label).toMatch(/круглосуточно/i);
    expect(info.detail).toMatch(/ограничени/);
  });

  it("00:00–00:00 — круглосуточно, а не «нулевое окно»", () => {
    const info = describeSchedule("00:00", "00:00");
    expect(info.kind).toBe("always");
    expect(info.tone).toBe("ok");
    expect(info.label).toMatch(/круглосуточно/i);
  });

  it("окно внутри суток показывает обе границы", () => {
    const info = describeSchedule("09:00", "18:00");
    expect(info.kind).toBe("day");
    expect(info.tone).toBe("ok");
    expect(info.label).toContain("09:00");
    expect(info.label).toContain("18:00");
  });

  it("ночное окно 23:00–06:00 легально и показано как ночное", () => {
    const info = describeSchedule("23:00", "06:00");
    expect(info.kind).toBe("overnight");
    expect(info.tone).toBe("ok");
    expect(info.label).toContain("23:00");
    expect(info.label).toContain("06:00");
    expect(info.detail).toMatch(/полноч/);
  });

  it("ночное окно не помечается ошибкой формата", () => {
    const info = describeSchedule("20:30", "06:15");
    expect(info.kind).toBe("overnight");
    expect(info.tone).toBe("ok");
  });

  it("частично заданное окно — ошибка: движок отвергает (IntervalError)", () => {
    const info = describeSchedule("09:00", "");
    expect(info.kind).toBe("partial");
    expect(info.tone).toBe("error");
    expect(info.detail).toContain("running_interval_end");
    expect(info.detail).toContain("IntervalError");
  });

  it("окно короче 10 минут — ошибка: движок не запустит воркеры", () => {
    const info = describeSchedule("12:25", "12:30");
    expect(info.kind).toBe("too_short");
    expect(info.tone).toBe("error");
    expect(info.detail).toMatch(/10 минут/);
    expect(info.detail).toContain("5 мин");
  });

  it("ровно 10 минут — легальное окно", () => {
    expect(describeSchedule("12:25", "12:35").kind).toBe("day");
  });

  it("равные границы кроме полуночи — длительность 0, ошибка", () => {
    expect(describeSchedule("12:00", "12:00").kind).toBe("too_short");
  });

  it("формат не ЧЧ:ММ — ошибка формата, движок такое не разберёт", () => {
    const info = describeSchedule("9:00", "18:00");
    expect(info.kind).toBe("bad_format");
    expect(info.tone).toBe("error");
  });
});

describe("querySource: источник запросов", () => {
  it("задан только файл — активен файл", () => {
    const source = querySource({ ...CONFIG, query: "" });
    expect(source.kind).toBe("file");
    expect(source.label).toContain("queries.txt");
    expect(source.label).toMatch(/файл/i);
    expect(source.hint).toContain("paths.query_file");
    expect(source.hint).toContain("behavior.query");
  });

  it("задан только одиночный запрос — активен он", () => {
    const source = querySource({ ...CONFIG, queryFile: "", query: "кофейня москва" });
    expect(source.kind).toBe("single");
    expect(source.label).toContain("кофейня москва");
    expect(source.hint).toMatch(/взаимно исключающ/);
  });

  it("оба источника заполнены — конфликт, а не молчаливый выбор", () => {
    const source = querySource({ ...CONFIG, query: "кофейня москва" });
    expect(source.kind).toBe("conflict");
    expect(source.hint).toMatch(/взаимно исключающ|оставьте что-то одно/);
  });

  it("ни одного — источник не задан", () => {
    const source = querySource({ ...CONFIG, queryFile: "", query: "" });
    expect(source.kind).toBe("none");
    expect(source.label).toMatch(/не задан/i);
  });

  it("подсказка про exclusivity одна на все случаи", () => {
    const file = querySource(CONFIG);
    const single = querySource({ ...CONFIG, queryFile: "" });
    expect(file.hint).toContain("paths.query_file");
    expect(file.hint).toContain("behavior.query");
    expect(single.hint).toBe(file.hint);
  });
});

describe("waitRanges: справка по диапазонам пауз", () => {
  it("ад/не-ад диапазоны и loop_wait_time в секундах", () => {
    const rows = waitRanges(CONFIG);
    expect(rows.map((row) => row.key)).toEqual(["ad", "nonad", "loop"]);
    const byKey = Object.fromEntries(rows.map((row) => [row.key, row.value]));
    expect(byKey.ad).toBe("10–15 с");
    expect(byKey.nonad).toBe("15–20 с");
    expect(byKey.loop).toBe("60 с");
  });

  it("подписи ссылаются на поля конфига", () => {
    const rows = waitRanges(CONFIG);
    expect(rows[0].label).toContain("ad_page_min_wait");
    expect(rows[1].label).toContain("nonad_page_min_wait");
    expect(rows[2].label).toContain("loop_wait_time");
  });
});

describe("pickTasksConfig: разбор ответа /control/config", () => {
  it("разбирает поля экрана из секций paths/behavior", () => {
    expect(pickTasksConfig(payload())).toEqual(CONFIG);
  });

  it("пустой источник запросов разбирается как пустые строки", () => {
    const config = pickTasksConfig(
      payload({ query: "" }, { query_file: "" }),
    );
    expect(config.queryFile).toBe("");
    expect(config.query).toBe("");
  });

  it("ответ без config — читаемая ошибка с указанием пути", () => {
    expect(() => pickTasksConfig({})).toThrow(/\/control\/config/);
    expect(() => pickTasksConfig("нет")).toThrow(/\/control\/config/);
  });

  it("нет секции behavior — ошибка называет секцию", () => {
    expect(() => pickTasksConfig({ config: { paths: {} } })).toThrow(/behavior/);
  });

  it("нет поля — ошибка называет поле", () => {
    expect(() =>
      pickTasksConfig({ config: { paths: {}, behavior: {} } }),
    ).toThrow(/running_interval_start/);
  });

  it("поле неверного типа — ошибка называет поле", () => {
    expect(() =>
      pickTasksConfig(payload({ loop_wait_time: "60" })),
    ).toThrow(/loop_wait_time/);
  });
});

describe("createTasksApi: чтение конфига", () => {
  it("loadConfig уходит GET-ом на /control/config", async () => {
    const { transport, calls } = transportOf({
      status: 200,
      body: JSON.stringify(payload()),
    });
    const api = createTasksApi(transport);

    const config = await api.loadConfig();

    expect(calls).toEqual([
      { path: "/control/config", method: "GET", body: undefined },
    ]);
    expect(config).toEqual(CONFIG);
  });

  it("400 invalid_config — сообщение демона, а не сырой JSON", async () => {
    const { transport } = transportOf({
      status: 400,
      body: JSON.stringify({
        error: { code: "invalid_config", message: "behavior.query: мусор" },
      }),
    });
    const api = createTasksApi(transport);

    await expect(api.loadConfig()).rejects.toThrow("behavior.query: мусор");
  });

  it("не-JSON тело — читаемая ошибка", async () => {
    const { transport } = transportOf({ status: 200, body: "<html>oops</html>" });
    const api = createTasksApi(transport);

    await expect(api.loadConfig()).rejects.toThrow(/JSON/i);
  });

  it("JSON без config — ошибка с указанием endpoint'а", async () => {
    const { transport } = transportOf({ status: 200, body: '{"ok":true}' });
    const api = createTasksApi(transport);

    await expect(api.loadConfig()).rejects.toThrow(/\/control\/config/);
  });

  it("транспорт бросил (нет соединения) — ошибка доходит", async () => {
    const { transport } = transportOf(
      new Error("не удалось подключиться к демону на 127.0.0.1:8787"),
    );
    const api = createTasksApi(transport);

    await expect(api.loadConfig()).rejects.toThrow("не удалось подключиться");
  });
});
