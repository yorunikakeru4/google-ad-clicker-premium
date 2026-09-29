// Открытие боевой БД для экранов: один db_open на всё приложение.
//
// Команда зовётся без аргументов — путь решает Rust (env ADCLICKER_DB →
// adclicker.db в cwd, см. resolve_db_path в src-tauri/src/commands.rs).
// Ошибка не ретраится сама: фильтры дашборда/логов молча опрашивали бы
// упавшее состояние каждый тик, поэтому неудача — это явный отказ экрана
// с кнопкой «Повторить», а не спам в консоль.

import { ref, type Ref } from "vue";
import { dbApi, dbErrorMessage, type DbApi } from "../lib/dbApi";

export type DbPhase = "idle" | "opening" | "open" | "failed";

export interface DbStatus {
  phase: Ref<DbPhase>;
  /** Резолвленный путь, которым база открылась. */
  path: Ref<string | null>;
  /** Текст ошибки последнего открытия; null, пока всё хорошо. */
  error: Ref<string | null>;
  /**
   * «Файл просто ещё не создан» — дружелюбное состояние (движок не
   * запускали), а не поломка UI: алерт показывается информационным, а не
   * ошибочным.
   */
  missing: Ref<boolean>;
  /** Открыть БД; повторный вызов во время открытия — no-op. */
  ensureOpen(): Promise<boolean>;
  /** Ручной повтор после ошибки. */
  retry(): Promise<boolean>;
}

export function createDbStatus(api: DbApi = dbApi): DbStatus {
  const phase = ref<DbPhase>("idle");
  const path = ref<string | null>(null);
  const error = ref<string | null>(null);
  const missing = ref(false);
  let inFlight: Promise<boolean> | null = null;

  async function open(): Promise<boolean> {
    if (phase.value === "opening" && inFlight !== null) return inFlight;
    if (phase.value === "open") return true;

    phase.value = "opening";
    const promise = (async () => {
      try {
        path.value = await api.open();
        phase.value = "open";
        error.value = null;
        missing.value = false;
        return true;
      } catch (caught) {
        const text = dbErrorMessage(caught);
        error.value = text;
        missing.value =
          typeof caught === "object" &&
          caught !== null &&
          (caught as { kind?: unknown }).kind === "DatabaseNotFound";
        phase.value = "failed";
        path.value = null;
        return false;
      } finally {
        inFlight = null;
      }
    })();

    inFlight = promise;
    return promise;
  }

  return {
    phase,
    path,
    error,
    missing,
    ensureOpen: open,
    retry: open,
  };
}

// Единственный экземпляр: Dashboard и Logs делят одно открытие соединения.
let shared: DbStatus | null = null;

export function useDb(): DbStatus {
  shared ??= createDbStatus();
  return shared;
}
