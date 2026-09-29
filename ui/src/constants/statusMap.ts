export type StatusKind = "ok" | "warn" | "error" | "idle" | "info";

export interface StatusMeta {
  color: string;
  icon: string;
  label: string;
}

export const statusMap: Record<StatusKind, StatusMeta> = {
  ok: { color: "success", icon: "mdi-check-circle", label: "активен" },
  warn: { color: "warning", icon: "mdi-alert", label: "на паузе" },
  error: { color: "error", icon: "mdi-close-circle", label: "ошибка" },
  idle: { color: "on-surface", icon: "mdi-circle-outline", label: "неактивен" },
  info: { color: "info", icon: "mdi-information", label: "нейтрально" },
};

export function statusMeta(status: StatusKind): StatusMeta {
  return statusMap[status] ?? statusMap.idle;
}

/**
 * Статусы профиля — контракт GET /control/profiles (план §5, фаза 6).
 *
 * Порядок — как в контракте: свободен → назначен → активен, плюс два
 * состояния, требующих вмешательства.
 */
export const PROFILE_STATUSES = [
  "free",
  "assigned",
  "active",
  "error",
  "blocked",
] as const;

export type ProfileStatus = (typeof PROFILE_STATUSES)[number];

/**
 * Статус профиля → kind шаблона.
 *
 * - `free` — idle: профиль просто ждёт, серый не кричит;
 * - `assigned` — info: назначен потоку, но ещё не работает — синий;
 * - `active` — ok: работает, зелёный;
 * - `error` — error: сбой, красный;
 * - `blocked` — error: блокировка хуже обычной ошибки и тем более не
 *   «на паузе», поэтому тоже красный, но со своим текстом чипа.
 *
 * Неизвестный статус (новый в движке, гонка миграций) уходит в idle:
 * строка остаётся читаемой, а не красится в success без данных.
 */
const PROFILE_STATUS_KINDS: Record<string, StatusKind> = {
  free: "idle",
  assigned: "info",
  active: "ok",
  error: "error",
  blocked: "error",
};

/**
 * Подпись чипа статуса профиля.
 *
 * Общие label'ы шаблона («активен», «нейтрально») здесь не годятся: экрану
 * нужны ровно слова контракта. Неизвестный статус показываем как есть —
 * пустая подпись хуже сырого значения.
 */
const PROFILE_STATUS_LABELS: Record<string, string> = {
  free: "свободен",
  assigned: "назначен",
  active: "активен",
  error: "ошибка",
  blocked: "заблокирован",
};

export function profileStatusKind(status: string): StatusKind {
  return PROFILE_STATUS_KINDS[status] ?? "idle";
}

export function profileStatusLabel(status: string): string {
  return PROFILE_STATUS_LABELS[status] ?? status;
}
