<script setup lang="ts">
import type { RouteLocationRaw } from "vue-router";
import PageLayout from "../components/layout/PageLayout.vue";
import ControlPanel from "../components/ControlPanel.vue";
import MetricCard from "../components/data/MetricCard.vue";

const metrics: { label: string; value: string; to?: RouteLocationRaw }[] = [
  { label: "Успешные сценарии", value: "—", to: { name: "tasks" } },
  { label: "Неуспешные сценарии", value: "—", to: { name: "tasks" } },
  { label: "Ошибки", value: "—", to: { name: "logs", query: { level: "ERROR" } } },
  { label: "Время работы", value: "—" },
  { label: "Запросы/час", value: "—" },
  { label: "Доля CAPTCHA", value: "—" },
];
</script>

<template>
  <PageLayout title="Dashboard" subtitle="Статус демона, статистика сценариев и запросы/час">
    <v-row dense>
      <v-col cols="12" lg="8">
        <ControlPanel class="h-100" />
      </v-col>

      <v-col cols="12" lg="4">
        <v-card class="h-100">
          <v-card-title class="text-subtitle-2 font-weight-bold pb-0">
            Цели
          </v-card-title>
          <v-card-text class="pt-2">
            <div class="d-flex align-center justify-space-between py-1">
              <span class="text-body-2">Запросы/час, не менее 50</span>
              <span class="text-body-2 text-muted">—</span>
            </div>
            <div class="d-flex align-center justify-space-between py-1">
              <span class="text-body-2">Доля CAPTCHA, менее 5%</span>
              <span class="text-body-2 text-muted">—</span>
            </div>
            <div class="d-flex align-center justify-space-between py-1">
              <span class="text-body-2">Uptime, не менее 99%</span>
              <span class="text-body-2 text-muted">—</span>
            </div>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>

    <v-row dense class="mt-1">
      <v-col v-for="metric in metrics" :key="metric.label" cols="12" sm="6" lg="4">
        <MetricCard :label="metric.label" :value="metric.value" :to="metric.to" />
      </v-col>
    </v-row>

    <v-row dense class="mt-1">
      <v-col cols="12" lg="7">
        <v-card class="h-100">
          <v-card-title class="text-subtitle-2 font-weight-bold pb-0">
            Клики/час
          </v-card-title>
          <v-card-text>
            <div class="empty-state">
              <v-icon icon="mdi-chart-line" size="40px" />
              <div class="empty-state__title">Нет данных</div>
              <div>График появится после первых сценариев</div>
            </div>
          </v-card-text>
        </v-card>
      </v-col>

      <v-col cols="12" lg="5">
        <v-card class="h-100">
          <v-card-title class="text-subtitle-2 font-weight-bold pb-0">
            CAPTCHA по времени
          </v-card-title>
          <v-card-text>
            <div class="empty-state">
              <v-icon icon="mdi-chart-bar" size="40px" />
              <div class="empty-state__title">Нет данных</div>
              <div>График появится после первых сценариев</div>
            </div>
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>
  </PageLayout>
</template>
