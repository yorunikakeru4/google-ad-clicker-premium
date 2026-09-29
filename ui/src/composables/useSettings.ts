// Экран Settings: форма поверх GET/POST /control/config (план §5, фаза 2).
//
// Хранение — «снапшот + значения»: снапшот это то, что демон отдал при
// загрузке, значения — что в форме. Правка сравнивается со снапшотом, и
// в POST уходит патч только из изменившихся полей (lib/settings.buildPatch).
// Отсюда же следствия, на которых стоит форме держаться:
//
//   * неудачное сохранение НЕ откатывает значения: пользователь правит то,
//     что ввёл, а не начинает заново;
//   * 400 invalid_config раскладывается по полям (splitProblems): подпись
//     появляется рядом с конкретным полем, обе стороны перекрёстного
//     правила подсвечиваются сразу, а не относящаяся к полю ошибка
//     (например, «config: ожидается объект») не теряется — она в сводке;
//   * маска ******** нетронута — не равна изменению, в патч не попадает,
//     секрет на сервере остаётся прежним.

import { computed, ref, type ComputedRef, type Ref } from "vue";
import { errorMessage } from "../lib/control";
import {
  SettingsValidationError,
  buildPatch,
  fieldPath,
  flattenConfig,
  normalizeConfig,
  pluralFields,
  settingsApi,
  splitProblems,
  type SettingsApi,
  type SettingsConfig,
  type SettingsValues,
  type SettingValue,
} from "../lib/settings";

export interface SettingsState {
  /** Значения формы: "section.key" -> значение. */
  values: Ref<SettingsValues>;
  /** Первая загрузка конфига. */
  loading: Ref<boolean>;
  saving: Ref<boolean>;
  /** Снапшот получен — форма вообще можно править и сохранять. */
  loaded: Ref<boolean>;
  loadError: Ref<string | null>;
  /** Сводка неудачного сохранения (сеть, структура, неотносимые проблемы). */
  saveError: Ref<string | null>;
  /** Подписи ошибок валидации: "section.key" -> текст демона. */
  fieldErrors: Ref<Record<string, string>>;
  success: Ref<string | null>;
  dirtyKeys: ComputedRef<string[]>;
  dirty: ComputedRef<boolean>;
  canSave: ComputedRef<boolean>;
  load(options?: { force?: boolean }): Promise<void>;
  save(): Promise<boolean>;
  reset(): void;
  setValue(path: string, value: SettingValue): void;
}

function sectionCount(patch: SettingsConfig): number {
  return Object.values(patch).reduce((total, fields) => total + Object.keys(fields).length, 0);
}

export function createSettings(api: SettingsApi = settingsApi): SettingsState {
  const snapshot = ref<SettingsConfig | null>(null);
  // До первого ответа демона форма показывает дефолты схемы: сравнивать пока
  // не с чем, поэтому save заблокирован, а «изменено» не горит.
  const values = ref<SettingsValues>(flattenConfig(normalizeConfig(null)));
  const loading = ref(false);
  const saving = ref(false);
  const loaded = ref(false);
  const loadError = ref<string | null>(null);
  const saveError = ref<string | null>(null);
  const fieldErrors = ref<Record<string, string>>({});
  const success = ref<string | null>(null);

  const patch = computed(() =>
    snapshot.value === null ? {} : buildPatch(snapshot.value, values.value),
  );

  const dirtyKeys = computed(() =>
    Object.entries(patch.value).flatMap(([section, fields]) =>
      Object.keys(fields).map((key) => fieldPath(section, key)),
    ),
  );

  const dirty = computed(() => dirtyKeys.value.length > 0);
  const canSave = computed(() => dirty.value && !saving.value && !loading.value);

  /** Новый эталон формы: снапшот и значения всегда согласованы. */
  function apply(config: SettingsConfig): void {
    const normalized = normalizeConfig(config);
    snapshot.value = normalized;
    values.value = flattenConfig(normalized);
    loaded.value = true;
  }

  async function load(options: { force?: boolean } = {}): Promise<void> {
    // Кэш: повторное открытие экрана не ходит в сеть и не перетирает
    // несохранённые правки. force — явное перечитывание.
    if (loaded.value && !options.force) return;
    if (loading.value) return;

    loading.value = true;
    loadError.value = null;
    try {
      apply(await api.load());
    } catch (caught) {
      loadError.value = errorMessage(caught);
    } finally {
      loading.value = false;
    }
  }

  async function save(): Promise<boolean> {
    const current = snapshot.value;
    if (current === null || saving.value) return false;

    const changes = buildPatch(current, values.value);
    // Нечего слать: POST без изменений — это перезапись файла вхолостую.
    if (Object.keys(changes).length === 0) return true;

    saving.value = true;
    success.value = null;
    saveError.value = null;
    fieldErrors.value = {};
    try {
      apply(await api.save(changes));
      const count = sectionCount(changes);
      success.value = `Настройки сохранены: ${count} ${pluralFields(count)}.`;
      return true;
    } catch (caught) {
      if (caught instanceof SettingsValidationError) {
        const { byField, unattributed } = splitProblems(caught.problems);
        fieldErrors.value = byField;
        const parts: string[] = [];
        const count = Object.keys(byField).length;
        if (count > 0) parts.push(`исправьте ${count} ${pluralFields(count)}`);
        parts.push(...unattributed);
        saveError.value = `Сохранение не выполнено: ${parts.join("; ")}.`;
      } else {
        saveError.value = errorMessage(caught);
      }
      // Значения и снапшот не трогаем: форма остаётся с введённым,
      // dirty сохраняется, следующая попытка снова уйдёт в демон.
      return false;
    } finally {
      saving.value = false;
    }
  }

  function reset(): void {
    if (snapshot.value === null) return;
    apply(snapshot.value);
    fieldErrors.value = {};
    saveError.value = null;
    success.value = null;
  }

  function setValue(path: string, value: SettingValue): void {
    values.value = { ...values.value, [path]: value };
    // Ошибка гаснет, как только поле правят: подпись описывает старое значение.
    if (path in fieldErrors.value) {
      const remaining = { ...fieldErrors.value };
      delete remaining[path];
      fieldErrors.value = remaining;
    }
    success.value = null;
  }

  return {
    values,
    loading,
    saving,
    loaded,
    loadError,
    saveError,
    fieldErrors,
    success,
    dirtyKeys,
    dirty,
    canSave,
    load,
    save,
    reset,
    setValue,
  };
}

// Единственный экземпляр на приложение: экран читает одни ref'ы.
let shared: SettingsState | null = null;

export function useSettings(): SettingsState {
  shared ??= createSettings();
  return shared;
}
