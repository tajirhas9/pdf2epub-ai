<script setup lang="ts">
// Footnote marker; `to` is the footnote block id.
defineProps<{ to: string; label: string }>()
</script>

<template>
  <a :id="`ref-${to}`" epub:type="noteref" role="doc-noteref" :href="`#${to}`" class="noteref">{{ label }}</a>
</template>
