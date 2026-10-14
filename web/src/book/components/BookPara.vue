<script setup lang="ts">
// `cont` marks a paragraph continued from a previous page (no first-line indent).
defineProps<{ bid?: string; lang?: string; dir?: string; cont?: boolean; kind?: string }>()
</script>

<template>
  <p :data-bid="bid" :lang="lang" :dir="dir" :class="[cont && 'cont', kind]"><slot /></p>
</template>
