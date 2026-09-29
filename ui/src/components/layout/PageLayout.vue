<script setup lang="ts">
import { computed, useSlots } from "vue";

defineProps<{
  title: string;
  subtitle?: string;
}>();

const slots = useSlots();
const hasToolbar = computed(() => Boolean(slots["toolbar-left"] || slots["toolbar-right"]));
</script>

<template>
  <div class="page pa-7">
    <header class="page__header mb-4">
      <h1 class="page__title">
        <span class="page__prefix">## </span>{{ title }}
      </h1>
      <p v-if="subtitle" class="page__subtitle text-muted">{{ subtitle }}</p>
    </header>

    <div v-if="hasToolbar" class="page__toolbar d-flex align-center flex-wrap ga-2 mb-4">
      <slot name="toolbar-left" />
      <v-spacer />
      <slot name="toolbar-right" />
    </div>

    <slot />
  </div>
</template>

<style scoped>
.page {
  min-height: 100%;
  box-sizing: border-box;
}

.page__header {
  margin: 0;
}

.page__title {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
  line-height: 1.3;
  color: rgb(var(--v-theme-info));
}

.page__prefix {
  color: rgb(var(--v-muted));
  font-weight: 400;
}

.page__subtitle {
  margin: 4px 0 0;
  font-size: 14px;
}
</style>
