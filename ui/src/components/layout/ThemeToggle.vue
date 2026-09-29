<script setup lang="ts">
import { useThemeToggle } from "../../composables/useThemeToggle";

const { isDark, toggleTheme } = useThemeToggle();
</script>

<template>
  <v-btn
    :icon="isDark ? 'mdi-weather-sunny' : 'mdi-weather-night'"
    variant="text"
    :aria-label="isDark ? 'Включить светлую тему' : 'Включить тёмную тему'"
    :title="isDark ? 'Светлая тема' : 'Тёмная тема'"
    data-test="theme-toggle"
    @click="toggleTheme"
  />
</template>
