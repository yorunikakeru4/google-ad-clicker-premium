import { describe, expect, it } from "vitest";
import {
  formatBytes,
  formatDuration,
  formatLastError,
  formatLocalDateTime,
  formatPid,
  formatProblems,
  formatUptime,
  formatClockTime,
  formatLastUsed,
} from "./format";

describe("formatUptime", () => {
  it("без started_at показывает тире", () => {
    expect(formatUptime(null, 1_000_000)).toBe("—");
    expect(formatUptime(undefined, 1_000_000)).toBe("—");
  });

  it("форматирует секунды как HH:MM:SS", () => {
    expect(formatUptime(1_000_000, 1_000_000)).toBe("00:00:00");
    expect(formatUptime(1_000_000, 1_000_065)).toBe("00:01:05");
    expect(formatUptime(1_000_000, 1_003_661)).toBe("01:01:01");
  });

  it("суточный переход помечает дни", () => {
    expect(formatUptime(1_000_000, 1_090_061)).toBe("1д 01:01:01");
  });

  it("аптайм в будущем не уходит в минус", () => {
    expect(formatUptime(1_000_100, 1_000_000)).toBe("00:00:00");
  });
});

describe("formatPid", () => {
  it("отсутствующий PID — тире", () => {
    expect(formatPid(null)).toBe("—");
    expect(formatPid(undefined)).toBe("—");
  });

  it("наличие PID — число как есть", () => {
    expect(formatPid(4242)).toBe("4242");
  });
});

describe("formatLastError", () => {
  it("пустая ошибка — тире", () => {
    expect(formatLastError(null)).toBe("—");
    expect(formatLastError("")).toBe("—");
  });

  it("текст ошибки проходит без изменений", () => {
    expect(formatLastError("chrome crashed")).toBe("chrome crashed");
  });
});

describe("formatClockTime", () => {
  it("отсутствующее время — тире", () => {
    expect(formatClockTime(null)).toBe("—");
  });

  it("время — HH:MM:SS локального часового пояса", () => {
    const at = new Date(2026, 8, 29, 7, 5, 3).getTime();
    expect(formatClockTime(at)).toBe("07:05:03");
  });
});

describe("formatLastUsed", () => {
  it("неиспользованный профиль — тире", () => {
    expect(formatLastUsed(null)).toBe("—");
    expect(formatLastUsed(undefined)).toBe("—");
  });

  it("эпоха в секундах (REAL в SQLite) — дата и время локального пояса", () => {
    const at = new Date(2026, 8, 29, 7, 5, 3).getTime() / 1000;
    expect(formatLastUsed(at)).toBe("2026-09-29 07:05");
  });

  it("дробные секунды не ломают формат", () => {
    const at = new Date(2026, 0, 1, 23, 59, 59).getTime() / 1000 + 0.999;
    expect(formatLastUsed(at)).toBe("2026-01-01 23:59");
  });
});

describe("formatLocalDateTime", () => {
  it("без значения — тире, а не «Invalid Date»", () => {
    expect(formatLocalDateTime(null)).toBe("—");
    expect(formatLocalDateTime(undefined)).toBe("—");
    expect(formatLocalDateTime(Number.NaN)).toBe("—");
  });

  it("эпоха в секундах — дата и время локального пояса", () => {
    const at = new Date(2026, 9, 30, 4, 0, 0).getTime() / 1000;
    expect(formatLocalDateTime(at)).toBe("2026-10-30 04:00");
  });

  it("дробная эпоха (float в kv) не ломает минуту", () => {
    const at = new Date(2026, 0, 1, 23, 59, 59).getTime() / 1000 + 0.4;
    expect(formatLocalDateTime(at)).toBe("2026-01-01 23:59");
  });
});

describe("formatBytes", () => {
  it("без значения и мусор — тире", () => {
    expect(formatBytes(null)).toBe("—");
    expect(formatBytes(undefined)).toBe("—");
    expect(formatBytes(-1)).toBe("—");
    expect(formatBytes(Number.NaN)).toBe("—");
  });

  it("меньше килобайта — целые байты", () => {
    expect(formatBytes(0)).toBe("0 Б");
    expect(formatBytes(512)).toBe("512 Б");
    expect(formatBytes(1023)).toBe("1023 Б");
  });

  it("килобайты и мегабайты с одной цифрой после запятой", () => {
    expect(formatBytes(1024)).toBe("1.0 КБ");
    expect(formatBytes(1536)).toBe("1.5 КБ");
    expect(formatBytes(1024 * 1024 - 1)).toBe("1024.0 КБ");
    expect(formatBytes(1024 * 1024)).toBe("1.0 МБ");
    expect(formatBytes(Math.round(1.5 * 1024 * 1024))).toBe("1.5 МБ");
  });
});

describe("formatDuration", () => {
  it("без значения — тире", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
    expect(formatDuration(Number.NaN)).toBe("—");
    expect(formatDuration(-5)).toBe("—");
  });

  it("миллисекунды — секунды с десятой", () => {
    expect(formatDuration(1200)).toBe("1.2 с");
    expect(formatDuration(400)).toBe("0.4 с");
    expect(formatDuration(10_000)).toBe("10.0 с");
    expect(formatDuration(0)).toBe("0.0 с");
  });
});

describe("formatProblems", () => {
  it("строки проходят как есть — так их шлёт демон для delete", () => {
    expect(
      formatProblems(["id=5: назначен воркеру br-1", "id=6: профиль не найден"]),
    ).toEqual(["id=5: назначен воркеру br-1", "id=6: профиль не найден"]);
  });

  it("{line_index, message} → «строка N» с 1-based нумерацией", () => {
    expect(
      formatProblems([{ line_index: 0, message: "дубликат: user_agent уже есть" }]),
    ).toEqual(["строка 1: дубликат: user_agent уже есть"]);
    expect(formatProblems([{ line_index: 52, message: "пусто" }])).toEqual([
      "строка 53: пусто",
    ]);
  });

  it("{index, message} → «запись N» с 1-based нумерацией", () => {
    expect(formatProblems([{ index: 0, message: "имя уже занято" }])).toEqual([
      "запись 1: имя уже занято",
    ]);
    expect(formatProblems([{ index: 12, message: "дубликат" }])).toEqual([
      "запись 13: дубликат",
    ]);
  });

  it("мусор отбрасывается, а не превращается в [object Object]", () => {
    expect(
      formatProblems([
        null,
        42,
        true,
        { message: "без индекса" },
        { line_index: "2", message: "строка не число" },
        { index: 1 },
        {},
      ]),
    ).toEqual([]);
  });

  it("смешанный список сохраняет порядок и отбрасывает только мусор", () => {
    expect(
      formatProblems([
        "готовая строка",
        { line_index: 1, message: "нет порта" },
        null,
        { index: 3, message: "дубликат" },
      ]),
    ).toEqual(["готовая строка", "строка 2: нет порта", "запись 4: дубликат"]);
  });
});
