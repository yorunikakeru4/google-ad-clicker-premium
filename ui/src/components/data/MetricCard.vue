<script setup lang="ts">
import { computed } from "vue";
import type { RouteLocationRaw } from "vue-router";
import StatusChip from "../status/StatusChip.vue";
import type { StatusKind } from "../../constants/statusMap";

const props = withDefaults(
  defineProps<{
    label: string;
    /** Приписка к подписи: окно метрики, единицы, источник. */
    hint?: string;
    value?: string;
    /** Тест-хук: data-test на числе (корень карточки занят card-*). */
    valueTest?: string;
    delta?: string;
    deltaDirection?: "up" | "down" | "flat";
    status?: StatusKind;
    /** Текст статуса вместо подписи по умолчанию из statusMap. */
    statusLabel?: string;
    /** Тест-хук: data-test на чипе статуса. */
    statusTest?: string;
    to?: RouteLocationRaw;
  }>(),
  {
    hint: undefined,
    value: "—",
    valueTest: undefined,
    delta: undefined,
    deltaDirection: "flat",
    status: undefined,
    statusLabel: undefined,
    statusTest: undefined,
    to: undefined,
  },
);

const deltaIcon = computed(() => {
  switch (props.deltaDirection) {
    case "up":
      return "mdi-arrow-up";
    case "down":
      return "mdi-arrow-down";
    default:
      return "mdi-arrow-right";
  }
});

const deltaColor = computed(() => {
  if (props.deltaDirection === "up") return "success";
  if (props.deltaDirection === "down") return "error";
  return "on-surface";
});
</script>

<template>
  <v-card :to="to" class="metric h-100" :class="{ 'metric--clickable': Boolean(to) }">
    <v-card-text class="pa-4">
      <div class="metric__label text-muted">
        {{ label }}<span v-if="hint"> · {{ hint }}</span>
      </div>

      <div class="metric__value" :data-test="valueTest">{{ value }}</div>

      <slot />

      <div v-if="delta || status" class="metric__footer d-flex align-center ga-2 mt-2">
        <template v-if="delta">
          <v-icon :icon="deltaIcon" :color="deltaColor" size="14px" />
          <span class="metric__delta text-muted">{{ delta }}</span>
        </template>
        <StatusChip
          v-if="status"
          :status="status"
          :label="statusLabel"
          :data-test="statusTest"
        />
      </div>
    </v-card-text>
  </v-card>
</template>

<style scoped>
.metric {
  border-radius: 2px;
}

.metric--clickable {
  cursor: pointer;
}

.metric__label {
  font-size: 13px;
}

.metric__value {
  margin-top: 4px;
  font-size: 32px;
  font-weight: 700;
  line-height: 1.15;
  color: rgb(var(--v-theme-primary));
}

.metric__footer {
  min-height: 24px;
}

.metric__delta {
  font-size: 13px;
}
</style>
