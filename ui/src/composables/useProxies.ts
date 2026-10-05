// Экран Proxies: список с периодическим обновлением и действия через
// control API демона (добавить / импорт / удалить / проверить).
//
// Читает экран только список; любая мутация идёт в демон и после успеха
// перечитывает список — ридер БД остаётся read-only. Прогресс проверки
// считается по `last_checked_at` строк: проверка «идёт», пока хотя бы одна
// строка из списка на момент запуска не обновилась (или не исчезла).

import { computed, ref, type ComputedRef, type Ref } from "vue";
import { openPath } from "@tauri-apps/plugin-opener";
import { errorMessage } from "../lib/control";
import { tauriTransport } from "../lib/daemonApi";
import {
  createProxiesApi,
  type FileOpener,
  type ProxiesApi,
  type ProxyChangeResult,
  type ProxyDeleteResult,
  type ProxyRow,
} from "../lib/proxies";

/** Интервал обновления списка: 3 с — живо, но без ДДОСа БД и демона. */
export const PROXIES_POLL_MS = 3000;

/** Какое действие сейчас в полёте; null — ничего. */
export type ProxiesPending = "add" | "import" | "delete" | "check" | "file";

/** Прогресс проверки: сколько строк уже перепроверено из сколько. */
export interface CheckProgress {
  done: number;
  total: number;
}

export interface ProxiesState {
  rows: Ref<ProxyRow[]>;
  /** Первая загрузка: скелетон, а не мигание таблицы на каждом тике. */
  loading: Ref<boolean>;
  /** Ошибка чтения списка; текст чистится первым успешным тиком. */
  error: Ref<string | null>;
  /** Ошибка действия (добавить/импорт/удалить/проверить). */
  actionError: Ref<string | null>;
  pending: Ref<ProxiesPending | null>;
  /** Итог последнего добавления: added/skipped/problems. */
  addResult: Ref<ProxyChangeResult | null>;
  /** Итог последнего импорта из proxies.txt. */
  importResult: Ref<ProxyChangeResult | null>;
  /** Итог последнего батчевого удаления: deleted/skipped/problems. */
  deleteResult: Ref<ProxyDeleteResult | null>;
  /** Проверка запущена и ещё не перепроверила все строки. */
  checking: Ref<boolean>;
  checkProgress: ComputedRef<CheckProgress>;
  /** Первый тик + интервал; повторный вызов — no-op. */
  start(): Promise<void>;
  stop(): void;
  /** Один тик списка; параллельный тик возвращает тот же промис. */
  tick(): Promise<void>;
  add(lines: string[]): Promise<boolean>;
  importFile(): Promise<boolean>;
  remove(id: number): Promise<boolean>;
  /** Батчевое удаление: success — в deleteResult уходит отчёт демона. */
  removeMany(ids: number[]): Promise<boolean>;
  runCheck(): Promise<boolean>;
  /** Открыть proxies.txt в системной программе по умолчанию. */
  openFile(): Promise<boolean>;
}

export function createProxies(
  api: ProxiesApi = createProxiesApi(tauriTransport),
  pollMs: number = PROXIES_POLL_MS,
  opener: FileOpener = openPath,
): ProxiesState {
  const rows = ref<ProxyRow[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const actionError = ref<string | null>(null);
  const pending = ref<ProxiesPending | null>(null);
  const addResult = ref<ProxyChangeResult | null>(null);
  const importResult = ref<ProxyChangeResult | null>(null);
  const deleteResult = ref<ProxyDeleteResult | null>(null);
  const checking = ref(false);
  const checkDone = ref(0);
  const checkTotal = ref(0);

  let timer: ReturnType<typeof setInterval> | null = null;
  /** Тик в полёте: параллельные вызовы ждут его, а не плодят запросы. */
  let inFlight: Promise<void> | null = null;
  /** Экран хочет опрос: stop во время start гасит будущий таймер. */
  let desired = false;
  /** Список уже хотя бы раз читался: скелетон больше не нужен. */
  let loaded = false;
  /** id → last_checked_at на момент запуска проверки (-1 — ещё не checked). */
  let baseline: Map<number, number> | null = null;

  function updateCheckProgress(): void {
    if (!checking.value || baseline === null) return;
    const current = new Map(
      rows.value.map((row) => [row.id, row.last_checked_at ?? -1]),
    );
    let done = 0;
    for (const [id, before] of baseline) {
      const after = current.get(id);
      // Удалённую строку проверять нечего — она уже не «ждёт проверки».
      if (after === undefined || after > before) done += 1;
    }
    checkDone.value = done;
    if (done === baseline.size) {
      checking.value = false;
      baseline = null;
    }
  }

  async function runLoad(): Promise<void> {
    if (!loaded) loading.value = true;
    try {
      const list = await api.list();
      rows.value = list;
      error.value = null;
      updateCheckProgress();
    } catch (caught) {
      error.value = errorMessage(caught);
    } finally {
      loaded = true;
      loading.value = false;
    }
  }

  function tick(): Promise<void> {
    if (inFlight !== null) return inFlight;
    // Отвязка guard'а — здесь, а не в runLoad: API, бросающий синхронно,
    // завершает runLoad раньше, чем у промиса появится имя, и guard
    // навсегда остался бы «в полёте», зажав опрос.
    const load = runLoad().finally(() => {
      if (inFlight === load) inFlight = null;
    });
    inFlight = load;
    return load;
  }

  function beginCheck(): void {
    baseline = new Map(
      rows.value.map((row) => [row.id, row.last_checked_at ?? -1]),
    );
    checkTotal.value = baseline.size;
    checkDone.value = 0;
    // Пустой список проверять нечего: индикатор не показываем вовсе.
    checking.value = baseline.size > 0;
    if (!checking.value) baseline = null;
  }

  async function add(lines: string[]): Promise<boolean> {
    if (pending.value !== null) return false;
    pending.value = "add";
    try {
      const result = await api.add(lines);
      addResult.value = result;
      actionError.value = null;
      await tick();
      return true;
    } catch (caught) {
      actionError.value = errorMessage(caught);
      addResult.value = null;
      return false;
    } finally {
      pending.value = null;
    }
  }

  async function importFile(): Promise<boolean> {
    if (pending.value !== null) return false;
    pending.value = "import";
    try {
      const result = await api.importFile();
      importResult.value = result;
      actionError.value = null;
      await tick();
      return true;
    } catch (caught) {
      actionError.value = errorMessage(caught);
      importResult.value = null;
      return false;
    } finally {
      pending.value = null;
    }
  }

  async function remove(id: number): Promise<boolean> {
    if (pending.value !== null) return false;
    pending.value = "delete";
    try {
      await api.remove(id);
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

  async function removeMany(ids: number[]): Promise<boolean> {
    if (ids.length === 0 || pending.value !== null) return false;
    pending.value = "delete";
    try {
      const result = await api.removeMany(ids);
      deleteResult.value = result;
      actionError.value = null;
      await tick();
      return true;
    } catch (caught) {
      actionError.value = errorMessage(caught);
      deleteResult.value = null;
      return false;
    } finally {
      pending.value = null;
    }
  }

  async function runCheck(): Promise<boolean> {
    if (pending.value !== null) return false;
    pending.value = "check";
    try {
      await api.check();
      actionError.value = null;
      // Baseline снимается до обновления: уже перепроверенные строки и есть
      // прогресс, а не «проверка не началась».
      beginCheck();
      await tick();
      return true;
    } catch (caught) {
      actionError.value = errorMessage(caught);
      return false;
    } finally {
      pending.value = null;
    }
  }

  async function openFile(): Promise<boolean> {
    if (pending.value !== null) return false;
    pending.value = "file";
    try {
      const file = await api.filePath();
      // Файла может ещё не быть (чистая установка): opener в этом случае
      // получил бы путь в никуда, поэтому причина показывается наша.
      if (!file.exists) {
        actionError.value = `Файл прокси не найден: ${file.path}`;
        return false;
      }
      await opener(file.path);
      actionError.value = null;
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

  const checkProgress = computed(() => ({
    done: checkDone.value,
    total: checkTotal.value,
  }));

  return {
    rows,
    loading,
    error,
    actionError,
    pending,
    addResult,
    importResult,
    deleteResult,
    checking,
    checkProgress,
    start,
    stop,
    tick,
    add,
    importFile,
    remove,
    removeMany,
    runCheck,
    openFile,
  };
}

// Единственный экземпляр на приложение: экран читает одни ref'ы.
let shared: ProxiesState | null = null;

export function useProxies(): ProxiesState {
  shared ??= createProxies();
  return shared;
}
