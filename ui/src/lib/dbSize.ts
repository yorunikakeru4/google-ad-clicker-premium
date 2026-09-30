// Индикатор размера БД в тулбаре Logs (план §5, фаза 9).
//
// Честность — главное правило модуля: размер и лимит сравниваются так же,
// как их сравнивает автозащита демона (байты основного файла + `-wal`
// против `behavior.db_size_limit_mb` в МБ), выключенный лимит (0) и
// незагруженный конфиг — разные состояния, ипутать их друг за друга нельзя:
// «превышено» можно утверждать только при limit > 0 и строгом больше.

/** Байт в мегабайте — те же 1024², что у лимита в движке. */
export const MB = 1024 * 1024;

/** Цвет индикатора: Vuetify-токен `color` для chip/alert. */
export type DbSizeTone = "neutral" | "error";

export interface DbSizeStatus {
  /** Размер в МБ одной цифрой; null — ещё не читали. */
  sizeMb: string | null;
  /** Лимит из конфига; null — конфиг не загружен. */
  limitMb: number | null;
  /** Превышение есть только при limit > 0 && size > limit. */
  exceeded: boolean;
  tone: DbSizeTone;
  /** Подпись под размером: лимит, превышение или состояние лимита. */
  detail: string;
}

/** Байты → МБ одной цифрой: 1.5, 0, 1024. */
export function formatMb(bytes: number): string {
  const mb = bytes / MB;
  return Number.isInteger(mb) ? String(mb) : mb.toFixed(1);
}

/**
 * Статус индикатора по размеру в байтах и лимиту в МБ.
 *
 * `sizeBytes = null` — размер ещё не прочитан (показываем «—», ничего не
 * утверждаем). `limitMb = null` — конфиг не загружен; `limitMb <= 0` —
 * лимит выключен или битое значение из чужого хранилища: в обоих случаях
 * превышения нет и об этом прямо сказано.
 */
export function dbSizeStatus(
  sizeBytes: number | null,
  limitMb: number | null,
): DbSizeStatus {
  const size = sizeBytes === null ? null : formatMb(sizeBytes);

  if (limitMb === null) {
    return {
      sizeMb: size,
      limitMb: null,
      exceeded: false,
      tone: "neutral",
      detail: "лимит не загружен",
    };
  }

  if (limitMb <= 0) {
    return {
      sizeMb: size,
      limitMb,
      exceeded: false,
      tone: "neutral",
      detail: "лимит не задан",
    };
  }

  const exceeded = sizeBytes !== null && sizeBytes > limitMb * MB;
  return {
    sizeMb: size,
    limitMb,
    exceeded,
    tone: exceeded ? "error" : "neutral",
    detail: exceeded
      ? "превышен, работает автозащита"
      : `лимит ${limitMb} МБ`,
  };
}
