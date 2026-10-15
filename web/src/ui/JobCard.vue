<script setup lang="ts">
import { computed, ref } from 'vue'
import { api, formatSeconds, formatSize, type Job } from './api'
import { isActive, isPreparing, statusOf } from './jobs'
import { errorText, toast } from './store'

// `detailed`: the conversion's own page, with every step listed.
const props = defineProps<{ job: Job; detailed?: boolean }>()
const emit = defineEmits<{ changed: []; removed: []; created: [id: string] }>()

const preparing = computed(() => isPreparing(props.job))
const status = computed(() => statusOf(props.job))
const active = computed(() => isActive(props.job))
const pauseReason = computed(() => {
  const reason = props.job.reason
  if (reason.startsWith('llm-unreachable')) return { title: 'The AI review stopped', hint: 'Fix the cause below (server down, rate limit, credit, model or key), then press Resume.', detail: reason.slice(reason.indexOf(':') + 1).trim() }
  if (reason === 'interrupted') return { title: 'Interrupted', hint: 'The app or computer stopped during the conversion. Press Resume to continue.', detail: '' }
  return { title: 'Paused', hint: 'Press Resume to continue.', detail: '' }
})
const done = computed(() => {
  const s = [...props.job.steps].reverse().find((x) => x.total)
  return s ? `${s.label}: ${s.done} / ${s.total}` : ''
})
const current = computed(() => [...props.job.steps].reverse().find((s) => s.status === 'running'))
const showSteps = ref(!!props.detailed)
const r = computed(() => props.job.result)
const optionsLine = computed(() => {
  const o = props.job.options
  return [
    o.langs.length ? o.langs.join(', ') : 'auto languages',
    o.llm ? (o.review_policy === 'all' ? 'AI review: every page' : 'AI review') : 'no AI review',
  ].join(' · ')
})

async function act(fn: () => Promise<unknown>) {
  try {
    await fn()
    emit('changed')
  } catch (e) {
    toast(errorText(e), 'err')
  }
}
const stop = () => act(() => api.cancel(props.job.id))
const pause = () => act(() => api.pause(props.job.id))
const resume = () => act(() => api.resume(props.job.id))
const retryReview = () => act(() => api.retryReview(props.job.id))
const reviewFailed = computed(() => r.value?.review_failed ?? [])
function retryFrom(step: { key: string; label: string }) {
  if (!confirm(`Redo “${step.label}” and every step after it? The steps before it are kept.`)) return
  act(() => api.retryFrom(props.job.id, step.key))
}
// the step that failed or was interrupted, when it can be redone from
const stuckStep = computed(() => {
  if (props.job.status !== 'failed' && props.job.status !== 'paused') return undefined
  const s = props.job.steps.find((x) => x.status === 'failed' || x.status === 'paused')
  return s && props.job.retry_steps.includes(s.key) ? s : undefined
})
async function remove() {
  try {
    await api.removeJob(props.job.id)
    emit('removed')
  } catch (e) {
    toast(errorText(e), 'err')
  }
}
async function again() {
  try {
    const job = await api.convert(props.job.file.id, props.job.options)
    emit('created', job.id)
  } catch (e) {
    toast(errorText(e), 'err')
  }
}

async function copyPath() {
  try {
    await navigator.clipboard.writeText(r.value!.path)
    toast('Path copied')
  } catch {
    toast('Copy failed — select the path and copy it manually', 'err')
  }
}
const ICONS = { pending: '○', running: '', done: '✓', failed: '✕', paused: '⏸', skipped: '–' } as const
const pct = (s: { done: number; total: number | null }) => (s.total ? Math.round((100 * s.done) / s.total) : 0)
</script>

<template>
  <article class="card job" :aria-busy="active">
    <header class="top">
      <div class="title">
        <h3 :title="job.file.name">{{ job.file.name }}</h3>
        <p class="muted small">{{ job.file.pages }} pages · {{ optionsLine }}</p>
      </div>
      <span class="badge" :class="status.cls">
        <span v-if="job.status === 'running' || preparing" class="spinner" aria-hidden="true" />{{ status.text }}
      </span>
    </header>

    <!-- running: the current step, big and clear -->
    <div v-if="active" class="now">
      <template v-if="preparing">
        <div class="row small">
          <b>{{ job.waiting === 'download' ? 'Downloading the OCR models' : 'Loading the OCR models' }}</b>
        </div>
        <div class="bar indeterminate" aria-hidden="true"><span /></div>
        <p class="muted small">
          {{ job.waiting === 'download' ? 'First conversion only: about 250 MB, saved for next time. Starts right after.' : 'Starts in a few seconds.' }}
        </p>
      </template>
      <template v-else-if="job.status === 'queued'">
        <p class="muted small">Starts when the conversion before it finishes.</p>
      </template>
      <template v-else-if="current">
        <div class="row small">
          <b>{{ current.label }}</b>
          <span v-if="current.total" class="muted">{{ current.done }} / {{ current.total }}</span>
          <span class="spacer" />
          <span class="muted">{{ formatSeconds(job.elapsed) }}</span>
        </div>
        <div v-if="current.total" class="bar" role="progressbar" :aria-valuenow="pct(current)" aria-valuemin="0" aria-valuemax="100">
          <span :style="{ width: pct(current) + '%' }" />
        </div>
        <div v-else class="bar indeterminate" aria-hidden="true"><span /></div>
        <p v-if="current.note" class="muted small">{{ current.note }}</p>
      </template>
      <p v-else class="muted small">Starting…</p>
    </div>

    <!-- done -->
    <div v-if="job.status === 'done' && r" class="result">
      <div class="row wrap">
        <a class="btn primary" :href="api.epubUrl(job.id)" :download="r.file">↓ Download EPUB</a>
        <span class="small"><b>{{ r.file }}</b> <span class="muted">· {{ formatSize(r.size) }} · {{ r.pages }} pages · {{ r.chapters }} chapter{{ r.chapters === 1 ? '' : 's' }} · {{ formatSeconds(job.elapsed) }}</span></span>
      </div>
      <div class="row small saved">
        <span class="muted">Saved to</span>
        <code :title="r.path">{{ r.path }}</code>
        <button type="button" class="link-btn" @click="copyPath">Copy</button>
      </div>
      <p v-if="r.valid === true" class="small ok">✓ Valid EPUB 3 (checked with epubcheck)</p>
      <p v-else-if="r.valid === null" class="small muted">Not validated — install Java to check EPUBs with epubcheck.</p>
      <details v-else class="notice err small">
        <summary>epubcheck found problems — the book may not open in every reader</summary>
        <pre>{{ r.epubcheck }}</pre>
      </details>
      <div v-if="r.attention.length" class="attention">
        <h4>Worth a look</h4>
        <ul>
          <li v-for="a in r.attention" :key="a">{{ a }}</li>
        </ul>
      </div>
      <div v-if="reviewFailed.length" class="attention">
        <h4>{{ reviewFailed.length }} page{{ reviewFailed.length === 1 ? '' : 's' }} not reviewed by the AI</h4>
        <p class="small">The AI request for these pages failed, so they keep the OCR text as is.</p>
        <details class="small">
          <summary>Why</summary>
          <ul>
            <li v-for="f in reviewFailed" :key="f.page">p{{ f.page }} — {{ f.reason }}</li>
          </ul>
        </details>
        <button type="button" class="btn" @click="retryReview">Retry failed pages</button>
      </div>
      <p v-if="r.work_dir" class="muted small">Intermediate files kept: <code>{{ r.work_dir }}</code> — inspect with <code>pdf2epub dev</code></p>
    </div>

    <!-- paused: progress is saved after every page -->
    <div v-if="job.status === 'paused'" class="notice" :class="{ err: job.reason.startsWith('llm-unreachable') }">
      <p><b>{{ pauseReason.title }}</b><span v-if="done" class="muted"> · {{ done }}</span></p>
      <p class="small">{{ pauseReason.hint }} It continues from the last finished page; pages whose AI review failed are tried again.</p>
      <pre v-if="pauseReason.detail">{{ pauseReason.detail }}</pre>
    </div>

    <!-- failed -->
    <div v-if="job.status === 'failed' && job.error" class="notice err">
      <p><b>{{ job.error.message }}</b></p>
      <pre v-if="job.error.hint">{{ job.error.hint }}</pre>
      <p class="small">
        After fixing the cause, retry from the step that failed (or any step in the list) to keep the work before it.
        Converting again reuses the AI review results already done.
      </p>
    </div>

    <footer class="row wrap">
      <button
        v-if="job.steps.length && !detailed"
        type="button"
        class="btn ghost toggle"
        :aria-expanded="showSteps"
        :aria-controls="`steps-${job.id}`"
        @click="showSteps = !showSteps"
      >
        <span class="chev" :class="{ open: showSteps }" aria-hidden="true">›</span>
        {{ showSteps ? 'Hide steps' : `Show all steps (${job.steps.length})` }}
      </button>
      <span class="spacer" />
      <template v-if="active">
        <button
          v-if="job.status === 'running' || job.status === 'queued'"
          type="button"
          class="btn"
          title="Saves the progress and stops after the current page"
          @click="pause"
        >Pause</button>
        <button type="button" class="btn" :disabled="job.status === 'cancelling'" @click="stop">Stop</button>
      </template>
      <template v-else-if="job.status === 'paused'">
        <button type="button" class="btn primary" @click="resume">Resume</button>
        <button v-if="stuckStep" type="button" class="btn" title="Redo this step and every step after it" @click="retryFrom(stuckStep)">Retry {{ stuckStep.label.toLowerCase() }}</button>
        <button type="button" class="btn ghost" title="Discards the saved progress" @click="remove">Remove</button>
      </template>
      <template v-else>
        <button v-if="stuckStep" type="button" class="btn primary" title="Redo this step and every step after it" @click="retryFrom(stuckStep)">Retry {{ stuckStep.label.toLowerCase() }}</button>
        <button v-if="job.status !== 'done'" type="button" class="btn" @click="again">Convert again</button>
        <button type="button" class="btn ghost" @click="remove">Remove</button>
      </template>
    </footer>
    <h4 v-if="detailed && job.steps.length" class="steps-title">Steps</h4>
    <ol v-if="showSteps" :id="`steps-${job.id}`" class="steps">
      <li v-for="s in job.steps" :key="s.key" :class="s.status">
        <span class="icon" aria-hidden="true">
          <span v-if="s.status === 'running'" class="spinner" />
          <template v-else>{{ ICONS[s.status] }}</template>
        </span>
        <span class="name">
          <span :title="s.status === 'skipped' ? 'Not needed this time' : undefined">{{ s.label }}</span>
          <button
            v-if="!active && job.retry_steps.includes(s.key)"
            type="button"
            class="link-btn retry"
            :title="`Redo ${s.label.toLowerCase()} and every step after it`"
            @click="retryFrom(s)"
          >Retry from here</button>
        </span>
        <span class="count muted">{{ s.total ? `${s.done}/${s.total}` : '' }}</span>
        <span class="time muted">{{ s.seconds != null ? formatSeconds(s.seconds) : '' }}</span>
        <span v-if="s.note" class="note muted">{{ s.note }}</span>
      </li>
    </ol>
  </article>
</template>

<style scoped>
.job { display: grid; gap: 1rem; }
.top { display: flex; align-items: flex-start; gap: 0.75rem; margin: 0 !important; }
.title { flex: 1; min-width: 0; display: grid; gap: 0.1rem; }
.title h3 { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.now { display: grid; gap: 0.45rem; }
.indeterminate span { width: 30%; animation: slide 1.4s ease-in-out infinite; }
@keyframes slide { from { transform: translateX(-100%); } to { transform: translateX(340%); } }
.result { display: grid; gap: 0.6rem; }
.saved { min-width: 0; }
.saved code { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; color: var(--muted); }
.ok { color: var(--ok); }
.attention { background: var(--warn-soft); border-radius: 8px; padding: 0.6rem 0.85rem; font-size: 0.86rem; }
.attention h4 { margin: 0 0 0.25rem; font-size: 0.86rem; color: var(--warn); }
.attention ul { margin: 0; padding-left: 1.1rem; }
.attention p, .attention details { margin: 0 0 0.35rem; }
.attention .btn { margin-top: 0.35rem; }
pre { white-space: pre-wrap; word-break: break-word; margin: 0.4rem 0 0; font-size: 0.78rem; max-height: 14rem; overflow: auto; }
footer { border-top: 1px solid var(--line); padding-top: 0.75rem; }
.toggle { padding-left: 0; font-size: 0.86rem; }
.chev { display: inline-block; transition: transform 0.15s; font-size: 1.1em; }
.chev.open { transform: rotate(90deg); }
.steps-title { margin: 0; font-size: 0.86rem; color: var(--muted); }
.steps { list-style: none; padding: 0; margin: -0.4rem 0 0; display: grid; gap: 0.15rem; font-size: 0.86rem; }
.steps li { display: grid; grid-template-columns: 1.3rem 1fr auto 3.5rem; gap: 0.5rem; align-items: center; padding: 0.15rem 0; }
.steps .note { grid-column: 2 / -1; font-size: 0.8rem; margin-top: -0.2rem; }
.steps .retry { margin-left: 0.6rem; font-size: 0.8rem; opacity: 0; }
.steps li:hover .retry, .steps .retry:focus-visible, .steps li.failed .retry, .steps li.paused .retry { opacity: 1; }
@media (hover: none) { .steps .retry { opacity: 1; } }
.steps .time, .steps .count { text-align: right; font-variant-numeric: tabular-nums; }
.steps li.done .icon { color: var(--ok); }
.steps li.failed .icon { color: var(--err); }
.steps li.paused .icon { color: var(--warn); }
.steps li.pending, .steps li.skipped { color: var(--muted); }
</style>
