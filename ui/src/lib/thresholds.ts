// Проверяемые утверждения дашборда — план §5 «Фаза 4»:
//   * «не менее 50 запросов/час» — цель, нарушение красным;
//   * «доля CAPTCHA < 5%» — зелёный только строго ниже порога, ровно 5% уже
//     нарушение. Пороги только здесь: экраны читают константы, а не числа.

/** Целевая нагрузка: не менее 50 сетевых запросов в скользящем окне часа. */
export const REQUESTS_PER_HOUR_TARGET = 50;

/** Доля CAPTCHA считается нормальной строго меньше этого значения. */
export const CAPTCHA_SHARE_LIMIT = 0.05;

/** Цвет индикатора: Vuetify-токен `color` для chip/alert. */
export type ThresholdTone = "success" | "error" | "neutral";

export interface RequestsPerHourStatus {
  /** Утверждение «≥50/час» выполнено. */
  met: boolean;
  tone: "success" | "error";
}

export function requestsPerHourStatus(total: number): RequestsPerHourStatus {
  const met = total >= REQUESTS_PER_HOUR_TARGET;
  return { met, tone: met ? "success" : "error" };
}

export interface CaptchaShareStatus {
  /** Есть ли данные: `null` от БД — «н/д», не 0%. */
  known: boolean;
  /** Доля строго меньше порога. */
  withinLimit: boolean;
  tone: ThresholdTone;
}

export function captchaShareStatus(
  share: number | null | undefined,
): CaptchaShareStatus {
  if (share == null || !Number.isFinite(share)) {
    return { known: false, withinLimit: false, tone: "neutral" };
  }
  const withinLimit = share < CAPTCHA_SHARE_LIMIT;
  return {
    known: true,
    withinLimit,
    tone: withinLimit ? "success" : "error",
  };
}

/** Доля как процент с одним знаком; нет данных — «н/д». */
export function formatCaptchaShare(share: number | null | undefined): string {
  if (share == null || !Number.isFinite(share)) return "н/д";
  return `${(share * 100).toFixed(1)}%`;
}
