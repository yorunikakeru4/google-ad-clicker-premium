<script setup lang="ts">
import { computed, ref } from "vue";

const LEVELS = ["INFO", "WARN", "ERROR"];

withDefaults(
  defineProps<{
    profileItems?: { title: string; value: string }[];
    periodItems?: { title: string; value: string }[];
  }>(),
  {
    profileItems: () => [],
    periodItems: () => [],
  },
);

const levels = ref<string[]>([]);
const query = ref("");
const profile = ref<string | null>(null);
const period = ref<string | null>(null);

const hasActive = computed(
  () => levels.value.length > 0 || Boolean(query.value) || Boolean(profile.value) || Boolean(period.value),
);

const queryChip = computed(() => query.value.trim());

function clearLevels() {
  levels.value = [];
}

function clearQuery() {
  query.value = "";
}

function clearProfile() {
  profile.value = null;
}

function clearPeriod() {
  period.value = null;
}

function reset() {
  levels.value = [];
  query.value = "";
  profile.value = null;
  period.value = null;
}

const profileModel = computed({
  get: () => profile.value,
  set: (value: string | null) => {
    profile.value = value;
  },
});

const periodModel = computed({
  get: () => period.value,
  set: (value: string | null) => {
    period.value = value;
  },
});

const levelsModel = computed({
  get: () => levels.value,
  set: (value: unknown) => {
    levels.value = Array.isArray(value) ? (value as string[]) : [];
  },
});
</script>

<template>
  <div class="filter-bar">
    <div class="filter-bar__controls d-flex align-center flex-wrap ga-2">
      <v-chip-group v-model="levelsModel" multiple class="filter-bar__levels">
        <v-chip v-for="level in LEVELS" :key="level" :value="level" filter>
          {{ level }}
        </v-chip>
      </v-chip-group>

      <v-text-field
        v-model="query"
        prepend-inner-icon="mdi-magnify"
        placeholder="Текст сообщения"
        hide-details
        density="compact"
        clearable
        style="max-width: 240px"
        aria-label="Поиск по тексту сообщения"
      />

      <v-select
        v-model="profileModel"
        :items="profileItems"
        label="Профиль"
        hide-details
        density="compact"
        clearable
        style="max-width: 180px"
      />

      <v-select
        v-model="periodModel"
        :items="periodItems"
        label="Период"
        hide-details
        density="compact"
        clearable
        style="max-width: 180px"
      />
    </div>

    <div v-if="hasActive" class="filter-bar__active d-flex align-center flex-wrap ga-2 mt-2">
      <v-chip v-if="levels.length" closable size="small" @click:close="clearLevels">
        Уровни: {{ levels.join(", ") }}
      </v-chip>
      <v-chip v-if="queryChip" closable size="small" @click:close="clearQuery">
        Текст: {{ queryChip }}
      </v-chip>
      <v-chip v-if="profile" closable size="small" @click:close="clearProfile">
        Профиль: {{ profile }}
      </v-chip>
      <v-chip v-if="period" closable size="small" @click:close="clearPeriod">
        Период: {{ period }}
      </v-chip>
      <v-btn variant="text" size="small" @click="reset">Сбросить</v-btn>
    </div>
  </div>
</template>

<style scoped>
.filter-bar__levels :deep(.v-chip) {
  height: 26px;
}
</style>
