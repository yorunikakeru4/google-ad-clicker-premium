// Статус воркера из /state на kind статуса шаблона (constants/statusMap).
//
// WorkerStatus из engine/control_plane/state.py: starting, running, backoff,
// degraded, stopped, circuit_open. Цвета и иконки живут в statusMap — здесь
// только выбор kind, без собственных цветов. Неизвестный статус (новый в
// движке, гонка миграций) уходит в idle: строка остаётся читаемой, а не
// красится в success без данных.
//
// degraded — процесс жив, но прокси/CDP не работает: предупреждение, а не
// ошибка. Красный зарезервирован за circuit_open, где без человека не
// обойтись, а ротацию супервизор делает сам; серым (idle) сигнал воркера
// раствориться не должен.

import type { StatusKind } from "../constants/statusMap";

const WORKER_STATUS_KINDS: Record<string, StatusKind> = {
  starting: "info",
  running: "ok",
  backoff: "warn",
  degraded: "warn",
  stopped: "idle",
  circuit_open: "error",
};

export function workerStatusKind(status: string): StatusKind {
  return WORKER_STATUS_KINDS[status] ?? "idle";
}
