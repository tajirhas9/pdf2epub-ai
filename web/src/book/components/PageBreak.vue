<script setup lang="ts">
// Print page boundary: keeps citations by page number usable (EPUB page-list).
defineProps<{ label: string }>()
</script>

<template>
  <span :id="`page-${label}`" epub:type="pagebreak" role="doc-pagebreak" :aria-label="label" class="pagebreak"></span>
</template>
