<script setup lang="ts">
const props = withDefaults(defineProps<{ level?: number; bid?: string; lang?: string; dir?: string }>(), {
  level: 2,
})
const tag = `h${Math.min(Math.max(props.level, 1), 6)}`
</script>

<template>
  <component :is="tag" :data-bid="bid" :lang="lang" :dir="dir"><slot /></component>
</template>
