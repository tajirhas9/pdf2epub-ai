<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useBook } from './store'

const route = useRoute()
const book = useBook()
const bare = computed(() => route.meta.bare === true)
// Same step names as the CLI.
const LABELS: Record<string, string> = {
  rasterize: 'Reading pages',
  layout: 'Detecting layout',
  text: 'Recognizing text',
  figures: 'Extracting figures',
  review: 'LLM review',
}
const progress = computed(() => {
  const p = book.progress
  if (!p || p.stage === 'done' || !p.total) return null
  return { label: LABELS[p.stage] ?? p.stage, done: p.done, total: p.total, pct: Math.round((100 * p.done) / p.total) }
})
</script>

<template>
  <RouterView v-if="bare" />
  <div v-else class="shell">
    <header>
      <RouterLink to="/" class="title">{{ book.book?.title ?? 'pdf2epub' }}</RouterLink>
      <span class="muted">{{ book.pages.length }} pages</span>
      <span v-if="progress" class="progress">
        {{ progress.label }} · {{ progress.done }}/{{ progress.total }}
        <span class="track"><span class="fill" :style="{ width: progress.pct + '%' }" /></span>
      </span>
    </header>
    <main><RouterView /></main>
  </div>
</template>

<style>
:root {
  --bg: #f6f5f2;
  --panel: #ffffff;
  --ink: #1d1d1f;
  --muted: #6b6b70;
  --line: #dddad3;
  --accent: #b5532a;
  font-family: system-ui, sans-serif;
  color: var(--ink);
  background: var(--bg);
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #18181a; --panel: #222225; --ink: #ececee; --muted: #9a9aa2; --line: #36363b; --accent: #e08a5f; }
}
body { margin: 0; }
.shell header {
  display: flex; gap: 1rem; align-items: baseline; padding: 0.6rem 1rem;
  border-bottom: 1px solid var(--line); background: var(--panel); position: sticky; top: 0; z-index: 5;
}
.shell .title { font-weight: 600; color: var(--ink); text-decoration: none; }
.muted { color: var(--muted); font-size: 0.85rem; }
.progress { margin-left: auto; color: var(--muted); font-size: 0.8rem; display: flex; align-items: center; gap: 0.6rem; }
.track { width: 140px; height: 4px; border-radius: 2px; background: var(--line); overflow: hidden; }
.fill { display: block; height: 100%; background: var(--accent); transition: width 0.3s ease; }
main { padding: 1rem; }
</style>
