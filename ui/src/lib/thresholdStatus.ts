// Тон порога → статус словаря statusMap.
//
// Пороги дашборда (lib/thresholds) знают только цвет индикатора: success —
// утверждение выполнено, error — нарушено, neutral — данных нет. Карточки
// рисуют StatusChip, которому нужен статус из constants/statusMap (цвет +
// иконка + текст). Сводка живёт здесь, чтобы экраны не сравнивали строки
// тонов сами и чтобы «нет данных» не выглядело как ошибка.

import type { StatusKind } from "../constants/statusMap";
import type { ThresholdTone } from "./thresholds";

export function toneToStatus(tone: ThresholdTone): StatusKind {
  switch (tone) {
    case "success":
      return "ok";
    case "error":
      return "error";
    default:
      return "idle";
  }
}
