<script setup lang="ts">
// Экран Domains (план §5, фаза 13): чёрный список клика и его настройки.
//
// Список доменов живёт в файле (paths.filtered_domains), владеет им демон:
// мутации идут в control API (/control/domains*), после каждого действия
// снапшот перечитывается. Формат ответов совпадает с запросами, поэтому
// таблицей ровно тот же композитор useWordlist.
//
// Настройки вокруг списка (behavior.own_domain, behavior.excludes) — поля
// config.json: их читает и правит общий useSettings (GET/POST
// /control/config), тот же синглтон, что и на Settings. Семантика
// принятая: список = запрет клика, наш домен блокируется автоматически.

import { computed, onMounted, onUnmounted, ref } from "vue";
import ConfirmDialog from "../components/forms/ConfirmDialog.vue";
import DataTablePage from "../components/data/DataTablePage.vue";
import PageLayout from "../components/layout/PageLayout.vue";
import SettingField from "../components/forms/SettingField.vue";
import { createWordlist } from "../composables/useWordlist";
import { useSettings } from "../composables/useSettings";
import { domainsApi, type DomainsSnapshot } from "../lib/wordlist";
import type { DataTableHeader } from "vuetify";

const domains = createWordlist<DomainsSnapshot>(domainsApi, {
  items: (snapshot) => snapshot.domains,
  fileLabel: "domains.txt",
});

const settings = useSettings();

onMounted(() => {
  void domains.start();
  void settings.load();
});
onUnmounted(() => domains.stop());

const headers: DataTableHeader[] = [
  { key: "value", title: "Домен", sortable: true },
  { key: "actions", title: "", sortable: false, align: "end" },
];

/** Строки таблицы: id = сам домен (нормализован демоном, дублей нет). */
const rows = computed(() =>
  domains.items.value.map((value) => ({ id: value, value })),
);

const filePath = computed(() => domains.snapshot.value?.filtered_domains ?? "");

// --- настройки чёрного списка ---------------------------------------------

function errorOf(key: string): string | null {
  return settings.fieldErrors.value[`behavior.${key}`] ?? null;
}

function isDirty(key: string): boolean {
  return settings.dirtyKeys.value.includes(`behavior.${key}`);
}

async function saveSettings(): Promise<void> {
  if (!settings.canSave.value) return;
  await settings.save();
}

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
  domains.actionError.value = null;
  domains.addResult.value = null;
  addDialog.value = true;
}

function closeAdd(): void {
  addDialog.value = false;
}

async function submitAdd(): Promise<void> {
  if (parsedLines.value.length === 0) return;
  const ok = await domains.add(parsedLines.value);
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
  await domains.remove([target]);
}

// --- батчевое удаление ---------------------------------------------------

/** Ссылка на таблицу — нужна, чтобы снять выбор после удаления строк. */
const table = ref<InstanceType<typeof DataTablePage> | null>(null);

interface BulkTarget {
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
  domains.deleteResult.value = null;
  bulkTarget.value = {
    all: false,
    values,
    title: `Удалить выбранные домены (${values.length})?`,
    text: `${values.length} доменов уйдут из чёрного списка — их ссылки снова смогут кликаться.`,
  };
}

function askDeleteAll(): void {
  const count = rows.value.length;
  if (count === 0) return;
  domains.deleteResult.value = null;
  bulkTarget.value = {
    all: true,
    values: [],
    title: `Удалить все домены (${count})?`,
    text:
      `Из файла ${filePath.value || "domains.txt"} уйдут ${count} доменов. ` +
      "Наш домен (behavior.own_domain) останется заблокированным — он в список не входит.",
  };
}

async function confirmBulkDelete(): Promise<void> {
  const target = bulkTarget.value;
  bulkTarget.value = null;
  if (target === null) return;
  const ok = target.all
    ? await domains.removeAll()
    : await domains.remove(target.values);
  if (ok) table.value?.clearSelection();
}
</script>

<template>
  <PageLayout
    title="Domains"
    subtitle="Домены, ссылки на которые никогда не кликаются"
  >
    <v-alert
      v-if="domains.actionError.value"
      type="error"
      variant="tonal"
      class="mb-4"
      closable
      data-test="domains-action-error"
      @click:close="domains.actionError.value = null"
    >
      {{ domains.actionError.value }}
    </v-alert>

    <v-alert
      v-if="settings.saveError.value"
      type="error"
      variant="tonal"
      class="mb-4"
      closable
      data-test="domains-settings-error"
      @click:close="settings.saveError.value = null"
    >
      {{ settings.saveError.value }}
    </v-alert>

    <v-alert
      v-if="settings.success.value"
      type="success"
      variant="tonal"
      class="mb-4"
      closable
      data-test="domains-settings-success"
      @click:close="settings.success.value = null"
    >
      {{ settings.success.value }}
    </v-alert>

    <v-alert
      v-if="domains.deleteResult.value"
      type="success"
      variant="tonal"
      class="mb-4"
      closable
      data-test="domains-delete-result"
      @click:close="domains.deleteResult.value = null"
    >
      Удалено {{ domains.deleteResult.value.deleted }}, пропущено
      {{ domains.deleteResult.value.skipped }}.
      <div
        v-for="problem in domains.deleteResult.value.problems"
        :key="problem"
      >
        {{ problem }}
      </div>
    </v-alert>

    <v-row dense>
      <v-col cols="12" xl="8">
        <DataTablePage
          ref="table"
          :headers="headers"
          :items="rows"
          :loading="domains.loading.value"
          :error="domains.error.value"
          :selectable="true"
          empty-icon="mdi-domain-off"
          empty-title="Список пуст"
          empty-hint="Добавьте домены, на которые нельзя кликать"
          data-test="domains-table"
          @retry="domains.tick()"
        >
          <template #filters>
            <v-chip
              color="primary"
              variant="tonal"
              size="small"
              data-test="domains-file-chip"
            >
              {{ filePath || "domains.txt" }} · {{ rows.length }}
            </v-chip>
          </template>

          <template #bulk="{ count, selected }">
            <v-btn
              size="small"
              color="error"
              variant="tonal"
              prepend-icon="mdi-delete"
              data-test="domains-delete-selected"
              @click="
                askDeleteSelected((selected as { id: string }[]).map((row) => row.id))
              "
            >
              Удалить выбранные ({{ count }})
            </v-btn>
            <v-btn
              size="small"
              variant="text"
              data-test="domains-clear-selection"
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
              :loading="domains.pending.value === 'delete'"
              data-test="domains-delete-all"
              @click="askDeleteAll()"
            >
              Удалить все ({{ rows.length }})
            </v-btn>

            <v-btn
              size="small"
              variant="outlined"
              prepend-icon="mdi-open-in-new"
              :loading="domains.pending.value === 'file'"
              data-test="domains-open-file"
              @click="void domains.openFile()"
            >
              Открыть domains.txt
            </v-btn>

            <v-btn
              size="small"
              color="primary"
              prepend-icon="mdi-plus"
              data-test="domains-add"
              @click="openAdd"
            >
              Добавить
            </v-btn>
          </template>

          <template #empty>
            <div class="empty-state" data-test="domains-empty">
              <v-icon icon="mdi-domain-off" size="40px" />
              <div class="empty-state__title">Список пуст</div>
              <div>Добавьте домены, на которые нельзя кликать</div>
              <v-btn
                color="primary"
                prepend-icon="mdi-plus"
                class="mt-2"
                data-test="domains-empty-add"
                @click="openAdd"
              >
                Добавить домен
              </v-btn>
            </div>
          </template>

          <template #item.actions="{ item }">
            <v-btn
              icon="mdi-delete"
              variant="text"
              size="small"
              color="error"
              :data-test="`domains-delete-${item.id}`"
              aria-label="Удалить домен"
              @click="askDelete(item.value)"
            />
          </template>
        </DataTablePage>

        <v-alert
          v-if="domains.addResult.value"
          type="success"
          variant="tonal"
          class="mt-4"
          closable
          data-test="domains-add-result"
          @click:close="domains.addResult.value = null"
        >
          Добавлено {{ domains.addResult.value.added }}, пропущено
          {{ domains.addResult.value.skipped }}.
          <div
            v-for="problem in domains.addResult.value.problems"
            :key="problem"
          >
            {{ problem }}
          </div>
        </v-alert>
      </v-col>

      <v-col cols="12" xl="4">
        <v-card data-test="domains-settings">
          <v-card-title class="text-subtitle-1 font-weight-bold">
            Настройки исключений
            <span class="text-muted text-body-2"> — behavior.*</span>
          </v-card-title>

          <v-card-text class="pt-0">
            <v-alert
              type="info"
              variant="tonal"
              density="compact"
              class="mb-3"
              data-test="domains-settings-hint"
            >
              Список слева — чёрный список: реклама, товары и органика на эти
              домены не кликаются. Наш домен блокируется автоматически, а
              слова-исключения ловят ссылки и заголовки по вхождению.
            </v-alert>

            <SettingField
              name="own_domain"
              type="string"
              :model-value="settings.values.value['behavior.own_domain']"
              hint="Наш домен (хост или URL): попадает в чёрный список клика автоматически"
              :error="errorOf('own_domain')"
              :dirty="isDirty('own_domain')"
              @update:model-value="settings.setValue('behavior.own_domain', $event)"
            />

            <SettingField
              name="excludes"
              type="string"
              :model-value="settings.values.value['behavior.excludes']"
              hint="Слова-исключения через запятую: ссылки и объявления с ними не кликаются"
              :error="errorOf('excludes')"
              :dirty="isDirty('excludes')"
              @update:model-value="settings.setValue('behavior.excludes', $event)"
            />

            <v-btn
              color="primary"
              prepend-icon="mdi-content-save"
              class="mt-2"
              :disabled="!settings.canSave.value"
              :loading="settings.saving.value"
              data-test="domains-settings-save"
              @click="saveSettings"
            >
              Сохранить настройки
            </v-btn>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>

    <v-dialog v-model="addDialog" max-width="560">
      <v-card data-test="domains-add-dialog">
        <v-card-title class="text-subtitle-1 font-weight-bold pt-4 px-4">
          Добавить домены
        </v-card-title>

        <v-card-text class="px-4 pb-2">
          <v-textarea
            v-model="addText"
            label="Домены, по одному в строке"
            hint="Хост или URL — приводится к хосту; # — комментарий"
            rows="8"
            auto-grow
            data-test="domains-add-text"
          />

          <v-alert
            v-if="domains.actionError.value"
            type="error"
            variant="tonal"
            density="compact"
            class="mt-2"
            data-test="domains-add-error"
          >
            {{ domains.actionError.value }}
          </v-alert>

          <v-alert
            v-if="domains.addResult.value"
            type="success"
            variant="tonal"
            density="compact"
            class="mt-2"
            data-test="domains-add-dialog-result"
          >
            Добавлено {{ domains.addResult.value.added }}, пропущено
            {{ domains.addResult.value.skipped }}.
            <div
              v-for="problem in domains.addResult.value.problems"
              :key="problem"
            >
              {{ problem }}
            </div>
          </v-alert>
        </v-card-text>

        <v-card-actions class="px-4 pb-4">
          <v-btn variant="text" data-test="domains-add-close" @click="closeAdd">
            Закрыть
          </v-btn>
          <v-spacer />
          <v-btn
            color="primary"
            :loading="domains.pending.value === 'add'"
            :disabled="parsedLines.length === 0"
            data-test="domains-add-submit"
            @click="submitAdd"
          >
            Добавить
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <ConfirmDialog
      v-model="deleteDialog"
      title="Удалить домен?"
      :text="
        deleteTarget
          ? `Ссылки на ${deleteTarget} снова смогут кликаться.`
          : undefined
      "
      confirm-label="Удалить"
      destructive
      data-test="domains-delete-dialog"
      @confirm="confirmDelete"
    />

    <ConfirmDialog
      v-model="bulkDialog"
      :title="bulkTarget?.title ?? 'Удалить домены?'"
      :text="bulkTarget?.text"
      confirm-label="Удалить"
      destructive
      data-test="domains-bulk-delete-dialog"
      @confirm="confirmBulkDelete"
    />
  </PageLayout>
</template>
