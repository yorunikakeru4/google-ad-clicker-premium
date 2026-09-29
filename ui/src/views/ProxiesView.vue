<script setup lang="ts">
import PageLayout from "../components/layout/PageLayout.vue";
import DataTablePage from "../components/data/DataTablePage.vue";
import StatusChip from "../components/status/StatusChip.vue";
import type { DataTableHeader } from "vuetify";

const headers: DataTableHeader[] = [
  { key: "label", title: "Метка", sortable: true },
  { key: "address", title: "Адрес", sortable: true },
  { key: "country", title: "Страна", sortable: true },
  { key: "latency_ms", title: "Задержка", sortable: true },
  { key: "status", title: "Статус", sortable: false },
  { key: "usage_count", title: "Использований", sortable: true },
  { key: "assigned_to", title: "Поток", sortable: false },
  { key: "actions", title: "", sortable: false, align: "end" },
];
</script>

<template>
  <PageLayout title="Proxies" subtitle="Список прокси, проверка доступности и назначение">
    <DataTablePage :headers="headers">
      <template #actions>
        <v-btn variant="outlined" prepend-icon="mdi-import" class="mr-2">
          Импорт
        </v-btn>
        <v-btn variant="outlined" prepend-icon="mdi-check" class="mr-2">
          Проверить все
        </v-btn>
        <v-btn color="primary" prepend-icon="mdi-plus">Добавить прокси</v-btn>
      </template>

      <template #empty>
        <div class="empty-state">
          <v-icon icon="mdi-earth" size="40px" />
          <div class="empty-state__title">Добавьте первый прокси</div>
          <div>Импортируйте список из proxies.txt или добавьте вручную</div>
          <v-btn color="primary" prepend-icon="mdi-plus" class="mt-2">
            Добавить прокси
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
