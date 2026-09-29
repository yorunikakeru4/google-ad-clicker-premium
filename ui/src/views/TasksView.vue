<script setup lang="ts">
import PageLayout from "../components/layout/PageLayout.vue";
import DataTablePage from "../components/data/DataTablePage.vue";
import StatusChip from "../components/status/StatusChip.vue";
import type { DataTableHeader } from "vuetify";

const headers: DataTableHeader[] = [
  { key: "name", title: "Задача", sortable: true },
  { key: "schedule", title: "Расписание", sortable: true },
  { key: "status", title: "Статус", sortable: false },
  { key: "last_run_at", title: "Последний запуск", sortable: true },
  { key: "total_clicks", title: "Клики", sortable: true },
  { key: "actions", title: "", sortable: false, align: "end" },
];
</script>

<template>
  <PageLayout title="Tasks" subtitle="Сценарии, расписание и случайные паузы">
    <DataTablePage :headers="headers">
      <template #actions>
        <v-btn variant="outlined" prepend-icon="mdi-play" class="mr-2">
          Запустить
        </v-btn>
        <v-btn color="primary" prepend-icon="mdi-plus">Новая задача</v-btn>
      </template>

      <template #empty>
        <div class="empty-state">
          <v-icon icon="mdi-format-list-checks" size="40px" />
          <div class="empty-state__title">Задач пока нет</div>
          <div>Создайте первую задачу, чтобы запустить сценарий</div>
          <v-btn color="primary" prepend-icon="mdi-plus" class="mt-2">
            Новая задача
          </v-btn>
        </div>
      </template>

      <template #item.status="{ item }">
        <StatusChip :status="item.status" />
      </template>

      <template #item.actions>
        <v-btn
          icon="mdi-dots-vertical"
          variant="text"
          size="small"
          aria-label="Действия со строкой"
        />
      </template>
    </DataTablePage>
  </PageLayout>
</template>
