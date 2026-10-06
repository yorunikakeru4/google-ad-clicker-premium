<script setup lang="ts">
// Экран Key Words (план §5, фаза 13): список поисковых запросов.
//
// Список живёт в файле (paths.query_file), владеет им демон: мутации идут в
// control API (/control/queries*), после каждого действия снапшот
// перечитывается. UI не редактирует config.json напрямую — источник
// запросов (файл против одиночного behavior.query) показывается здесь
// только для чтения, правится он в Tasks/Settings.
//
// Пустой paths.query_file при добавлении демон переключает сам: создаёт
// queries.txt и переносит туда одиночный запрос первой строкой — об этом
// говорит алерт «switched» под таблицей.

import { computed, onMounted, onUnmounted, ref } from "vue";
import ConfirmDialog from "../components/forms/ConfirmDialog.vue";
import DataTablePage from "../components/data/DataTablePage.vue";
import PageLayout from "../components/layout/PageLayout.vue";
import { createWordlist } from "../composables/useWordlist";
import { queriesApi, type QueriesSnapshot } from "../lib/wordlist";
import type { DataTableHeader } from "vuetify";

const keywords = createWordlist<QueriesSnapshot>(queriesApi, {
  items: (snapshot) => snapshot.queries,
  fileLabel: "queries.txt",
});

onMounted(() => void keywords.start());
onUnmounted(() => keywords.stop());

const headers: DataTableHeader[] = [
  { key: "value", title: "Запрос", sortable: true },
  { key: "actions", title: "", sortable: false, align: "end" },
];

/** Строки таблицы: id = сам запрос — он уникален (дедупликация на стороне демона). */
const rows = computed(() =>
  keywords.items.value.map((value) => ({ id: value, value })),
);

const source = computed(() => keywords.snapshot.value?.source ?? null);
const queryFile = computed(() => keywords.snapshot.value?.query_file ?? "");
const singleQuery = computed(() => keywords.snapshot.value?.query ?? "");
const fileMode = computed(() => source.value === "file");

// --- диалог добавления ---------------------------------------------------

const addDialog = ref(false);
const addText = ref("");

/** Разбор только для UX; решает демон — здесь лишь подсветка пустых строк. */
const parsedLines = computed(() =>
  addText.value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line !== "" && !line.startsWith("#")),
);

function openAdd(): void {
  keywords.actionError.value = null;
  keywords.addResult.value = null;
  addDialog.value = true;
}

function closeAdd(): void {
  addDialog.value = false;
}

async function submitAdd(): Promise<void> {
  if (parsedLines.value.length === 0) return;
  const ok = await keywords.add(parsedLines.value);
  // Текст очищает только успех: при ошибке пользователь правит строки.
  if (ok) addText.value = "";
}

// --- удаление ------------------------------------------------------------

const deleteTarget = ref<string | null>(null);

const deleteDialog = computed({
  get: () => deleteTarget.value !== null,
  set: (open: boolean) => {
    if (!open) deleteTarget.value = null;
  },
});

function askDelete(value: string): void {
  deleteTarget.value = value;
}

async function confirmDelete(): Promise<void> {
  const target = deleteTarget.value;
  deleteTarget.value = null;
  if (target === null) return;
  await keywords.remove([target]);
}

// --- батчевое удаление ---------------------------------------------------

/** Ссылка на таблицу — нужна, чтобы снять выбор после удаления строк. */
const table = ref<InstanceType<typeof DataTablePage> | null>(null);

interface BulkTarget {
  /** true — уходит {"all": true}; values при этом не используются. */
  all: boolean;
  values: string[];
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

function askDeleteSelected(values: string[]): void {
  if (values.length === 0) return;
  keywords.deleteResult.value = null;
  bulkTarget.value = {
    all: false,
    values,
    title: `Удалить выбранные запросы (${values.length})?`,
    text: `${values.length} запросов уйдут из списка. В Tasks источник останется тем же.`,
  };
}

function askDeleteAll(): void {
  const count = rows.value.length;
  if (count === 0) return;
  keywords.deleteResult.value = null;
  bulkTarget.value = {
    all: true,
    values: [],
    title: `Удалить все запросы (${count})?`,
    text:
      `Из файла ${queryFile.value || "queries.txt"} уйдут ${count} запросов. ` +
      "Список можно восстановить, добавив запросы заново.",
  };
}

async function confirmBulkDelete(): Promise<void> {
  const target = bulkTarget.value;
  bulkTarget.value = null;
  if (target === null) return;
  const ok = target.all
    ? await keywords.removeAll()
    : await keywords.remove(target.values);
  // Удалённых строк больше нет в items, но Vuetify selection не чистит сам.
  if (ok) table.value?.clearSelection();
}
</script>

<template>
  <PageLayout
    title="Key Words"
    subtitle="Поисковые запросы: добавление и удаление"
  >
    <v-alert
      v-if="keywords.actionError.value"
      type="error"
      variant="tonal"
      class="mb-4"
      closable
      data-test="keywords-action-error"
      @click:close="keywords.actionError.value = null"
    >
      {{ keywords.actionError.value }}
    </v-alert>

    <v-alert
      v-if="source === 'single'"
      type="info"
      variant="tonal"
      class="mb-4"
      data-test="keywords-source-single"
    >
      Сейчас работает одиночный запрос: <strong>{{ singleQuery || "—" }}</strong>.
      Списком здесь управлять нельзя — при первом добавлении демон создаст
      <code>queries.txt</code> и перенесёт запрос в него.
      <div class="text-caption mt-1">
        Источник переключается в Tasks или Settings (paths.query_file /
        behavior.query — взаимоисключающие поля).
      </div>
    </v-alert>

    <v-alert
      v-if="keywords.deleteResult.value"
      type="success"
      variant="tonal"
      class="mb-4"
      closable
      data-test="keywords-delete-result"
      @click:close="keywords.deleteResult.value = null"
    >
      Удалено {{ keywords.deleteResult.value.deleted }}, пропущено
      {{ keywords.deleteResult.value.skipped }}.
      <div
        v-for="problem in keywords.deleteResult.value.problems"
        :key="problem"
      >
        {{ problem }}
      </div>
    </v-alert>

    <DataTablePage
      ref="table"
      :headers="headers"
      :items="rows"
      :loading="keywords.loading.value"
      :error="keywords.error.value"
      :selectable="true"
      empty-icon="mdi-key-variant"
      empty-title="Добавьте первый запрос"
      empty-hint="Один поисковый запрос в строке — файл читает движок"
      data-test="keywords-table"
      @retry="keywords.tick()"
    >
      <template #filters>
        <v-chip
          v-if="fileMode"
          color="primary"
          variant="tonal"
          size="small"
          data-test="keywords-source-file"
        >
          Файл: {{ queryFile || "queries.txt" }} · {{ rows.length }}
        </v-chip>
      </template>

      <template #bulk="{ count, selected }">
        <v-btn
          size="small"
          color="error"
          variant="tonal"
          prepend-icon="mdi-delete"
          data-test="keywords-delete-selected"
          @click="askDeleteSelected((selected as { id: string }[]).map((row) => row.id))"
        >
          Удалить выбранные ({{ count }})
        </v-btn>
        <v-btn
          size="small"
          variant="text"
          data-test="keywords-clear-selection"
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
          prepend-icon="mdi-delete-forever-outline"
          :disabled="rows.length === 0"
          :loading="keywords.pending.value === 'delete'"
          data-test="keywords-delete-all"
          @click="askDeleteAll()"
        >
          Удалить все ({{ rows.length }})
        </v-btn>

        <v-btn
          size="small"
          variant="outlined"
          prepend-icon="mdi-open-in-new"
          :disabled="!fileMode"
          :loading="keywords.pending.value === 'file'"
          data-test="keywords-open-file"
          @click="void keywords.openFile()"
        >
          Открыть queries.txt
        </v-btn>

        <v-btn
          size="small"
          color="primary"
          prepend-icon="mdi-plus"
          data-test="keywords-add"
          @click="openAdd"
        >
          Добавить
        </v-btn>
      </template>

      <template #empty>
        <div class="empty-state" data-test="keywords-empty">
          <v-icon icon="mdi-key-variant" size="40px" />
          <div class="empty-state__title">Добавьте первый запрос</div>
          <div>Один поисковый запрос в строке — файл читает движок</div>
          <v-btn
            color="primary"
            prepend-icon="mdi-plus"
            class="mt-2"
            data-test="keywords-empty-add"
            @click="openAdd"
          >
            Добавить запрос
          </v-btn>
        </div>
      </template>

      <template #item.actions="{ item }">
        <v-btn
          icon="mdi-delete"
          variant="text"
          size="small"
          color="error"
          :data-test="`keywords-delete-${item.id}`"
          aria-label="Удалить запрос"
          @click="askDelete(item.value)"
        />
      </template>
    </DataTablePage>

    <v-alert
      v-if="keywords.addResult.value"
      type="success"
      variant="tonal"
      class="mt-4"
      closable
      data-test="keywords-add-result"
      @click:close="keywords.addResult.value = null"
    >
      Добавлено {{ keywords.addResult.value.added }}, пропущено
      {{ keywords.addResult.value.skipped }}.
      <div
        v-for="problem in keywords.addResult.value.problems"
        :key="problem"
      >
        {{ problem }}
      </div>
    </v-alert>

    <v-dialog v-model="addDialog" max-width="560">
      <v-card data-test="keywords-add-dialog">
        <v-card-title class="text-subtitle-1 font-weight-bold pt-4 px-4">
          Добавить запросы
        </v-card-title>

        <v-card-text class="px-4 pb-2">
          <v-textarea
            v-model="addText"
            label="Поисковые запросы, по одному в строке"
            hint="# — комментарий; дубликаты пропускаются"
            rows="8"
            auto-grow
            data-test="keywords-add-text"
          />

          <v-alert
            v-if="keywords.actionError.value"
            type="error"
            variant="tonal"
            density="compact"
            class="mt-2"
            data-test="keywords-add-error"
          >
            {{ keywords.actionError.value }}
          </v-alert>

          <v-alert
            v-if="keywords.addResult.value"
            type="success"
            variant="tonal"
            density="compact"
            class="mt-2"
            data-test="keywords-add-dialog-result"
          >
            Добавлено {{ keywords.addResult.value.added }}, пропущено
            {{ keywords.addResult.value.skipped }}.
            <div
              v-for="problem in keywords.addResult.value.problems"
              :key="problem"
            >
              {{ problem }}
            </div>
          </v-alert>
        </v-card-text>

        <v-card-actions class="px-4 pb-4">
          <v-btn variant="text" data-test="keywords-add-close" @click="closeAdd">
            Закрыть
          </v-btn>
          <v-spacer />
          <v-btn
            color="primary"
            :loading="keywords.pending.value === 'add'"
            :disabled="parsedLines.length === 0"
            data-test="keywords-add-submit"
            @click="submitAdd"
          >
            Добавить
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <ConfirmDialog
      v-model="deleteDialog"
      title="Удалить запрос?"
      :text="
        deleteTarget ? `«${deleteTarget}» уйдёт из списка запросов.` : undefined
      "
      confirm-label="Удалить"
      destructive
      data-test="keywords-delete-dialog"
      @confirm="confirmDelete"
    />

    <ConfirmDialog
      v-model="bulkDialog"
      :title="bulkTarget?.title ?? 'Удалить запросы?'"
      :text="bulkTarget?.text"
      confirm-label="Удалить"
      destructive
      data-test="keywords-bulk-delete-dialog"
      @confirm="confirmBulkDelete"
    />
  </PageLayout>
</template>
