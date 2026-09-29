<script setup lang="ts">
// Панель фильтров экрана (дизайн.md §5): слева контролы вызывающего — слот,
// под ними активные фильтры чипами с крестиком и кнопка «Сбросить».
//
// Значения фильтров панель не хранит: форма живёт у вызывающего (валидация,
// localStorage, перечитывание списка) — панель только показывает, что
// включено, и просит очистить одно поле или сбросить всё.
interface FilterBarChip {
  /** Ключ поля у вызывающего: он же payload события clear. */
  key: string;
  label: string;
  value: string;
}

withDefaults(defineProps<{ chips?: FilterBarChip[] }>(), { chips: () => [] });

const emit = defineEmits<{
  /** Пользователь снял один активный фильтр (крестик на чипе). */
  clear: [key: string];
  /** Пользователь запросил сброс всех фильтров. */
  reset: [];
}>();
</script>

<template>
  <div class="filter-bar">
    <div class="filter-bar__controls d-flex align-center flex-wrap ga-2">
      <slot />
    </div>

    <div
      v-if="chips.length"
      class="filter-bar__active d-flex align-center flex-wrap ga-2 mt-2"
      data-test="filter-active"
    >
      <v-chip
        v-for="chip in chips"
        :key="chip.key"
        size="small"
        closable
        :data-test="`filter-chip-${chip.key}`"
        @click:close="emit('clear', chip.key)"
      >
        {{ chip.label }}: {{ chip.value }}
      </v-chip>

      <v-btn
        variant="text"
        size="small"
        data-test="filter-reset"
        @click="emit('reset')"
      >
        Сбросить
      </v-btn>
    </div>
  </div>
</template>
