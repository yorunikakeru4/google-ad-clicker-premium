<script setup lang="ts">
const props = withDefaults(
  defineProps<{
    modelValue: boolean;
    title: string;
    text?: string;
    confirmLabel?: string;
    cancelLabel?: string;
    destructive?: boolean;
  }>(),
  {
    text: undefined,
    confirmLabel: "Подтвердить",
    cancelLabel: "Отмена",
    destructive: false,
  },
);

const emit = defineEmits<{
  "update:modelValue": [value: boolean];
  confirm: [];
  cancel: [];
}>();

function close() {
  emit("update:modelValue", false);
}

function cancel() {
  emit("cancel");
  close();
}

function confirm() {
  emit("confirm");
  close();
}

function onKeydown(event: KeyboardEvent) {
  if (event.key !== "Enter") return;
  event.preventDefault();
  if (!props.destructive) confirm();
}
</script>

<template>
  <v-dialog
    :model-value="modelValue"
    max-width="440"
    persistent
    @update:model-value="(value: boolean) => emit('update:modelValue', value)"
    @keydown="onKeydown"
  >
    <v-card class="confirm">
      <v-card-title class="text-subtitle-1 font-weight-bold pt-4 px-4">
        {{ title }}
      </v-card-title>

      <v-card-text v-if="text" class="px-4 pb-2 text-body-2">
        {{ text }}
      </v-card-text>

      <v-card-actions class="px-4 pb-4">
        <v-btn variant="text" @click="cancel">{{ cancelLabel }}</v-btn>
        <v-spacer />
        <v-btn :color="destructive ? 'error' : 'primary'" @click="confirm">
          {{ confirmLabel }}
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
