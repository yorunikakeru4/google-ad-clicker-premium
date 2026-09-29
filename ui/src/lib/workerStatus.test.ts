// Маппинг статуса воркера из /state на kind статуса шаблона (statusMap).
// Цвета не дублируем: рисует их StatusChip по kind, а в чипе текстом остаётся
// каноничное значение демона — UI не выдумывает свои статусы.

import { describe, expect, it } from "vitest";
import { workerStatusKind } from "./workerStatus";

describe("workerStatusKind: статус воркера из /state", () => {
  it("работающий воркер — ok, стартующий — info", () => {
    expect(workerStatusKind("running")).toBe("ok");
    expect(workerStatusKind("starting")).toBe("info");
  });

  it("сбои различаются: backoff — предупреждение, circuit_open — ошибка", () => {
    expect(workerStatusKind("backoff")).toBe("warn");
    expect(workerStatusKind("circuit_open")).toBe("error");
  });

  it("остановленный воркер — idle, а не ошибка", () => {
    expect(workerStatusKind("stopped")).toBe("idle");
  });

  it("неизвестный статус не показывается живым и не роняет рендер", () => {
    const unknown = workerStatusKind("quantum_flux");
    expect(unknown).toBe("idle");
    expect(unknown).not.toBe(workerStatusKind("running"));
  });
});
