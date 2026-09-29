// Live-логи экрана Logs: опрос страниц, фильтры, пагинация и пауза.
//
// Порядок списка — новые сверху, ровно `ORDER BY ts DESC, id DESC` из БД:
// «подгрузить старше» продолжает список вниз по курсору, автоскролл живого
// режима возвращает к верху. «Живой режим» выключается кнопкой, чтобы читать
// старое, не сбиваясь новыми строками; после включения ближайший тик догоняет
// пропущенное тем же курсорным обходом — без дублей и потерь.
//
// Смена фильтра увеличивает `epoch`: ответы тиков, начатых при старом
// фильтре, отбрасываются и не вписывают чужие строки в обновлённый список.

import { ref, type Ref } from "vue";
import { dbApi, dbErrorMessage, type DbApi } from "../lib/dbApi";
import { useDb } from "./useDb";
import {
  EMPTY_LOG_FILTERS,
  loadLogFilters,
  saveLogFilters,
  sanitizeLogFilters,
  toQueryFilters,
  type LogFilterValues,
  type StorageLike,
} from "../lib/logFilters";
import {
  fetchNewRows,
  mergeLogRows,
  oldestCursor,
  type LogCursor,
  type LogPageFetch,
  type LogRow,
} from "../lib/logMerge";

/** Строк на страницу: первая загрузка, живой тик и подгрузка старых. */
export const LOGS_PAGE_SIZE = 100;

/** Потолок списка в памяти — зеркало MAX_LOGS_LIMIT из src-tauri/src/db.rs. */
export const MAX_LOGS_ROWS = 1000;

/** Интервал живого режима. */
export const LIVE_POLL_MS = 1000;

export interface LogsState {
  filters: Ref<LogFilterValues>;
  /** Строки экрана: новые сверху, без дублей, не больше MAX_LOGS_ROWS. */
  rows: Ref<LogRow[]>;
  /** count_logs под текущими фильтрами; null — ещё не считали. */
  total: Ref<number | null>;
  /** Живой режим: false — пауза, тики базу не читают. */
  live: Ref<boolean>;
  /**
   * Счётчик строк, привезённых живыми тиками (только возрастает): по нему
   * автоскролл отличает «пришло новое» от «подгрузили старое».
   */
  liveAdded: Ref<number>;
  loading: Ref<boolean>;
  loadingOlder: Ref<boolean>;
  /** Старше загружать нечего: база кончилась под этими фильтрами. */
  exhausted: Ref<boolean>;
  error: Ref<string | null>;
  /** Первая загрузка + таймер живого режима; повторный вызов — no-op. */
  start(): Promise<void>;
  stop(): void;
  /** Один тик живого режима; параллельный тик отсекается. */
  tick(): Promise<void>;
  /** Новая форма фильтра: валидация, сохранение, перечитывание списка. */
  updateFilters(patch: Partial<LogFilterValues>): Promise<void>;
  resetFilters(): Promise<void>;
  /** Подгрузка страницы старше самой старой строки. */
  loadOlder(): Promise<void>;
  /** Пауза/продолжение; продолжение сразу догоняет тиком. */
  toggleLive(): void;
}

export interface CreateLogsOptions {
  api: Pick<DbApi, "listLogsPage" | "countLogs">;
  storage: StorageLike;
  /** БД открыта: вне этого состояния тики и перечитывания — no-op. */
  isReady(): boolean;
  /** Строк на страницу; тесты уменьшают, чтобы не плодить строки. */
  pageSize?: number;
}

export function createLogs(options: CreateLogsOptions): LogsState {
  const { api, storage, isReady } = options;
  const pageSize = options.pageSize ?? LOGS_PAGE_SIZE;

  const filters = ref<LogFilterValues>(loadLogFilters(storage));
  const rows = ref<LogRow[]>([]);
  const total = ref<number | null>(null);
  const live = ref(true);
  const liveAdded = ref(0);
  const loading = ref(false);
  const loadingOlder = ref(false);
  const exhausted = ref(false);
  const error = ref<string | null>(null);

  let timer: ReturnType<typeof setInterval> | null = null;
  let ticking = false;
  /** Экран хочет живой режим: stop во время start гасит будущий таймер. */
  let desired = false;
  /** Поколение фильтра: ответы старого поколения в новый список не идут. */
  let epoch = 0;

  function pageFetch(query = toQueryFilters(filters.value)): LogPageFetch {
    return (cursor, limit) =>
      api.listLogsPage({ query, limit, cursor });
  }

  async function reload(): Promise<void> {
    if (!isReady()) return;
    epoch += 1;
    const myEpoch = epoch;

    loading.value = true;
    exhausted.value = false;
    rows.value = [];
    total.value = null;
    error.value = null;

    try {
      const query = toQueryFilters(filters.value);
      const [first, count] = await Promise.all([
        api.listLogsPage({ query, limit: pageSize, cursor: null }),
        api.countLogs(query),
      ]);
      if (myEpoch !== epoch) return;
      rows.value = mergeLogRows([], first, MAX_LOGS_ROWS);
      total.value = count;
    } catch (caught) {
      if (myEpoch !== epoch) return;
      error.value = dbErrorMessage(caught);
    } finally {
      if (myEpoch === epoch) loading.value = false;
    }
  }

  async function tick(): Promise<void> {
    if (!live.value || ticking || !isReady()) return;
    ticking = true;
    try {
      const myEpoch = epoch;
      const query = toQueryFilters(filters.value);
      // rows[0] — самая новая строка (порядок DESC), её позиция и есть курсор.
      const newest: LogCursor | null =
        rows.value.length > 0
          ? { ts: rows.value[0].ts, id: rows.value[0].id }
          : null;

      const fresh = await fetchNewRows(
        pageFetch(query),
        newest,
        pageSize,
        MAX_LOGS_ROWS,
      );
      if (myEpoch !== epoch) return;

      if (fresh.length > 0) {
        rows.value = mergeLogRows(rows.value, fresh, MAX_LOGS_ROWS);
        liveAdded.value += fresh.length;
        const count = await api.countLogs(query);
        if (myEpoch === epoch) total.value = count;
      }
    } catch (caught) {
      error.value = dbErrorMessage(caught);
    } finally {
      ticking = false;
    }
  }

  async function loadOlder(): Promise<void> {
    if (loadingOlder.value || exhausted.value || !isReady()) return;
    if (rows.value.length >= MAX_LOGS_ROWS) return;
    const cursor = oldestCursor(rows.value);
    if (cursor === null) return;

    loadingOlder.value = true;
    const myEpoch = epoch;
    try {
      const page = await api.listLogsPage({
        query: toQueryFilters(filters.value),
        limit: pageSize,
        cursor,
      });
      if (myEpoch !== epoch) return;
      if (page.length === 0) {
        exhausted.value = true;
      } else {
        rows.value = mergeLogRows(rows.value, page, MAX_LOGS_ROWS);
        if (page.length < pageSize) exhausted.value = true;
      }
    } catch (caught) {
      if (myEpoch === epoch) error.value = dbErrorMessage(caught);
    } finally {
      loadingOlder.value = false;
    }
  }

  async function updateFilters(
    patch: Partial<LogFilterValues>,
  ): Promise<void> {
    const next = sanitizeLogFilters({ ...filters.value, ...patch });
    if (JSON.stringify(next) === JSON.stringify(filters.value)) return;
    filters.value = next;
    saveLogFilters(storage, next);
    await reload();
  }

  async function resetFilters(): Promise<void> {
    await updateFilters({ ...EMPTY_LOG_FILTERS });
  }

  function toggleLive(): void {
    live.value = !live.value;
    if (live.value) void tick();
  }

  async function start(): Promise<void> {
    desired = true;
    if (timer !== null) return;
    await reload();
    if (!desired || timer !== null) return; // stop/повторный start за время reload
    timer = setInterval(() => void tick(), LIVE_POLL_MS);
  }

  function stop(): void {
    desired = false;
    if (timer === null) return;
    clearInterval(timer);
    timer = null;
  }

  return {
    filters,
    rows,
    total,
    live,
    liveAdded,
    loading,
    loadingOlder,
    exhausted,
    error,
    start,
    stop,
    tick,
    updateFilters,
    resetFilters,
    loadOlder,
    toggleLive,
  };
}

// Единственный экземпляр на приложение: экран читает одни ref'ы.
let shared: LogsState | null = null;

export function useLogs(): LogsState {
  shared ??= createLogs({
    api: dbApi,
    storage: window.localStorage,
    isReady: () => useDb().phase.value === "open",
  });
  return shared;
}
