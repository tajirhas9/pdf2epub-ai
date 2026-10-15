<script setup lang="ts">
import { computed } from 'vue'
import { formatSize, type Job } from './api'
import { isPreparing, jobs, statusOf } from './jobs'

defineEmits<{ convert: [] }>()

const list = computed(() => [...jobs.list].sort((a, b) => b.created - a.created))

const when = (j: Job) =>
  new Date(j.created * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })

function summary(j: Job): string {
  if (j.status === 'done' && j.result) return `${j.result.file} · ${formatSize(j.result.size)}`
  if (j.status === 'failed' && j.error) return j.error.message
  if (isPreparing(j)) return j.waiting === 'download' ? 'Downloading the OCR models' : 'Loading the OCR models'
  if (j.status === 'paused' && j.reason.startsWith('llm-unreachable')) return 'The AI review stopped'
  if (j.status === 'paused' && j.reason === 'interrupted') return 'Interrupted — open to resume'
  if (j.status === 'queued') return 'Starts when the conversion before it finishes'
  const s = [...j.steps].reverse().find((x) => x.status === 'running' || x.status === 'paused') ?? [...j.steps].reverse().find((x) => x.total)
  return s ? (s.total ? `${s.label} · ${s.done} / ${s.total}` : s.label) : ''
}
</script>

<template>
  <div class="stack">
    <h2>Conversions</h2>
    <div v-if="!jobs.loaded" class="row muted"><span class="spinner" /> Loading…</div>
    <div v-else-if="!list.length" class="card empty">
      <p>No conversions yet.</p>
      <button type="button" class="btn primary" @click="$emit('convert')">Convert a PDF</button>
    </div>
    <ul v-else class="list" aria-live="polite">
      <li v-for="j in list" :key="j.id">
        <a class="card item" :href="`#conversions/${j.id}`">
          <div class="meta">
            <b :title="j.file.name">{{ j.file.name }}</b>
            <span class="muted small">{{ when(j) }} · {{ j.file.pages }} page{{ j.file.pages === 1 ? '' : 's' }}</span>
            <span v-if="summary(j)" class="muted small line" :class="{ err: j.status === 'failed' }">{{ summary(j) }}</span>
          </div>
          <span class="badge" :class="statusOf(j).cls">
            <span v-if="j.status === 'running' || isPreparing(j)" class="spinner" aria-hidden="true" />{{ statusOf(j).text }}
          </span>
          <span class="chev" aria-hidden="true">›</span>
        </a>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.list { list-style: none; margin: 0; padding: 0; display: grid; gap: 0.6rem; }
.item {
  display: flex; align-items: center; gap: 0.75rem; color: inherit; text-decoration: none;
  transition: border-color 0.15s, background 0.15s;
}
.item:hover { border-color: var(--line-strong); background: var(--sunken); }
.meta { flex: 1; min-width: 0; display: grid; gap: 0.1rem; }
.meta b, .line { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.line.err { color: var(--err); }
.chev { color: var(--muted); font-size: 1.3rem; line-height: 1; }
.empty { display: grid; justify-items: center; gap: 0.75rem; text-align: center; color: var(--muted); }
</style>
