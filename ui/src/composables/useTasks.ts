// Экран Tasks: конфиг демона по требованию.
//
// В отличие от useDaemonStatus опроса здесь нет: конфиг меняется редко и
// только через Settings, поэтому GET /control/config уходит один раз при
// монтировании экрана и по кнопке «Обновить». Параллельные вызовы склеиваются
// в один запрос, чтобы двойной клик не плодил обращения к демону.

import { ref, type Ref } from "vue";
import { errorMessage } from "../lib/control";
import { tauriTransport } from "../lib/daemonApi";
import {
  createTasksApi,
  type TasksApi,
  type TasksConfig,
} from "../lib/tasks";

export type { TasksApi, TasksConfig };

export interface TasksState {
  /** Конфиг демона; null — ещё не читали или демон не ответил. */
  config: Ref<TasksConfig | null>;
  /** Чтение в полёте (скелетон кнопки «Обновить»). */
  loading: Ref<boolean>;
  /** Ошибка последнего чтения; чистится первым успешным load. */
  error: Ref<string | null>;
  /** Один GET /control/config; параллельные вызовы делят один запрос. */
  load(): Promise<boolean>;
}

export function createTasks(
  api: TasksApi = createTasksApi(tauriTransport),
): TasksState {
  const config = ref<TasksConfig | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);
  /** Текущий запрос; null — ничего не в полёте. */
  let inFlight: Promise<boolean> | null = null;

  async function runLoad(): Promise<boolean> {
    loading.value = true;
    try {
      config.value = await api.loadConfig();
      error.value = null;
      return true;
    } catch (caught) {
      error.value = errorMessage(caught);
      return false;
    } finally {
      loading.value = false;
    }
  }

  function load(): Promise<boolean> {
    if (inFlight !== null) return inFlight;
    // Отвязка guard'а здесь, а не в runLoad: API, бросающий синхронно,
    // завершает runLoad раньше, чем у промиса появится имя.
    const request = runLoad().finally(() => {
      if (inFlight === request) inFlight = null;
    });
    inFlight = request;
    return request;
  }

  return { config, loading, error, load };
}

// Единственный экземпляр на приложение: экран Tasks читает одни ref'ы.
let shared: TasksState | null = null;

export function useTasks(): TasksState {
  shared ??= createTasks();
  return shared;
}
