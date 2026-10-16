<script setup lang="ts">
const props = withDefaults(
  defineProps<{ level?: number; newpage?: boolean; bid?: string; lang?: string; dir?: string }>(),
  { level: 2, newpage: false },
)
const tag = `h${Math.min(Math.max(props.level, 1), 6)}`
</script>

<template>
  <component :is="tag" :class="newpage ? 'newpage' : undefined" :data-bid="bid" :lang="lang" :dir="dir"><slot /></component>
</template>
