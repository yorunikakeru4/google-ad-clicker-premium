<script setup lang="ts">
import PageLayout from "../components/layout/PageLayout.vue";
import StatusChip from "../components/status/StatusChip.vue";

const sessionFields = [
  "Внешний IP",
  "Внутренний IP",
  "User-Agent",
  "Accept-Language",
  "Timezone",
  "Разрешение экрана",
  "Платформа",
  "Версия браузера",
  "WebGL vendor",
  "WebGL renderer",
];

const headerFields = ["User-Agent", "Accept", "Accept-Language", "Sec-CH-UA", "Sec-CH-UA-Platform"];

const checks = [
  "Язык ↔ страна прокси",
  "Timezone ↔ гео прокси",
  "User-Agent ↔ ОС",
  "Разрешение ↔ viewport",
];
</script>

<template>
  <PageLayout title="Diagnostics" subtitle="Как сайт видит сессию: значения снимаются без изменений">
    <template #toolbar-right>
      <v-btn variant="outlined" prepend-icon="mdi-download" class="mr-2">
        Экспорт
      </v-btn>
      <v-btn color="primary" prepend-icon="mdi-check">Проверить</v-btn>
    </template>

    <v-row dense>
      <v-col cols="12" lg="7">
        <v-card class="h-100">
          <v-card-title class="text-subtitle-1 font-weight-bold">
            Параметры сессии
          </v-card-title>
          <v-card-text>
            <div
              v-for="field in sessionFields"
              :key="field"
              class="param-row d-flex align-center justify-space-between py-1"
            >
              <span class="text-body-2 text-muted">{{ field }}</span>
              <span class="text-body-2">—</span>
            </div>
          </v-card-text>
        </v-card>
      </v-col>

      <v-col cols="12" lg="5">
        <v-card class="h-100">
          <v-card-title class="text-subtitle-1 font-weight-bold">
            Согласованность
          </v-card-title>
          <v-card-text>
            <div
              v-for="check in checks"
              :key="check"
              class="param-row d-flex align-center justify-space-between py-1"
            >
              <span class="text-body-2">{{ check }}</span>
              <StatusChip status="idle" label="нет данных" />
            </div>
          </v-card-text>
        </v-card>
      </v-col>

      <v-col cols="12">
        <v-card>
          <v-card-title class="text-subtitle-1 font-weight-bold">Заголовки</v-card-title>
          <v-card-text>
            <div
              v-for="header in headerFields"
              :key="header"
              class="param-row d-flex align-center justify-space-between py-1"
            >
              <span class="text-body-2 text-muted">{{ header }}</span>
              <span class="text-body-2">—</span>
            </div>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>

    <v-alert type="info" variant="tonal" density="comfortable" class="mt-1">
      Значения выводятся как есть: снятие параметров не меняет поведение браузера.
    </v-alert>
  </PageLayout>
</template>

<style scoped>
.param-row {
  border-bottom: 1px solid rgb(var(--v-border-color), var(--v-border-opacity));
}

.param-row:last-child {
  border-bottom: none;
}
</style>
