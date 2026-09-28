<script setup lang="ts">
import ThemeToggle from "./components/ThemeToggle.vue";
</script>

<template>
  <v-app>
    <v-app-bar color="surface" density="comfortable" flat>
      <template #prepend>
        <v-app-bar-title class="font-weight-bold">
          Ad Clicker Premium
        </v-app-bar-title>
      </template>

      <template #append>
        <ThemeToggle />
      </template>
    </v-app-bar>

    <v-main>
      <router-view />
    </v-main>
  </v-app>
</template>
