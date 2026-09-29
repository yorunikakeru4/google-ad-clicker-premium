<script setup lang="ts">
// Экран Profiles (план §5, фаза 6): список профилей с состоянием, клиентские
// фильтры и массовые действия. Чтение — только через control API демона
// (`GET /control/profiles` + `GET /control/proxies` для колонки «Прокси»),
// любая мутация уходит в control API и после успеха перечитывает список —
// ридер БД остаётся read-only.
//
// Периодический refresh — 4 с (useProfiles.PROFILES_POLL_MS): 300 профилей —
// не проблема ни для демона, ни для клиента. Автопаузы нет: экрана хватает
// на закрытие вкладки (stop в onUnmounted).
//
// Креды в данных отсутствуют на всех уровнях: Rust-читалка не выбирает
// username/password прокси, API-слой собирает строку только из контрактных
// полей, а key_ref — это ссылка на ключ, а не сам ключ, поэтому он лежит в
// ячейке обычным текстом.

import { computed, onMounted, onUnmounted, ref } from "vue";
import ConfirmDialog from "../components/forms/ConfirmDialog.vue";
import DataTablePage from "../components/data/DataTablePage.vue";
import FilterBar from "../components/data/FilterBar.vue";
import PageLayout from "../components/layout/PageLayout.vue";
import StatusChip from "../components/status/StatusChip.vue";
import { useProfiles } from "../composables/useProfiles";
import {
  PROFILE_STATUSES,
  profileStatusKind,
  profileStatusLabel,
} from "../constants/statusMap";
import { formatLastUsed } from "../lib/format";
import {
  checkAssignRange,
  filterProfiles,
  parseProfileImportLines,
  toProfileTableRow,
  type NewProfile,
  type ProfileTableRow,
} from "../lib/profiles";
import type { DataTableHeader } from "vuetify";

const profiles = useProfiles();

onMounted(() => void profiles.start());
onUnmounted(() => profiles.stop());

const headers: DataTableHeader[] = [
  { key: "name", title: "Профиль", sortable: true },
  { key: "key_ref", title: "Ключ", sortable: true },
  { key: "proxy", title: "Прокси", sortable: true },
  { key: "user_agent", title: "User-Agent", sortable: true },
  { key: "status", title: "Статус", sortable: false },
  { key: "assigned_browser_id", title: "Поток", sortable: true },
  { key: "last_used_at", title: "Последнее использование", sortable: true },
  { key: "actions", title: "", sortable: false, align: "end" },
];

// --- фильтры -------------------------------------------------------------

const statusFilter = ref("");
const nameFilter = ref("");

/** Значения селекта статуса: «все» плюс контрактный список статусов. */
const statusItems = [
  { title: "все статусы", value: "" },
  ...PROFILE_STATUSES.map((status) => ({
    title: profileStatusLabel(status),
    value: status,
  })),
];

/** Активные фильтры чипами: их наличие и есть «фильтр включён». */
const filterChips = computed(() => {
  const chips: { key: string; label: string; value: string }[] = [];
  if (statusFilter.value !== "") {
    chips.push({
      key: "status",
      label: "Статус",
      value: profileStatusLabel(statusFilter.value),
    });
  }
  if (nameFilter.value.trim() !== "") {
    chips.push({ key: "name", label: "Имя", value: nameFilter.value.trim() });
  }
  return chips;
});

/** Крестик на чипе снимает ровно это поле, остальной фильтр остаётся. */
function onFilterClear(key: string): void {
  if (key === "status") statusFilter.value = "";
  if (key === "name") nameFilter.value = "";
}

function resetFilters(): void {
  statusFilter.value = "";
  nameFilter.value = "";
}

// --- таблица -------------------------------------------------------------

const proxiesById = computed(
  () => new Map(profiles.proxies.value.map((proxy) => [proxy.id, proxy])),
);

// Готовые к показу строки: join прокси на клиенте, прочерки вместо NULL,
// статус и время остаются в контрактных значениях — сортировка идёт по ним.
const tableRows = computed(() =>
  filterProfiles(profiles.rows.value, {
    status: statusFilter.value,
    name: nameFilter.value,
  }).map((row) => toProfileTableRow(row, proxiesById.value)),
);

// --- диалог добавления ---------------------------------------------------

const addDialog = ref(false);
const addName = ref("");
const addKeyRef = ref("");
const addProxyId = ref<number | null>(null);
const addUserAgent = ref("");
const addLocale = ref("");
const addTimezone = ref("");

/** Опции селекта прокси: «без прокси» + label/адрес каждого прокси. */
const proxySelectItems = computed(() => [
  { title: "— без прокси —", value: null },
  ...profiles.proxies.value.map((proxy) => {
    const label = proxy.label?.trim();
    return {
      title: label ? `${label} (${proxy.host}:${proxy.port})` : `${proxy.host}:${proxy.port}`,
      value: proxy.id,
    };
  }),
]);

function openAdd(): void {
  profiles.actionError.value = null;
  profiles.addResult.value = null;
  addDialog.value = true;
}

function resetAddForm(): void {
  addName.value = "";
  addKeyRef.value = "";
  addProxyId.value = null;
  addUserAgent.value = "";
  addLocale.value = "";
  addTimezone.value = "";
}

async function submitAdd(): Promise<void> {
  const name = addName.value.trim();
  if (name === "") return;
  // Опциональные поля уходят только заполненными: пустая строка в JSON —
  // это не «нет значения», а значение, которое демон не ждёт.
  const profile: NewProfile = { name };
  const keyRef = addKeyRef.value.trim();
  if (keyRef !== "") profile.key_ref = keyRef;
  if (addProxyId.value !== null) profile.proxy_id = addProxyId.value;
  const userAgent = addUserAgent.value.trim();
  if (userAgent !== "") profile.user_agent = userAgent;
  const locale = addLocale.value.trim();
  if (locale !== "") profile.locale = locale;
  const timezone = addTimezone.value.trim();
  if (timezone !== "") profile.timezone = timezone;

  // Текст очищает только успех: при ошибке пользователь правит поля.
  const ok = await profiles.add([profile]);
  if (ok) resetAddForm();
}

// --- диалог импорта ------------------------------------------------------

const importDialog = ref(false);
const importText = ref("");

const importLines = computed(() => parseProfileImportLines(importText.value));

function openImport(): void {
  profiles.actionError.value = null;
  profiles.importResult.value = null;
  importDialog.value = true;
}

async function submitImport(): Promise<void> {
  if (importLines.value.length === 0) return;
  const ok = await profiles.importLines(importLines.value);
  if (ok) importText.value = "";
}

// --- диалог назначения диапазона ----------------------------------------

const assignDialog = ref(false);
const assignStart = ref("");
const assignEnd = ref("");

const assignRange = computed(() =>
  checkAssignRange(assignStart.value, assignEnd.value),
);

function openAssign(): void {
  profiles.actionError.value = null;
  profiles.assignResult.value = null;
  assignStart.value = "";
  assignEnd.value = "";
  assignDialog.value = true;
}

async function submitAssign(): Promise<void> {
  const range = assignRange.value;
  if (!range.ok) return;
  await profiles.assign(range.start, range.end);
}

// --- сброс назначений ----------------------------------------------------

const unassignDialog = ref(false);

async function confirmUnassign(): Promise<void> {
  await profiles.unassign();
}

// --- удаление ------------------------------------------------------------

const deleteTarget = ref<ProfileTableRow | null>(null);

const deleteDialog = computed({
  get: () => deleteTarget.value !== null,
  set: (open: boolean) => {
    if (!open) deleteTarget.value = null;
  },
});

async function confirmDelete(): Promise<void> {
  const target = deleteTarget.value;
  deleteTarget.value = null;
  if (target === null) return;
  // 409 «профиль назначен потоку» придёт в actionError над таблицей.
  await profiles.remove(target.id);
}

// --- меню статуса строки -------------------------------------------------

/** Действия меню: только те статусы, которые принимает API. */
const statusActions = [
  { status: "blocked" as const, title: "Заблокировать", icon: "mdi-lock" },
  { status: "error" as const, title: "Отметить ошибкой", icon: "mdi-alert-circle" },
  { status: "free" as const, title: "Освободить", icon: "mdi-lock-open" },
];

function applyStatus(row: ProfileTableRow, status: "blocked" | "error" | "free"): void {
  void profiles.setStatus(row.id, status);
}
</script>

<template>
  <PageLayout
    title="Profiles"
    subtitle="Профили, ключи и их состояние"
  >
    <v-alert
      v-if="profiles.actionError.value"
      type="error"
      variant="tonal"
      class="mb-4"
      closable
      data-test="profiles-action-error"
      @click:close="profiles.actionError.value = null"
    >
      {{ profiles.actionError.value }}
    </v-alert>

    <v-alert
      v-if="profiles.unassignResult.value"
      type="success"
      variant="tonal"
      class="mb-4"
      closable
      data-test="profiles-unassign-result"
      @click:close="profiles.unassignResult.value = null"
    >
      Назначения сброшены: освобождено профилей
      {{ profiles.unassignResult.value.released }}.
    </v-alert>

    <DataTablePage
      :headers="headers"
      :items="tableRows"
      :loading="profiles.loading.value"
      :error="profiles.error.value"
      :selectable="false"
      :show-search="false"
      empty-icon="mdi-account-plus"
      empty-title="Добавьте первый профиль"
      empty-hint="Создайте профиль вручную или импортируйте key_ref списком"
      data-test="profiles-table"
      @retry="profiles.tick()"
    >
      <template #filters>
        <FilterBar
          :chips="filterChips"
          @clear="onFilterClear"
          @reset="resetFilters"
        >
          <v-select
            v-model="statusFilter"
            :items="statusItems"
            label="Статус"
            hide-details
            density="compact"
            style="max-width: 180px"
            data-test="profiles-filter-status"
          />
          <v-text-field
            v-model="nameFilter"
            label="Поиск по имени"
            hide-details
            density="compact"
            clearable
            style="max-width: 220px"
            data-test="profiles-filter-name"
          />
        </FilterBar>
      </template>

      <template #actions>
        <v-btn
          size="small"
          variant="outlined"
          prepend-icon="mdi-swap-horizontal"
          class="mr-2"
          data-test="profiles-unassign"
          @click="unassignDialog = true"
        >
          Сбросить назначения
        </v-btn>

        <v-btn
          size="small"
          variant="outlined"
          prepend-icon="mdi-call-split"
          class="mr-2"
          data-test="profiles-assign"
          @click="openAssign"
        >
          Назначить диапазон
        </v-btn>

        <v-btn
          size="small"
          variant="outlined"
          prepend-icon="mdi-import"
          class="mr-2"
          data-test="profiles-import"
          @click="openImport"
        >
          Импорт
        </v-btn>

        <v-btn
          size="small"
          color="primary"
          prepend-icon="mdi-plus"
          data-test="profiles-add"
          @click="openAdd"
        >
          Добавить
        </v-btn>
      </template>

      <template #empty>
        <div class="empty-state" data-test="profiles-empty">
          <v-icon icon="mdi-account-plus" size="40px" />
          <div class="empty-state__title">Добавьте первый профиль</div>
          <div>Создайте профиль вручную или импортируйте key_ref списком</div>
          <v-btn
            color="primary"
            prepend-icon="mdi-plus"
            class="mt-2"
            data-test="profiles-empty-add"
            @click="openAdd"
          >
            Добавить профиль
          </v-btn>
        </div>
      </template>

      <template #item.key_ref="{ item }">
        <span
          class="text-truncate cell-limit"
          :title="item.key_ref === '—' ? undefined : item.key_ref"
        >
          {{ item.key_ref }}
        </span>
      </template>

      <template #item.user_agent="{ item }">
        <span
          class="text-truncate cell-limit"
          :title="item.user_agent === '—' ? undefined : item.user_agent"
        >
          {{ item.user_agent }}
        </span>
      </template>

      <template #item.status="{ item }">
        <StatusChip
          :status="profileStatusKind(item.status)"
          :label="profileStatusLabel(item.status)"
        />
      </template>

      <template #item.last_used_at="{ item }">
        {{ formatLastUsed(item.last_used_at) }}
      </template>

      <template #item.actions="{ item }">
        <v-menu>
          <template #activator="{ props: menuProps }">
            <v-btn
              v-bind="menuProps"
              icon="mdi-dots-vertical"
              variant="text"
              size="small"
              :data-test="`profiles-status-menu-${item.id}`"
              aria-label="Статус профиля"
            />
          </template>
          <v-list density="compact">
            <v-list-item
              v-for="action in statusActions"
              :key="action.status"
              :prepend-icon="action.icon"
              :data-test="`profiles-status-${action.status}-${item.id}`"
              @click="applyStatus(item, action.status)"
            >
              <v-list-item-title>{{ action.title }}</v-list-item-title>
            </v-list-item>
          </v-list>
        </v-menu>

        <v-btn
          icon="mdi-delete"
          variant="text"
          size="small"
          color="error"
          :data-test="`profiles-delete-${item.id}`"
          aria-label="Удалить профиль"
          @click="deleteTarget = item"
        />
      </template>
    </DataTablePage>

    <!-- добавление -->
    <v-dialog v-model="addDialog" max-width="560">
      <v-card data-test="profiles-add-dialog">
        <v-card-title class="text-subtitle-1 font-weight-bold pt-4 px-4">
          Добавить профиль
        </v-card-title>

        <v-card-text class="px-4 pb-2">
          <v-text-field
            v-model="addName"
            label="Имя профиля*"
            hint="Уникально в списке профилей"
            persistent-hint
            data-test="profiles-add-name"
          />

          <v-text-field
            v-model="addKeyRef"
            label="Ссылка на ключ (key_ref)"
            hint="Не сам ключ, а ссылка на него в hooks.py"
            persistent-hint
            data-test="profiles-add-key-ref"
          />

          <v-select
            v-model="addProxyId"
            :items="proxySelectItems"
            label="Прокси"
            data-test="profiles-add-proxy"
          />

          <v-text-field
            v-model="addUserAgent"
            label="User-Agent"
            data-test="profiles-add-user-agent"
          />

          <v-text-field
            v-model="addLocale"
            label="Локаль"
            hint="Например, de-DE"
            persistent-hint
            data-test="profiles-add-locale"
          />

          <v-text-field
            v-model="addTimezone"
            label="Часовой пояс"
            hint="Например, Europe/Berlin"
            persistent-hint
            data-test="profiles-add-timezone"
          />

          <v-alert
            v-if="profiles.actionError.value"
            type="error"
            variant="tonal"
            density="compact"
            class="mt-2"
            data-test="profiles-add-error"
          >
            {{ profiles.actionError.value }}
          </v-alert>

          <v-alert
            v-if="profiles.addResult.value"
            type="success"
            variant="tonal"
            density="compact"
            class="mt-2"
            data-test="profiles-add-result"
          >
            Добавлено {{ profiles.addResult.value.added }}, пропущено
            {{ profiles.addResult.value.skipped }}.
            <div
              v-for="problem in profiles.addResult.value.problems"
              :key="problem"
            >
              {{ problem }}
            </div>
          </v-alert>
        </v-card-text>

        <v-card-actions class="px-4 pb-4">
          <v-btn variant="text" data-test="profiles-add-close" @click="addDialog = false">
            Закрыть
          </v-btn>
          <v-spacer />
          <v-btn
            color="primary"
            :loading="profiles.pending.value === 'add'"
            :disabled="addName.trim() === ''"
            data-test="profiles-add-submit"
            @click="submitAdd"
          >
            Добавить
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- импорт -->
    <v-dialog v-model="importDialog" max-width="560">
      <v-card data-test="profiles-import-dialog">
        <v-card-title class="text-subtitle-1 font-weight-bold pt-4 px-4">
          Импорт профилей
        </v-card-title>

        <v-card-text class="px-4 pb-2">
          <v-textarea
            v-model="importText"
            label="key_ref, по одному в строке"
            hint="Одна строка = один key_ref; пустые строки и # — комментарии"
            rows="8"
            auto-grow
            data-test="profiles-import-text"
          />

          <v-alert
            v-if="profiles.actionError.value"
            type="error"
            variant="tonal"
            density="compact"
            class="mt-2"
            data-test="profiles-import-error"
          >
            {{ profiles.actionError.value }}
          </v-alert>

          <v-alert
            v-if="profiles.importResult.value"
            type="success"
            variant="tonal"
            density="compact"
            class="mt-2"
            data-test="profiles-import-result"
          >
            Импортировано: добавлено
            {{ profiles.importResult.value.added }}, пропущено
            {{ profiles.importResult.value.skipped }}.
            <div
              v-for="problem in profiles.importResult.value.problems"
              :key="problem"
            >
              {{ problem }}
            </div>
          </v-alert>
        </v-card-text>

        <v-card-actions class="px-4 pb-4">
          <v-btn variant="text" data-test="profiles-import-close" @click="importDialog = false">
            Закрыть
          </v-btn>
          <v-spacer />
          <v-btn
            color="primary"
            :loading="profiles.pending.value === 'import'"
            :disabled="importLines.length === 0"
            data-test="profiles-import-submit"
            @click="submitImport"
          >
            Импортировать
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- назначение диапазона -->
    <v-dialog v-model="assignDialog" max-width="520">
      <v-card data-test="profiles-assign-dialog">
        <v-card-title class="text-subtitle-1 font-weight-bold pt-4 px-4">
          Назначить диапазон профилей
        </v-card-title>

        <v-card-text class="px-4 pb-2">
          <v-text-field
            v-model="assignStart"
            label="start_id*"
            hint="Первый id диапазона, целое от 1"
            persistent-hint
            data-test="profiles-assign-start"
          />

          <v-text-field
            v-model="assignEnd"
            label="end_id*"
            hint="Последний id диапазона, не меньше start_id"
            persistent-hint
            data-test="profiles-assign-end"
          />

          <v-alert
            v-if="assignStart !== '' || assignEnd !== ''"
            :type="assignRange.ok ? 'info' : 'warning'"
            variant="tonal"
            density="compact"
            class="mt-2"
            data-test="profiles-assign-hint"
          >
            {{ assignRange.ok ? `Диапазон ${assignRange.start}–${assignRange.end}` : assignRange.error }}
          </v-alert>

          <v-alert
            v-if="profiles.actionError.value"
            type="error"
            variant="tonal"
            density="compact"
            class="mt-2"
            data-test="profiles-assign-error"
          >
            {{ profiles.actionError.value }}
          </v-alert>

          <v-alert
            v-if="profiles.assignResult.value"
            type="success"
            variant="tonal"
            density="compact"
            class="mt-2"
            data-test="profiles-assign-result"
          >
            Назначено {{ profiles.assignResult.value.assigned }}, свободно осталось
            {{ profiles.assignResult.value.available }}.
          </v-alert>
        </v-card-text>

        <v-card-actions class="px-4 pb-4">
          <v-btn variant="text" data-test="profiles-assign-close" @click="assignDialog = false">
            Закрыть
          </v-btn>
          <v-spacer />
          <v-btn
            color="primary"
            :loading="profiles.pending.value === 'assign'"
            :disabled="!assignRange.ok"
            data-test="profiles-assign-submit"
            @click="submitAssign"
          >
            Назначить
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- подтверждения -->
    <ConfirmDialog
      v-model="unassignDialog"
      title="Сбросить все назначения?"
      text="Все профили перестанут быть назначенными на потоки. Потоки продолжат работу с текущими профилями до перезапуска."
      confirm-label="Сбросить"
      destructive
      data-test="profiles-unassign-dialog"
      @confirm="confirmUnassign"
    />

    <ConfirmDialog
      v-model="deleteDialog"
      title="Удалить профиль?"
      :text="
        deleteTarget
          ? `«${deleteTarget.name}» будет удалён из списка. Назначения потоков не трогаются.`
          : undefined
      "
      confirm-label="Удалить"
      destructive
      data-test="profiles-delete-dialog"
      @confirm="confirmDelete"
    />
  </PageLayout>
</template>

<style scoped>
/* Длинные значения (key_ref, User-Agent) — одна строка с полным текстом в
   title: колонка не разъезжается, а всё читается по наведению. */
.cell-limit {
  display: inline-block;
  max-width: 240px;
  vertical-align: middle;
}
</style>
