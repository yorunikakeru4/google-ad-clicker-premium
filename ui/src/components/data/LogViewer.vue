<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

interface LogEntry {
  id: string | number;
  ts: string;
  level: string;
  category?: string;
  browserId?: string;
  message: string;
}

const props = withDefaults(
  defineProps<{
    entries?: LogEntry[];
    height?: number;
  }>(),
  {
    entries: () => [],
    height: 420,
  },
);

const root = ref<HTMLElement | null>(null);
const pinned = ref(true);

const showJump = computed(() => !pinned.value && props.entries.length > 0);

function levelTone(level: string): string {
  const normalized = level.toUpperCase();
  if (normalized === "ERROR") return "error";
  if (normalized === "WARN" || normalized === "WARNING") return "warning";
  if (normalized === "DEBUG") return "muted";
  return "success";
}

function scroller(): HTMLElement | null {
  return root.value?.querySelector<HTMLElement>(".v-virtual-scroll") ?? null;
}

function jumpToBottom() {
  const el = scroller();
  if (!el) return;
  el.scrollTop = el.scrollHeight;
  pinned.value = true;
}

function onScroll() {
  const el = scroller();
  if (!el) return;
  const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
  pinned.value = distance < 24;
}

onMounted(() => {
  scroller()?.addEventListener("scroll", onScroll);
});

onBeforeUnmount(() => {
  scroller()?.removeEventListener("scroll", onScroll);
});

watch(
  () => props.entries.length,
  async () => {
    if (!pinned.value) return;
    await nextTick();
    jumpToBottom();
  },
);
</script>

<template>
  <v-card>
    <div ref="root" class="log-viewer">
      <div class="log-viewer__bar d-flex align-center ga-2 px-3 py-2">
        <slot name="toolbar" />
        <v-spacer />
        <v-btn
          v-if="showJump"
          size="small"
          variant="tonal"
          prepend-icon="mdi-arrow-down"
          @click="jumpToBottom"
        >
          К новым
        </v-btn>
      </div>

      <v-virtual-scroll
        v-if="entries.length"
        :items="entries"
        :height="height"
        class="log-viewer__scroll"
      >
        <template #default="{ item }">
          <div class="log-line" :class="`log-line--${levelTone(item.level)}`">
            <span class="log-line__ts">{{ item.ts }}</span>
            <span class="log-line__level">{{ item.level }}</span>
            <span v-if="item.category" class="log-line__meta">{{ item.category }}</span>
            <span v-if="item.browserId" class="log-line__meta">{{ item.browserId }}</span>
            <span class="log-line__message">{{ item.message }}</span>
          </div>
        </template>
      </v-virtual-scroll>

      <div v-else class="empty-state">
        <v-icon icon="mdi-format-list-bulleted" size="40px" />
        <div class="empty-state__title">Нет записей</div>
        <div>Логи появятся здесь после запуска демона</div>
      </div>
    </div>
  </v-card>
</template>

<style scoped>
.log-viewer__scroll {
  font-size: 14px;
}

.log-line {
  display: flex;
  gap: 12px;
  padding: 2px 12px;
  font-size: 14px;
  white-space: nowrap;
}

.log-line__ts {
  flex: 0 0 auto;
  opacity: 0.75;
}

.log-line__level {
  flex: 0 0 52px;
}

.log-line__meta {
  flex: 0 0 auto;
  opacity: 0.75;
}

.log-line__message {
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
}

.log-line--success {
  color: rgb(var(--v-theme-success));
}

.log-line--warning {
  color: rgb(var(--v-theme-warning));
}

.log-line--error {
  color: rgb(var(--v-theme-error));
}

.log-line--muted {
  color: rgb(var(--v-muted));
}
</style>
