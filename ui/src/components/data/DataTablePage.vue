<script setup lang="ts">
import { computed, ref, useSlots } from "vue";
import type { DataTableHeader } from "vuetify";

type Row = Record<string, any>;

const props = withDefaults(
  defineProps<{
    headers: DataTableHeader[];
    items?: Row[];
    loading?: boolean;
    error?: string | null;
    selectable?: boolean;
    showSearch?: boolean;
    searchPlaceholder?: string;
    emptyIcon?: string;
    emptyTitle?: string;
    emptyHint?: string;
  }>(),
  {
    items: () => [],
    loading: false,
    error: null,
    selectable: true,
    showSearch: true,
    searchPlaceholder: "Поиск",
    emptyIcon: "mdi-table-large",
    emptyTitle: "Нет данных",
    emptyHint: undefined,
  },
);

const emit = defineEmits<{
  retry: [];
}>();

const slots = useSlots();
const RESERVED_SLOTS = ["filters", "actions", "bulk", "empty"];
const forwardedSlots = computed(() =>
  Object.keys(slots).filter((name) => !RESERVED_SLOTS.includes(name)),
);

const search = ref("");
const selected = ref<Row[]>([]);

/**
 * Снять выделение снаружи: после батчевого удаления выбранные строки уходят
 * из `items`, но Vuetify не чистит `selected` сам — панель «Удалить
 * выбранные» залипала бы на несуществующих id.
 */
function clearSelection(): void {
  selected.value = [];
}

defineExpose({ clearSelection });
</script>

<template>
  <v-card>
    <v-card-text class="d-flex align-center flex-wrap ga-2 px-4 py-3">
      <v-text-field
        v-if="showSearch"
        v-model="search"
        prepend-inner-icon="mdi-magnify"
        :placeholder="searchPlaceholder"
        hide-details
        density="compact"
        clearable
        style="max-width: 260px"
        aria-label="Поиск по таблице"
      />
      <slot name="filters" />
      <v-spacer />
      <slot name="actions" />
    </v-card-text>

    <v-divider />

    <div v-if="selected.length" class="d-flex align-center flex-wrap ga-2 px-4 py-2">
      <slot name="bulk" :count="selected.length" :selected="selected">
        <v-chip color="primary">Выбрано: {{ selected.length }}</v-chip>
      </slot>
    </div>

    <v-alert
      v-if="error"
      type="error"
      variant="tonal"
      class="ma-4"
      :text="error"
    >
      <template #append>
        <v-btn variant="text" size="small" @click="emit('retry')">Повторить</v-btn>
      </template>
    </v-alert>

    <v-skeleton-loader
      v-else-if="loading"
      type="table-thead, table-tbody"
      class="px-4 pb-4"
    />

    <v-data-table
      v-else
      v-model:selected="selected"
      :headers="headers"
      :items="items"
      :search="search"
      :show-select="selectable"
      :hide-no-data="false"
      item-value="id"
    >
      <template #no-data>
        <slot name="empty">
          <div class="empty-state">
            <v-icon :icon="emptyIcon" size="40px" />
            <div class="empty-state__title">{{ emptyTitle }}</div>
            <div v-if="emptyHint">{{ emptyHint }}</div>
          </div>
        </slot>
      </template>

      <template v-for="name in forwardedSlots" :key="name" #[name]="slotProps">
        <slot :name="name" v-bind="slotProps || {}" />
      </template>
    </v-data-table>
  </v-card>
</template>
