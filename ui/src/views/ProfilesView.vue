<script setup lang="ts">
import PageLayout from "../components/layout/PageLayout.vue";
import DataTablePage from "../components/data/DataTablePage.vue";
import StatusChip from "../components/status/StatusChip.vue";
import type { DataTableHeader } from "vuetify";

const headers: DataTableHeader[] = [
  { key: "name", title: "Профиль", sortable: true },
  { key: "key_ref", title: "Ключ", sortable: true },
  { key: "proxy", title: "Прокси", sortable: false },
  { key: "status", title: "Статус", sortable: false },
  { key: "last_used_at", title: "Последнее использование", sortable: true },
  { key: "actions", title: "", sortable: false, align: "end" },
];
</script>

<template>
  <PageLayout title="Profiles" subtitle="Профили, ключи и их состояние">
    <DataTablePage :headers="headers">
      <template #actions>
        <v-btn variant="outlined" prepend-icon="mdi-import" class="mr-2">
          Импорт
        </v-btn>
        <v-btn color="primary" prepend-icon="mdi-plus">Добавить профиль</v-btn>
      </template>

      <template #empty>
        <div class="empty-state">
          <v-icon icon="mdi-account-plus" size="40px" />
          <div class="empty-state__title">Добавьте первый профиль</div>
          <v-btn color="primary" prepend-icon="mdi-plus" class="mt-2">
            Добавить профиль
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
