// Экран Diagnostics: последние снимки сессий с периодическим обновлением и
// сбор по кнопке через control API демона.
//
// Чтение — только Rust-читалка (`list_diagnostics`): последний снимок на
// воркера плюс живые воркеры, чтобы карточка без данных была видна сразу.
// Сбор уходит в демон, а строка появляется в БД асинхронно — её и подхватывает
// обычный тик опроса, никакой отдельной «дожидашки».
//
// Тики без открытой БД — no-op, а не ошибка чтения: экран честно ждёт, пока
// база откроется (состояние показывает DbUnavailableAlert), и не пугает
// «База не открыта» каждые 4 с.

import { computed, ref, type ComputedRef, type Ref } from "vue";
import { errorMessage } from "../lib/control";
import { tauriTransport } from "../lib/daemonApi";
import { dbApi, dbErrorMessage } from "../lib/dbApi";
import {
  buildDiagnosticCards,
  createDiagnosticsApi,
  type DiagnosticCard,
  type DiagnosticSnapshot,
  type DiagnosticsApi,
} from "../lib/diagnostics";
import { useDb } from "./useDb";

/** Интервал обновления снимков: 4 с — свежесть без ДДОСа БД и читалки. */
export const DIAGNOSTICS_POLL_MS = 4000;

/** Какое действие сейчас в полёте; null — ничего. */
export type DiagnosticsPending = "collect" | "collect-all";

/** Итог последнего сбора: сколько воркеров демон взял в работу. */
export interface CollectResult {
  requested: number;
}

export interface CreateDiagnosticsOptions {
  /** БД открыта: вне этого состояния тики — no-op, а не ошибка чтения. */
  isReady?: () => boolean;
  /** Интервал опроса; по умолчанию [`DIAGNOSTICS_POLL_MS`]. */
  pollMs?: number;
}

export interface DiagnosticsState {
  /** Последние снимки из читалки: по одному на воркера. */
  snapshots: Ref<DiagnosticSnapshot[]>;
  /** browser_id воркеров с живым heartbeat. */
  workerIds: Ref<string[]>;
  /** Карточки экрана: снимки ∪ живые воркеры без снимка. */
  cards: ComputedRef<DiagnosticCard[]>;
  /** Первая загрузка: скелетон, а не мигание сетки на каждом тике. */
  loading: Ref<boolean>;
  /** Ошибка чтения; текст чистится первым успешным тиком. */
  error: Ref<string | null>;
  /** Ошибка сбора (400 от демона, сеть). */
  actionError: Ref<string | null>;
  pending: Ref<DiagnosticsPending | null>;
  /** Итог последнего сбора; null — сбор ещё не звали или он упал. */
  collectResult: Ref<CollectResult | null>;
  /** Первый тик + интервал; повторный вызов — no-op. */
  start(): Promise<void>;
  stop(): void;
  /** Один тик списка; параллельный тик возвращает тот же промис. */
  tick(): Promise<void>;
  /** Сбор для одного воркера. */
  collect(browserId: string): Promise<boolean>;
  /** Сбор для всех воркеров. */
  collectAll(): Promise<boolean>;
}

export function createDiagnostics(
  api: DiagnosticsApi = createDiagnosticsApi(dbApi, tauriTransport),
  options: CreateDiagnosticsOptions = {},
): DiagnosticsState {
  const isReady = options.isReady ?? (() => true);
  const pollMs = options.pollMs ?? DIAGNOSTICS_POLL_MS;

  const snapshots = ref<DiagnosticSnapshot[]>([]);
  const workerIds = ref<string[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const actionError = ref<string | null>(null);
  const pending = ref<DiagnosticsPending | null>(null);
  const collectResult = ref<CollectResult | null>(null);

  let timer: ReturnType<typeof setInterval> | null = null;
  /** Тик в полёте: параллельные вызовы ждут его, а не плодят запросы. */
  let inFlight: Promise<void> | null = null;
  /** Экран хочет опрос: stop во время start гасит будущий таймер. */
  let desired = false;
  /** Список уже хотя бы раз читался: скелетон больше не нужен. */
  let loaded = false;

  async function runLoad(): Promise<void> {
    if (!isReady()) return;
    if (!loaded) loading.value = true;
    try {
      // Оба чтения с одной читалки: карточки без снимка живут по воркерам,
      // поэтому рассинхрон двух выборок дал бы мигание лишней карточки.
      const [snapshotRows, liveIds] = await Promise.all([
        api.list(),
        api.liveWorkerIds(),
      ]);
      snapshots.value = snapshotRows;
      workerIds.value = liveIds;
      error.value = null;
    } catch (caught) {
      error.value = dbErrorMessage(caught);
    } finally {
      loaded = true;
      loading.value = false;
    }
  }

  function tick(): Promise<void> {
    if (inFlight !== null) return inFlight;
    const load = runLoad().finally(() => {
      if (inFlight === load) inFlight = null;
    });
    inFlight = load;
    return load;
  }

  /**
   * Общий каркас сбора: одна блокировка на экран, чистка прошлого итога,
   * перечитывание списка только после успеха.
   */
  async function collectWith(
    kind: DiagnosticsPending,
    browserId: string | null,
  ): Promise<boolean> {
    if (pending.value !== null) return false;
    pending.value = kind;
    collectResult.value = null;
    try {
      const requested = await api.collect(browserId);
      collectResult.value = { requested };
      actionError.value = null;
      await tick();
      return true;
    } catch (caught) {
      actionError.value = errorMessage(caught);
      return false;
    } finally {
      pending.value = null;
    }
  }

  async function start(): Promise<void> {
    desired = true;
    if (timer !== null) return;
    await tick();
    if (!desired || timer !== null) return; // stop/повторный start за время тика
    timer = setInterval(() => void tick(), pollMs);
  }

  function stop(): void {
    desired = false;
    if (timer === null) return;
    clearInterval(timer);
    timer = null;
  }

  const cards = computed(() => buildDiagnosticCards(snapshots.value, workerIds.value));

  return {
    snapshots,
    workerIds,
    cards,
    loading,
    error,
    actionError,
    pending,
    collectResult,
    start,
    stop,
    tick,
    collect: (browserId) => collectWith("collect", browserId),
    collectAll: () => collectWith("collect-all", null),
  };
}

// Единственный экземпляр на приложение: экран читает одни ref'ы.
let shared: DiagnosticsState | null = null;

export function useDiagnostics(): DiagnosticsState {
  shared ??= createDiagnostics(createDiagnosticsApi(dbApi, tauriTransport), {
    isReady: () => useDb().phase.value === "open",
    pollMs: DIAGNOSTICS_POLL_MS,
  });
  return shared;
}
