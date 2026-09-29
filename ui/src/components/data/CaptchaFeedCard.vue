<script setup lang="ts">
// Карточка ленты CAPTCHA на Dashboard (план §5, фаза 8): последние события
// из captcha_events — время, воркер, страница, исход решения, длительность
// и кнопка «Скриншот».
//
// Компонент только рисует: события, ошибки и открытие скриншота приходят
// из useCaptchaFeed, здесь нет ни опроса, ни состояния. Страница усечена
// для колонки, полный адрес — в title; без сохранённого пути кнопка
// скриншота disabled — вызывать opener нечего.

import { formatElapsed, formatEventTime, pageUrlLabel } from "../../lib/captcha";
import type { CaptchaEvent } from "../../lib/captcha";

defineProps<{
  events: CaptchaEvent[];
  /** Первая загрузка: индикатор вместо «событий ещё не было». */
  loading?: boolean;
  error?: string | null;
  screenshotError?: string | null;
}>();

const emit = defineEmits<{
  "open-screenshot": [path: string | null];
}>();
</script>

<template>
  <v-card data-test="captcha-feed">
    <v-card-title class="text-subtitle-1 font-weight-medium pb-0">
      CAPTCHA
    </v-card-title>
    <v-card-subtitle class="pb-1">
      последние события из captcha_events · порядок от новых к старым
    </v-card-subtitle>

    <v-card-text class="pt-2">
      <v-alert
        v-if="error"
        type="error"
        variant="tonal"
        density="comfortable"
        class="mb-3"
        data-test="captcha-feed-error"
      >
        {{ error }}
      </v-alert>

      <v-alert
        v-if="screenshotError"
        type="warning"
        variant="tonal"
        density="comfortable"
        class="mb-3"
        data-test="captcha-shot-error"
      >
        {{ screenshotError }}
      </v-alert>

      <v-progress-linear
        v-if="loading && events.length === 0"
        indeterminate
        class="mb-3"
        data-test="captcha-feed-loading"
      />

      <v-table
        v-else-if="events.length > 0"
        density="compact"
        data-test="captcha-events"
      >
        <thead>
          <tr>
            <th>время</th>
            <th>воркер</th>
            <th>страница</th>
            <th>решение</th>
            <th class="text-right">длительность</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="event in events"
            :key="event.id"
            :data-test="`captcha-event-${event.id}`"
          >
            <td :data-test="`captcha-time-${event.id}`">
              {{ formatEventTime(event.ts) }}
            </td>
            <td :data-test="`captcha-browser-${event.id}`">
              {{ event.browser_id ?? "—" }}
            </td>
            <td>
              <span
                class="captcha-url text-truncate"
                :title="event.page_url ?? ''"
                :data-test="`captcha-page-${event.id}`"
              >
                {{ pageUrlLabel(event.page_url) }}
              </span>
            </td>
            <td :data-test="`captcha-status-${event.id}`">
              <v-chip
                size="small"
                variant="tonal"
                :color="event.solved ? 'success' : 'warning'"
              >
                {{ event.solved ? "решена" : "не решена" }}
              </v-chip>
            </td>
            <td
              class="text-right"
              :data-test="`captcha-elapsed-${event.id}`"
            >
              {{ formatElapsed(event.elapsed_ms) }}
            </td>
            <td class="text-right">
              <v-btn
                size="small"
                variant="text"
                density="compact"
                :disabled="!event.screenshot_path"
                :title="
                  event.screenshot_path
                    ? 'Открыть скриншот'
                    : 'Скриншот для этого события не сохранён'
                "
                :data-test="`captcha-shot-${event.id}`"
                @click="emit('open-screenshot', event.screenshot_path)"
              >
                Скриншот
              </v-btn>
            </td>
          </tr>
        </tbody>
      </v-table>

      <div v-else class="text-body-2 text-muted" data-test="captcha-feed-empty">
        Событий CAPTCHA ещё не было
      </div>
    </v-card-text>
  </v-card>
</template>

<style scoped>
/* Длинная ссылка не растягивает колонку: полный адрес — в title. */
.captcha-url {
  display: inline-block;
  max-width: 260px;
  vertical-align: bottom;
}
</style>
