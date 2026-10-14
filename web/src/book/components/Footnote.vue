<script setup lang="ts">
// `back`: a note marker in the text points here, so link back to it (only then does #ref-<id> exist).
defineProps<{ id: string; bid?: string; label?: string; back?: boolean; lang?: string; dir?: string }>()
</script>

<template>
  <aside :id="id" :data-bid="bid" epub:type="footnote" role="doc-footnote" class="footnote" :lang="lang" :dir="dir">
    <p>
      <a v-if="label && back" :href="`#ref-${id}`" class="note-back">{{ label }}</a>
      <span v-else-if="label" class="note-label">{{ label }}</span>
      <slot />
    </p>
  </aside>
</template>
