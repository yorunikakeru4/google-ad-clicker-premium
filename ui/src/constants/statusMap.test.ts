// Маппинг статусов профиля (контракт free|assigned|active|error|blocked)
// на kind статуса шаблона (constants/statusMap) и русские подписи чипов.
//
// Экран не должен сводить эти словаря сам: цвет/иконка живут в statusMap,
// здесь — только выбор kind и подпись, один источник для чипа и фильтра.

import { describe, expect, it } from "vitest";
import {
  PROFILE_STATUSES,
  profileStatusKind,
  profileStatusLabel,
  statusMeta,
} from "./statusMap";

describe("PROFILE_STATUSES", () => {
  it("контрактный список статусов без самовольства", () => {
    expect([...PROFILE_STATUSES].sort()).toEqual([
      "active",
      "blocked",
      "error",
      "free",
      "assigned",
    ].sort());
  });
});

describe("profileStatusKind: статус профиля → kind шаблона", () => {
  it("свободен — нейтрально, не «активен» и не «ошибка»", () => {
    expect(profileStatusKind("free")).toBe("idle");
  });

  it("назначен — ожидание работы (info), активен — работает (ok)", () => {
    expect(profileStatusKind("assigned")).toBe("info");
    expect(profileStatusKind("active")).toBe("ok");
    expect(profileStatusKind("assigned")).not.toBe(profileStatusKind("active"));
  });

  it("ошибка и блокировка — обе требуют вмешательства", () => {
    expect(profileStatusKind("error")).toBe("error");
    expect(profileStatusKind("blocked")).toBe("error");
  });

  it("неизвестный статус не красится в success без данных", () => {
    const unknown = profileStatusKind("quantum_flux");
    expect(unknown).toBe("idle");
    expect(unknown).not.toBe(profileStatusKind("active"));
  });
});

describe("profileStatusLabel: подписи чипов", () => {
  it("контрактные статусы подписаны по-русски", () => {
    expect(profileStatusLabel("free")).toBe("свободен");
    expect(profileStatusLabel("assigned")).toBe("назначен");
    expect(profileStatusLabel("active")).toBe("активен");
    expect(profileStatusLabel("error")).toBe("ошибка");
    expect(profileStatusLabel("blocked")).toBe("заблокирован");
  });

  it("каждый статус читается своим текстом, а не общим «нейтрально»", () => {
    const labels = PROFILE_STATUSES.map((status) => profileStatusLabel(status));
    expect(new Set(labels).size).toBe(PROFILE_STATUSES.length);
  });

  it("неизвестный статус показывает как есть, а не пустую строку", () => {
    expect(profileStatusLabel("legacy")).toBe("legacy");
  });
});

describe("statusMeta: kind профильного статуса разрешается в цвет и иконку", () => {
  it("каждый контрактный статус даёт валидную мету шаблона", () => {
    for (const status of PROFILE_STATUSES) {
      const meta = statusMeta(profileStatusKind(status));
      expect(meta.color).toBeTruthy();
      expect(meta.icon).toBeTruthy();
      expect(meta.label).toBeTruthy();
    }
  });

  it("заблокирован выглядит как ошибка, но подписан своим текстом", () => {
    const blocked = profileStatusKind("blocked");
    expect(statusMeta(blocked).color).toBe(statusMeta("error").color);
    expect(profileStatusLabel("blocked")).not.toBe(statusMeta(blocked).label);
  });
});
