<script setup lang="ts">
// Dashboard (план §5, фаза 4): успешные/неуспешные сценарии, аптайм демона,
// запросы/час как проверяемое утверждение ≥50, доля CAPTCHA с порогом 5%
// и три графика (клики/час, CAPTCHA по часам, нагрузка на воркеры).
import { computed, onMounted, onUnmounted } from "vue";
import BarChartCard from "../components/charts/BarChartCard.vue";
import DbUnavailableAlert from "../components/DbUnavailableAlert.vue";
import { useDashboard } from "../composables/useDashboard";
import { useDb } from "../composables/useDb";
import { formatLastError, formatUptime } from "../lib/format";
import {
  REQUESTS_PER_HOUR_TARGET,
  captchaShareStatus,
  formatCaptchaShare,
  requestsPerHourStatus,
} from "../lib/thresholds";

const db = useDb();
const dash = useDashboard();

onMounted(() => {
  void db.ensureOpen();
  dash.start();
});
onUnmounted(() => dash.stop());

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

const shareStatus = computed(() => captchaShareStatus(dash.captchaShare.value));

const shareClaim = computed(() => {
  const status = shareStatus.value;
  if (!status.known) return "нет данных за окно — н/д";
  return status.withinLimit
    ? "доля CAPTCHA < 5% — норма"
    : "доля CAPTCHA ≥ 5% — порог превышен";
});

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

const demoWindowHint = "окно 24 часа";
</script>

<template>
  <div>
    <h1 class="text-h5 mb-4">Dashboard</h1>

    <DbUnavailableAlert class="mb-4" />

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
        <v-card data-test="card-runs" fill-height>
          <v-card-title class="text-subtitle-1 font-weight-medium">
            Сценарии
            <span class="text-medium-emphasis font-weight-regular">
              · {{ demoWindowHint }}
            </span>
          </v-card-title>
          <v-card-text class="pt-0">
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
            <div class="text-body-2 text-medium-emphasis">
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
          </v-card-text>
        </v-card>
      </v-col>

      <v-col cols="12" md="6" lg="3">
        <v-card data-test="card-uptime" fill-height>
          <v-card-title class="text-subtitle-1 font-weight-medium">
            Время работы
          </v-card-title>
          <v-card-text class="pt-0">
            <div class="text-h5 mb-1" data-test="uptime-value">
              {{ uptimeLabel }}
            </div>
            <div class="text-body-2 text-medium-emphasis mb-2">
              демон отвечает непрерывно
            </div>
            <v-chip color="primary" variant="tonal" size="small" data-test="active-workers">
              активные воркеры: {{ workersLabel }}
            </v-chip>
          </v-card-text>
        </v-card>
      </v-col>

      <v-col cols="12" md="6" lg="3">
        <v-card data-test="card-requests" fill-height>
          <v-card-title class="text-subtitle-1 font-weight-medium">
            Запросы/час
          </v-card-title>
          <v-card-text class="pt-0">
            <div class="text-h5 mb-1" data-test="requests-total">
              {{ dash.requests.value?.total ?? "—" }}
            </div>
            <v-chip
              v-if="requestsStatus"
              :color="requestsStatus.tone"
              variant="tonal"
              size="small"
              data-test="requests-claim"
            >
              {{ requestsClaim }}
            </v-chip>
            <div v-else class="text-body-2 text-medium-emphasis">нет данных</div>
          </v-card-text>
        </v-card>
      </v-col>

      <v-col cols="12" md="6" lg="3">
        <v-card data-test="card-captcha" fill-height>
          <v-card-title class="text-subtitle-1 font-weight-medium">
            Доля CAPTCHA
          </v-card-title>
          <v-card-text class="pt-0">
            <div class="text-h5 mb-1" data-test="captcha-value">
              {{ formatCaptchaShare(dash.captchaShare.value) }}
            </div>
            <v-chip
              :color="shareStatus.tone"
              variant="tonal"
              size="small"
              data-test="captcha-claim"
            >
              {{ shareClaim }}
            </v-chip>
          </v-card-text>
        </v-card>
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
  </div>
</template>
