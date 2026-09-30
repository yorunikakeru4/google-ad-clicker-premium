<script setup lang="ts">
// Экран Logs (план §5, фазы 4 и 9): live-логи с фильтрами (уровень, категория,
// browser_id, время), пагинация подгрузкой старых по курсору, счётчик
// count_logs, экспорт диапазона в CSV/JSON, пауза живого режима и индикатор
// размера БД.
//
// Порядок строк — новые сверху (как в БД). Автоскролл ведёт к верху только
// когда живой тик привёз новые строки: подгрузка старого и пауза чтение не
// сбивают. Фильтры валидируются и сохраняются в localStorage — они
// переживают перезагрузку (см. lib/logFilters).
//
// Экспорт — диалог с диапазоном дат поверх текущих фильтров (lib/logExport):
// выборка грузится курсорными страницами до исчерпания или EXPORT_ROW_CAP
// строк, при усечении диалог честно предупреждает. Старый экспорт «того,
// что на экране» заменён этой операцией: диалог по умолчанию предзаполнен
// окном текущих фильтров, то есть прежний сценарий — один клик, а поверх —
// формат, больший диапазон и потолок с предупреждением.
//
// Вёрстка — шаблон: PageLayout (заголовок + тулбар) и FilterBar (контролы,
// чипы активных фильтров, «Сбросить»). Сам список остаётся локальным:
// LogViewer шаблона рисует новые снизу и сам скроллит при каждом изменении
// списка, а нам нужно «новые сверху», автоскролл только по живому тику и
// подгрузка «Старше» без сброса позиции. Плюс это таблица с заголовком
// колонок и сырыми полями в подсказке, а не однострочные лог-линии, и
// свой empty-state на v-virtual-scroll — наш empty-state зависит от фазы
// БД, а v-virtual-scroll выгружает невидимые строки из DOM.

import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import DbUnavailableAlert from "../components/DbUnavailableAlert.vue";
import FilterBar from "../components/data/FilterBar.vue";
import PageLayout from "../components/layout/PageLayout.vue";
import { useDb } from "../composables/useDb";
import { useDbSize } from "../composables/useDbSize";
import { MAX_LOGS_ROWS, useLogs } from "../composables/useLogs";
import { useSettings } from "../composables/useSettings";
import { dbApi, dbErrorMessage } from "../lib/dbApi";
import { dbSizeStatus } from "../lib/dbSize";
import { activeFilterChips } from "../lib/logFilterChips";
import {
  LOG_CATEGORIES,
  LOG_LEVELS,
  type LogFilterValues,
} from "../lib/logFilters";
import {
  EXPORT_ROW_CAP,
  buildExportQuery,
  collectExportRows,
  exportFile,
  exportFilename,
  rangeProblem,
  type ExportFormat,
} from "../lib/logExport";

const db = useDb();
const logs = useLogs();
const size = useDbSize();
const settings = useSettings();

onMounted(() => {
  void db.ensureOpen();
  void logs.start();
  // Лимит индикатора — из того же GET /control/config, что и Settings:
  // синглтон кэширует ответ, повторное открытие экрана не ходит в сеть.
  void settings.load();
  size.start();
});
onUnmounted(() => {
  logs.stop();
  size.stop();
});

// --- индикатор размера БД -------------------------------------------------

// Лимит честен только после ответа демона: до load() конфиг не загружен и
// limit = null, а не дефолт схемы 0 — «лимит не задан» у непрочитанного
// конфига врало бы.
const limitMb = computed(() => {
  if (!settings.loaded.value) return null;
  const raw = settings.values.value["behavior.db_size_limit_mb"];
  return typeof raw === "number" ? raw : null;
});

const sizeStatus = computed(() => dbSizeStatus(size.bytes.value, limitMb.value));

const sizeChipColor = computed(() =>
  sizeStatus.value.tone === "error" ? "error" : undefined,
);

const sizeTitle = computed(
  () => size.path.value ?? size.error.value ?? "размер БД ещё не прочитан",
);

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

/** Строка «что применяется» в диалоге экспорта. */
const filterSummary = computed(() =>
  filterChips.value.length === 0
    ? "фильтры не заданы"
    : filterChips.value.map((chip) => `${chip.label}: ${chip.value}`).join(", "),
);

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

// --- экспорт -------------------------------------------------------------

const exportDialog = ref(false);
const exportSince = ref<string | null>(null);
const exportUntil = ref<string | null>(null);
const exportFormat = ref<ExportFormat>("csv");
const exportBusy = ref(false);
const exportError = ref<string | null>(null);
/** Текст усечения; null — выгрузка полная. */
const exportTruncated = ref<string | null>(null);
/** Текст успеха; null — ещё не выгружали. */
const exportDone = ref<string | null>(null);

const exportProblem = computed(() =>
  rangeProblem({ since: exportSince.value, until: exportUntil.value }),
);

// Экспорт доступен, пока открыта база: диалог перечитывает БД курсорными
// страницами и не зависит от того, что успело загрузиться на экран.
const canExport = computed(() => db.phase.value === "open");

function openExport(): void {
  // Диалог стартует с окна текущих фильтров: «экспорт того, что вижу» —
  // это дефолт, а не отдельная кнопка.
  exportSince.value = logs.filters.value.since;
  exportUntil.value = logs.filters.value.until;
  exportFormat.value = "csv";
  exportError.value = null;
  exportTruncated.value = null;
  exportDone.value = null;
  exportDialog.value = true;
}

async function runExport(): Promise<void> {
  if (exportBusy.value || exportProblem.value !== null) return;

  exportBusy.value = true;
  exportError.value = null;
  exportTruncated.value = null;
  exportDone.value = null;
  try {
    const query = buildExportQuery(logs.filters.value, {
      since: exportSince.value,
      until: exportUntil.value,
    });
    const { rows, truncated } = await collectExportRows((cursor, limit) =>
      dbApi.listLogsPage({ query, limit, cursor }),
    );

    const file = exportFile(rows, exportFormat.value);
    download(file.content, exportFilename(exportFormat.value, new Date()), file.mime);

    exportTruncated.value = truncated
      ? `Диапазон усечён до ${EXPORT_ROW_CAP} строк: выгружено ${rows.length}, старше потолка не попало в файл. Сузьте окно времени для полной выгрузки.`
      : null;
    exportDone.value = `Выгружено строк: ${rows.length}.`;
  } catch (caught) {
    exportError.value = dbErrorMessage(caught);
  } finally {
    exportBusy.value = false;
  }
}

/** Скачивание готового файла; отложенная revoke — браузер должен забрать blob. */
function download(content: string, filename: string, mime: string): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
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
      <v-chip
        size="small"
        variant="tonal"
        :color="sizeChipColor"
        :title="sizeTitle"
        data-test="db-size"
      >
        {{ sizeStatus.sizeMb === null ? "БД: …" : `БД: ${sizeStatus.sizeMb} МБ` }}
        · {{ sizeStatus.detail }}
      </v-chip>

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
        data-test="export-open"
        @click="openExport"
      >
        Экспорт
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
        class="empty-state"
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

    <v-dialog v-model="exportDialog" max-width="560">
      <v-card data-test="export-dialog">
        <v-card-title>Экспорт логов</v-card-title>
        <v-card-text>
          <p class="text-body-2 text-muted mb-3">
            Текущие фильтры применяются: {{ filterSummary }}. Максимум
            {{ EXPORT_ROW_CAP.toLocaleString("ru-RU") }} строк за выгрузку.
          </p>

          <div class="d-flex flex-wrap ga-3">
            <v-text-field
              v-model="exportSince"
              label="с"
              type="datetime-local"
              hide-details
              density="compact"
              clearable
              style="max-width: 230px"
              data-test="export-since"
            />
            <v-text-field
              v-model="exportUntil"
              label="по"
              type="datetime-local"
              hide-details
              density="compact"
              clearable
              style="max-width: 230px"
              data-test="export-until"
            />
          </div>

          <v-btn-toggle
            v-model="exportFormat"
            mandatory
            density="compact"
            class="mt-4"
            data-test="export-format"
          >
            <v-btn value="csv">CSV</v-btn>
            <v-btn value="json">JSON</v-btn>
          </v-btn-toggle>

          <v-alert
            v-if="exportProblem"
            type="warning"
            variant="tonal"
            class="mt-4"
            data-test="export-problem"
          >
            {{ exportProblem }}
          </v-alert>

          <v-alert
            v-if="exportError"
            type="error"
            variant="tonal"
            class="mt-4"
            data-test="export-error"
          >
            {{ exportError }}
          </v-alert>

          <v-alert
            v-if="exportTruncated"
            type="warning"
            variant="tonal"
            class="mt-4"
            data-test="export-truncated"
          >
            {{ exportTruncated }}
          </v-alert>

          <v-alert
            v-if="exportDone && !exportTruncated"
            type="success"
            variant="tonal"
            class="mt-4"
            data-test="export-done"
          >
            {{ exportDone }}
          </v-alert>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" data-test="export-close" @click="exportDialog = false">
            Закрыть
          </v-btn>
          <v-btn
            color="primary"
            prepend-icon="mdi-download"
            :loading="exportBusy"
            :disabled="exportProblem !== null"
            data-test="export-run"
            @click="runExport"
          >
            Выгрузить
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
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

/* Пустой список занимает всю панель и центрируется; border-box не даёт
   отступам empty-state вылезти за высоту и включить лишний скролл. */
.logs-scroller .empty-state {
  box-sizing: border-box;
  height: 100%;
}
</style>
