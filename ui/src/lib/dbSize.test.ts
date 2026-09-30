// Индикатор размера БД в тулбаре Logs (план §5, фаза 9): честное сравнение
// размера (байты основного файла + -wal) с лимитом из config, включая
// выключенный лимит (0) и незагруженный конфиг.

import { describe, expect, it } from "vitest";
import { dbSizeStatus, formatMb, MB } from "./dbSize";

describe("formatMb", () => {
  it("байты в мегабайты с одним знаком", () => {
    expect(formatMb(0)).toBe("0");
    expect(formatMb(MB)).toBe("1");
    expect(formatMb(1.5 * MB)).toBe("1.5");
    expect(formatMb(1024 * 1024 * 1024)).toBe("1024");
  });
});

describe("dbSizeStatus", () => {
  it("лимит задан и размер в пределах — норма", () => {
    const status = dbSizeStatus(10 * MB, 100);

    expect(status.exceeded).toBe(false);
    expect(status.tone).toBe("neutral");
    expect(status.limitMb).toBe(100);
    expect(status.sizeMb).toBe("10");
    expect(status.detail).toContain("100");
  });

  it("размер строго больше лимита — превышение, работает автозащита", () => {
    const status = dbSizeStatus(101 * MB, 100);

    expect(status.exceeded).toBe(true);
    expect(status.tone).toBe("error");
    expect(status.detail).toContain("превышен");
    expect(status.detail).toContain("автозащита");
  });

  it("ровно на лимите — ещё не превышение", () => {
    const status = dbSizeStatus(100 * MB, 100);

    expect(status.exceeded).toBe(false);
    expect(status.tone).toBe("neutral");
  });

  it("лимит 0 — выключен, про превышение ничего не утверждаем", () => {
    const status = dbSizeStatus(1024 * MB, 0);

    expect(status.exceeded).toBe(false);
    expect(status.tone).toBe("neutral");
    expect(status.detail).toContain("не задан");
    expect(status.detail).not.toContain("превышен");
  });

  it("конфиг не загружен — лимит неизвестен, не выдумываем выключенный", () => {
    const status = dbSizeStatus(10 * MB, null);

    expect(status.exceeded).toBe(false);
    expect(status.tone).toBe("neutral");
    expect(status.limitMb).toBeNull();
    expect(status.detail).toContain("не загружен");
  });

  it("размер ещё не прочитан — честное «—», а не ноль", () => {
    const status = dbSizeStatus(null, 100);

    expect(status.exceeded).toBe(false);
    expect(status.tone).toBe("neutral");
    expect(status.sizeMb).toBeNull();
  });

  it("отрицательный лимит из чужого хранилища не превращается в превышение", () => {
    const status = dbSizeStatus(10 * MB, -5);

    expect(status.exceeded).toBe(false);
    expect(status.detail).toContain("не задан");
  });
});
