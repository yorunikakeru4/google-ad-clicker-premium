<script setup lang="ts">
import { computed } from "vue";
import { statusMeta, type StatusKind } from "../../constants/statusMap";

const props = defineProps<{
  status: StatusKind;
  label?: string;
}>();

const meta = computed(() => statusMeta(props.status));
const text = computed(() => props.label ?? meta.value.label);
</script>

<template>
  <v-chip :color="meta.color" :prepend-icon="meta.icon" class="status-chip">
    {{ text }}
  </v-chip>
</template>

<style scoped>
.status-chip {
  text-transform: none;
}
</style>
