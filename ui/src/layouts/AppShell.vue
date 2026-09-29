<script setup lang="ts">
import { computed } from "vue";
import { useDisplay } from "vuetify";
import DaemonStatusChip from "../components/status/DaemonStatusChip.vue";
import HeartbeatIndicator from "../components/status/HeartbeatIndicator.vue";
import DaemonControls from "../components/layout/DaemonControls.vue";
import ThemeToggle from "../components/layout/ThemeToggle.vue";
import type { DaemonState } from "../constants/daemon";

const props = withDefaults(
  defineProps<{
    daemonState?: DaemonState;
    heartbeatAge?: number | null;
    heartbeatInterval?: number;
    activeTasks?: number;
    pending?: "start" | "pause" | "stop" | null;
  }>(),
  {
    daemonState: "unknown",
    heartbeatAge: null,
    heartbeatInterval: 5,
    activeTasks: 0,
    pending: null,
  },
);

const { width } = useDisplay();

const narrow = computed(() => width.value <= 700);

const offline = computed(
  () => props.heartbeatAge !== null && props.heartbeatAge > props.heartbeatInterval * 4,
);

const navItems = [
  { to: "/", title: "Dashboard", icon: "mdi-view-dashboard" },
  { to: "/profiles", title: "Profiles", icon: "mdi-account" },
  { to: "/proxies", title: "Proxies", icon: "mdi-earth" },
  { to: "/tasks", title: "Tasks", icon: "mdi-format-list-checks" },
  { to: "/logs", title: "Logs", icon: "mdi-format-list-bulleted" },
  { to: "/settings", title: "Settings", icon: "mdi-cog" },
  { to: "/diagnostics", title: "Diagnostics", icon: "mdi-pulse" },
];
</script>

<template>
  <v-app>
    <v-app-bar color="surface" flat height="68">
      <template #title>
        <span class="brand">Ad Clicker Premium</span>
      </template>

      <template #append>
        <div class="app-bar__actions d-flex align-center ga-3 pa-4">
          <DaemonStatusChip :state="daemonState" />
          <HeartbeatIndicator
            v-if="!narrow"
            :age-seconds="heartbeatAge"
            :interval-seconds="heartbeatInterval"
          />
          <v-divider v-if="!narrow" vertical />
          <DaemonControls
            :state="daemonState"
            :active-tasks="activeTasks"
            :pending="pending"
          />
          <ThemeToggle />
        </div>
      </template>
    </v-app-bar>

    <v-navigation-drawer permanent :rail="narrow" width="250" color="surface">
      <v-list nav density="comfortable" class="nav pt-4">
        <v-list-item
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          :exact="item.to === '/'"
          :prepend-icon="item.icon"
          :title="item.title"
          rounded="sm"
          class="nav__item mx-2"
        />
      </v-list>
    </v-navigation-drawer>

    <v-main>
      <v-alert
        v-if="offline"
        type="warning"
        variant="tonal"
        density="comfortable"
        class="ma-4 mb-0"
        text="Нет связи с демоном. Данные на экранах могут быть устаревшими."
      />
      <router-view />
    </v-main>
  </v-app>
</template>

<style scoped>
.brand {
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: rgb(var(--v-theme-primary));
}

.app-bar__actions {
  flex-wrap: nowrap;
}

.nav :deep(.v-list-item--active) {
  background-color: rgb(var(--v-theme-surface-variant));
  box-shadow: inset 3px 0 0 rgb(var(--v-theme-primary));
  color: rgb(var(--v-theme-on-surface));
}

.nav :deep(.v-list-item--active > .v-list-item__overlay) {
  opacity: 0;
}
</style>
