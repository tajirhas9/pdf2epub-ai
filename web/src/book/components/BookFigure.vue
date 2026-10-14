<script setup lang="ts">
import { computed, inject } from 'vue'
import { ASSET_BASE } from './keys'

const props = withDefaults(
  defineProps<{ src: string; alt?: string; layout?: string; bid?: string; width?: number; height?: number }>(),
  { alt: '', layout: 'full' },
)
const base = inject(ASSET_BASE, '')
const url = computed(() => base + props.src)
</script>

<template>
  <figure :data-bid="bid" :class="['fig', `fig--${layout}`]">
    <img :src="url" :alt="alt" :width="width" :height="height" />
    <figcaption v-if="$slots.default"><slot /></figcaption>
  </figure>
</template>
