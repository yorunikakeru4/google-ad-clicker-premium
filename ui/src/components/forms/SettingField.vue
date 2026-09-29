<script setup lang="ts">
import { computed } from "vue";
import type { SettingOption, SettingType } from "../../constants/settingsSchema";

const props = withDefaults(
  defineProps<{
    name: string;
    type: SettingType;
    modelValue?: string | number | boolean;
    hint?: string;
    unit?: string;
    min?: number;
    max?: number;
    step?: number;
    options?: SettingOption[];
    error?: string | null;
    dirty?: boolean;
    restart?: boolean;
    secret?: boolean;
  }>(),
  {
    modelValue: "",
    hint: undefined,
    unit: undefined,
    min: undefined,
    max: undefined,
    step: undefined,
    options: () => [],
    error: null,
    dirty: false,
    restart: false,
    secret: false,
  },
);

const emit = defineEmits<{
  "update:modelValue": [value: string | number | boolean];
}>();

const badge = computed(() => (props.unit ? `${props.type} · ${props.unit}` : props.type));

const switchValue = computed(() => Boolean(props.modelValue));

const selectItems = computed(() =>
  props.options.map((option) => ({ title: option.title, value: option.value })),
);

function onSwitch(value: boolean | null) {
  emit("update:modelValue", Boolean(value));
}

function onSelect(value: unknown) {
  emit("update:modelValue", typeof value === "number" ? value : String(value ?? ""));
}

function onText(value: unknown) {
  emit("update:modelValue", typeof value === "string" ? value : String(value ?? ""));
}

function onNumber(value: unknown) {
  const raw = typeof value === "string" ? value.trim() : String(value ?? "");
  if (raw === "") {
    emit("update:modelValue", "");
    return;
  }
  const parsed = Number(raw);
  emit("update:modelValue", Number.isNaN(parsed) ? raw : parsed);
}
</script>

<template>
  <div class="setting py-3" :class="{ 'setting--invalid': Boolean(error) }">
    <div class="setting__row d-flex align-center flex-wrap ga-3">
      <div class="setting__label d-flex align-center flex-wrap ga-2">
        <span class="setting__name">{{ name }}</span>
        <v-chip size="x-small" variant="outlined" class="setting__badge">
          {{ badge }}
        </v-chip>
        <v-icon
          v-if="restart"
          icon="mdi-restart"
          size="14px"
          class="text-muted"
          title="Требует перезапуска демона"
          aria-label="Требует перезапуска демона"
        />
        <v-icon
          v-if="secret"
          icon="mdi-lock"
          size="14px"
          class="text-muted"
          title="Секрет: значение скрыто; чтобы заменить, введите новое"
          aria-label="Секретное значение"
        />
        <span
          v-if="dirty"
          class="setting__dot"
          title="Изменено"
          aria-label="Изменено"
        />
      </div>

      <div class="setting__control">
        <!-- Рендер строго по типу. type="path" уходит в обычное текстовое
             поле: файловый диалог — это отдельный @tauri-apps/plugin-dialog,
             а кнопка Browse без диалога была бы заглушкой. -->
        <v-switch
          v-if="type === 'bool'"
          :model-value="switchValue"
          hide-details
          density="compact"
          color="primary"
          @update:model-value="onSwitch"
        />

        <v-select
          v-else-if="type === 'enum'"
          :model-value="modelValue"
          :items="selectItems"
          hide-details
          density="compact"
          @update:model-value="onSelect"
        />

        <v-text-field
          v-else-if="type === 'int' || type === 'float'"
          :model-value="modelValue"
          type="number"
          :min="min"
          :max="max"
          :step="step ?? (type === 'int' ? 1 : undefined)"
          :suffix="unit"
          hide-details
          density="compact"
          @update:model-value="onNumber"
        />

        <!-- type="path": строка, как и любая другая — файловый диалог даст
             подключаемый @tauri-apps/plugin-dialog, кнопка без диалога была
             бы заглушкой. -->
        <v-text-field
          v-else
          :model-value="modelValue"
          hide-details
          density="compact"
          @update:model-value="onText"
        />
      </div>
    </div>

    <p v-if="hint" class="setting__hint text-muted mb-0">{{ hint }}</p>
    <p v-if="error" class="setting__error mb-0" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.setting {
  border-bottom: 1px solid rgb(var(--v-border-color), var(--v-border-opacity));
}

.setting:last-child {
  border-bottom: none;
}

.setting__label {
  flex: 0 0 300px;
  min-width: 220px;
}

.setting__name {
  font-weight: 600;
  word-break: break-all;
}

.setting__badge {
  text-transform: none;
  letter-spacing: 0;
}

.setting__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: rgb(var(--v-theme-primary));
  flex: 0 0 auto;
}

.setting__control {
  flex: 1 1 220px;
  max-width: 320px;
}

.setting__hint,
.setting__error {
  margin-top: 4px;
  font-size: 13px;
  line-height: 1.4;
}

.setting__error {
  color: rgb(var(--v-theme-error));
}
</style>
