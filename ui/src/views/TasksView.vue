<script setup lang="ts">
// Экран Tasks (план §5, фаза 2): расписание запуска, источник запросов,
// пауза/продолжить и справка по диапазонам пауз.
//
// Экран читает и показывает, но не правит: конфиг и расписание меняются в
// Settings (ссылки в карточках), редактором здесь быть нельзя — иначе два
// места правды для одних и тех же полей.
//
// Источники данных:
// - /state + /health — синглтон useDaemonStatus, его же опрос держит AppShell;
//   экран не заводит свой интервал, сюда приходят готовые ref'ы;
// - GET /control/config — useTasks, один запрос на монтирование плюс кнопка
//   «Обновить»;
// - пауза/продолжить — те же send()/disabledReason, что и в панели шапки:
//   правила блокировки живут в lib/control.ts и не дублируются здесь.

import { computed, onMounted } from "vue";
import PageLayout from "../components/layout/PageLayout.vue";
import { useDaemonStatus } from "../composables/useDaemonStatus";
import { useTasks } from "../composables/useTasks";
import { disabledReason } from "../lib/control";
import { describeSchedule, querySource, waitRanges } from "../lib/tasks";

const tasks = useTasks();
const daemon = useDaemonStatus();

onMounted(() => {
  void tasks.load();
});

// --- расписание и источник -------------------------------------------------

const schedule = computed(() => {
  const config = tasks.config.value;
  if (config === null) return null;
  return describeSchedule(config.intervalStart, config.intervalEnd);
});

const source = computed(() =>
  tasks.config.value === null ? null : querySource(tasks.config.value),
);

const waitRows = computed(() =>
  tasks.config.value === null ? [] : waitRanges(tasks.config.value),
);

// --- пауза -----------------------------------------------------------------

// Disabled-правила — не логика экрана, а вызов общей функции: причины
// совпадают с панелью управления в шапке по построению.
const pauseDisabled = computed(() =>
  disabledReason("pause", daemon.view.value),
);
const resumeDisabled = computed(() =>
  disabledReason("resume", daemon.view.value),
);

const paused = computed(() => daemon.view.value.paused);

/** Сводка воркеров из /state: активны N из M. Полная таблица — на Dashboard. */
const workersSummary = computed(() => {
  const snapshot = daemon.state.value.snapshot;
  if (snapshot === null) return null;
  return `${snapshot.alive_count} из ${snapshot.worker_count}`;
});

async function sendPause(): Promise<void> {
  if (pauseDisabled.value !== null) return;
  await daemon.send("pause");
}

async function sendResume(): Promise<void> {
  if (resumeDisabled.value !== null) return;
  await daemon.send("resume");
}
</script>

<template>
  <PageLayout
    title="Tasks"
    subtitle="Расписание запуска, источник запросов и паузы"
  >
    <template #toolbar-right>
      <v-btn
        variant="outlined"
        prepend-icon="mdi-refresh"
        :loading="tasks.loading.value"
        data-test="tasks-refresh"
        @click="void tasks.load()"
      >
        Обновить
      </v-btn>
    </template>

    <v-alert
      v-if="tasks.error.value"
      type="error"
      variant="tonal"
      class="mb-4"
      closable
      data-test="tasks-config-error"
      @click:close="tasks.error.value = null"
    >
      {{ tasks.error.value }}
    </v-alert>

    <v-alert
      v-if="daemon.controlError.value"
      type="error"
      variant="tonal"
      class="mb-4"
      closable
      data-test="tasks-control-error"
      @click:close="daemon.controlError.value = null"
    >
      {{ daemon.controlError.value }}
    </v-alert>

    <v-alert
      v-if="tasks.config.value === null && !tasks.error.value"
      type="info"
      variant="tonal"
      density="comfortable"
      class="mb-4"
      data-test="tasks-config-pending"
    >
      {{ tasks.loading.value ? "Читаю конфиг демона…" : "Конфиг демона ещё не прочитан." }}
    </v-alert>

    <v-row dense>
      <!-- Расписание -->
      <v-col cols="12" md="6">
        <v-card class="h-100" data-test="tasks-schedule">
          <v-card-title class="text-subtitle-1 font-weight-bold">
            Расписание
          </v-card-title>
          <v-card-text class="pt-0">
            <template v-if="schedule">
              <v-chip
                :color="schedule.tone === 'error' ? 'error' : 'primary'"
                variant="tonal"
                data-test="tasks-schedule-label"
              >
                {{ schedule.label }}
              </v-chip>
              <div
                v-if="schedule.tone === 'error'"
                class="text-body-2 text-error mt-2"
                data-test="tasks-schedule-error"
              >
                {{ schedule.detail }}
              </div>
              <div
                v-else
                class="text-body-2 text-muted mt-2"
                data-test="tasks-schedule-detail"
              >
                {{ schedule.detail }}
              </div>
            </template>
            <div v-else class="text-body-2 text-muted">
              Нет данных — конфиг не прочитан.
            </div>

            <v-btn
              size="small"
              variant="text"
              color="primary"
              prepend-icon="mdi-cog"
              class="mt-2"
              :to="{ name: 'settings' }"
              data-test="tasks-schedule-settings"
            >
              Изменить в Настройках
            </v-btn>
          </v-card-text>
        </v-card>
      </v-col>

      <!-- Источник запросов -->
      <v-col cols="12" md="6">
        <v-card class="h-100" data-test="tasks-source">
          <v-card-title class="text-subtitle-1 font-weight-bold">
            Источник запросов
          </v-card-title>
          <v-card-text class="pt-0">
            <template v-if="source">
              <v-chip
                :color="source.kind === 'conflict' ? 'error' : 'primary'"
                variant="tonal"
                data-test="tasks-source-label"
              >
                {{ source.label }}
              </v-chip>
              <div
                class="text-body-2 text-muted mt-2"
                data-test="tasks-source-hint"
              >
                {{ source.hint }}
              </div>
            </template>
            <div v-else class="text-body-2 text-muted">
              Нет данных — конфиг не прочитан.
            </div>

            <v-btn
              size="small"
              variant="text"
              color="primary"
              prepend-icon="mdi-cog"
              class="mt-2"
              :to="{ name: 'settings' }"
              data-test="tasks-source-settings"
            >
              Изменить в Настройках
            </v-btn>
          </v-card-text>
        </v-card>
      </v-col>

      <!-- Пауза -->
      <v-col cols="12" md="6">
        <v-card class="h-100" data-test="tasks-pause">
          <v-card-title class="text-subtitle-1 font-weight-bold">
            Пауза
          </v-card-title>
          <v-card-text class="pt-0">
            <div class="d-flex align-center flex-wrap ga-2 mb-2">
              <v-chip
                :color="paused ? 'warning' : 'success'"
                variant="tonal"
                data-test="tasks-pause-status"
              >
                {{ paused ? "Пауза активна" : "Пауза не установлена" }}
              </v-chip>
              <v-chip variant="tonal" data-test="tasks-workers">
                <template v-if="workersSummary !== null">
                  Активны {{ workersSummary }} воркеров
                </template>
                <template v-else>Воркеры: данных нет</template>
              </v-chip>
            </div>

            <div class="text-body-2 text-muted mb-3">
              {{ paused
                ? "Флаг PAUSE_REQUESTED взведён: воркеры дорабатывают текущий сценарий, новые не стартуют."
                : "Флаг PAUSE_REQUESTED не взведён: воркеры берут сценарии как обычно." }}
            </div>

            <div class="d-flex ga-2">
              <v-btn
                size="small"
                variant="outlined"
                prepend-icon="mdi-pause"
                :disabled="pauseDisabled !== null"
                :title="pauseDisabled ?? undefined"
                :loading="daemon.busy.value && paused === false"
                data-test="tasks-pause-btn"
                @click="void sendPause()"
              >
                Пауза
              </v-btn>
              <v-btn
                size="small"
                variant="outlined"
                prepend-icon="mdi-play"
                :disabled="resumeDisabled !== null"
                :title="resumeDisabled ?? undefined"
                :loading="daemon.busy.value && paused"
                data-test="tasks-resume-btn"
                @click="void sendResume()"
              >
                Продолжить
              </v-btn>
            </div>
          </v-card-text>
        </v-card>
      </v-col>

      <!-- Диапазоны пауз: справка, не редактор -->
      <v-col cols="12" md="6">
        <v-card class="h-100" data-test="tasks-waits">
          <v-card-title class="text-subtitle-1 font-weight-bold">
            Диапазоны пауз
          </v-card-title>
          <v-card-text class="pt-0">
            <div class="text-body-2 text-muted mb-2">
              Справка: случайные паузы в сценарии. Меняется в Настройках.
            </div>

            <template v-if="waitRows.length > 0">
              <v-table density="compact">
                <thead>
                  <tr>
                    <th>Параметр</th>
                    <th>Значение</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="row in waitRows"
                    :key="row.key"
                    :data-test="`tasks-wait-${row.key}`"
                  >
                    <td>{{ row.label }}</td>
                    <td>{{ row.value }}</td>
                  </tr>
                </tbody>
              </v-table>
            </template>
            <div v-else class="text-body-2 text-muted">
              Нет данных — конфиг не прочитан.
            </div>

            <v-btn
              size="small"
              variant="text"
              color="primary"
              prepend-icon="mdi-cog"
              class="mt-2"
              :to="{ name: 'settings' }"
              data-test="tasks-waits-settings"
            >
              Изменить в Настройках
            </v-btn>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>
  </PageLayout>
</template>
