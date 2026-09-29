<script setup lang="ts">
// Экран Logs (план §5, фаза 4): live-логи с фильтрами (уровень, категория,
// browser_id, время), пагинация подгрузкой старых по курсору, счётчик
// count_logs, экспорт выгрузки в CSV и пауза живого режима.
//
// Порядок строк — новые сверху (как в БД). Автоскролл ведёт к верху только
// когда живой тик привёз новые строки: подгрузка старого и пауза чтение не
// сбивают. Фильтры валидируются и сохраняются в localStorage — они
// переживают перезагрузку (см. lib/logFilters).

import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import DbUnavailableAlert from "../components/DbUnavailableAlert.vue";
import { useDb } from "../composables/useDb";
import { MAX_LOGS_ROWS, useLogs } from "../composables/useLogs";
import { logsToCsv } from "../lib/csv";
import {
  EMPTY_LOG_FILTERS,
  LOG_CATEGORIES,
  LOG_LEVELS,
  type LogFilterValues,
} from "../lib/logFilters";

const db = useDb();
const logs = useLogs();

onMounted(() => {
  void db.ensureOpen();
  void logs.start();
});
onUnmounted(() => logs.stop());

// --- фильтры -------------------------------------------------------------

function field<K extends keyof LogFilterValues>(key: K) {
  return computed({
    get: () => logs.filters.value[key],
    set: (value: LogFilterValues[K]) =>
      void logs.updateFilters({ [key]: value } as Partial<LogFilterValues>),
  });
}

const levelField = field("level");
const categoryField = field("category");
const browserIdField = field("browserId");
const sinceField = field("since");
const untilField = field("until");

const levelItems = [
  { title: "все уровни", value: "" },
  ...LOG_LEVELS.map((level) => ({ title: level, value: level })),
];

const categoryItems = [
  { title: "все категории", value: "" },
  ...LOG_CATEGORIES.map((category) => ({ title: category, value: category })),
];

const pristine = computed(
  () => JSON.stringify(logs.filters.value) === JSON.stringify(EMPTY_LOG_FILTERS),
);

// --- таблица и подгрузка -------------------------------------------------

const hasRows = computed(() => logs.rows.value.length > 0);
const atRowCap = computed(() => logs.rows.value.length >= MAX_LOGS_ROWS);

const shownLabel = computed(() => {
  const shown = logs.rows.value.length;
  const total = logs.total.value;
  if (atRowCap.value) return `${shown} из ${total ?? "?"} · лимит ${MAX_LOGS_ROWS}`;
  return `${shown} из ${total ?? "…"}`;
});

async function onLoadOlder(): Promise<void> {
  await logs.loadOlder();
  // Подгрузка не трогает автоскролл — читатель остаётся на своём месте.
}

const canExport = computed(() => hasRows.value);

function csvStamp(): string {
  const at = new Date();
  const p = (value: number) => String(value).padStart(2, "0");
  return (
    `${at.getFullYear()}-${p(at.getMonth() + 1)}-${p(at.getDate())}` +
    `-${p(at.getHours())}${p(at.getMinutes())}${p(at.getSeconds())}`
  );
}

// Экспорт — из реально загруженных строк (≤ MAX_LOGS_ROWS), без повторного
// чтения базы: файл соответствует тому, что видит пользователь.
function exportCsv(): void {
  const csv = logsToCsv(logs.rows.value);
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `adclicker-logs-${csvStamp()}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Отложенная отмена: браузер должен успеть забрать blob до revoke.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function formatTs(ts: number): string {
  const at = new Date(ts * 1000);
  const p = (value: number) => String(value).padStart(2, "0");
  return (
    `${at.getFullYear()}-${p(at.getMonth() + 1)}-${p(at.getDate())} ` +
    `${p(at.getHours())}:${p(at.getMinutes())}:${p(at.getSeconds())}`
  );
}

const LEVEL_COLORS: Record<string, string | undefined> = {
  ERROR: "error",
  WARNING: "warning",
  INFO: "info",
  DEBUG: undefined,
};

// --- автоскролл ----------------------------------------------------------

const scroller = ref<HTMLElement | null>(null);

watch(logs.liveAdded, async () => {
  if (!logs.live.value) return;
  await nextTick();
  scroller.value?.scrollTo({ top: 0 });
});
</script>

<template>
  <div>
    <h1 class="text-h5 mb-4">Logs</h1>

    <DbUnavailableAlert class="mb-4" />

    <v-alert
      v-if="logs.error.value"
      type="error"
      variant="tonal"
      class="mb-4"
      data-test="logs-error"
    >
      {{ logs.error.value }}
    </v-alert>

    <v-card class="mb-4" data-test="logs-filters">
      <v-card-text class="py-3">
        <v-row dense align="center">
          <v-col cols="6" md="2">
            <v-select
              v-model="levelField"
              :items="levelItems"
              label="Уровень"
              density="compact"
              hide-details
              data-test="filter-level"
            />
          </v-col>
          <v-col cols="6" md="2">
            <v-select
              v-model="categoryField"
              :items="categoryItems"
              label="Категория"
              density="compact"
              hide-details
              data-test="filter-category"
            />
          </v-col>
          <v-col cols="6" md="2">
            <v-text-field
              v-model="browserIdField"
              label="browser_id"
              density="compact"
              hide-details
              clearable
              data-test="filter-browser"
            />
          </v-col>
          <v-col cols="6" md="2">
            <v-text-field
              v-model="sinceField"
              label="с"
              type="datetime-local"
              density="compact"
              hide-details
              clearable
              data-test="filter-since"
            />
          </v-col>
          <v-col cols="6" md="2">
            <v-text-field
              v-model="untilField"
              label="по"
              type="datetime-local"
              density="compact"
              hide-details
              clearable
              data-test="filter-until"
            />
          </v-col>
          <v-col cols="6" md="1">
            <v-btn
              block
              size="small"
              :disabled="pristine"
              data-test="filter-reset"
              @click="logs.resetFilters()"
            >
              Сброс
            </v-btn>
          </v-col>
        </v-row>
      </v-card-text>
    </v-card>

    <div class="d-flex align-center flex-wrap ga-2 mb-3">
      <v-chip
        :color="logs.live.value ? 'success' : 'warning'"
        variant="tonal"
        size="small"
        data-test="live-status"
      >
        {{ logs.live.value ? "живой режим" : "пауза" }}
      </v-chip>

      <span class="text-body-2 text-medium-emphasis" data-test="logs-count">
        {{ shownLabel }}
      </span>

      <v-spacer />

      <v-btn
        size="small"
        :prepend-icon="logs.live.value ? 'mdi-pause' : 'mdi-play'"
        :color="logs.live.value ? undefined : 'warning'"
        data-test="live-toggle"
        @click="logs.toggleLive()"
      >
        {{ logs.live.value ? "Пауза" : "Продолжить" }}
      </v-btn>

      <v-btn
        size="small"
        prepend-icon="mdi-chevron-up"
        :disabled="!hasRows || logs.exhausted.value || atRowCap"
        :loading="logs.loadingOlder.value"
        data-test="load-older"
        @click="onLoadOlder"
      >
        {{ logs.exhausted.value ? "Старше нет" : "Старше" }}
      </v-btn>

      <v-btn
        size="small"
        prepend-icon="mdi-download"
        :disabled="!canExport"
        :title="atRowCap ? `Экспорт ограничен ${MAX_LOGS_ROWS} строками` : undefined"
        data-test="export-csv"
        @click="exportCsv"
      >
        Экспорт CSV
      </v-btn>
    </div>

    <v-progress-linear
      v-if="logs.loading.value"
      indeterminate
      class="mb-2"
      data-test="logs-loading"
    />

    <div ref="scroller" class="logs-scroller mb-2" data-test="logs-scroll">
      <v-table v-if="hasRows" density="compact" hover data-test="logs-table">
        <thead>
          <tr>
            <th class="w-ts">время</th>
            <th class="w-level">уровень</th>
            <th class="w-browser">браузер</th>
            <th class="w-category">категория</th>
            <th>сообщение</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in logs.rows.value" :key="row.id" :data-test="`log-row-${row.id}`">
            <td class="text-no-wrap text-medium-emphasis">{{ formatTs(row.ts) }}</td>
            <td>
              <v-chip size="x-small" variant="tonal" :color="LEVEL_COLORS[row.level]">
                {{ row.level }}
              </v-chip>
            </td>
            <td class="text-no-wrap">{{ row.browser_id ?? "—" }}</td>
            <td class="text-no-wrap">{{ row.category ?? "—" }}</td>
            <td>
              <span :title="row.fields ?? undefined">{{ row.message }}</span>
              <span
                v-if="row.fields"
                class="text-medium-emphasis ml-2"
                :title="row.fields"
              >
                {{ row.fields }}
              </span>
            </td>
          </tr>
        </tbody>
      </v-table>

      <p
        v-else-if="!logs.loading.value && db.phase.value === 'open'"
        class="text-medium-emphasis ma-4"
        data-test="logs-empty"
      >
        Нет записей под текущими фильтрами
      </p>
    </div>

    <p
      v-if="atRowCap"
      class="text-body-2 text-warning mb-0"
      data-test="logs-cap-hint"
    >
      Достигнут лимит просмотра — сузьте фильтр по времени, чтобы увидеть
      старее.
    </p>
  </div>
</template>

<style scoped>
.logs-scroller {
  height: min(62vh, 640px);
  overflow-y: auto;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 8px;
}
</style>
