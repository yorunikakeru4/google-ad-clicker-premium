<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useDisplay } from "vuetify";
import DaemonStatusChip from "../components/status/DaemonStatusChip.vue";
import HeartbeatIndicator from "../components/status/HeartbeatIndicator.vue";
import DaemonControls from "../components/layout/DaemonControls.vue";
import ThemeToggle from "../components/layout/ThemeToggle.vue";
import { useDaemonStatus } from "../composables/useDaemonStatus";
import { disabledReason, offlineBannerText, type ControlAction } from "../lib/control";
import type { DaemonState } from "../constants/daemon";
import { NAV_ITEMS } from "../router";

const HEARTBEAT_INTERVAL_SECONDS = 5;
const HEARTBEAT_OFFLINE_FACTOR = 4;

const { state, view, controlError, supervisor, startPolling, stop, send } =
  useDaemonStatus();

onMounted(() => startPolling());
onUnmounted(() => stop());

const { width } = useDisplay();

const narrow = computed(() => width.value <= 700);

const daemonState = computed<DaemonState>(() => {
  if (!view.value.online) return "unknown";
  if (view.value.paused || view.value.state === "paused") return "paused";
  switch (view.value.state) {
    case "running":
      return "running";
    case "stopping":
    case "stopped":
      return "stopped";
    default:
      return "unknown";
  }
});

const heartbeatAge = computed(() => {
  const lastOkAt = state.value.lastOkAt;
  if (lastOkAt === null) return null;
  return Math.max(0, Math.round((Date.now() - lastOkAt) / 1000));
});

const offline = computed(() => {
  const age = heartbeatAge.value;
  if (age !== null && age > HEARTBEAT_INTERVAL_SECONDS * HEARTBEAT_OFFLINE_FACTOR) {
    return true;
  }
  return !view.value.online && state.value.lastOkAt !== null;
});

// Причина в баннере: сначала то, что знает супервизор (почему демон не
// поднят), иначе — текст ошибки самого опроса («неверный токен», «нет
// соединения»). Без этого баннер говорил только «нет связи» и не вёл к
// первопричине.
const offlineText = computed(() =>
  offlineBannerText(supervisor.value?.last_error ?? null, state.value.lastError),
);

const activeTasks = computed(() => view.value.workersAlive);

const controlDisabled = computed(() => ({
  start: disabledReason("start", view.value),
  pause: disabledReason("pause", view.value),
  stop: disabledReason("kill", view.value),
}));

const pending = ref<"start" | "pause" | "stop" | null>(null);

async function run(action: ControlAction, key: "start" | "pause" | "stop") {
  pending.value = key;
  try {
    await send(action);
  } finally {
    pending.value = null;
  }
}

const snackOpen = ref(false);

watch(controlError, (value) => {
  if (value) snackOpen.value = true;
});

function onSnackChange(open: boolean) {
  snackOpen.value = open;
  if (!open) controlError.value = null;
}
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
            :interval-seconds="HEARTBEAT_INTERVAL_SECONDS"
          />
          <v-divider v-if="!narrow" vertical />
          <DaemonControls
            :state="daemonState"
            :disabled="controlDisabled"
            :active-tasks="activeTasks"
            :pending="pending"
            @start="run('start', 'start')"
            @pause="run('pause', 'pause')"
            @stop="run('kill', 'stop')"
          />
          <ThemeToggle />
        </div>
      </template>
    </v-app-bar>

    <v-navigation-drawer permanent :rail="narrow" width="250" color="surface">
      <v-list nav density="comfortable" class="nav pt-4">
        <v-list-item
          v-for="item in NAV_ITEMS"
          :key="item.path"
          :to="item.path"
          :exact="item.path === '/'"
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
        :text="offlineText"
      />
      <router-view />
    </v-main>

    <v-snackbar
      :model-value="snackOpen"
      :timeout="-1"
      color="error"
      location="bottom end"
      data-test="control-error"
      @update:model-value="onSnackChange"
    >
      {{ controlError }}
      <template #actions>
        <v-btn variant="text" @click="snackOpen = false">Закрыть</v-btn>
      </template>
    </v-snackbar>
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
