// Индикатор размера БД экрана Logs: дешёвое чтение при монтировании и по
// таймеру (план §5, фаза 9).
//
// Команда `db_size` — два `stat` без чтения страниц, поэтому таймер дешёвый;
// интервал 45 с — в согласованном окне 30–60 с, чтобы не долбить диск
// чаще, чем индикатору нужна свежесть. База не открыта — тик не зовёт
// команду вовсе: NotOpen на каждые 45 секунд был бы шумом, а не сигналом.

import { ref, type Ref } from "vue";
import { dbApi, dbErrorMessage, type DbApi, type DbSize } from "../lib/dbApi";
import { useDb } from "./useDb";

/** Интервал обновления размера БД. */
export const DB_SIZE_POLL_MS = 45_000;

export interface DbSizeState {
  /** Сумма основного файла и -wal в байтах; null — ещё не читали. */
  bytes: Ref<number | null>;
  /** Путь, которым открыта БД (для отладки в title индикатора). */
  path: Ref<string | null>;
  /** Текст ошибки последнего чтения; null — всё хорошо. */
  error: Ref<string | null>;
  /** Один замер; параллельный замер отсекается. */
  refresh(): Promise<void>;
  /** Замер сразу + по таймеру; повторный вызов — no-op. */
  start(): void;
  stop(): void;
}

export interface CreateDbSizeOptions {
  api: Pick<DbApi, "dbSize">;
  /** БД открыта: вне этого состояния тики команду не зовут. */
  isReady(): boolean;
  /** Интервал таймера; тесты уменьшают, чтобы не ждать. */
  pollMs?: number;
}

export function createDbSize(options: CreateDbSizeOptions): DbSizeState {
  const { api, isReady } = options;
  const pollMs = options.pollMs ?? DB_SIZE_POLL_MS;

  const bytes = ref<number | null>(null);
  const path = ref<string | null>(null);
  const error = ref<string | null>(null);

  let timer: ReturnType<typeof setInterval> | null = null;
  let inFlight = false;

  async function refresh(): Promise<void> {
    if (inFlight || !isReady()) return;
    inFlight = true;
    try {
      const size: DbSize = await api.dbSize();
      bytes.value = size.bytes + size.wal_bytes;
      path.value = size.path;
      error.value = null;
    } catch (caught) {
      error.value = dbErrorMessage(caught);
    } finally {
      inFlight = false;
    }
  }

  function tick(): void {
    void refresh();
  }

  function start(): void {
    if (timer !== null) return;
    void refresh();
    timer = setInterval(tick, pollMs);
  }

  function stop(): void {
    if (timer === null) return;
    clearInterval(timer);
    timer = null;
  }

  return { bytes, path, error, refresh, start, stop };
}

// Единственный экземпляр на приложение: экран Logs читает одни ref'ы.
let shared: DbSizeState | null = null;

export function useDbSize(): DbSizeState {
  shared ??= createDbSize({
    api: dbApi,
    isReady: () => useDb().phase.value === "open",
  });
  return shared;
}
