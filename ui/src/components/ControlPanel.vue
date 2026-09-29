<script setup lang="ts">
// Панель управления демоном: кнопки семантики §2, подтверждение Kill,
// ошибка последней команды и таблица воркеров из /state.
import { computed, ref } from "vue";
import { useDaemonStatus } from "../composables/useDaemonStatus";
import {
  disabledReason,
  type ControlAction,
} from "../lib/control";
import {
  formatLastError,
  formatPid,
  formatUptime,
} from "../lib/format";

const { state, view, controlError, send, busy } = useDaemonStatus();

interface ButtonSpec {
  action: ControlAction;
  label: string;
  icon: string;
  color?: string;
}

const buttons: ButtonSpec[] = [
  { action: "start", label: "Start", icon: "mdi-play" },
  { action: "pause", label: "Pause", icon: "mdi-pause" },
  { action: "resume", label: "Resume", icon: "mdi-play-pause" },
  { action: "restart", label: "Restart", icon: "mdi-restart" },
  { action: "kill", label: "Kill", icon: "mdi-stop", color: "error" },
];

const killDialog = ref(false);

function reason(action: ControlAction): string | null {
  return disabledReason(action, view.value);
}

async function onButton(action: ControlAction): Promise<void> {
  // Kill не отправляется напрямую: только через подтверждение.
  if (action === "kill") {
    killDialog.value = true;
    return;
  }
  await send(action);
}

async function confirmKill(): Promise<void> {
  killDialog.value = false;
  await send("kill");
}

const workers = computed(() => state.value.snapshot?.workers ?? []);

const offline = computed(() => state.value.phase === "offline");

const emptyText = computed(() => {
  if (offline.value) return "Демон недоступен — данные о воркерах устарели";
  if (state.value.phase === "online") return "Воркеров нет";
  return "Ожидание первого ответа демона…";
});

// Аптайм считается от started_at до снимка /state (updated_at), а не до
// локальных часов: у демона и UI могут расходиться часы.
const nowSeconds = computed(
  () => state.value.snapshot?.updated_at ?? Date.now() / 1000,
);

const STATE_LABELS: Record<string, string> = {
  running: "работает",
  paused: "на паузе",
  stopping: "останавливается",
  stopped: "остановлен",
};

const stateLabel = computed(() => {
  const runState = view.value.state;
  if (!view.value.online) return "демон недоступен";
  if (runState == null) return "неизвестно";
  return STATE_LABELS[runState] ?? runState;
});

const stateColor = computed(() => {
  if (!view.value.online) return "error";
  switch (view.value.state) {
    case "running":
      return "success";
    case "paused":
      return "warning";
    case "stopping":
      return "info";
    default:
      return undefined;
  }
});

const STATUS_COLORS: Record<string, string | undefined> = {
  running: "success",
  starting: "info",
  backoff: "warning",
  stopped: undefined,
  circuit_open: "error",
};
</script>

<template>
  <v-card data-test="control-panel">
    <v-card-text class="d-flex align-center flex-wrap ga-2 py-3">
      <v-chip :color="stateColor" variant="tonal" size="small" data-test="run-state">
        {{ stateLabel }}
      </v-chip>

      <v-spacer />

      <v-btn
        v-for="button in buttons"
        :key="button.action"
        size="small"
        :color="button.color"
        :prepend-icon="button.icon"
        :disabled="reason(button.action) !== null"
        :title="reason(button.action) ?? button.label"
        :data-test="`control-${button.action}`"
        @click="onButton(button.action)"
      >
        {{ button.label }}
      </v-btn>
    </v-card-text>

    <v-alert
      v-if="controlError"
      class="mx-4 mb-2"
      type="error"
      variant="tonal"
      closable
      data-test="control-error"
      @click:close="controlError = null"
    >
      {{ controlError }}
    </v-alert>

    <v-table density="compact" data-test="workers-table">
      <thead>
        <tr>
          <th>browser_id</th>
          <th>статус</th>
          <th class="text-right">PID</th>
          <th>uptime</th>
          <th class="text-right">рестарты</th>
          <th>последняя ошибка</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="worker in workers" :key="worker.browser_id">
          <td>{{ worker.browser_id }}</td>
          <td>
            <v-chip
              size="x-small"
              variant="tonal"
              :color="STATUS_COLORS[worker.status]"
            >
              {{ worker.status }}
            </v-chip>
          </td>
          <td class="text-right">{{ formatPid(worker.pid) }}</td>
          <td>{{ formatUptime(worker.started_at, nowSeconds) }}</td>
          <td class="text-right">{{ worker.restart_count }}</td>
          <td class="text-medium-emphasis">
            {{ formatLastError(worker.last_error) }}
          </td>
        </tr>
        <tr v-if="workers.length === 0">
          <td colspan="6" class="text-medium-emphasis" data-test="workers-empty">
            {{ emptyText }}
          </td>
        </tr>
      </tbody>
    </v-table>

    <v-dialog v-model="killDialog" max-width="460" data-test="kill-dialog">
      <v-card>
        <v-card-title class="text-h6">Остановить все воркеры?</v-card-title>
        <v-card-text>
          Воркеры получат SIGTERM, а через 10 секунд — SIGKILL. Текущие
          сценарии будут прерваны, профили браузеров очищены. Действие
          необратимо.
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" data-test="kill-cancel" @click="killDialog = false">
            Отмена
          </v-btn>
          <v-btn
            color="error"
            variant="flat"
            :loading="busy"
            data-test="kill-confirm"
            @click="confirmKill"
          >
            Остановить
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </v-card>
</template>
