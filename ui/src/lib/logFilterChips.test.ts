// Чипы активных фильтров Logs: что показывает FilterBar строкой активных
// фильтров и когда кнопка «Сбросить» имеет смысл. Ключ чипа — ключ самого
// фильтра: экран сбрасывает ровно одно поле, а не все разом.

import { describe, expect, it } from "vitest";
import { activeFilterChips } from "./logFilterChips";
import { EMPTY_LOG_FILTERS, type LogFilterValues } from "./logFilters";

function filters(patch: Partial<LogFilterValues> = {}): LogFilterValues {
  return { ...EMPTY_LOG_FILTERS, ...patch };
}

describe("activeFilterChips", () => {
  it("фильтры по умолчанию неактивны — чипов нет", () => {
    expect(activeFilterChips(filters())).toEqual([]);
  });

  it("каждый непустой фильтр даёт чип с ключом, подписью и значением", () => {
    expect(
      activeFilterChips(
        filters({ level: "ERROR", category: "captcha", browserId: "b-7" }),
      ),
    ).toEqual([
      { key: "level", label: "Уровень", value: "ERROR" },
      { key: "category", label: "Категория", value: "captcha" },
      { key: "browserId", label: "Браузер", value: "b-7" },
    ]);
  });

  it("границы окна времени — отдельные чипы, пустые значения не дают чипов", () => {
    expect(
      activeFilterChips(
        filters({ since: "2026-09-29T10:00", until: "2026-09-29T11:00" }),
      ),
    ).toEqual([
      { key: "since", label: "С", value: "2026-09-29T10:00" },
      { key: "until", label: "По", value: "2026-09-29T11:00" },
    ]);

    expect(
      activeFilterChips(
        filters({ level: "", since: null, until: null }),
      ),
    ).toEqual([]);
  });

  it("непустой фильтр не порождает чипов для пустых соседних полей", () => {
    expect(activeFilterChips(filters({ category: "click" }))).toEqual([
      { key: "category", label: "Категория", value: "click" },
    ]);
  });

  it("порядок чипов — порядок контролов на панели, не порядок объекта", () => {
    const chips = activeFilterChips(
      filters({
        until: "2026-09-29T11:00",
        browserId: "b-1",
        level: "INFO",
        since: "2026-09-29T10:00",
        category: "click",
      }),
    );

    expect(chips.map((chip) => chip.key)).toEqual([
      "level",
      "category",
      "browserId",
      "since",
      "until",
    ]);
  });
});
