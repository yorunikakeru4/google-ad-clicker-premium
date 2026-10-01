<script setup lang="ts">
// Единое состояние «БД недоступна» для экранов Dashboard и Logs:
// информационное, когда файла базы ещё нет (демон не поднимался),
// ошибочное — когда база есть, но открыть её не удалось. Ретрай ручной:
// автоматический повторил бы ту же ошибку раз в секунду.
import { computed } from "vue";
import { useDb } from "../composables/useDb";

const db = useDb();

const alertType = computed(() => (db.missing.value ? "info" : "error"));
const retrying = computed(() => db.phase.value === "opening");
</script>

<template>
  <v-alert
    v-if="db.phase.value === 'failed'"
    :type="alertType"
    variant="tonal"
    data-test="db-unavailable"
  >
    {{ db.error }}
    <template #append>
      <v-btn
        size="small"
        variant="text"
        :loading="retrying"
        data-test="db-retry"
        @click="db.retry()"
      >
        Повторить
      </v-btn>
    </template>
  </v-alert>

  <v-alert
    v-else-if="db.phase.value === 'opening'"
    type="info"
    variant="tonal"
    data-test="db-opening"
  >
    Открываю базу данных…
  </v-alert>
</template>
