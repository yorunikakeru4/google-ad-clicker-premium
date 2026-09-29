<script setup lang="ts">
import { computed, reactive } from "vue";
import PageLayout from "../components/layout/PageLayout.vue";
import SettingField from "../components/forms/SettingField.vue";
import { settingsSections } from "../constants/settingsSchema";

type SettingValue = string | number | boolean;

function fieldPath(section: string, key: string) {
  return `${section}.${key}`;
}

const initialValues: Record<string, SettingValue> = {};
for (const section of settingsSections) {
  for (const field of section.fields) {
    initialValues[fieldPath(section.key, field.key)] = field.default;
  }
}

const values = reactive<Record<string, SettingValue>>({ ...initialValues });

const dirtyKeys = computed(() =>
  Object.keys(initialValues).filter((key) => values[key] !== initialValues[key]),
);

const dirty = computed(() => dirtyKeys.value.length > 0);

function isDirty(section: string, key: string) {
  return dirtyKeys.value.includes(fieldPath(section, key));
}
</script>

<template>
  <PageLayout title="Settings" subtitle="Все параметры config.json: типы, значения и подсказки">
    <template #toolbar-right>
      <v-btn variant="outlined" class="mr-2" :disabled="!dirty">Отменить</v-btn>
      <v-btn color="primary" prepend-icon="mdi-content-save" :disabled="!dirty">
        Сохранить
      </v-btn>
    </template>

    <v-alert type="info" variant="tonal" density="comfortable" class="mb-4">
      Форма перенесена из gui.py. Чтение и запись config.json подключаются при
      запуске демона.
    </v-alert>

    <v-row dense>
      <v-col v-for="section in settingsSections" :key="section.key" cols="12" xl="6">
        <v-card class="h-100">
          <v-card-title class="text-subtitle-1 font-weight-bold">
            {{ section.title }}
            <span class="text-muted text-body-2"> — {{ section.key }}.*</span>
          </v-card-title>

          <v-card-text class="pt-0">
            <SettingField
              v-for="field in section.fields"
              :key="field.key"
              :name="field.key"
              :type="field.type"
              :model-value="values[fieldPath(section.key, field.key)]"
              :hint="field.hint"
              :unit="field.unit"
              :min="field.min"
              :max="field.max"
              :step="field.step"
              :options="field.options"
              :restart="field.restart"
              :dirty="isDirty(section.key, field.key)"
              @update:model-value="(value) => (values[fieldPath(section.key, field.key)] = value)"
            />
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>
  </PageLayout>
</template>
