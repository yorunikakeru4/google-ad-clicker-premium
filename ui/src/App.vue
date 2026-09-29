<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { useRoute } from "vue-router";
import ControlPanel from "./components/ControlPanel.vue";
import HeartbeatChip from "./components/HeartbeatChip.vue";
import ThemeToggle from "./components/ThemeToggle.vue";
import { useDaemonStatus } from "./composables/useDaemonStatus";
import { NAV_ITEMS } from "./router";

const drawer = ref(true);
const route = useRoute();
const status = useDaemonStatus();

// Опрос живёт, пока живо приложение: секундный интервал без дублей
// (guard в composable), оффлайн — явный off-стан, без спама в консоль.
onMounted(() => status.startPolling());
onUnmounted(() => status.stop());
</script>

<template>
  <v-app>
    <v-navigation-drawer v-model="drawer" data-test="nav-drawer">
      <v-list nav density="comfortable">
        <v-list-item
          v-for="item in NAV_ITEMS"
          :key="item.path"
          :to="item.path"
          :prepend-icon="item.icon"
          :title="item.title"
          :active="route.path === item.path"
          rounded="lg"
        />
      </v-list>
    </v-navigation-drawer>

    <v-app-bar color="surface" density="comfortable" flat>
      <template #prepend>
        <v-app-bar-nav-icon
          aria-label="Меню"
          data-test="drawer-toggle"
          @click="drawer = !drawer"
        />
      </template>

      <v-app-bar-title class="font-weight-bold">
        Ad Clicker Premium
      </v-app-bar-title>

      <template #append>
        <HeartbeatChip class="mr-2" />
        <ThemeToggle />
      </template>
    </v-app-bar>

    <v-main>
      <v-container fluid class="pa-4">
        <ControlPanel class="mb-4" />
        <router-view />
      </v-container>
    </v-main>
  </v-app>
</template>
