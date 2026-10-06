// Опрос и действия списков (Key Words, Domains).
//
// Один композитор на оба экрана: списки живут в файлах, владеет ими демон,
// поэтому после каждого действия снапшот перечитывается — ровно как у
// прокси, только опрос медленнее (5 с): файлы правит не только UI.
//
// Правит список демон, а не экран: мутация уходит в control API и после
// успеха обновляет снапшот, а ошибки (400/500) приходят в actionError с
// текстом демона.

import { computed, ref, type ComputedRef, type Ref } from "vue";
import { openPath } from "@tauri-apps/plugin-opener";
import { errorMessage } from "../lib/control";
import type {
  FileOpener,
  WordlistApi,
  WordlistChangeResult,
  WordlistDeleteResult,
} from "../lib/wordlist";

/** Интервал обновления списка: 5 с — файлы меняются реже, чем пул прокси. */
export const WORDLIST_POLL_MS = 5000;

/** Какое действие сейчас в полёте; null — ничего. */
export type WordlistPending = "add" | "delete" | "file";

export interface WordlistOptions<T> {
  /** Список строк из снапшота: queries у запросов, domains у доменов. */
  items(snapshot: T): string[];
  /** Имя файла для сообщений вида «Файл queries.txt не найден». */
  fileLabel: string;
  pollMs?: number;
  opener?: FileOpener;
}

export interface WordlistState<T> {
  snapshot: Ref<T | null>;
  /** Строки списка; пусто до первого ответа демона. */
  items: ComputedRef<string[]>;
  /** Первая загрузка: скелетон, а не мигание таблицы на каждом тике. */
  loading: Ref<boolean>;
  /** Ошибка чтения списка; чистится первым успешным тиком. */
  error: Ref<string | null>;
  /** Ошибка действия (добавить/удалить/открыть файл). */
  actionError: Ref<string | null>;
  pending: Ref<WordlistPending | null>;
  /** Итог последнего добавления: added/skipped/problems. */
  addResult: Ref<WordlistChangeResult | null>;
  /** Итог последнего удаления. */
  deleteResult: Ref<WordlistDeleteResult | null>;
  /** Первый тик + интервал; повторный вызов — no-op. */
  start(): Promise<void>;
  stop(): void;
  /** Один тик снапшота; параллельный тик возвращает тот же промис. */
  tick(): Promise<void>;
  add(lines: string[]): Promise<boolean>;
  remove(values: string[]): Promise<boolean>;
  /** Весь список; отчёт — тот же deleteResult. */
  removeAll(): Promise<boolean>;
  /** Открыть файл списка в системной программе по умолчанию. */
  openFile(): Promise<boolean>;
}

export function createWordlist<T>(
  api: WordlistApi<T>,
  options: WordlistOptions<T>,
): WordlistState<T> {
  const pollMs = options.pollMs ?? WORDLIST_POLL_MS;
  const opener = options.opener ?? openPath;

  // as Ref<T | null>: ref<T | null> из-за generic T возвращает
  // Ref<UnwrapRef<T> | null>, а контракт экрана — именно Ref<T | null>.
  const snapshot = ref<T | null>(null) as Ref<T | null>;
  const loading = ref(false);
  const error = ref<string | null>(null);
  const actionError = ref<string | null>(null);
  const pending = ref<WordlistPending | null>(null);
  const addResult = ref<WordlistChangeResult | null>(null);
  const deleteResult = ref<WordlistDeleteResult | null>(null);

  const items = computed(() =>
    snapshot.value === null ? [] : options.items(snapshot.value),
  );

  let timer: ReturnType<typeof setInterval> | null = null;
  /** Тик в полёте: параллельные вызовы ждут его, а не плодят запросы. */
  let inFlight: Promise<void> | null = null;
  /** Экран хочет опрос: stop во время start гасит будущий таймер. */
  let desired = false;
  /** Снапшот уже хотя бы раз читался: скелетон больше не нужен. */
  let loaded = false;

  async function runLoad(): Promise<void> {
    if (!loaded) loading.value = true;
    try {
      snapshot.value = await api.list();
      error.value = null;
    } catch (caught) {
      error.value = errorMessage(caught);
    } finally {
      loaded = true;
      loading.value = false;
    }
  }

  function tick(): Promise<void> {
    if (inFlight !== null) return inFlight;
    // Отвязка guard'а здесь, а не в runLoad: API, бросающий синхронно,
    // завершает runLoad раньше, чем у промиса появится имя.
    const load = runLoad().finally(() => {
      if (inFlight === load) inFlight = null;
    });
    inFlight = load;
    return load;
  }

  async function add(lines: string[]): Promise<boolean> {
    if (lines.length === 0 || pending.value !== null) return false;
    pending.value = "add";
    try {
      addResult.value = await api.add(lines);
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

  async function remove(values: string[]): Promise<boolean> {
    if (values.length === 0 || pending.value !== null) return false;
    pending.value = "delete";
    try {
      deleteResult.value = await api.remove(values);
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

  async function removeAll(): Promise<boolean> {
    if (pending.value !== null) return false;
    pending.value = "delete";
    try {
      deleteResult.value = await api.removeAll();
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

  async function openFile(): Promise<boolean> {
    if (pending.value !== null) return false;
    pending.value = "file";
    try {
      const file = await api.filePath();
      // Файла может ещё не быть (чистая установка): opener в этом случае
      // получил бы путь в никуда, поэтому причина показывается наша.
      if (!file.exists) {
        actionError.value = `Файл ${options.fileLabel} не найден: ${file.path}`;
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

  return {
    snapshot,
    items,
    loading,
    error,
    actionError,
    pending,
    addResult,
    deleteResult,
    start,
    stop,
    tick,
    add,
    remove,
    removeAll,
    openFile,
  };
}
