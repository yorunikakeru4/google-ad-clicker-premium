import { describe, expect, it } from "vitest";
import {
  formatLastError,
  formatPid,
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
