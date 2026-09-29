// Экран Profiles: список с периодическим обновлением и действия через
// control API демона (добавить / импорт / удалить / назначить / сбросить /
// статус).
//
// Читает экран только список; любая мутация идёт в демон и после успеха
// перечитывает список — ридер БД остаётся read-only. Список прокси тянется
// тем же тиком: колонка «Прокси» резолвит `proxy_id` в label/адрес на
// клиенте, потому что контракт профилей отдаёт только `proxy_id`.

import { ref, type Ref } from "vue";
import { errorMessage } from "../lib/control";
import { tauriTransport } from "../lib/daemonApi";
import {
  createProfilesApi,
  type AssignResult,
  type NewProfile,
  type ProfileChangeResult,
  type ProfileRow,
  type ProfilesApi,
  type UnassignResult,
  type WritableProfileStatus,
} from "../lib/profiles";
import type { ProxyRow } from "../lib/proxies";

/** Интервал обновления списка: 4 с — живо, но без ДДОСа БД и демона. */
export const PROFILES_POLL_MS = 4000;

/** Какое действие сейчас в полёте; null — ничего. */
export type ProfilesPending =
  | "add"
  | "import"
  | "delete"
  | "assign"
  | "unassign"
  | "status";

export interface ProfilesState {
  rows: Ref<ProfileRow[]>;
  /** Список прокси для join'а колонки «Прокси». */
  proxies: Ref<ProxyRow[]>;
  /** Первая загрузка: скелетон, а не мигание таблицы на каждом тике. */
  loading: Ref<boolean>;
  /** Ошибка чтения списка; текст чистится первым успешным тиком. */
  error: Ref<string | null>;
  /** Ошибка действия (добавить/импорт/удалить/назначить/статус). */
  actionError: Ref<string | null>;
  pending: Ref<ProfilesPending | null>;
  /** Итог последнего добавления: added/skipped/problems. */
  addResult: Ref<ProfileChangeResult | null>;
  /** Итог последнего импорта из textarea. */
  importResult: Ref<ProfileChangeResult | null>;
  /** Итог последнего назначения диапазона: assigned/available. */
  assignResult: Ref<AssignResult | null>;
  /** Итог последнего сброса назначений: released. */
  unassignResult: Ref<UnassignResult | null>;
  /** Первый тик + интервал; повторный вызов — no-op. */
  start(): Promise<void>;
  stop(): void;
  /** Один тик списка; параллельный тик возвращает тот же промис. */
  tick(): Promise<void>;
  add(profiles: NewProfile[]): Promise<boolean>;
  importLines(lines: string[]): Promise<boolean>;
  remove(id: number): Promise<boolean>;
  assign(startId: number, endId: number): Promise<boolean>;
  unassign(): Promise<boolean>;
  setStatus(id: number, status: WritableProfileStatus): Promise<boolean>;
}

export function createProfiles(
  api: ProfilesApi = createProfilesApi(tauriTransport),
  pollMs: number = PROFILES_POLL_MS,
): ProfilesState {
  const rows = ref<ProfileRow[]>([]);
  const proxies = ref<ProxyRow[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const actionError = ref<string | null>(null);
  const pending = ref<ProfilesPending | null>(null);
  const addResult = ref<ProfileChangeResult | null>(null);
  const importResult = ref<ProfileChangeResult | null>(null);
  const assignResult = ref<AssignResult | null>(null);
  const unassignResult = ref<UnassignResult | null>(null);

  let timer: ReturnType<typeof setInterval> | null = null;
  /** Тик в полёте: параллельные вызовы ждут его, а не плодят запросы. */
  let inFlight: Promise<void> | null = null;
  /** Экран хочет опрос: stop во время start гасит будущий таймер. */
  let desired = false;
  /** Список уже хотя бы раз читался: скелетон больше не нужен. */
  let loaded = false;

  async function runLoad(): Promise<void> {
    if (!loaded) loading.value = true;
    try {
      // Оба списка с одного демона: если упал прокси-список, колонка
      // «Прокси» показала бы молчаливые прочерки — лучше честная ошибка.
      const [profileList, proxyList] = await Promise.all([
        api.list(),
        api.listProxies(),
      ]);
      rows.value = profileList;
      proxies.value = proxyList;
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
    // Отвязка guard'а — здесь, а не в runLoad: API, бросающий синхронно,
    // завершает runLoad раньше, чем у промиса появится имя, и guard
    // навсегда остался бы «в полёте», зажав опрос.
    const load = runLoad().finally(() => {
      if (inFlight === load) inFlight = null;
    });
    inFlight = load;
    return load;
  }

  /**
   * Общий каркас действия: одна блокировка на экран, чистка прошлой ошибки,
   * перечитывание списка только после успеха.
   */
  async function act(
    kind: ProfilesPending,
    run: () => Promise<void>,
    clearResult?: () => void,
  ): Promise<boolean> {
    if (pending.value !== null) return false;
    pending.value = kind;
    clearResult?.();
    try {
      await run();
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

  async function add(profiles: NewProfile[]): Promise<boolean> {
    return act(
      "add",
      async () => {
        addResult.value = await api.add(profiles);
      },
      () => {
        addResult.value = null;
      },
    );
  }

  async function importLines(lines: string[]): Promise<boolean> {
    return act(
      "import",
      async () => {
        importResult.value = await api.importLines(lines);
      },
      () => {
        importResult.value = null;
      },
    );
  }

  async function remove(id: number): Promise<boolean> {
    return act("delete", async () => {
      await api.remove(id);
    });
  }

  async function assign(startId: number, endId: number): Promise<boolean> {
    return act(
      "assign",
      async () => {
        assignResult.value = await api.assign(startId, endId);
      },
      () => {
        assignResult.value = null;
      },
    );
  }

  async function unassign(): Promise<boolean> {
    return act(
      "unassign",
      async () => {
        unassignResult.value = await api.unassign();
      },
      () => {
        unassignResult.value = null;
      },
    );
  }

  async function setStatus(
    id: number,
    status: WritableProfileStatus,
  ): Promise<boolean> {
    return act("status", async () => {
      await api.setStatus(id, status);
    });
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
    rows,
    proxies,
    loading,
    error,
    actionError,
    pending,
    addResult,
    importResult,
    assignResult,
    unassignResult,
    start,
    stop,
    tick,
    add,
    importLines,
    remove,
    assign,
    unassign,
    setStatus,
  };
}

// Единственный экземпляр на приложение: экран читает одни ref'ы.
let shared: ProfilesState | null = null;

export function useProfiles(): ProfilesState {
  shared ??= createProfiles();
  return shared;
}
