<script setup lang="ts">
defineProps<{ label: string; hint?: string; disabled?: boolean }>()
const model = defineModel<boolean>({ required: true })
</script>

<template>
  <label class="switch">
    <input v-model="model" type="checkbox" role="switch" :disabled="disabled" />
    <span class="track" aria-hidden="true" />
    <span class="text">
      <b>{{ label }}</b>
      <span v-if="hint" class="hint">{{ hint }}</span>
    </span>
  </label>
</template>
