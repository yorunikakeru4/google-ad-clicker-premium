<script setup lang="ts">
import { useThemeToggle } from "../composables/useThemeToggle";

const { isDark, toggleTheme } = useThemeToggle();
</script>

<template>
  <v-btn
    :icon="isDark ? 'mdi-weather-sunny' : 'mdi-weather-night'"
    variant="text"
    :aria-label="isDark ? 'Switch to light theme' : 'Switch to dark theme'"
    :title="isDark ? 'Light theme' : 'Dark theme'"
    data-test="theme-toggle"
    @click="toggleTheme"
  />
</template>
