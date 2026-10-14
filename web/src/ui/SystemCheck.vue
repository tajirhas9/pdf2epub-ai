<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { loadSystem, store } from './store'

onMounted(() => store.system || loadSystem())
const checking = ref(false)
async function recheck() {
  checking.value = true
  await loadSystem()
  checking.value = false
}
const problems = computed(() => !!store.system && (!store.system.node_ok || !store.system.paddle))

type Row = { name: string; level: 'ok' | 'warn' | 'err' | 'info'; text: string }
const rows = computed<Row[]>(() => {
  const s = store.system
  if (!s) return []
  return [
    {
      name: 'Node.js',
      level: s.node_ok ? 'ok' : 'err',
      text: s.node_ok
        ? `${s.node} — builds the EPUB`
        : `${s.node ? `${s.node} is too old` : 'Not found'} — install Node.js ${s.node_min} or newer from nodejs.org`,
    },
    {
      name: 'OCR engine',
      level: s.paddle ? 'ok' : 'err',
      text: s.paddle
        ? 'PaddlePaddle installed'
        : 'PaddlePaddle missing — reinstall with the [cpu] or [gpu] extra (README › Install)',
    },
    {
      name: 'Graphics card',
      level: s.gpu ? 'ok' : 'warn',
      text: s.gpu ? 'NVIDIA GPU in use — fast' : 'CPU only — works, but scanned pages take a few seconds each',
    },
    {
      name: 'Vision models',
      level: s.models ? 'ok' : 'info',
      text: s.models ? 'Downloaded' : 'Downloaded automatically on the first conversion (~250 MB, once)',
    },
    {
      name: 'Java',
      level: s.java ? 'ok' : 'info',
      text: s.java ? 'Found — EPUBs are validated with epubcheck' : 'Optional — install a Java runtime to validate EPUBs',
    },
  ]
})
const ICON = { ok: '✓', warn: '!', err: '✕', info: '○' }
</script>

<template>
  <div v-if="!store.system" class="row muted small"><span class="spinner" /> Checking this computer…</div>
  <ul v-else class="checks">
    <li v-for="r in rows" :key="r.name">
      <span class="icon" :class="r.level" aria-hidden="true">{{ ICON[r.level] }}</span>
      <span class="name">{{ r.name }}</span>
      <span class="text" :class="{ muted: r.level === 'info' }">{{ r.text }}</span>
    </li>
  </ul>
  <div v-if="problems" class="row recheck">
    <span class="hint">Installed something? No restart needed.</span>
    <button type="button" class="btn" :disabled="checking" @click="recheck">
      <span v-if="checking" class="spinner" /> Check again
    </button>
  </div>
</template>

<style scoped>
.checks { list-style: none; margin: 0; padding: 0; display: grid; gap: 0.1rem; }
li {
  display: grid;
  grid-template-columns: 1.5rem 8.5rem 1fr;
  align-items: baseline;
  padding: 0.45rem 0;
  border-bottom: 1px solid var(--line);
  font-size: 0.9rem;
}
li:last-child { border-bottom: 0; }
.recheck { justify-content: flex-end; margin-top: 0.75rem; }
.name { font-weight: 560; }
.icon { font-weight: 700; }
.icon.ok { color: var(--ok); }
.icon.warn { color: var(--warn); }
.icon.err { color: var(--err); }
.icon.info { color: var(--muted); }
@media (max-width: 520px) {
  li { grid-template-columns: 1.5rem 1fr; }
  .text { grid-column: 2; }
}
</style>
