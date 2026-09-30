// Секция «Очистка профилей» на Settings: статус расписания и ручной запуск
// (план §5, фаза 10: «время и периодичность настраиваются в UI», «отчёт в UI»,
// «ручной запуск очистки по кнопке»).
//
// Статус читается один раз при монтировании, после каждого прогона и по
// кнопке «Обновить» — без опроса по таймеру: расписание меняется только
// сохранением настроек, а гонять GET каждые секунды незачем.
//
// Прогон один на экран: `pending` блокирует вторую кнопку, пока идёт первая,
// — ручные запуски не должны идти в демон параллельно. Отчёт с кнопки живёт
// отдельно от `status.last`: dry_run не двигает метку последнего прогона,
// и превью не должно затирать статус настоящей очистки.

import { computed, ref, type ComputedRef, type Ref } from "vue";
import { errorMessage } from "../lib/control";
import { formatLocalDateTime } from "../lib/format";
import {
  cleanupApi,
  type CleanupApi,
  type CleanupReport,
  type CleanupStatus,
} from "../lib/cleanup";

/** Какое действие сейчас в полёте; null — ничего. */
export type CleanupPending = "run" | "preview";

export interface CleanupState {
  status: Ref<CleanupStatus | null>;
  /** Статус первый раз прочитан: до этого строки — заглушки, а не «нет». */
  loaded: Ref<boolean>;
  loading: Ref<boolean>;
  /** Ошибка чтения статуса; текст чистится первым успешным refresh. */
  statusError: Ref<string | null>;
  /** Ошибка запуска (400 от демона, сеть). */
  actionError: Ref<string | null>;
  /** Итог последнего прогона с кнопки; null — ещё не запускали. */
  report: Ref<CleanupReport | null>;
  pending: Ref<CleanupPending | null>;
  /** «Последняя очистка: …» — одна строка статуса. */
  lastLine: ComputedRef<string>;
  /** «Следующая очистка: …» — вторая строка статуса. */
  nextLine: ComputedRef<string>;
  /** Один тик статуса; параллельный вызов возвращает тот же промис. */
  refresh(): Promise<void>;
  /** Прогон: dryRun=true — превью без удаления. false — не выполнялось. */
  run(dryRun: boolean): Promise<boolean>;
}

export function createCleanup(api: CleanupApi = cleanupApi): CleanupState {
  const status = ref<CleanupStatus | null>(null);
  const loaded = ref(false);
  const loading = ref(false);
  const statusError = ref<string | null>(null);
  const actionError = ref<string | null>(null);
  const report = ref<CleanupReport | null>(null);
  const pending = ref<CleanupPending | null>(null);

  /** Тик в полёте: параллельные вызовы ждут его, а не плодят запросы. */
  let inFlight: Promise<void> | null = null;

  async function runLoad(): Promise<void> {
    loading.value = true;
    try {
      status.value = await api.status();
      loaded.value = true;
      statusError.value = null;
    } catch (caught) {
      statusError.value = errorMessage(caught);
    } finally {
      loading.value = false;
    }
  }

  function refresh(): Promise<void> {
    if (inFlight !== null) return inFlight;
    // Отвязка guard'а — здесь, а не в runLoad: API, бросающий синхронно,
    // завершает runLoad раньше, чем у промиса появится имя, и guard
    // навсегда остался бы «в полёте», зажав статус.
    const load = runLoad().finally(() => {
      if (inFlight === load) inFlight = null;
    });
    inFlight = load;
    return load;
  }

  async function run(dryRun: boolean): Promise<boolean> {
    // Один прогон на экран: вторая кнопка отклоняется до ухода в сеть.
    if (pending.value !== null) return false;
    pending.value = dryRun ? "preview" : "run";
    // Итог прошлого прогона не должен пережить новый: ошибка показывает
    // ошибку, а не старые цифры под ней.
    report.value = null;
    try {
      report.value = await api.run(dryRun);
      actionError.value = null;
      // После реального прогона демон сдвигает метку и next_run — статус
      // обязан это показать, а не ждать повторного открытия экрана.
      await refresh();
      return true;
    } catch (caught) {
      actionError.value = errorMessage(caught);
      return false;
    } finally {
      pending.value = null;
    }
  }

  const lastLine = computed(() => {
    if (!loaded.value) return "—";
    const last = status.value?.last;
    if (!last) return "Последняя очистка: не выполнялась";
    const { removed, skipped_active, errors } = last.report;
    return (
      `Последняя очистка: ${formatLocalDateTime(last.ts)} ` +
      `(удалено ${removed}, пропущено активных ${skipped_active}, ошибок ${errors})`
    );
  });

  const nextLine = computed(() => {
    if (!loaded.value) return "—";
    const nextRun = status.value?.next_run;
    if (nextRun == null) return "Следующая очистка: по расписанию выключено";
    return `Следующая очистка: ${formatLocalDateTime(nextRun)}`;
  });

  return {
    status,
    loaded,
    loading,
    statusError,
    actionError,
    report,
    pending,
    lastLine,
    nextLine,
    refresh,
    run,
  };
}

// Единственный экземпляр на приложение: экран читает одни ref'ы.
let shared: CleanupState | null = null;

export function useCleanup(): CleanupState {
  shared ??= createCleanup();
  return shared;
}
