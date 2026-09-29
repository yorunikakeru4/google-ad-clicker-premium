// Типы ответов демона control plane (engine/control_plane/api.py).
// Только то, что UI реально читает: /health, /state и их вложенные объекты.

/** Ответ GET /health: сводка супервизора плюс состояние прогона. */
export interface Health {
  status: string;
  version: number;
  state: string;
  supervisor_alive: boolean;
  workers_total: number;
  workers_alive: number;
  workers_failed: number;
  circuits_open: (string | number)[];
  paused: boolean;
}

/** Строка воркера из снимка GET /state (_SNAPSHOT_COLUMNS в state.py). */
export interface WorkerRow {
  browser_id: string;
  pid: number | null;
  status: string;
  restart_count: number;
  started_at: number | null;
  heartbeat_at: number | null;
  last_error: string | null;
}

/** Ответ GET /state: согласованный срез состояния демона. */
export interface StateSnapshot {
  state: string;
  paused: boolean;
  workers: WorkerRow[];
  worker_count: number;
  alive_count: number;
  updated_at: number;
}
