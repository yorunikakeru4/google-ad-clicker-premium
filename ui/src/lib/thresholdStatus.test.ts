// Мост «тон порога → статус словаря»: пороги дашборда считают только цвет
// индикатора (lib/thresholds), карточки рисуют StatusChip — цвет, иконку и
// текст из statusMap. Экран не должен сводить эти словаря сам.

import { describe, expect, it } from "vitest";
import { toneToStatus } from "./thresholdStatus";

describe("toneToStatus", () => {
  it("выполненный порог → ok", () => {
    expect(toneToStatus("success")).toBe("ok");
  });

  it("нарушенный порог → error", () => {
    expect(toneToStatus("error")).toBe("error");
  });

  it("нет данных (нейтральный тон) → idle, а не ошибка", () => {
    expect(toneToStatus("neutral")).toBe("idle");
  });

  it("неизвестный тон деградирует в idle, а не ломает карточку", () => {
    expect(toneToStatus("magenta" as never)).toBe("idle");
  });
});
