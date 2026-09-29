<script setup lang="ts">
import PageLayout from "../components/layout/PageLayout.vue";
import FilterBar from "../components/data/FilterBar.vue";
import LogViewer from "../components/data/LogViewer.vue";

const periodItems = [
  { title: "Последний час", value: "1h" },
  { title: "Сегодня", value: "today" },
  { title: "7 дней", value: "7d" },
  { title: "30 дней", value: "30d" },
];
</script>

<template>
  <PageLayout title="Logs" subtitle="Структурированные записи: уровень, категория, поток и время">
    <template #toolbar-left>
      <FilterBar :period-items="periodItems" />
    </template>

    <LogViewer />
  </PageLayout>
</template>
