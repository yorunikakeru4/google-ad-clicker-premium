<script setup lang="ts">
// Всплывающее уведомление о новом событии CAPTCHA (план §5, фаза 8):
// воркер и исход решения, закрытие вручную.
//
// Всплывашка — v-alert, а не v-snackbar: оверлейные компоненты Vuetify
// (snackbar/dialog/menu) не рендерят содержимое в SSR, а смоук-тесты
// экранов идут через vue/server-renderer в node — уведомление обязано
// проверяться, а не существовать только в браузере. Пока уведомления нет,
// разметки нет вовсе. Очередь и дедупликация — забота useCaptchaFeed,
// компонент только показывает голову очереди.

import { computed } from "vue";
import { captchaNoticeText, type CaptchaNotice } from "../../lib/captcha";

const props = defineProps<{
  /** Голова очереди уведомлений; null — показывать нечего. */
  notice: CaptchaNotice | null;
}>();

const emit = defineEmits<{
  dismiss: [];
}>();

const text = computed(() =>
  props.notice === null ? "" : captchaNoticeText(props.notice),
);
</script>

<template>
  <v-alert
    v-if="props.notice !== null"
    type="warning"
    variant="tonal"
    density="comfortable"
    class="mb-4"
    data-test="captcha-notice"
  >
    <div class="d-flex align-center justify-space-between flex-wrap ga-3">
      <span data-test="captcha-notice-text">{{ text }}</span>
      <v-btn
        size="small"
        variant="text"
        density="compact"
        data-test="captcha-notice-close"
        @click="emit('dismiss')"
      >
        Закрыть
      </v-btn>
    </div>
  </v-alert>
</template>
