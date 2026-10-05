<script setup lang="ts">
// Экран Proxies (план §5, фаза 5): список из БД, проверка доступности и
// редактор списка. Чтение — только read-only читалка (`list_proxies`),
// любая мутация уходит в control API демона (`/control/proxies*`) и после
// успеха перечитывает список: ридер не нарушается, владеет данными демон.
//
// Периодический refresh — 3 с (useProxies.PROXIES_POLL_MS): живо для
// прогресса проверки и не долбит БД. Автопаузы нет: экрана хватает на
// закрытие вкладки (stop в onUnmounted).
//
// Креды в данных отсутствуют на всех уровнях: Rust-читалка не выбирает
// username/password, API-слой собирает строку только из контрактных полей.
//
// Файл proxies.txt открывается внешней программой (opener), а не здесь:
// путь к нему отдаёт демон (`GET /control/proxies/file`) абсолютным — UI
// не знает каталога, от которого резолвится относительный путь конфига.
//
// Массовое удаление — best-effort: и выбор в таблице, и кнопка «Удалить с
// ошибкой» шлют один батч `{"ids": [...]}`, а занятые воркером строки
// возвращаются в отчёте под таблицей, а не ошибкой.

import { computed, onMounted, onUnmounted, ref } from "vue";
import ConfirmDialog from "../components/forms/ConfirmDialog.vue";
import DataTablePage from "../components/data/DataTablePage.vue";
import PageLayout from "../components/layout/PageLayout.vue";
import StatusChip from "../components/status/StatusChip.vue";
import { useProxies } from "../composables/useProxies";
import {
  parseProxyLines,
  toProxyTableRow,
  type ProxyTableRow,
} from "../lib/proxies";
import type { DataTableHeader } from "vuetify";

const proxies = useProxies();

onMounted(() => void proxies.start());
onUnmounted(() => proxies.stop());

const headers: DataTableHeader[] = [
  { key: "address", title: "Адрес", sortable: true },
  { key: "label", title: "Метка", sortable: true },
  { key: "country", title: "Страна", sortable: true },
  { key: "latency_ms", title: "Задержка", sortable: true },
  { key: "status", title: "Статус", sortable: false },
  { key: "fail_count", title: "Неудач", sortable: true },
  { key: "last_error", title: "Ошибка", sortable: false },
  { key: "assigned_browser_id", title: "Поток", sortable: true },
  { key: "usage_count", title: "Использований", sortable: true },
  { key: "actions", title: "", sortable: false, align: "end" },
];

// Готовые к показу строки: прочерки вместо NULL, статус из is_alive, адрес
// одной колонкой — сортировка идёт по значениям, а не по шаблону.
const tableRows = computed(() => proxies.rows.value.map(toProxyTableRow));

const checkDone = computed(() => proxies.checkProgress.value.done);
const checkTotal = computed(() => proxies.checkProgress.value.total);

// --- диалог добавления ---------------------------------------------------

const addDialog = ref(false);
const addText = ref("");

// Разбор только для UX: подсветить подозрительные строки, но не мешать
// отправке — сервер решает, что считать дубликатом и ошибкой.
const parsedLines = computed(() => parseProxyLines(addText.value));

function openAdd(): void {
  proxies.actionError.value = null;
  proxies.addResult.value = null;
  addDialog.value = true;
}

function closeAdd(): void {
  addDialog.value = false;
}

async function submitAdd(): Promise<void> {
  const { lines } = parsedLines.value;
  if (lines.length === 0) return;
  const ok = await proxies.add(lines);
  // Текст очищает только успех: при ошибке пользователь правит строки.
  if (ok) addText.value = "";
}

// --- удаление ------------------------------------------------------------

const deleteTarget = ref<ProxyTableRow | null>(null);

const deleteDialog = computed({
  get: () => deleteTarget.value !== null,
  set: (open: boolean) => {
    if (!open) deleteTarget.value = null;
  },
});

function askDelete(row: ProxyTableRow): void {
  deleteTarget.value = row;
}

async function confirmDelete(): Promise<void> {
  const target = deleteTarget.value;
  deleteTarget.value = null;
  if (target === null) return;
  // 409 «прокси назначен потоку» придёт в actionError над таблицей.
  await proxies.remove(target.id);
}

// --- батчевое удаление ---------------------------------------------------

/** Ссылка на таблицу — нужна, чтобы снять выбор после удаления строк. */
const table = ref<InstanceType<typeof DataTablePage> | null>(null);

/** Цель подтверждения батча: id и подписи диалога; null — диалог закрыт. */
interface BulkTarget {
  ids: number[];
  title: string;
  text: string;
}

const bulkTarget = ref<BulkTarget | null>(null);

const bulkDialog = computed({
  get: () => bulkTarget.value !== null,
  set: (open: boolean) => {
    if (!open) bulkTarget.value = null;
  },
});

/** Строки со статусом «ошибка» — цель кнопки «Удалить с ошибкой». */
const failedRows = computed(() =>
  tableRows.value.filter((row) => row.status === "error"),
);

function askBulkDelete(ids: number[], title: string, text: string): void {
  if (ids.length === 0) return;
  // Итог прошлой операции не должен пережить новое подтверждение.
  proxies.deleteResult.value = null;
  bulkTarget.value = { ids, title, text };
}

function askDeleteSelected(rows: ReadonlyArray<{ id: number }>): void {
  askBulkDelete(
    rows.map((row) => row.id),
    `Удалить выбранные (${rows.length})?`,
    `${rows.length} прокси уйдёт из пула. Назначенные живому воркеру останутся — они попадут в отчёт под таблицей.`,
  );
}

function askDeleteFailed(): void {
  const rows = failedRows.value;
  askBulkDelete(
    rows.map((row) => row.id),
    `Удалить прокси со статусом «ошибка» (${rows.length})?`,
    `Будут удалены ${rows.length} прокси с неудачной проверкой. Назначенные живому воркеру останутся — они попадут в отчёт под таблицей.`,
  );
}

async function confirmBulkDelete(): Promise<void> {
  const target = bulkTarget.value;
  bulkTarget.value = null;
  if (target === null) return;
  const ok = await proxies.removeMany(target.ids);
  // Удалённых строк больше нет в items, но Vuetify selection не чистит сам.
  if (ok) table.value?.clearSelection();
}
</script>

<template>
  <PageLayout
    title="Proxies"
    subtitle="Список прокси, проверка доступности и назначение"
  >
    <v-alert
      v-if="proxies.actionError.value"
      type="error"
      variant="tonal"
      class="mb-4"
      closable
      data-test="proxies-action-error"
      @click:close="proxies.actionError.value = null"
    >
      {{ proxies.actionError.value }}
    </v-alert>

    <v-alert
      v-if="proxies.importResult.value"
      type="success"
      variant="tonal"
      class="mb-4"
      closable
      data-test="proxies-import-result"
      @click:close="proxies.importResult.value = null"
    >
      Импорт из proxies.txt: добавлено
      {{ proxies.importResult.value.added }}, пропущено
      {{ proxies.importResult.value.skipped }}.
      <div
        v-for="problem in proxies.importResult.value.problems"
        :key="problem"
      >
        {{ problem }}
      </div>
    </v-alert>

    <v-alert
      v-if="proxies.deleteResult.value"
      type="success"
      variant="tonal"
      class="mb-4"
      closable
      data-test="proxies-delete-result"
      @click:close="proxies.deleteResult.value = null"
    >
      Удалено {{ proxies.deleteResult.value.deleted }}, пропущено
      {{ proxies.deleteResult.value.skipped }}.
      <div
        v-for="problem in proxies.deleteResult.value.problems"
        :key="problem"
      >
        {{ problem }}
      </div>
    </v-alert>

    <DataTablePage
      ref="table"
      :headers="headers"
      :items="tableRows"
      :loading="proxies.loading.value"
      :error="proxies.error.value"
      :selectable="true"
      empty-icon="mdi-earth"
      empty-title="Добавьте первый прокси"
      empty-hint="Импортируйте список из proxies.txt или добавьте вручную"
      data-test="proxies-table"
      @retry="proxies.tick()"
    >
      <template #filters>
        <v-chip
          v-if="proxies.checking.value"
          color="info"
          variant="tonal"
          size="small"
          data-test="proxies-check-progress"
        >
          Проверка: {{ checkDone }}/{{ checkTotal }}
        </v-chip>
      </template>

      <template #bulk="{ count, selected }">
        <v-btn
          size="small"
          color="error"
          variant="tonal"
          prepend-icon="mdi-delete"
          data-test="proxies-delete-selected"
          @click="askDeleteSelected(selected as ProxyTableRow[])"
        >
          Удалить выбранные ({{ count }})
        </v-btn>
        <v-btn
          size="small"
          variant="text"
          data-test="proxies-clear-selection"
          @click="table?.clearSelection()"
        >
          Снять выбор
        </v-btn>
      </template>

      <template #actions>
        <v-btn
          size="small"
          variant="outlined"
          color="error"
          prepend-icon="mdi-delete-sweep"
          :disabled="failedRows.length === 0"
          :loading="proxies.pending.value === 'delete'"
          data-test="proxies-delete-failed"
          @click="askDeleteFailed()"
        >
          Удалить с ошибкой ({{ failedRows.length }})
        </v-btn>

        <v-btn
          size="small"
          variant="outlined"
          prepend-icon="mdi-open-in-new"
          :loading="proxies.pending.value === 'file'"
          data-test="proxies-open-file"
          @click="void proxies.openFile()"
        >
          Открыть proxies.txt
        </v-btn>

        <v-btn
          size="small"
          variant="outlined"
          prepend-icon="mdi-import"
          :loading="proxies.pending.value === 'import'"
          data-test="proxies-import"
          @click="void proxies.importFile()"
        >
          Импорт из proxies.txt
        </v-btn>

        <v-btn
          size="small"
          variant="outlined"
          prepend-icon="mdi-check-network"
          class="mr-2"
          :loading="proxies.pending.value === 'check'"
          :disabled="proxies.checking.value"
          data-test="proxies-check"
          @click="void proxies.runCheck()"
        >
          Проверить
        </v-btn>

        <v-btn
          size="small"
          color="primary"
          prepend-icon="mdi-plus"
          data-test="proxies-add"
          @click="openAdd"
        >
          Добавить
        </v-btn>
      </template>

      <template #empty>
        <div class="empty-state" data-test="proxies-empty">
          <v-icon icon="mdi-earth" size="40px" />
          <div class="empty-state__title">Добавьте первый прокси</div>
          <div>Импортируйте список из proxies.txt или добавьте вручную</div>
          <v-btn
            color="primary"
            prepend-icon="mdi-plus"
            class="mt-2"
            data-test="proxies-empty-add"
            @click="openAdd"
          >
            Добавить прокси
          </v-btn>
        </div>
      </template>

      <template #item.status="{ item }">
        <StatusChip :status="item.status" />
      </template>

      <template #item.latency_ms="{ item }">
        {{ item.latency_ms === null ? "—" : `${item.latency_ms} мс` }}
      </template>

      <template #item.last_error="{ item }">
        <span
          class="text-truncate last-error-cell"
          :title="item.last_error ?? undefined"
        >
          {{ item.last_error ?? "—" }}
        </span>
      </template>

      <template #item.actions="{ item }">
        <v-btn
          icon="mdi-delete"
          variant="text"
          size="small"
          color="error"
          :data-test="`proxies-delete-${item.id}`"
          aria-label="Удалить прокси"
          @click="askDelete(item)"
        />
      </template>
    </DataTablePage>

    <v-dialog v-model="addDialog" max-width="560">
      <v-card data-test="proxies-add-dialog">
        <v-card-title class="text-subtitle-1 font-weight-bold pt-4 px-4">
          Добавить прокси
        </v-card-title>

        <v-card-text class="px-4 pb-2">
          <v-textarea
            v-model="addText"
            label="Прокси, по одному в строке"
            hint="http://host:port, host:port или user:pass@host:port; # — комментарий"
            rows="8"
            auto-grow
            data-test="proxies-add-text"
          />

          <v-alert
            v-if="proxies.actionError.value"
            type="error"
            variant="tonal"
            density="compact"
            class="mt-2"
            data-test="proxies-add-error"
          >
            {{ proxies.actionError.value }}
          </v-alert>

          <v-alert
            v-if="parsedLines.problems.length"
            type="warning"
            variant="tonal"
            density="compact"
            class="mt-2"
            data-test="proxies-add-problems"
          >
            <div v-for="problem in parsedLines.problems" :key="problem">
              {{ problem }}
            </div>
            <div class="text-caption mt-1">
              Строки уйдут в запрос как есть — решит демон.
            </div>
          </v-alert>

          <v-alert
            v-if="proxies.addResult.value"
            type="success"
            variant="tonal"
            density="compact"
            class="mt-2"
            data-test="proxies-add-result"
          >
            Добавлено {{ proxies.addResult.value.added }}, пропущено
            {{ proxies.addResult.value.skipped }}.
            <div v-for="problem in proxies.addResult.value.problems" :key="problem">
              {{ problem }}
            </div>
          </v-alert>
        </v-card-text>

        <v-card-actions class="px-4 pb-4">
          <v-btn variant="text" data-test="proxies-add-close" @click="closeAdd">
            Закрыть
          </v-btn>
          <v-spacer />
          <v-btn
            color="primary"
            :loading="proxies.pending.value === 'add'"
            :disabled="parsedLines.lines.length === 0"
            data-test="proxies-add-submit"
            @click="submitAdd"
          >
            Добавить
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <ConfirmDialog
      v-model="deleteDialog"
      title="Удалить прокси?"
      :text="
        deleteTarget
          ? `${deleteTarget.address} будет удалён из списка. Назначения потоков не трогаются.`
          : undefined
      "
      confirm-label="Удалить"
      destructive
      data-test="proxies-delete-dialog"
      @confirm="confirmDelete"
    />

    <ConfirmDialog
      v-model="bulkDialog"
      :title="bulkTarget?.title ?? 'Удалить прокси?'"
      :text="bulkTarget?.text"
      confirm-label="Удалить"
      destructive
      data-test="proxies-bulk-delete-dialog"
      @confirm="confirmBulkDelete"
    />
  </PageLayout>
</template>

<style scoped>
/* Ошибка — одна строка с полным текстом в title: колонка не разъезжается. */
.last-error-cell {
  display: inline-block;
  max-width: 220px;
  vertical-align: middle;
}
</style>
