<script setup lang="ts">
import { computed } from "vue";
import { statusMeta, type StatusKind } from "../../constants/statusMap";
import type { DaemonState } from "../../constants/daemon";

const props = withDefaults(
  defineProps<{
    state?: DaemonState;
  }>(),
  { state: "unknown" },
);

const status = computed<StatusKind>(() => {
  switch (props.state) {
    case "running":
      return "ok";
    case "paused":
      return "warn";
    case "stopped":
      return "error";
    default:
      return "idle";
  }
});

const label = computed(() => {
  switch (props.state) {
    case "running":
      return "демон работает";
    case "paused":
      return "демон на паузе";
    case "stopped":
      return "демон остановлен";
    default:
      return "нет данных";
  }
});

const meta = computed(() => statusMeta(status.value));
</script>

<template>
  <v-chip
    :to="{ name: 'diagnostics' }"
    :color="meta.color"
    :prepend-icon="meta.icon"
    aria-label="Состояние демона, открыть диагностику"
    class="daemon-status"
  >
    {{ label }}
  </v-chip>
</template>

<style scoped>
.daemon-status {
  text-transform: none;
}
</style>
