<script setup lang="ts">
// Единая заглушка экрана: честно сообщает, в какой фиче он наполнится.
// Содержимое добавляют соответствующие фазы плана §5.
defineProps<{
  title: string;
  note: string;
}>();
</script>

<template>
  <div>
    <h1 class="text-h5 mb-2">{{ title }}</h1>
    <p class="text-body-1 text-medium-emphasis mb-0">{{ note }}</p>
  </div>
</template>
