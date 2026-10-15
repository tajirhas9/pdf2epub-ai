<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, type PaddleInstall } from './api'
import { errorText, loadSystem, store, toast } from './store'

const checking = ref(false)
async function recheck() {
  checking.value = true
  await loadSystem()
  checking.value = false
}

// PaddlePaddle: `pdf2epub ui` starts the install in the background; this step shows it (or starts it).
const install = ref<PaddleInstall | null>(null)
let poll: ReturnType<typeof setInterval> | undefined
function follow(st: PaddleInstall) {
  install.value = st
  clearInterval(poll)
  if (!st.running) return
  poll = setInterval(async () => {
    const next = await api.paddleStatus().catch(() => null)
    if (!next) return
    install.value = next
    if (!next.running) {
      clearInterval(poll)
      await loadSystem()
      if (next.error) toast('PaddlePaddle could not be installed — see the details below', 'err')
    }
  }, 1000)
}
async function startInstall(want: 'auto' | 'gpu' | 'cpu' = 'auto') {
  try {
    follow(await api.installPaddle(want))
  } catch (e) {
    toast(errorText(e), 'err')
  }
}
onMounted(async () => {
  if (!store.system) await loadSystem()
  const s = store.system
  if (!s) return
  follow(s.paddle_install)
  const auto = !s.paddle && s.paddle_auto && !s.paddle_manual && s.paddle_choice.build
  if (auto && !s.paddle_install.running && !s.paddle_install.done) startInstall()
})
onUnmounted(() => clearInterval(poll))

const installing = computed(() => !!install.value?.running)
const failed = computed(() => !installing.value && !!install.value?.error)
const problems = computed(
  () => !!store.system && !installing.value && (!store.system.node_ok || !store.system.paddle),
)

type Row = { name: string; level: 'ok' | 'warn' | 'err' | 'info' | 'busy'; text: string }
function paddleRow(): Row {
  const s = store.system!
  const st = install.value
  if (st?.running) {
    const what = s.paddle_choice.build ? `${s.paddle_choice.build.label} build` : 'best build'
    return { name: 'OCR engine', level: 'busy', text: `Installing PaddlePaddle (${what}) — one time, a few hundred MB…` }
  }
  if (s.paddle && s.paddle_installed) {
    const gpu = s.paddle_installed.package === 'paddlepaddle-gpu'
    const restart = st?.done && !st.error && st.restart ? ' — restart pdf2epub ui to use the new build' : ''
    return { name: 'OCR engine', level: 'ok', text: `PaddlePaddle ${s.paddle_installed.version} (${gpu ? 'GPU' : 'CPU'} build)${restart}` }
  }
  if (s.paddle) return { name: 'OCR engine', level: 'ok', text: 'PaddlePaddle installed' }
  if (!s.paddle_choice.build) return { name: 'OCR engine', level: 'err', text: s.paddle_choice.reason }
  if (s.paddle_manual) return { name: 'OCR engine', level: 'err', text: 'PaddlePaddle missing — install it with the command below' }
  if (st?.error) return { name: 'OCR engine', level: 'err', text: 'PaddlePaddle could not be installed' }
  return { name: 'OCR engine', level: 'err', text: 'PaddlePaddle missing' }
}

const rows = computed<Row[]>(() => {
  const s = store.system
  if (!s) return []
  const gpuName = s.hardware.gpus[0]
  const apple = s.ocr_engines.recommended === 'apple'
  return [
    {
      name: 'Node.js',
      level: s.node_ok ? 'ok' : 'err',
      text: s.node_ok
        ? `${s.node} — builds the EPUB`
        : `${s.node ? `${s.node} is too old` : 'Not found'} — install Node.js ${s.node_min} or newer from nodejs.org`,
    },
    paddleRow(),
    apple
      ? { name: 'Text recognition', level: 'ok', text: s.ocr_engines.reason }
      : {
          name: 'Graphics card',
          level: s.gpu ? 'ok' : 'warn',
          text: s.gpu
            ? `${gpuName ?? 'GPU'} in use — fast`
            : `${s.paddle_choice.reason} — works, but scanned pages take a few seconds each`,
        },
    {
      name: 'This computer',
      level: 'info',
      text: `${s.hardware.cpus} CPU cores · ${s.hardware.ram_gb} GB memory — pages are processed in parallel`,
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
const ICON = { ok: '✓', warn: '!', err: '✕', info: '○', busy: '' }
</script>

<template>
  <div v-if="!store.system" class="row muted small"><span class="spinner" /> Checking this computer…</div>
  <template v-else>
    <ul class="checks">
      <li v-for="r in rows" :key="r.name">
        <span class="icon" :class="r.level" aria-hidden="true">
          <span v-if="r.level === 'busy'" class="spinner" />
          <template v-else>{{ ICON[r.level] }}</template>
        </span>
        <span class="name">{{ r.name }}</span>
        <span class="text" :class="{ muted: r.level === 'info' }">{{ r.text }}</span>
      </li>
    </ul>

    <div v-if="store.system.paddle_manual && !store.system.paddle" class="notice small">
      Run this in a terminal, then click <b>Check again</b>:
      <pre>{{ store.system.paddle_manual }}</pre>
    </div>

    <details v-if="install && install.log.length && (installing || failed)" class="notice small" :class="{ err: failed }" :open="failed">
      <summary>{{ failed ? install.error : 'Install progress' }}</summary>
      <pre>{{ install.log.slice(-12).join('\n') }}</pre>
    </details>

    <div v-if="!installing && (failed || store.system.paddle_upgrade)" class="row recheck">
      <span class="hint">
        {{ failed ? 'Try again, or pick the CPU build (works everywhere).' : 'A GPU build fits this computer — much faster OCR.' }}
      </span>
      <button v-if="failed" type="button" class="btn ghost" @click="startInstall('cpu')">Install CPU build</button>
      <button type="button" class="btn" @click="startInstall(failed ? 'auto' : 'gpu')">
        {{ failed ? 'Try again' : 'Switch to GPU build' }}
      </button>
    </div>

    <div v-if="problems" class="row recheck">
      <span class="hint">Installed something? No restart needed.</span>
      <button type="button" class="btn" :disabled="checking" @click="recheck">
        <span v-if="checking" class="spinner" /> Check again
      </button>
    </div>
  </template>
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
.recheck { justify-content: flex-end; flex-wrap: wrap; margin-top: 0.75rem; }
.name { font-weight: 560; }
.icon { font-weight: 700; }
.icon.ok { color: var(--ok); }
.icon.warn { color: var(--warn); }
.icon.err { color: var(--err); }
.icon.info { color: var(--muted); }
.notice { margin-top: 0.75rem; }
pre { white-space: pre-wrap; word-break: break-all; margin: 0.5rem 0 0; font-size: 0.78rem; max-height: 14rem; overflow: auto; }
@media (max-width: 520px) {
  li { grid-template-columns: 1.5rem 1fr; }
  .text { grid-column: 2; }
}
</style>
