<script setup lang="ts">
// A special printed mark (e.g. an honorific ligature) kept as the original glyph image because its
// text could not be verified. Sized to the surrounding text.
import { computed, inject } from 'vue'
import { ASSET_BASE } from './keys'

const props = withDefaults(defineProps<{ src: string; width?: number; height?: number; alt?: string }>(), {
  alt: '[mark]',
})
const base = inject(ASSET_BASE, '')
const url = computed(() => base + props.src)
</script>

<template>
  <img class="glyph" :src="url" :alt="alt" :width="width" :height="height" />
</template>
