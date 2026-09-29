<script setup lang="ts">
// Экран Settings (план §5, фаза 2): перенос формы config.json из gui.py —
// секции Paths/Webdriver/Behavior, типы, диапазоны и подсказки из схемы.
//
// Чтение и запись — GET/POST /control/config через useSettings:
//   * загрузка при монтировании (повторное открытие — из кэша);
//   * dirty считается против загруженного снапшота, в POST уходит патч
//     только изменённых полей;
//   * 400 invalid_config раскладывается по полям: подпись демона рядом с
//     конкретным полем, сводка над формой — для того, что полю отнести
//     нельзя; неудачное сохранение введённые значения не откатывает;
//   * маска ******** нетронута — в патч не попадает, секрет не затирается.

import { onMounted } from "vue";
import PageLayout from "../components/layout/PageLayout.vue";
import SettingField from "../components/forms/SettingField.vue";
import { settingsSections } from "../constants/settingsSchema";
import { useSettings } from "../composables/useSettings";
import { fieldPath } from "../lib/settings";

const settings = useSettings();

onMounted(() => void settings.load());

function errorOf(section: string, key: string): string | null {
  return settings.fieldErrors.value[fieldPath(section, key)] ?? null;
}

function isDirty(section: string, key: string): boolean {
  return settings.dirtyKeys.value.includes(fieldPath(section, key));
}
</script>

<template>
  <PageLayout
    title="Settings"
    subtitle="Все параметры config.json: типы, значения и подсказки"
  >
    <template #toolbar-left>
      <v-chip
        v-if="settings.dirty.value"
        size="small"
        variant="tonal"
        color="warning"
        data-test="settings-dirty"
      >
        Изменено: {{ settings.dirtyKeys.value.length }}
      </v-chip>
    </template>

    <template #toolbar-right>
      <v-btn
        variant="outlined"
        class="mr-2"
        :disabled="!settings.dirty.value || settings.saving.value"
        data-test="settings-reset"
        @click="settings.reset()"
      >
        Отменить
      </v-btn>
      <v-btn
        color="primary"
        prepend-icon="mdi-content-save"
        :loading="settings.saving.value"
        :disabled="!settings.canSave.value"
        data-test="settings-save"
        @click="void settings.save()"
      >
        Сохранить
      </v-btn>
    </template>

    <v-alert
      v-if="settings.loadError.value"
      type="error"
      variant="tonal"
      class="mb-4"
      closable
      data-test="settings-load-error"
      @click:close="settings.loadError.value = null"
    >
      Не удалось загрузить настройки: {{ settings.loadError.value }}
    </v-alert>

    <v-alert
      v-if="settings.saveError.value"
      type="error"
      variant="tonal"
      class="mb-4"
      closable
      data-test="settings-save-error"
      @click:close="settings.saveError.value = null"
    >
      {{ settings.saveError.value }}
    </v-alert>

    <v-alert
      v-if="settings.success.value"
      type="success"
      variant="tonal"
      class="mb-4"
      closable
      data-test="settings-success"
      @click:close="settings.success.value = null"
    >
      {{ settings.success.value }}
    </v-alert>

    <v-alert
      v-if="settings.loading.value"
      type="info"
      variant="tonal"
      density="comfortable"
      class="mb-4"
      data-test="settings-loading"
    >
      Чтение config.json…
    </v-alert>

    <v-row dense>
      <v-col
        v-for="section in settingsSections"
        :key="section.key"
        cols="12"
        xl="6"
        :data-test="`settings-section-${section.key}`"
      >
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
              :model-value="settings.values.value[fieldPath(section.key, field.key)]"
              :hint="field.hint"
              :unit="field.unit"
              :min="field.min"
              :max="field.max"
              :step="field.step"
              :options="field.options"
              :restart="field.restart"
              :secret="field.secret"
              :error="errorOf(section.key, field.key)"
              :dirty="isDirty(section.key, field.key)"
              @update:model-value="
                settings.setValue(fieldPath(section.key, field.key), $event)
              "
            />
          </v-card-text>
        </v-card>
      </v-col>
    </v-row>
  </PageLayout>
</template>
