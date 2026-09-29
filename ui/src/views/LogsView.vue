<script setup lang="ts">
// Экран Logs (план §5, фаза 4): live-логи с фильтрами (уровень, категория,
// browser_id, время), пагинация подгрузкой старых по курсору, счётчик
// count_logs, экспорт выгрузки в CSV и пауза живого режима.
//
// Порядок строк — новые сверху (как в БД). Автоскролл ведёт к верху только
// когда живой тик привёз новые строки: подгрузка старого и пауза чтение не
// сбивают. Фильтры валидируются и сохраняются в localStorage — они
// переживают перезагрузку (см. lib/logFilters).
//
// Вёрстка — шаблон: PageLayout (заголовок + тулбар) и FilterBar (контролы,
// чипы активных фильтров, «Сбросить»). Сам список остаётся локальным:
// LogViewer шаблона пришит к низу (новые внизу), живёт на v-virtual-scroll
// и на своём empty-state — наше поведение (новые сверху, курсор, «Старше»
// без сброса позиции, условный empty-state по фазе БД) он не вмещает.

import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import DbUnavailableAlert from "../components/DbUnavailableAlert.vue";
import FilterBar from "../components/data/FilterBar.vue";
import PageLayout from "../components/layout/PageLayout.vue";
import { useDb } from "../composables/useDb";
import { MAX_LOGS_ROWS, useLogs } from "../composables/useLogs";
import { logsToCsv } from "../lib/csv";
import { activeFilterChips } from "../lib/logFilterChips";
import {
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

/** Активные фильтры чипами: их наличие и есть «фильтр включён». */
const filterChips = computed(() => activeFilterChips(logs.filters.value));

/** Крестик на чипе снимает ровно это поле, остальные фильтры остаются. */
function onFilterClear(key: string): void {
  const fieldKey = key as keyof LogFilterValues;
  const patch: Partial<LogFilterValues> = {};
  if (fieldKey === "since" || fieldKey === "until") {
    patch[fieldKey] = null;
  } else {
    patch[fieldKey] = "";
  }
  void logs.updateFilters(patch);
}

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

// Цвет уровня — тот же словарь, что у LogViewer шаблона: ERROR — error,
// WARNING — warning, INFO — success, DEBUG — без цвета.
const LEVEL_COLORS: Record<string, string | undefined> = {
  ERROR: "error",
  WARNING: "warning",
  INFO: "success",
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
  <PageLayout title="Logs" subtitle="Новые записи сверху, старые — кнопкой «Старше»">
    <template #toolbar-left>
      <FilterBar
        :chips="filterChips"
        @clear="onFilterClear"
        @reset="logs.resetFilters()"
      >
        <v-select
          v-model="levelField"
          :items="levelItems"
          label="Уровень"
          hide-details
          density="compact"
          style="max-width: 170px"
          data-test="filter-level"
        />
        <v-select
          v-model="categoryField"
          :items="categoryItems"
          label="Категория"
          hide-details
          density="compact"
          style="max-width: 180px"
          data-test="filter-category"
        />
        <v-text-field
          v-model="browserIdField"
          label="browser_id"
          hide-details
          density="compact"
          clearable
          style="max-width: 170px"
          data-test="filter-browser"
        />
        <v-text-field
          v-model="sinceField"
          label="с"
          type="datetime-local"
          hide-details
          density="compact"
          clearable
          style="max-width: 210px"
          data-test="filter-since"
        />
        <v-text-field
          v-model="untilField"
          label="по"
          type="datetime-local"
          hide-details
          density="compact"
          clearable
          style="max-width: 210px"
          data-test="filter-until"
        />
      </FilterBar>
    </template>

    <template #toolbar-right>
      <v-btn
        size="small"
        variant="outlined"
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
        variant="outlined"
        prepend-icon="mdi-download"
        :disabled="!canExport"
        :title="atRowCap ? `Экспорт ограничен ${MAX_LOGS_ROWS} строками` : undefined"
        data-test="export-csv"
        @click="exportCsv"
      >
        Экспорт CSV
      </v-btn>

      <v-btn
        size="small"
        :color="logs.live.value ? 'primary' : 'warning'"
        :prepend-icon="logs.live.value ? 'mdi-pause' : 'mdi-play'"
        data-test="live-toggle"
        @click="logs.toggleLive()"
      >
        {{ logs.live.value ? "Пауза" : "Продолжить" }}
      </v-btn>
    </template>

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

    <div class="d-flex align-center flex-wrap ga-2 mb-3">
      <v-chip
        :color="logs.live.value ? 'success' : 'warning'"
        variant="tonal"
        size="small"
        data-test="live-status"
      >
        {{ logs.live.value ? "живой режим" : "пауза" }}
      </v-chip>

      <span class="text-body-2 text-muted" data-test="logs-count">
        {{ shownLabel }}
      </span>
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
            <th>время</th>
            <th>уровень</th>
            <th>браузер</th>
            <th>категория</th>
            <th>сообщение</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in logs.rows.value" :key="row.id" :data-test="`log-row-${row.id}`">
            <td class="text-no-wrap text-muted">{{ formatTs(row.ts) }}</td>
            <td>
              <v-chip size="x-small" variant="tonal" :color="LEVEL_COLORS[row.level]">
                {{ row.level }}
              </v-chip>
            </td>
            <td class="text-no-wrap">{{ row.browser_id ?? "—" }}</td>
            <td class="text-no-wrap">{{ row.category ?? "—" }}</td>
            <td>
              <span :title="row.fields ?? undefined">{{ row.message }}</span>
              <span v-if="row.fields" class="text-muted ml-2" :title="row.fields">
                {{ row.fields }}
              </span>
            </td>
          </tr>
        </tbody>
      </v-table>

      <div
        v-else-if="!logs.loading.value && db.phase.value === 'open'"
        class="empty-state h-100"
        data-test="logs-empty"
      >
        <v-icon icon="mdi-format-list-bulleted" size="40px" />
        <div class="empty-state__title">Нет записей под текущими фильтрами</div>
        <v-btn
          v-if="filterChips.length"
          color="primary"
          prepend-icon="mdi-filter-remove"
          @click="logs.resetFilters()"
        >
          Сбросить фильтры
        </v-btn>
      </div>
    </div>

    <p
      v-if="atRowCap"
      class="text-body-2 text-warning mb-0"
      data-test="logs-cap-hint"
    >
      Достигнут лимит просмотра — сузьте фильтр по времени, чтобы увидеть
      старее.
    </p>
  </PageLayout>
</template>

<style scoped>
.logs-scroller {
  height: min(62vh, 640px);
  overflow-y: auto;
  background-color: rgb(var(--v-theme-surface));
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  /* радиус sm и 14px — токены дизайн.md §2.2–2.3 для логов */
  border-radius: 2px;
  font-size: 14px;
}
</style>
