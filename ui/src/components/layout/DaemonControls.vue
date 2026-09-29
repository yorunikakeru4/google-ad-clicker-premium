<script setup lang="ts">
import { computed, ref } from "vue";
import ConfirmDialog from "../forms/ConfirmDialog.vue";
import type { DaemonState } from "../../constants/daemon";

type ControlKey = "start" | "pause" | "stop";

const props = withDefaults(
  defineProps<{
    state?: DaemonState;
    pending?: ControlKey | null;
    activeTasks?: number;
    disabled?: Partial<Record<ControlKey, string | null>>;
  }>(),
  {
    state: "unknown",
    pending: null,
    activeTasks: 0,
    disabled: () => ({}),
  },
);

const emit = defineEmits<{
  start: [];
  pause: [];
  stop: [];
}>();

const confirmOpen = ref(false);

const reasons = computed<Record<ControlKey, string | null>>(() => {
  const provided = props.disabled ?? {};
  return {
    start:
      provided.start !== undefined
        ? provided.start
        : props.state === "running"
          ? "воркеры уже запущены"
          : null,
    pause:
      provided.pause !== undefined
        ? provided.pause
        : props.state === "stopped"
          ? "нет запущенных воркеров"
          : null,
    stop:
      provided.stop !== undefined
        ? provided.stop
        : props.state === "stopped"
          ? "все воркеры уже остановлены"
          : null,
  };
});

const blocked = (key: ControlKey) => reasons.value[key] !== null;
const hint = (key: ControlKey) => reasons.value[key] ?? undefined;

function requestStop() {
  if (blocked("stop")) return;
  if (props.activeTasks > 0) {
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
      :disabled="blocked('start')"
      :title="hint('start')"
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
      :disabled="blocked('pause')"
      :title="hint('pause')"
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
      :disabled="blocked('stop')"
      :title="hint('stop')"
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
