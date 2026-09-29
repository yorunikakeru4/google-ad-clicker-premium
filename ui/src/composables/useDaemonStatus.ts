// Composable опроса демона: индикатор heartbeat + снимок состояния.
//
// Одна копия состояния на приложение (см. useDaemonStatus): App.vue держит
// интервал, панель статуса только читает ref'ы. Тик управляется reducer'ом
// из lib/poll — параллельные тики и опоздавшие ответы отсекаются там.

import { computed, ref, type ComputedRef, type Ref } from "vue";
import {
  errorMessage,
  toStatusView,
  type ControlAction,
  type StatusView,
} from "../lib/control";
import {
  createDaemonApi,
  tauriTransport,
  type DaemonApi,
} from "../lib/daemonApi";
import { createPollState, reducePoll, type PollState } from "../lib/poll";

export const POLL_INTERVAL_MS = 1000;

export interface DaemonStatus {
  state: Ref<PollState>;
  view: ComputedRef<StatusView>;
  busy: Ref<boolean>;
  controlError: Ref<string | null>;
  /** Один тик опроса; повторный вызов во время тика — no-op. */
  tick(): Promise<void>;
  startPolling(intervalMs?: number): void;
  stop(): void;
  /** Отправка команды демону; false — ошибка, текст в controlError. */
  send(action: ControlAction): Promise<boolean>;
}

export function createDaemonStatus(
  options: { api?: DaemonApi } = {},
): DaemonStatus {
  const api = options.api ?? createDaemonApi(tauriTransport);
  const state = ref<PollState>(createPollState());
  const busy = ref(false);
  const controlError = ref<string | null>(null);
  let timer: ReturnType<typeof setInterval> | null = null;

  async function tick(): Promise<void> {
    const begun = reducePoll(state.value, { kind: "begin" });
    if (begun === state.value) return; // тик уже в полёте
    state.value = begun;
    const seq = begun.seq;

    try {
      const [health, snapshot] = await Promise.all([
        api.health(),
        api.state(),
      ]);
      state.value = reducePoll(state.value, {
        kind: "success",
        seq,
        health,
        snapshot,
        at: Date.now(),
      });
    } catch (error) {
      state.value = reducePoll(state.value, {
        kind: "failure",
        seq,
        error: errorMessage(error),
        at: Date.now(),
      });
    }
  }

  function startPolling(intervalMs = POLL_INTERVAL_MS): void {
    if (timer !== null) return; // повторный вызов не плодит интервалы
    void tick();
    timer = setInterval(() => void tick(), intervalMs);
  }

  function stop(): void {
    if (timer === null) return;
    clearInterval(timer);
    timer = null;
  }

  async function send(action: ControlAction): Promise<boolean> {
    busy.value = true;
    try {
      await api.control(action);
      controlError.value = null;
      return true;
    } catch (error) {
      controlError.value = errorMessage(error);
      return false;
    } finally {
      busy.value = false;
    }
  }

  const view = computed(() =>
    toStatusView({
      online: state.value.phase === "online",
      busy: busy.value,
      health: state.value.health,
      state: state.value.snapshot,
    }),
  );

  return { state, view, busy, controlError, tick, startPolling, stop, send };
}

// Единственный экземпляр на приложение: панель статуса, кнопки и будущие
// экраны читают одни и те же ref'ы и один интервал опроса.
let shared: DaemonStatus | null = null;

export function useDaemonStatus(): DaemonStatus {
  shared ??= createDaemonStatus();
  return shared;
}
