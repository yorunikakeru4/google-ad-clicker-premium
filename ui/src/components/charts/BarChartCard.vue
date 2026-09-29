<script setup lang="ts">
// Столбчатый график дашборда на chart.js (vue-chartjs — декларативная
// обёртка для Vue 3). Один компонент на все три графика: клики/час, CAPTCHA
// по часам, нагрузка на воркеры (горизонтальные столбцы).
import { computed } from "vue";
import { Bar } from "vue-chartjs";
import {
  BarElement,
  CategoryScale,
  Chart as ChartJS,
  Legend,
  LinearScale,
  Tooltip,
  type ChartData,
  type ChartOptions,
} from "chart.js";
import { useTheme } from "vuetify";

ChartJS.register(BarElement, CategoryScale, LinearScale, Tooltip, Legend);

const props = withDefaults(
  defineProps<{
    title: string;
    /** Метки оси времени/категорий — уже отформатированы вызывающим. */
    labels: string[];
    values: number[];
    /** Ключ цвета темы (primary, warning, …); по умолчанию primary. */
    colorToken?: string;
    /** Подпись под заголовком: окно и источник данных. */
    hint?: string;
    /** Горизонтальные столбцы — нагрузка по воркерам. */
    horizontal?: boolean;
  }>(),
  { colorToken: undefined, hint: "", horizontal: false },
);

const theme = useTheme();

// Цвета берём из состояния темы, а не из CSS: chart.js рисует на canvas и
// CSS-переменные ему не видны. Значения — те же secondary/нейтрали, что
// используются в UI (см. plugins/vuetify.ts).
const palette = computed(() => {
  const dark = theme.current.value.dark;
  const colors = theme.current.value.colors;
  const token = props.colorToken ?? "primary";
  return {
    bar: colors[token] ?? colors.primary,
    text: dark ? "#9aa0a6" : "#5f6368",
    grid: dark ? "rgba(255,255,255,0.10)" : "rgba(0,0,0,0.08)",
  };
});

const chartData = computed<ChartData<"bar">>(() => ({
  labels: props.labels,
  datasets: [
    {
      label: props.title,
      data: props.values,
      backgroundColor: palette.value.bar,
      borderRadius: 4,
    },
  ],
}));

const chartOptions = computed<ChartOptions<"bar">>(() => {
  const axis = {
    ticks: { color: palette.value.text, font: { size: 11 } },
    grid: { color: palette.value.grid },
  };
  const countAxis = {
    ...axis,
    beginAtZero: true,
    ticks: { ...axis.ticks, precision: 0 },
  };

  return {
    responsive: true,
    maintainAspectRatio: false,
    indexAxis: props.horizontal ? "y" : "x",
    plugins: {
      legend: { display: false },
      tooltip: { mode: "index", intersect: false },
    },
    scales: props.horizontal
      ? { x: countAxis, y: axis }
      : { x: axis, y: countAxis },
  };
});

const empty = computed(() => props.labels.length === 0);
</script>

<template>
  <v-card :data-test="`chart-${title}`">
    <v-card-title class="text-subtitle-1 font-weight-medium pb-0">
      {{ title }}
    </v-card-title>
    <v-card-subtitle v-if="hint" class="pb-1">{{ hint }}</v-card-subtitle>

    <v-card-text class="pt-2">
      <p v-if="empty" class="text-muted mb-0" data-test="chart-empty">
        Нет данных за окно графика
      </p>
      <div v-else class="chart-box">
        <Bar :data="chartData" :options="chartOptions" />
      </div>
    </v-card-text>
  </v-card>
</template>

<style scoped>
.chart-box {
  position: relative;
  height: 220px;
}
</style>
