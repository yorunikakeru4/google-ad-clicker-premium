<script setup lang="ts">
// Индикатор heartbeat демона для app bar: жив/недоступен + время последнего
// ответа. Данные — из общего singleton-состояния опроса.
import { computed } from "vue";
import { useDaemonStatus } from "../composables/useDaemonStatus";
import { formatClockTime } from "../lib/format";

const { state } = useDaemonStatus();

const online = computed(() => state.value.phase === "online");
const offline = computed(() => state.value.phase === "offline");
const lastOkAt = computed(() => state.value.lastOkAt);

const color = computed(() => {
  if (online.value) return "success";
  if (offline.value) return "error";
  return undefined; // idle/inflight — нейтральный
});

const label = computed(() => {
  if (online.value) return "жив";
  if (offline.value) return "нет связи";
  return "проверка…";
});

const detail = computed(() =>
  lastOkAt.value == null ? "нет ответов" : `ответ в ${formatClockTime(lastOkAt.value)}`,
);

const title = computed(() =>
  lastOkAt.value == null
    ? "Демон ещё не отвечал"
    : `Последний ответ демона: ${formatClockTime(lastOkAt.value)}`,
);
</script>

<template>
  <v-chip
    :color="color"
    variant="tonal"
    size="small"
    :title="title"
    data-test="heartbeat"
  >
    <v-icon start size="small">
      {{ online ? "mdi-heart-pulse" : offline ? "mdi-heart-off" : "mdi-heart-cog" }}
    </v-icon>
    {{ label }} · {{ detail }}
  </v-chip>
</template>
