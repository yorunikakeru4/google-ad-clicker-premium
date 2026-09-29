import { describe, expect, it } from "vitest";
import {
  EMPTY_LOG_FILTERS,
  FILTERS_STORAGE_KEY,
  loadLogFilters,
  sanitizeLogFilters,
  saveLogFilters,
  toQueryFilters,
  type LogFilterValues,
  type StorageLike,
} from "./logFilters";

/** Фейковое хранилище: карта плюс возможность сломать любую из операций. */
function fakeStorage(initial?: Record<string, string>): StorageLike & {
  data: Map<string, string>;
  failOnSet?: boolean;
  failOnGet?: boolean;
} {
  const data = new Map<string, string>(Object.entries(initial ?? {}));
  return {
    data,
    getItem(key) {
      if (this.failOnGet) throw new Error("хранилище недоступно");
      return data.get(key) ?? null;
    },
    setItem(key, value) {
      if (this.failOnSet) throw new Error("хранилище недоступно");
      data.set(key, value);
    },
  };
}

function values(partial: Partial<LogFilterValues> = {}): LogFilterValues {
  return { ...EMPTY_LOG_FILTERS, ...partial };
}

describe("sanitizeLogFilters", () => {
  it("не-объект — значения по умолчанию", () => {
    expect(sanitizeLogFilters(null)).toEqual(EMPTY_LOG_FILTERS);
    expect(sanitizeLogFilters("строка")).toEqual(EMPTY_LOG_FILTERS);
    expect(sanitizeLogFilters(42)).toEqual(EMPTY_LOG_FILTERS);
    expect(sanitizeLogFilters(["ERROR"])).toEqual(EMPTY_LOG_FILTERS);
  });

  it("валидные значения проходят без изменений", () => {
    const raw = {
      level: "ERROR",
      category: "click",
      browserId: "b-7",
      since: "2026-09-29T10:00",
      until: "2026-09-29T11:00",
    };
    expect(sanitizeLogFilters(raw)).toEqual(raw);
  });

  it("уровень и категория нормализуются и проверяются по словарю", () => {
    expect(sanitizeLogFilters({ level: "  error " }).level).toBe("ERROR");
    expect(sanitizeLogFilters({ category: " Click " }).category).toBe("click");
    // Неизвестные значения из чужого localStorage отбрасываются, а не
    // превращаются в фильтр, который ничего не найдёт молча.
    expect(sanitizeLogFilters({ level: "TRACE" }).level).toBe("");
    expect(sanitizeLogFilters({ category: "quantum" }).category).toBe("");
    expect(sanitizeLogFilters({ level: 42 }).level).toBe("");
    expect(sanitizeLogFilters({ category: null }).category).toBe("");
  });

  it("browser_id: только строка в разумных пределах", () => {
    expect(sanitizeLogFilters({ browserId: "  b1  " }).browserId).toBe("b1");
    expect(sanitizeLogFilters({ browserId: "" }).browserId).toBe("");
    expect(sanitizeLogFilters({ browserId: 7 }).browserId).toBe("");
    expect(sanitizeLogFilters({ browserId: "x".repeat(200) }).browserId).toBe(
      "",
    );
  });

  it("время: неправильный формат и несуществующая дата отбрасываются", () => {
    expect(sanitizeLogFilters({ since: "вчера" }).since).toBeNull();
    expect(sanitizeLogFilters({ until: "2026-13-45T99:99" }).until).toBeNull();
    expect(sanitizeLogFilters({ since: "" }).since).toBeNull();
    expect(sanitizeLogFilters({ since: 1_760_000_000 }).since).toBeNull();
    expect(sanitizeLogFilters({ until: null }).until).toBeNull();
    expect(sanitizeLogFilters({ since: "2026-09-29T10:30" }).since).toBe(
      "2026-09-29T10:30",
    );
  });

  it("инвертированное окно (since позже until) сбрасывается целиком", () => {
    const sanitized = sanitizeLogFilters({
      since: "2026-09-29T12:00",
      until: "2026-09-29T11:00",
      level: "ERROR",
    });
    expect(sanitized.since).toBeNull();
    expect(sanitized.until).toBeNull();
    expect(sanitized.level).toBe("ERROR", "остальные поля окно не трогает");
  });

  it("отсутствующие поля значений равны пустым", () => {
    expect(sanitizeLogFilters({})).toEqual(EMPTY_LOG_FILTERS);
  });
});

describe("loadLogFilters / saveLogFilters", () => {
  it("сохранённые фильтры переживают перезагрузку", () => {
    const storage = fakeStorage();
    const saved = values({
      level: "WARNING",
      category: "proxy",
      browserId: "b-3",
      since: "2026-09-29T09:00",
      until: "2026-09-29T10:00",
    });

    saveLogFilters(storage, saved);

    expect(loadLogFilters(storage)).toEqual(saved);
    expect(storage.data.has(FILTERS_STORAGE_KEY)).toBe(true);
  });

  it("пустое хранилище и битый JSON — значения по умолчанию", () => {
    expect(loadLogFilters(fakeStorage())).toEqual(EMPTY_LOG_FILTERS);
    expect(
      loadLogFilters(fakeStorage({ [FILTERS_STORAGE_KEY]: "{привет" })),
    ).toEqual(EMPTY_LOG_FILTERS);
    expect(
      loadLogFilters(fakeStorage({ [FILTERS_STORAGE_KEY]: "42" })),
    ).toEqual(EMPTY_LOG_FILTERS);
  });

  it("ошибки хранилища не роняют чтение и запись", () => {
    const reading = fakeStorage();
    reading.failOnGet = true;
    expect(loadLogFilters(reading)).toEqual(EMPTY_LOG_FILTERS);

    const writing = fakeStorage();
    writing.failOnSet = true;
    expect(() => saveLogFilters(writing, values({ level: "INFO" }))).not.toThrow();
  });

  it("значения из чужого хранилища проходят валидацию при загрузке", () => {
    const storage = fakeStorage({
      [FILTERS_STORAGE_KEY]: JSON.stringify({
        level: "TRACE",
        category: "click",
        browserId: "b1",
        since: "не время",
        until: null,
      }),
    });

    expect(loadLogFilters(storage)).toEqual(
      values({ level: "", category: "click", browserId: "b1" }),
    );
  });
});

describe("toQueryFilters", () => {
  it("пустые поля превращаются в null, значения — в запросные типы", () => {
    expect(toQueryFilters(EMPTY_LOG_FILTERS)).toEqual({
      level: null,
      category: null,
      browserId: null,
      since: null,
      until: null,
    });

    expect(
      toQueryFilters(
        values({ level: "ERROR", category: "click", browserId: "b1" }),
      ),
    ).toEqual({
      level: "ERROR",
      category: "click",
      browserId: "b1",
      since: null,
      until: null,
    });
  });

  it("время переводится в unix-секунды локального пояса", () => {
    const since = new Date("2026-09-29T10:00").getTime() / 1000;
    expect(toQueryFilters(values({ since: "2026-09-29T10:00" })).since).toBe(
      since,
    );
  });

  it("until включает выбранную минуту целиком", () => {
    const minuteStart = new Date("2026-09-29T11:00").getTime() / 1000;
    const query = toQueryFilters(values({ until: "2026-09-29T11:00" }));

    expect(query.until).toBeGreaterThan(minuteStart);
    expect(query.until).toBeLessThan(minuteStart + 60);
    expect(Math.floor(query.until ?? 0)).toBe(Math.floor(minuteStart) + 59);
  });
});
