<script setup lang="ts">
// Dashboard (план §5, фазы 4 и 8): успешные/неуспешные сценарии, аптайм
// демона, запросы/час как проверяемое утверждение ≥50, доля CAPTCHA с
// порогом 5% и три графика (клики/час, CAPTCHA по часам, нагрузка на
// воркеры). Под метриками — таблица воркеров из /state (WorkersTable): опрос
// и AppShell, экран только читает снимок; воркеры с нерешённой капчей за
// 30 минут получают warning-чип — значение приходит из ленты CAPTCHA,
// отдельного запроса по воркерам нет. Ниже — лента последних событий
// CAPTCHA с кнопкой скриншота и всплывающее уведомление о новом событии.
//
// Вёрстка — шаблон экрана: PageLayout + MetricCard + StatusChip. Пороговые
// правила остаются в lib/thresholds, данные — в useDashboard: карточка только
// показывает утверждение цветом и текстом, ничего не пересчитывает.
import { computed, onMounted, onUnmounted } from "vue";
import BarChartCard from "../components/charts/BarChartCard.vue";
import CaptchaFeedCard from "../components/data/CaptchaFeedCard.vue";
import DbUnavailableAlert from "../components/DbUnavailableAlert.vue";
import MetricCard from "../components/data/MetricCard.vue";
import PageLayout from "../components/layout/PageLayout.vue";
import CaptchaNoticeAlert from "../components/status/CaptchaNoticeAlert.vue";
import WorkersTable from "../components/WorkersTable.vue";
import type { StatusKind } from "../constants/statusMap";
import { useCaptchaFeed } from "../composables/useCaptchaFeed";
import { useDashboard } from "../composables/useDashboard";
import { useDaemonStatus } from "../composables/useDaemonStatus";
import { useDb } from "../composables/useDb";
import { formatLastError, formatUptime } from "../lib/format";
import { toneToStatus } from "../lib/thresholdStatus";
import {
  REQUESTS_PER_HOUR_TARGET,
  captchaShareStatus,
  formatCaptchaShare,
  requestsPerHourStatus,
} from "../lib/thresholds";

const db = useDb();
const dash = useDashboard();
// Лента CAPTCHA: события, уведомления о новых и подсветка воркеров —
// один цикл опроса на экран, как у метрик.
const feed = useCaptchaFeed();
// Опрос /state уже идёт в AppShell: здесь читается тот же снимок, без
// собственного интервала (план §5, фаза 4).
const daemon = useDaemonStatus();

onMounted(() => {
  void db.ensureOpen();
  dash.start();
  void feed.start();
});
onUnmounted(() => {
  dash.stop();
  feed.stop();
});

const requestsStatus = computed(() => {
  const load = dash.requests.value;
  return load === null ? null : requestsPerHourStatus(load.total);
});

/** Текст утверждения — не просто число: выполнено/не выполнено явно. */
const requestsClaim = computed(() => {
  const load = dash.requests.value;
  const status = requestsStatus.value;
  if (load === null || status === null) return "нет данных";
  return status.met
    ? `≥${REQUESTS_PER_HOUR_TARGET}/час: выполнено (${load.total})`
    : `≥${REQUESTS_PER_HOUR_TARGET}/час: не выполнено (${load.total})`;
});

/** Статус карточки: порог выполнен/нарушен, данных нет — idle, не ошибка. */
const requestsStatusKind = computed<StatusKind>(() =>
  requestsStatus.value === null ? "idle" : toneToStatus(requestsStatus.value.tone),
);

const requestsTotal = computed(() => {
  const load = dash.requests.value;
  return load === null ? undefined : String(load.total);
});

const shareStatus = computed(() => captchaShareStatus(dash.captchaShare.value));

const shareClaim = computed(() => {
  const status = shareStatus.value;
  if (!status.known) return "нет данных за окно — н/д";
  return status.withinLimit
    ? "доля CAPTCHA < 5% — норма"
    : "доля CAPTCHA ≥ 5% — порог превышен";
});

const captchaStatusKind = computed<StatusKind>(() =>
  toneToStatus(shareStatus.value.tone),
);

// Аптайм пересчитывается на каждом тике опроса: uptimeSeconds меняется
// каждую секунду, поэтому строка не застынет на «00:00:01».
const uptimeLabel = computed(() => {
  const seconds = dash.uptimeSeconds.value;
  if (seconds == null) return "—";
  const now = Date.now() / 1000;
  return formatUptime(now - seconds, now);
});

const lastError = computed(() => dash.runs.value?.last_error ?? null);

const workersLabel = computed(() => String(dash.workers.value.length));

/** Всего сценарий за окно: показывает масштаб, разбивка — в чипах ниже. */
const runsTotal = computed(() => {
  const runs = dash.runs.value;
  if (runs === null) return undefined;
  return String(runs.succeeded + runs.failed + runs.other);
});

const demoWindowHint = "окно 24 часа";
</script>

<template>
  <PageLayout title="Dashboard" subtitle="Сценарии, запросы/час и доля CAPTCHA">
    <DbUnavailableAlert class="mb-4" />

    <!-- Уведомление о новом событии: очередь и дедупликация — в composable -->
    <CaptchaNoticeAlert
      :notice="feed.notice.value"
      @dismiss="feed.dismiss"
    />

    <v-alert
      v-if="dash.error.value"
      type="error"
      variant="tonal"
      class="mb-4"
      data-test="dashboard-error"
    >
      {{ dash.error.value }}
    </v-alert>

    <v-progress-linear
      v-if="dash.loading.value && dash.runs.value === null"
      indeterminate
      class="mb-4"
      data-test="dashboard-loading"
    />

    <v-row dense>
      <v-col cols="12" md="6" lg="3">
        <MetricCard
          data-test="card-runs"
          label="Сценарии"
          :hint="demoWindowHint"
          :value="runsTotal"
          value-test="runs-total"
        >
          <template #default>
            <div class="d-flex flex-wrap ga-2 mb-2">
              <v-chip color="success" variant="tonal" size="small" data-test="runs-succeeded">
                {{ dash.runs.value?.succeeded ?? "—" }} успешно
              </v-chip>
              <v-chip color="error" variant="tonal" size="small" data-test="runs-failed">
                {{ dash.runs.value?.failed ?? "—" }} с ошибкой
              </v-chip>
              <v-chip variant="tonal" size="small" data-test="runs-other">
                {{ dash.runs.value?.other ?? "—" }} другое
              </v-chip>
            </div>
            <div class="text-body-2 text-muted">
              Последняя ошибка:
              <span
                class="text-truncate d-inline-block"
                style="max-width: 100%"
                :title="lastError ?? ''"
                data-test="runs-last-error"
              >
                {{ formatLastError(lastError) }}
              </span>
            </div>
          </template>
        </MetricCard>
      </v-col>

      <v-col cols="12" md="6" lg="3">
        <MetricCard
          data-test="card-uptime"
          label="Время работы"
          :value="uptimeLabel"
          value-test="uptime-value"
        >
          <template #default>
            <div class="text-body-2 text-muted mb-2">
              демон отвечает непрерывно
            </div>
            <v-chip color="primary" variant="tonal" size="small" data-test="active-workers">
              активные воркеры: {{ workersLabel }}
            </v-chip>
          </template>
        </MetricCard>
      </v-col>

      <v-col cols="12" md="6" lg="3">
        <MetricCard
          data-test="card-requests"
          label="Запросы/час"
          :value="requestsTotal"
          value-test="requests-total"
          :status="requestsStatusKind"
          :status-label="requestsClaim"
          status-test="requests-claim"
        />
      </v-col>

      <v-col cols="12" md="6" lg="3">
        <MetricCard
          data-test="card-captcha"
          label="Доля CAPTCHA"
          :value="formatCaptchaShare(dash.captchaShare.value)"
          value-test="captcha-value"
          :status="captchaStatusKind"
          :status-label="shareClaim"
          status-test="captcha-claim"
        />
      </v-col>
    </v-row>

    <!-- Воркеры из /state: под метриками, до графиков (план §5, фаза 4).
         Подсветка CAPTCHA — производная от ленты ниже, не новый запрос. -->
    <v-row dense class="mt-1">
      <v-col cols="12">
        <WorkersTable
          :phase="daemon.state.value.phase"
          :snapshot="daemon.state.value.snapshot"
          :captcha-alert-ids="feed.alertIds.value"
        />
      </v-col>
    </v-row>

    <!-- Лента событий CAPTCHA: последние строки captcha_events -->
    <v-row dense class="mt-1">
      <v-col cols="12">
        <CaptchaFeedCard
          :events="feed.events.value"
          :error="feed.error.value"
          :screenshot-error="feed.screenshotError.value"
          @open-screenshot="feed.openShot"
        />
      </v-col>
    </v-row>

    <v-row dense class="mt-1">
      <v-col cols="12" md="6" lg="4">
        <BarChartCard
          title="Клики/час"
          :hint="demoWindowHint"
          :labels="dash.series.value.labels"
          :values="dash.series.value.clicks"
          color-token="primary"
        />
      </v-col>
      <v-col cols="12" md="6" lg="4">
        <BarChartCard
          title="CAPTCHA по часам"
          hint="события из логов (категория captcha), 24 часа"
          :labels="dash.series.value.labels"
          :values="dash.series.value.captcha"
          color-token="warning"
        />
      </v-col>
      <v-col cols="12" md="6" lg="4">
        <BarChartCard
          title="Нагрузка на воркеры"
          hint="запросы за последний час по browser_id"
          :labels="dash.series.value.loadLabels"
          :values="dash.series.value.loadValues"
          color-token="success"
          horizontal
        />
      </v-col>
    </v-row>

  </PageLayout>
</template>
