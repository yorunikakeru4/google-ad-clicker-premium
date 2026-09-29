import { describe, expect, it } from "vitest";
import {
  formatLastError,
  formatPid,
  formatUptime,
  formatClockTime,
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
