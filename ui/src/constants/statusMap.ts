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
