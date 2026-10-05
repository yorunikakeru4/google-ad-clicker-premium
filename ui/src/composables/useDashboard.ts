// Данные дашборда: одна выборка метрик из Rust-команд + аптайм демона.
//
// Метрики читаются реже опроса демона (каждые 5 с): они агрегируются из
// SQLite и не меняются каждый тик. Окно — сутки: сценарии, клики, CAPTCHA и
// запросы считаются от now − 24 ч, чтобы у графиков и карточек была одна
// и та же база отсчёта.

import { ref, watch, watchEffect, type Ref } from "vue";
import {
  dbApi,
  dbErrorMessage,
  type ActiveWorker,
  type DbApi,
  type RequestsLastHour,
  type RunsSummary,
  type UptimeSummary,
} from "../lib/dbApi";
import { alignCounts, buildHourlyBuckets, hourLabel } from "../lib/series";
import { useDaemonStatus } from "./useDaemonStatus";
import { useDb } from "./useDb";

/** Окно дашборда — сутки: сценарии, клики, CAPTCHA и запросы за 24 часа. */
export const DASHBOARD_WINDOW_SECONDS = 24 * 60 * 60;

/**
 * Окно карточки Uptime — часы, а не секунды: Rust-команда строит окно по
 * часовой сетке от текущего бакета. 24 часа — то же окно, что у соседних
 * карточек, чтобы доли читались в одном масштабе.
 */
export const UPTIME_WINDOW_HOURS = DASHBOARD_WINDOW_SECONDS / 3600;

/** Обновление метрик: реже опроса демона — выборка из SQLite дороже тика. */
export const DASHBOARD_REFRESH_MS = 5000;

/**
 * Свежесть heartbeat для «активных воркеров»: три интервала heartbeat (5 с)
 * с запасом — тот же порог, что `DEFAULT_STALE_AFTER_SECONDS` в
 * engine/control_plane/supervisor.py.
 */
export const ACTIVE_WORKER_STALE_SECS = 15;

export interface DashboardSeries {
  /** Часовые метки окна, локальные HH:MM. */
  labels: string[];
  /** Клики по часам, выровненные по labels. */
  clicks: number[];
  /** События CAPTCHA по часам, выровненные по labels. */
  captcha: number[];
  /** Нагрузка на воркеры за последний час (подписи и запросы). */
  loadLabels: string[];
  loadValues: number[];
}

const EMPTY_SERIES: DashboardSeries = {
  labels: [],
  clicks: [],
  captcha: [],
  loadLabels: [],
  loadValues: [],
};

export interface DashboardState {
  runs: Ref<RunsSummary | null>;
  requests: Ref<RequestsLastHour | null>;
  captchaShare: Ref<number | null>;
  /** Uptime-доля за окно UPTIME_WINDOW_HOURS; null — ещё не читалась. */
  uptime: Ref<UptimeSummary | null>;
  workers: Ref<ActiveWorker[]>;
  series: Ref<DashboardSeries>;
  /** Текст ошибки последней выборки; null — всё прочитано. */
  error: Ref<string | null>;
  loading: Ref<boolean>;
  /**
   * Момент, с которого демон непрерывно отвечает (эпоха, мс); null — офлайн.
   * Аптайм процесса демона control plane не отдаёт, поэтому UI честно меряет
   * непрерывную доступность со своего первого ответа.
   */
  onlineSince: Ref<number | null>;
  /** Секунд непрерывной доступности; null — демон недоступен. */
  uptimeSeconds: Ref<number | null>;
  refresh(): Promise<void>;
  /** Цикл обновления метрик; повторный вызов — no-op. */
  start(): void;
  stop(): void;
}

export function createDashboard(api: DbApi = dbApi): DashboardState {
  const db = useDb();
  const status = useDaemonStatus();

  const runs = ref<RunsSummary | null>(null);
  const requests = ref<RequestsLastHour | null>(null);
  const captchaShare = ref<number | null>(null);
  const uptime = ref<UptimeSummary | null>(null);
  const workers = ref<ActiveWorker[]>([]);
  const series = ref<DashboardSeries>(EMPTY_SERIES);
  const error = ref<string | null>(null);
  const loading = ref(false);
  const onlineSince = ref<number | null>(null);
  const uptimeSeconds = ref<number | null>(null);

  let timer: ReturnType<typeof setInterval> | null = null;

  async function refresh(): Promise<void> {
    if (db.phase.value !== "open" || loading.value) return;

    loading.value = true;
    try {
      const now = Date.now() / 1000;
      const since = now - DASHBOARD_WINDOW_SECONDS;
      const buckets = buildHourlyBuckets(since, now);

      const [summary, clicks, load, share, active, captchas, uptimeSummary] =
        await Promise.all([
          api.runsSummary(since),
          api.clicksPerHour(since, buckets.length),
          api.requestsLastHour(now),
          api.captchaShare(since),
          api.activeWorkers(now, ACTIVE_WORKER_STALE_SECS),
          api.captchasPerHour(since, buckets.length),
          api.uptimeSummary(UPTIME_WINDOW_HOURS),
        ]);

      runs.value = summary;
      requests.value = load;
      captchaShare.value = share;
      uptime.value = uptimeSummary;
      workers.value = active;
      series.value = {
        labels: buckets.map(hourLabel),
        clicks: alignCounts(clicks, buckets),
        captcha: alignCounts(captchas, buckets),
        loadLabels: load.per_browser.map((row) => row.browser_id ?? "—"),
        loadValues: load.per_browser.map((row) => row.count),
      };
      error.value = null;
    } catch (caught) {
      // Одна строка ошибки вместо спама: следующая удачная выборка её снимет.
      error.value = dbErrorMessage(caught);
    } finally {
      loading.value = false;
    }
  }

  function start(): void {
    if (timer !== null) return;
    void refresh();
    timer = setInterval(() => {
      if (db.phase.value === "open") void refresh();
    }, DASHBOARD_REFRESH_MS);
  }

  function stop(): void {
    if (timer === null) return;
    clearInterval(timer);
    timer = null;
  }

  // База могла открыться позже (retry в алерте): фаза — сигнал обновиться.
  // Watch живёт столько, сколько composable, а не столько, сколько экран:
  // инстанс один, дублей наблюдателей не будет.
  watch(db.phase, (phase) => {
    if (phase === "open") void refresh();
  });

  // Аптайм: первый ответ демона запускает отсчёт, офлайн его обнуляет —
  // показываем именно непрерывную доступность, а не «сколько живёт UI».
  watchEffect(() => {
    if (status.state.value.phase !== "online") {
      onlineSince.value = null;
      uptimeSeconds.value = null;
      return;
    }
    if (onlineSince.value === null) onlineSince.value = Date.now();
    uptimeSeconds.value = (Date.now() - onlineSince.value) / 1000;
  });

  return {
    runs,
    requests,
    captchaShare,
    uptime,
    workers,
    series,
    error,
    loading,
    onlineSince,
    uptimeSeconds,
    refresh,
    start,
    stop,
  };
}

// Единственный цикл на приложение: экраны читают одни ref'ы.
let shared: DashboardState | null = null;

export function useDashboard(): DashboardState {
  shared ??= createDashboard();
  return shared;
}
