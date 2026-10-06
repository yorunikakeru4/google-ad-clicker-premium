// Поведение списков (Key Words, Domains): опрос снапшота и действия через
// control API демона.
//
// API подменяется целиком: здесь важен порядок вызовов, состояние ref'ов и
// читаемость ошибок. Композитор один на оба экрана, поэтому тесты гоняются
// на queries-фикстуре, а различия экранов — в options.items.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createWordlist, WORDLIST_POLL_MS, type WordlistState } from "./useWordlist";
import type {
  QueriesSnapshot,
  WordlistApi,
  WordlistChangeResult,
  WordlistDeleteResult,
} from "../lib/wordlist";

function snapshot(overrides: Partial<QueriesSnapshot> = {}): QueriesSnapshot {
  return {
    queries: ["usb hub", "webcam"],
    source: "file",
    query_file: "queries.txt",
    query: "",
    ...overrides,
  };
}

/** Управляемый фейк API: каждая функция падает по своей ошибке из state. */
function fakeApi(initial: QueriesSnapshot = snapshot()) {
  const state = {
    current: { ...initial },
    listError: null as Error | null,
    addResult: { added: 0, skipped: 0, problems: [] } as WordlistChangeResult,
    addError: null as Error | null,
    removeResult: { deleted: 0, skipped: 0, problems: [] } as WordlistDeleteResult,
    removeError: null as Error | null,
    removeAllResult: { deleted: 0, skipped: 0, problems: [] } as WordlistDeleteResult,
    removeAllError: null as Error | null,
    file: { path: "/data/queries.txt", exists: true },
    fileError: null as Error | null,
    listCalls: 0,
  };

  const api: WordlistApi<QueriesSnapshot> = {
    list: vi.fn(async () => {
      state.listCalls += 1;
      if (state.listError) throw state.listError;
      return { ...state.current, queries: [...state.current.queries] };
    }),
    add: vi.fn(async () => {
      if (state.addError) throw state.addError;
      return { ...state.addResult, problems: [...state.addResult.problems] };
    }),
    remove: vi.fn(async () => {
      if (state.removeError) throw state.removeError;
      return { ...state.removeResult, problems: [...state.removeResult.problems] };
    }),
    removeAll: vi.fn(async () => {
      if (state.removeAllError) throw state.removeAllError;
      return {
        ...state.removeAllResult,
        problems: [...state.removeAllResult.problems],
      };
    }),
    filePath: vi.fn(async () => {
      if (state.fileError) throw state.fileError;
      return { ...state.file };
    }),
  };

  return { api, state };
}

function setup(
  api: WordlistApi<QueriesSnapshot>,
  overrides: { pollMs?: number; opener?: (path: string) => Promise<void> } = {},
): WordlistState<QueriesSnapshot> {
  return createWordlist(api, {
    items: (value) => value.queries,
    fileLabel: "queries.txt",
    pollMs: overrides.pollMs ?? WORDLIST_POLL_MS,
    opener: overrides.opener ?? vi.fn(async () => undefined),
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("опрос списка", () => {
  it("первый тик читает снапшот и наполняет строки", async () => {
    const { api } = fakeApi();
    const list = setup(api);

    await list.start();

    expect(list.loading.value).toBe(false);
    expect(list.error.value).toBeNull();
    expect(list.items.value).toEqual(["usb hub", "webcam"]);
    expect(list.snapshot.value?.query_file).toBe("queries.txt");
    list.stop();
  });

  it("start заводит интервал, stop его гасит", async () => {
    const { api, state } = fakeApi();
    const list = setup(api, { pollMs: 1000 });

    await list.start();
    await vi.advanceTimersByTimeAsync(3000);

    // первый тик + три по интервалу
    expect(state.listCalls).toBe(4);

    list.stop();
    await vi.advanceTimersByTimeAsync(5000);
    expect(state.listCalls).toBe(4);
  });

  it("параллельные тики ждут один запрос, а не плодят новые", async () => {
    const { api, state } = fakeApi();
    // `!`, а не `| null`: присваивание внутри then-callback TS не отслеживает,
    // и к месту вызова переменная сужалась бы до null.
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    vi.mocked(api.list).mockImplementation(async () => {
      state.listCalls += 1;
      await gate;
      return snapshot();
    });
    const list = setup(api);

    const first = list.tick();
    const second = list.tick();
    release();
    await Promise.all([first, second]);

    expect(state.listCalls).toBe(1);
  });

  it("ошибка чтения попадает в error и чистится успешным тиком", async () => {
    const { api, state } = fakeApi();
    const list = setup(api);
    state.listError = new Error("нет связи");

    await list.start();
    expect(list.error.value).toBe("нет связи");

    state.listError = null;
    await list.tick();
    expect(list.error.value).toBeNull();
    expect(list.items.value).toEqual(["usb hub", "webcam"]);
    list.stop();
  });
});

describe("действия", () => {
  it("add перечитывает список и запоминает счётчики", async () => {
    const { api, state } = fakeApi();
    const list = setup(api);
    await list.start();

    state.addResult = { added: 2, skipped: 1, problems: ["уже есть: webcam"] };
    const ok = await list.add(["usb hub", "webcam", "ssd"]);

    expect(ok).toBe(true);
    expect(api.add).toHaveBeenCalledWith(["usb hub", "webcam", "ssd"]);
    expect(list.addResult.value).toEqual(state.addResult);
    expect(list.actionError.value).toBeNull();
    expect(list.items.value).toEqual(["usb hub", "webcam"]);
    list.stop();
  });

  it("пустой список строк не создаёт запрос", async () => {
    const { api } = fakeApi();
    const list = setup(api);

    expect(await list.add([])).toBe(false);
    expect(api.add).not.toHaveBeenCalled();
  });

  it("ошибка add уходит в actionError, а не в error списка", async () => {
    const { api, state } = fakeApi();
    const list = setup(api);
    state.addError = new Error("paths.query_file не задан");

    expect(await list.add(["ssd"])).toBe(false);
    expect(list.actionError.value).toBe("paths.query_file не задан");
    expect(list.error.value).toBeNull();
    expect(list.addResult.value).toBeNull();
  });

  it("remove шлёт значения и возвращает отчёт в deleteResult", async () => {
    const { api, state } = fakeApi();
    const list = setup(api);
    await list.start();

    state.removeResult = { deleted: 1, skipped: 0, problems: [] };
    expect(await list.remove(["usb hub"])).toBe(true);

    expect(api.remove).toHaveBeenCalledWith(["usb hub"]);
    expect(list.deleteResult.value?.deleted).toBe(1);
    list.stop();
  });

  it("removeAll пустым списком значений не ходит в сеть", async () => {
    const { api } = fakeApi(snapshot({ queries: [] }));
    const list = setup(api);

    expect(await list.remove([])).toBe(false);
    expect(api.remove).not.toHaveBeenCalled();
  });

  it("removeAll строит {all:true} на стороне API и перечитывает список", async () => {
    const { api, state } = fakeApi();
    const list = setup(api);
    await list.start();

    state.removeAllResult = { deleted: 2, skipped: 0, problems: [] };
    expect(await list.removeAll()).toBe(true);

    expect(api.removeAll).toHaveBeenCalledTimes(1);
    expect(list.deleteResult.value?.deleted).toBe(2);
    list.stop();
  });
});

describe("открытие файла", () => {
  it("путь из ответа демона уходит в opener", async () => {
    const opener = vi.fn(async () => undefined);
    const { api } = fakeApi();
    const list = setup(api, { opener });

    expect(await list.openFile()).toBe(true);
    expect(opener).toHaveBeenCalledWith("/data/queries.txt");
    expect(list.actionError.value).toBeNull();
  });

  it("отсутствующий файл не открывается, причина — своя", async () => {
    const opener = vi.fn(async () => undefined);
    const { api, state } = fakeApi();
    state.file = { path: "/data/queries.txt", exists: false };
    const list = setup(api, { opener });

    expect(await list.openFile()).toBe(false);
    expect(opener).not.toHaveBeenCalled();
    expect(list.actionError.value).toBe(
      "Файл queries.txt не найден: /data/queries.txt",
    );
  });

  it("ошибка opener'а показывается как actionError", async () => {
    const { api } = fakeApi();
    const list = setup(api, {
      opener: vi.fn(async () => {
        throw new Error("нет программы по умолчанию");
      }),
    });

    expect(await list.openFile()).toBe(false);
    expect(list.actionError.value).toBe("нет программы по умолчанию");
  });
});

describe("конкуренция действий", () => {
  it("второе действие не стартует, пока первое в полёте", async () => {
    const { api } = fakeApi();
    const list = setup(api);
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    vi.mocked(api.add).mockImplementation(async () => {
      await gate;
      return { added: 1, skipped: 0, problems: [] };
    });

    const first = list.add(["a"]);
    const second = list.add(["b"]);
    release();

    expect(await first).toBe(true);
    expect(await second).toBe(false);
    expect(api.add).toHaveBeenCalledTimes(1);
  });
});
