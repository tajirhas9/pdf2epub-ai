<script setup lang="ts">
import { assetBase } from 'virtual:book'
import { useBook } from './store'

const book = useBook()
const status = (flags: string[]) => flags.find((f) => f.startsWith('review:'))?.slice(7) ?? 'not reviewed'
</script>

<template>
  <div class="grid">
    <RouterLink v-for="p in book.pages" :key="p.key" :to="`/page/${p.key}`" class="card">
      <img v-if="p.image" :src="assetBase + p.image.path" :alt="`page ${p.label}`" loading="lazy" />
      <div class="meta">
        <strong>{{ p.label }}</strong>
        <span :class="['badge', status(p.flags)]">{{ status(p.flags) }}</span>
        <span v-if="p.locked" class="badge locked">locked</span>
        <span v-if="p.flags.includes('ocr')" class="badge">ocr</span>
      </div>
    </RouterLink>
  </div>
</template>

<style scoped>
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 1rem; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; text-decoration: none; color: inherit; overflow: hidden; }
.card:hover { border-color: var(--accent); }
img { width: 100%; display: block; aspect-ratio: 2 / 3; object-fit: cover; object-position: top; }
.meta { display: flex; gap: 0.4rem; align-items: center; padding: 0.4rem 0.5rem; flex-wrap: wrap; font-size: 0.8rem; }
.badge { border: 1px solid var(--line); border-radius: 999px; padding: 0 0.45rem; color: var(--muted); }
.badge.ok { color: #2f7d32; border-color: #2f7d32; }
.badge.fixed { color: #1f5fa8; border-color: #1f5fa8; }
.badge.needs_human, .badge.failed, .badge.locked { color: var(--accent); border-color: var(--accent); }
</style>
