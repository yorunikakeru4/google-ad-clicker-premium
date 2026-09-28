<script setup lang="ts">
import { ref } from "vue";
import { invoke } from "@tauri-apps/api/core";
import { useThemeToggle } from "../composables/useThemeToggle";

const { isDark, current } = useThemeToggle();

const stack = [
  { name: "Vue 3", detail: "reactive UI layer" },
  { name: "TypeScript", detail: "typed build, vue-tsc in CI" },
  { name: "Vuetify 3", detail: "material components + theming" },
  { name: "vue-router", detail: "client-side routing" },
  { name: "Tauri 2", detail: "Rust shell, native IPC" },
];

const bridge = ref("not checked");
const bridgeError = ref("");

async function checkBridge() {
  bridgeError.value = "";
  try {
    bridge.value = await invoke<string>("greet", { name: "Rust" });
  } catch (err) {
    bridgeError.value = String(err);
    bridge.value = "failed";
  }
}
</script>

<template>
  <v-container class="py-8" max-width="880">
    <v-card class="pa-6">
      <v-card-title class="text-h5 mb-1">
        Google Ad Clicker Premium
      </v-card-title>
      <v-card-subtitle>
        Phase 0 scaffold — the foundation is in place, screens come later.
      </v-card-subtitle>
    </v-card>

    <v-card class="mt-4 pa-6">
      <v-card-title class="text-subtitle-1 font-weight-bold mb-3">
        Stack
      </v-card-title>
      <v-list density="compact" bg-color="transparent">
        <v-list-item
          v-for="item in stack"
          :key="item.name"
          :title="item.name"
          :subtitle="item.detail"
        />
      </v-list>
    </v-card>

    <v-alert
      class="mt-4"
      type="info"
      variant="tonal"
      density="comfortable"
      text="Placeholder route. The real screens are not implemented yet."
    />

    <v-card class="mt-4 pa-6">
      <v-card-title class="text-subtitle-1 font-weight-bold mb-1">
        Scaffold self-check
      </v-card-title>
      <v-card-subtitle class="mb-4">
        Confirms router, theming and the Rust bridge are actually wired.
      </v-card-subtitle>

      <v-row dense>
        <v-col cols="12" sm="6">
          <div class="text-caption text-medium-emphasis">Route</div>
          <div class="text-body-2">{{ $route.name }} ({{ $route.path }})</div>
        </v-col>
        <v-col cols="12" sm="6">
          <div class="text-caption text-medium-emphasis">Theme</div>
          <div class="text-body-2">
            {{ current }} — {{ isDark ? "dark" : "light" }}
          </div>
        </v-col>
      </v-row>

      <v-divider class="my-4" />

      <div class="text-caption text-medium-emphasis">
        Rust IPC (command <code>greet</code>)
      </div>
      <div class="d-flex align-center ga-2 mt-1">
        <code class="text-body-2">{{ bridge }}</code>
        <v-spacer />
        <v-btn
          size="small"
          color="primary"
          variant="tonal"
          data-test="bridge-check"
          @click="checkBridge"
        >
          Check bridge
        </v-btn>
      </div>
      <v-alert
        v-if="bridgeError"
        class="mt-3"
        type="error"
        variant="tonal"
        density="compact"
        :text="bridgeError"
      />
    </v-card>
  </v-container>
</template>
