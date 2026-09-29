<script setup lang="ts">
import { ref } from "vue";
import ConfirmDialog from "../forms/ConfirmDialog.vue";
import type { DaemonState } from "../../constants/daemon";

const props = withDefaults(
  defineProps<{
    state?: DaemonState;
    pending?: "start" | "pause" | "stop" | null;
    activeTasks?: number;
  }>(),
  {
    state: "unknown",
    pending: null,
    activeTasks: 0,
  },
);

const emit = defineEmits<{
  start: [];
  pause: [];
  stop: [];
}>();

const confirmOpen = ref(false);

const startDisabled = () => props.state === "running";
const holdDisabled = () => props.state === "stopped";
const busy = () => props.activeTasks > 0;

function requestStop() {
  if (busy()) {
    confirmOpen.value = true;
    return;
  }
  emit("stop");
}
</script>

<template>
  <div class="daemon-controls d-flex align-center ga-2">
    <v-btn
      size="small"
      color="primary"
      prepend-icon="mdi-play"
      :disabled="startDisabled()"
      :loading="pending === 'start'"
      aria-label="Старт"
      @click="emit('start')"
    >
      Старт
    </v-btn>

    <v-btn
      size="small"
      variant="outlined"
      prepend-icon="mdi-pause"
      :disabled="holdDisabled()"
      :loading="pending === 'pause'"
      aria-label="Пауза"
      @click="emit('pause')"
    >
      Пауза
    </v-btn>

    <v-btn
      size="small"
      variant="outlined"
      color="error"
      prepend-icon="mdi-stop"
      :disabled="holdDisabled()"
      :loading="pending === 'stop'"
      aria-label="Стоп"
      @click="requestStop"
    >
      Стоп
    </v-btn>

    <ConfirmDialog
      v-model="confirmOpen"
      title="Остановить демон?"
      text="Выполняющиеся сценарии будут прерваны, браузеры закрыты."
      confirm-label="Остановить"
      destructive
      @confirm="emit('stop')"
    />
  </div>
</template>
