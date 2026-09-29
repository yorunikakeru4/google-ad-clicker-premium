<script setup lang="ts">
import { computed } from "vue";

const props = withDefaults(
  defineProps<{
    ageSeconds?: number | null;
    intervalSeconds?: number;
  }>(),
  {
    ageSeconds: null,
    intervalSeconds: 5,
  },
);

const level = computed<"idle" | "ok" | "warn" | "error">(() => {
  const age = props.ageSeconds;
  if (age === null || age === undefined) return "idle";
  if (age <= props.intervalSeconds * 2) return "ok";
  if (age <= props.intervalSeconds * 4) return "warn";
  return "error";
});

const colorToken = computed(
  () =>
    ({
      idle: "on-surface",
      ok: "success",
      warn: "warning",
      error: "error",
    })[level.value],
);

const dotStyle = computed(() => ({
  background: `rgb(var(--v-theme-${colorToken.value}))`,
}));

const pulsing = computed(() => level.value === "ok");

const text = computed(() => {
  if (level.value === "idle") return "—";
  if (level.value === "error") return "нет сигнала";
  return `${Math.round(props.ageSeconds ?? 0)} с назад`;
});

const label = computed(() => {
  if (level.value === "idle") return "heartbeat: нет данных";
  if (level.value === "error") return "heartbeat: нет сигнала";
  return `heartbeat: ${text.value}`;
});
</script>

<template>
  <div class="heartbeat d-flex align-center ga-2" :title="label" :aria-label="label">
    <span
      class="heartbeat__dot"
      :class="{ 'heartbeat__dot--pulsing': pulsing }"
      :style="dotStyle"
      aria-hidden="true"
    />
    <span class="heartbeat__text text-muted">{{ text }}</span>
  </div>
</template>

<style scoped>
.heartbeat {
  white-space: nowrap;
}

.heartbeat__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: 0 0 auto;
}

.heartbeat__dot--pulsing {
  animation: heartbeat-pulse 1.6s ease-in-out infinite;
}

.heartbeat__text {
  font-size: 13px;
}
</style>
