<script setup lang="ts">
// Таблица воркеров из GET /state (план §5, фаза 4): browser_id, статус,
// PID, uptime, restart_count и последняя ошибка.
//
// Опроса нет: AppShell уже опрашивает /state раз в секунду, компонент только
// читает готовые phase/snapshot. Управление демоном остаётся в AppShell —
// здесь нет ни кнопок, ни дублей чипов статуса демона.
//
// Порядок строк фиксирован — как приходит из /state (бэкенд отдаёт
// list_workers c ORDER BY browser_id): пересортировка на каждом тике
// дёргала бы строки местами у пользователя перед глазами.
import { computed } from "vue";
import StatusChip from "./status/StatusChip.vue";
import { formatLastError, formatPid, formatUptime } from "../lib/format";
import type { PollPhase } from "../lib/poll";
import type { StateSnapshot, WorkerRow } from "../lib/types";
import { workerStatusKind } from "../lib/workerStatus";

const props = defineProps<{
  /** Фаза опроса демона: офлайн чистит снимок (см. lib/poll). */
  phase: PollPhase;
  /** Снимок GET /state; null — ответа ещё не было или он провалился. */
  snapshot: StateSnapshot | null;
}>();

const workers = computed(() => props.snapshot?.workers ?? []);

// Аптайм меряется от updated_at снимка, а не от локальных часов: у демона
// и UI могут расходиться часы. Снимка нет — тире, NaN в ячейку не попадает.
const nowSeconds = computed(() => props.snapshot?.updated_at ?? null);

const offline = computed(() => props.phase === "offline");
const waiting = computed(() => !offline.value && props.snapshot === null);

const emptyIcon = computed(() => {
  if (offline.value) return "mdi-cloud-off-outline";
  if (waiting.value) return "mdi-progress-clock";
  return "mdi-account-group-outline";
});

const emptyTitle = computed(() => {
  if (offline.value) return "Демон недоступен";
  if (waiting.value) return "Ожидание первого ответа демона…";
  return "Воркеров нет";
});

const emptyHint = computed(() => {
  if (offline.value) return "Связь восстановится сама — опрос идёт раз в секунду";
  if (waiting.value) return "Первый ответ /state придёт через секунду";
  return "Запустите воркеры кнопкой Start в верхней панели";
});

function uptime(worker: WorkerRow): string {
  if (nowSeconds.value === null) return "—";
  return formatUptime(worker.started_at, nowSeconds.value);
}
</script>

<template>
  <v-card data-test="workers-card">
    <v-card-title class="text-subtitle-1 font-weight-medium pb-0">
      Воркеры
    </v-card-title>
    <v-card-subtitle class="pb-1">
      статусы из /state · опрос раз в секунду
    </v-card-subtitle>

    <v-card-text class="pt-2">
      <v-table v-if="workers.length > 0" density="compact" data-test="workers-table">
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
          <tr
            v-for="worker in workers"
            :key="worker.browser_id"
            :data-test="`worker-row-${worker.browser_id}`"
          >
            <td>{{ worker.browser_id }}</td>
            <td :data-test="`worker-status-${worker.browser_id}`">
              <StatusChip :status="workerStatusKind(worker.status)" :label="worker.status" />
            </td>
            <td class="text-right" :data-test="`worker-pid-${worker.browser_id}`">
              {{ formatPid(worker.pid) }}
            </td>
            <td :data-test="`worker-uptime-${worker.browser_id}`">
              {{ uptime(worker) }}
            </td>
            <td class="text-right" :data-test="`worker-restarts-${worker.browser_id}`">
              {{ worker.restart_count }}
            </td>
            <td
              class="text-medium-emphasis"
              :data-test="`worker-error-${worker.browser_id}`"
            >
              <span class="worker-error text-truncate" :title="worker.last_error ?? ''">
                {{ formatLastError(worker.last_error) }}
              </span>
            </td>
          </tr>
        </tbody>
      </v-table>

      <div v-else class="empty-state" data-test="workers-empty">
        <v-icon :icon="emptyIcon" size="40px" />
        <div class="empty-state__title">{{ emptyTitle }}</div>
        <div>{{ emptyHint }}</div>
      </div>
    </v-card-text>
  </v-card>
</template>

<style scoped>
/* Длинная ошибка не растягивает колонку: полный текст — в title. */
.worker-error {
  display: inline-block;
  max-width: 320px;
  vertical-align: bottom;
}
</style>
