<script setup lang="ts">
// Inline run in another language/script (e.g. Arabic inside English text).
defineProps<{ lang: string; dir?: string }>()
</script>

<template>
  <span :lang="lang" :dir="dir"><slot /></span>
</template>
