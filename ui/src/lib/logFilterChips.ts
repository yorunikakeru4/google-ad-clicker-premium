// Чипы активных фильтров Logs — данные для панели FilterBar.
//
// Панель не знает про логи: она рисует переданные чипы, крестик возвращает
// ключ поля, кнопка «Сбросить» просит сбросить всё. Здесь форма фильтров
// (lib/logFilters) сводится к описанию чипов — по одному на реально
// включённое поле, в порядке контролов на панели.

import type { LogFilterValues } from "./logFilters";

export interface LogFilterChip {
  /** Ключ исходного фильтра: по нему сбрасывается ровно одно поле. */
  key: keyof LogFilterValues;
  /** Подпись поля в чипе. */
  label: string;
  /** Отображаемое значение. */
  value: string;
}

const FILTER_LABELS: Record<keyof LogFilterValues, string> = {
  level: "Уровень",
  category: "Категория",
  browserId: "Браузер",
  since: "С",
  until: "По",
};

export function activeFilterChips(filters: LogFilterValues): LogFilterChip[] {
  const chips: LogFilterChip[] = [];

  const push = (key: keyof LogFilterValues, value: string | null): void => {
    if (value) chips.push({ key, label: FILTER_LABELS[key], value });
  };

  push("level", filters.level);
  push("category", filters.category);
  push("browserId", filters.browserId);
  push("since", filters.since);
  push("until", filters.until);

  return chips;
}
