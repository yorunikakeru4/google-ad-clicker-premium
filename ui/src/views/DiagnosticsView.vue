<script setup lang="ts">
// Экран Diagnostics (план §5, фаза 7): карточка на воркер с последним
// снимком сессии, подсветка suspicion_flags чипами, режим «как сайт видит
// сессию» с raw-значениями, сбор по кнопке и экспорт снимка в JSON/CSV.
//
// Чтение — read-only читалка (`list_diagnostics`) с опросом 4 с; сбор —
// control API демона (`/control/diagnostics/collect`), и снимок появляется в
// БД асинхронно — его подхватывает обычный тик. Ошибки сбора (400
// invalid_request и т.п.) показываются текстом демона.

import { computed, onMounted, onUnmounted, ref } from "vue";
import DbUnavailableAlert from "../components/DbUnavailableAlert.vue";
import PageLayout from "../components/layout/PageLayout.vue";
import StatusChip from "../components/status/StatusChip.vue";
import { useDb } from "../composables/useDb";
import { useDiagnostics } from "../composables/useDiagnostics";
import {
  diagnosticToCsv,
  diagnosticToJson,
  flagSeverity,
  parseSuspicionFlags,
  rawParams,
  sessionParams,
  type DiagnosticCard,
  type DiagnosticSnapshot,
  type RawParam,
  type SessionParam,
} from "../lib/diagnostics";

const db = useDb();
const diagnostics = useDiagnostics();

onMounted(() => {
  void db.ensureOpen();
  void diagnostics.start();
});
onUnmounted(() => diagnostics.stop());

// --- вид карточки ---------------------------------------------------------

/** Разобранное состояние флагов: отдельные виды, чтобы шаблон сужал типы. */
type FlagsView =
  | { kind: "none" }
  | { kind: "broken"; raw: string | null }
  | { kind: "ok" }
  | { kind: "flags"; flags: string[] };

interface CardView {
  card: DiagnosticCard;
  /** Идентификатор в data-test и ключ режима: browser_id либо «anon». */
  key: string;
  snapshot: DiagnosticSnapshot | null;
  params: SessionParam[];
  raw: RawParam[];
  flags: FlagsView;
  /** true — режим «как сайт видит сессию». */
  rawMode: boolean;
}

function toFlagsView(snapshot: DiagnosticSnapshot | null): FlagsView {
  if (snapshot === null) return { kind: "none" };
  const parsed = parseSuspicionFlags(snapshot.suspicion_flags);
  if (parsed === null) return { kind: "broken", raw: snapshot.suspicion_flags };
  if (parsed.length === 0) return { kind: "ok" };
  return { kind: "flags", flags: parsed };
}

function toCardView(card: DiagnosticCard, rawMode: boolean): CardView {
  const snapshot = card.snapshot;
  return {
    card,
    key: card.browserId ?? "anon",
    snapshot,
    params: snapshot === null ? [] : sessionParams(snapshot),
    raw: snapshot === null ? [] : rawParams(snapshot),
    flags: toFlagsView(snapshot),
    rawMode,
  };
}

/** Режим «как сайт видит» — по ключу карточки, состояние переживает тики. */
const rawModes = ref<Record<string, boolean>>({});

const cardViews = computed(() =>
  diagnostics.cards.value.map((card) =>
    toCardView(card, rawModes.value[card.browserId ?? "anon"] === true),
  ),
);

function toggleRaw(key: string, value: boolean): void {
  rawModes.value = { ...rawModes.value, [key]: value };
}

// --- сбор и экспорт -------------------------------------------------------

function formatTs(ts: number): string {
  const at = new Date(ts * 1000);
  const p = (value: number) => String(value).padStart(2, "0");
  return (
    `${at.getFullYear()}-${p(at.getMonth() + 1)}-${p(at.getDate())} ` +
    `${p(at.getHours())}:${p(at.getMinutes())}:${p(at.getSeconds())}`
  );
}

function fileStamp(): string {
  const at = new Date();
  const p = (value: number) => String(value).padStart(2, "0");
  return (
    `${at.getFullYear()}-${p(at.getMonth() + 1)}-${p(at.getDate())}` +
    `-${p(at.getHours())}${p(at.getMinutes())}${p(at.getSeconds())}`
  );
}

/**
 * Скачивание готового файла. Отложенная отмена — браузер должен успеть
 * забрать blob до revoke, как в Logs.
 */
function download(content: string, filename: string, mime: string): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

function exportJson(view: CardView): void {
  if (view.snapshot === null) return;
  download(
    diagnosticToJson(view.snapshot),
    `adclicker-diagnostics-${view.key}-${fileStamp()}.json`,
    "application/json;charset=utf-8",
  );
}

function exportCsv(view: CardView): void {
  if (view.snapshot === null) return;
  download(
    diagnosticToCsv(view.snapshot),
    `adclicker-diagnostics-${view.key}-${fileStamp()}.csv`,
    "text/csv;charset=utf-8",
  );
}

/** Сбор со своей карточки: анонимный снимок воркера не с чем собирать. */
function collectCard(view: CardView): void {
  const browserId = view.card.browserId;
  if (browserId === null) return;
  void diagnostics.collect(browserId);
}
</script>

<template>
  <PageLayout
    title="Diagnostics"
    subtitle="Как сайт видит сессию: значения снимаются без изменений"
  >
    <template #toolbar-right>
      <v-btn
        color="primary"
        prepend-icon="mdi-camera-flare"
        :loading="diagnostics.pending.value === 'collect-all'"
        :disabled="diagnostics.pending.value !== null"
        data-test="diagnostics-collect-all"
        @click="void diagnostics.collectAll()"
      >
        Собрать для всех
      </v-btn>
    </template>

    <DbUnavailableAlert />

    <v-alert
      v-if="diagnostics.error.value"
      type="error"
      variant="tonal"
      class="mb-4"
      data-test="diagnostics-read-error"
    >
      {{ diagnostics.error.value }}
    </v-alert>

    <v-alert
      v-if="diagnostics.actionError.value"
      type="error"
      variant="tonal"
      class="mb-4"
      data-test="diagnostics-action-error"
    >
      {{ diagnostics.actionError.value }}
    </v-alert>

    <v-alert
      v-if="diagnostics.collectResult.value"
      type="success"
      variant="tonal"
      class="mb-4"
      data-test="diagnostics-collect-result"
    >
      Запрошено: {{ diagnostics.collectResult.value.requested }}
    </v-alert>

    <v-alert
      v-if="diagnostics.loading.value"
      type="info"
      variant="tonal"
      density="comfortable"
      class="mb-4"
      data-test="diagnostics-loading"
    >
      Читаю снимки диагностики…
    </v-alert>

    <v-row v-if="cardViews.length > 0" dense>
      <v-col
        v-for="view in cardViews"
        :key="view.key"
        cols="12"
        md="6"
        xl="4"
      >
        <v-card class="h-100" :data-test="`diagnostics-card-${view.key}`">
          <v-card-title
            class="d-flex align-center justify-space-between text-subtitle-1 font-weight-bold"
          >
            <span>{{ view.card.browserId ?? "снимок без воркера" }}</span>
            <span v-if="view.snapshot" class="text-caption text-muted">
              {{ formatTs(view.snapshot.ts) }}
            </span>
          </v-card-title>

          <v-card-text>
            <v-switch
              :model-value="view.rawMode"
              label="Как сайт видит сессию"
              color="primary"
              hide-details
              density="compact"
              class="mt-0"
              :data-test="`diagnostics-raw-${view.key}`"
              @update:model-value="toggleRaw(view.key, Boolean($event))"
            />

            <div
              v-if="view.snapshot === null"
              class="text-body-2 text-muted py-2"
              :data-test="`diagnostics-nodata-${view.key}`"
            >
              нет данных — нажмите «Собрать»
            </div>

            <div v-else class="param-list mt-1">
              <template v-if="!view.rawMode">
                <div
                  v-for="param in view.params"
                  :key="param.label"
                  class="param-row d-flex align-center justify-space-between py-1"
                >
                  <span class="text-body-2 text-muted">{{ param.label }}</span>
                  <span
                    class="text-body-2 text-right param-value"
                    :title="param.value === null ? undefined : String(param.value)"
                  >
                    {{ param.value ?? "нет данных" }}
                  </span>
                </div>
              </template>

              <template v-else>
                <div
                  v-for="param in view.raw"
                  :key="param.key"
                  class="param-row d-flex align-start justify-space-between py-1"
                >
                  <span class="text-body-2 text-muted">{{ param.key }}</span>
                  <pre class="raw-value text-body-2">{{
                    param.value ?? "нет данных"
                  }}</pre>
                </div>
              </template>

              <div
                class="flags d-flex flex-wrap align-center ga-1 pt-2"
                :data-test="`diagnostics-flags-${view.key}`"
              >
                <template v-if="view.flags.kind === 'broken'">
                  <v-chip
                    color="warning"
                    variant="tonal"
                    prepend-icon="mdi-alert"
                    :data-test="`diagnostics-flags-broken-${view.key}`"
                  >
                    список флагов не разобран
                  </v-chip>
                  <span class="text-caption text-muted">
                    {{ view.flags.raw }}
                  </span>
                </template>

                <StatusChip
                  v-else-if="view.flags.kind === 'ok'"
                  status="ok"
                  label="ок"
                  :data-test="`diagnostics-flags-ok-${view.key}`"
                />

                <v-chip
                  v-for="flag in view.flags.kind === 'flags'
                    ? view.flags.flags
                    : []"
                  :key="flag"
                  :color="flagSeverity(flag) === 'error' ? 'error' : 'warning'"
                  variant="tonal"
                  prepend-icon="mdi-alert-circle"
                  :data-test="`diagnostics-flag-${view.key}`"
                >
                  {{ flag }}
                </v-chip>
              </div>
            </div>
          </v-card-text>

          <v-card-actions class="px-4 pb-3 d-flex flex-wrap ga-2">
            <v-btn
              v-if="view.card.browserId !== null"
              size="small"
              variant="tonal"
              color="primary"
              prepend-icon="mdi-camera"
              :loading="diagnostics.pending.value === 'collect'"
              :disabled="diagnostics.pending.value !== null"
              :data-test="`diagnostics-collect-${view.key}`"
              @click="collectCard(view)"
            >
              Собрать
            </v-btn>

            <v-spacer />

            <v-btn
              size="small"
              variant="text"
              prepend-icon="mdi-code-json"
              :disabled="view.snapshot === null"
              :data-test="`diagnostics-export-json-${view.key}`"
              @click="exportJson(view)"
            >
              JSON
            </v-btn>
            <v-btn
              size="small"
              variant="text"
              prepend-icon="mdi-download"
              :disabled="view.snapshot === null"
              :data-test="`diagnostics-export-csv-${view.key}`"
              @click="exportCsv(view)"
            >
              CSV
            </v-btn>
          </v-card-actions>
        </v-card>
      </v-col>
    </v-row>

    <v-card v-else data-test="diagnostics-empty">
      <v-card-text class="text-center py-8">
        <v-icon icon="mdi-stethoscope" size="40px" />
        <div class="text-subtitle-1 mt-2">Снимков пока нет</div>
        <div class="text-body-2 text-muted">
          Нажмите «Собрать для всех» — воркеры пришлют параметры сессии
        </div>
      </v-card-text>
    </v-card>

    <v-alert type="info" variant="tonal" density="comfortable" class="mt-4">
      Значения выводятся как есть: снятие параметров не меняет поведение
      браузера.
    </v-alert>
  </PageLayout>
</template>

<style scoped>
.param-row {
  border-bottom: 1px solid rgb(var(--v-border-color), var(--v-border-opacity));
}

.param-row:last-child {
  border-bottom: none;
}

/* Длинные значения (UA, renderer) не раздувают карточку: обрезаем строку,
   полный текст — в title/в raw-режиме. */
.param-value {
  max-width: 60%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.raw-value {
  margin: 0;
  max-width: 70%;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
  text-align: right;
}
</style>
