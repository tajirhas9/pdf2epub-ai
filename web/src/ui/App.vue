<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { hostOf } from './api'
import ConversionsView from './ConversionsView.vue'
import ConvertView from './ConvertView.vue'
import { isActive, jobs, refreshJobs } from './jobs'
import JobView from './JobView.vue'
import { canLeave } from './leave'
import SettingsView from './SettingsView.vue'
import SetupWizard from './SetupWizard.vue'
import { loadState, store } from './store'

const REPO_URL = 'https://github.com/tajirhas9/pdf2epub-ai'

// Hash routes: (none) convert · #conversions · #conversions/<id> · #settings · #setup
type View = 'convert' | 'conversions' | 'job' | 'settings' | 'setup'
function fromHash(): { view: View; id: string } {
  const [head = '', id = ''] = location.hash.slice(1).split('/')
  if (head === 'conversions') return id ? { view: 'job', id } : { view: 'conversions', id: '' }
  return { view: ['settings', 'setup'].includes(head) ? (head as View) : 'convert', id: '' }
}
const view = ref<View>(fromHash().view)
const jobId = ref(fromHash().id)
const hashOf = (v: View, id: string) => (v === 'convert' ? '' : v === 'job' ? `#conversions/${id}` : `#${v}`)

function go(next: View, id = '') {
  if ((next === view.value && id === jobId.value) || !canLeave()) return
  view.value = next
  jobId.value = id
  history.pushState(null, '', hashOf(next, id) || location.pathname)
  window.scrollTo({ top: 0 })
}
function sync() {
  const r = fromHash()
  view.value = r.view
  jobId.value = r.id
}
window.addEventListener('popstate', sync)
window.addEventListener('hashchange', sync)

onMounted(loadState)
onMounted(refreshJobs) // polls from then on, so status toasts show on every page
const running = computed(() => jobs.list.filter(isActive).length)
watch(
  () => store.state?.configured,
  (configured) => configured === false && (view.value = 'setup'),
)

const llm = computed(() => {
  const v = store.state?.values
  if (!v) return ''
  return v['llm.enabled'] ? `AI review: ${v['llm.model'] || hostOf(v['llm.base_url'])}` : 'AI review off'
})
</script>

<template>
  <div class="app">
    <header class="top">
      <div class="inner">
        <a class="brand" href="#" @click.prevent="go('convert')">
          <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden="true">
            <rect x="6" y="3" width="20" height="26" rx="3" fill="var(--accent)" />
            <path d="M11 10h10M11 15h10M11 20h6" stroke="var(--panel)" stroke-width="2" stroke-linecap="round" />
          </svg>
          pdf2epub
        </a>
        <nav v-if="store.state && view !== 'setup'" aria-label="Main">
          <button type="button" :aria-current="view === 'convert' ? 'page' : undefined" @click="go('convert')">Convert</button>
          <button type="button" :aria-current="view === 'conversions' || view === 'job' ? 'page' : undefined" @click="go('conversions')">
            Conversions<span v-if="running" class="count" :title="`${running} in progress`">{{ running }}</span>
          </button>
          <button type="button" :aria-current="view === 'settings' ? 'page' : undefined" @click="go('settings')">Settings</button>
        </nav>
        <span class="spacer" />
        <button v-if="store.state && view !== 'setup'" type="button" class="status" title="Change in Settings" @click="go('settings')">
          <span class="led" :class="{ on: store.state.values['llm.enabled'] }" aria-hidden="true" />{{ llm }}
        </button>
      </div>
    </header>

    <main>
      <div v-if="store.loadError" class="card stack">
        <h2>Can't open pdf2epub</h2>
        <p>{{ store.loadError }}</p>
        <p class="muted small">Start it with <code>pdf2epub ui</code> and use the link it prints.</p>
      </div>
      <div v-else-if="!store.state" class="row muted"><span class="spinner" /> Loading…</div>
      <SetupWizard v-else-if="view === 'setup'" @done="go('convert')" />
      <SettingsView v-else-if="view === 'settings'" @setup="go('setup')" />
      <ConversionsView v-else-if="view === 'conversions'" @convert="go('convert')" />
      <JobView v-else-if="view === 'job'" :id="jobId" @back="go('conversions')" @open="(id) => go('job', id)" />
      <ConvertView v-else @settings="go('settings')" @created="(id) => go('job', id)" />
    </main>

    <footer class="credit">
      Developed by <a href="https://tajirhasnain.com" target="_blank" rel="noopener">tajirhas9</a>
      <span aria-hidden="true">·</span>
      <a :href="REPO_URL" target="_blank" rel="noopener" class="gh">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
          <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
        </svg>
        Source on GitHub
      </a>
    </footer>

    <Transition name="toast">
      <div v-if="store.toast" class="toast" :class="store.toast.kind" role="status">{{ store.toast.text }}</div>
    </Transition>
  </div>
</template>

<style scoped>
.top {
  position: sticky;
  top: 0;
  z-index: 10;
  background: color-mix(in srgb, var(--panel) 88%, transparent);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--line);
}
.inner, main { max-width: 760px; margin: 0 auto; padding-inline: 1rem; }
.inner { display: flex; align-items: center; gap: 1.5rem; height: 3.5rem; }
.brand { display: flex; align-items: center; gap: 0.5rem; font-weight: 680; color: var(--ink); text-decoration: none; letter-spacing: -0.01em; }
nav { display: flex; gap: 0.25rem; }
nav button {
  font: inherit; font-size: 0.92rem; background: none; border: 0; cursor: pointer; color: var(--muted);
  padding: 0.4rem 0.7rem; border-radius: 7px;
}
nav button:hover { color: var(--ink); background: var(--sunken); }
nav .count {
  margin-left: 0.35rem; padding: 0 0.4rem; border-radius: 999px; font-size: 0.75rem; font-weight: 600;
  background: var(--accent-soft); color: var(--accent);
}
nav button[aria-current='page'] { color: var(--ink); background: var(--sunken); font-weight: 560; }
.status {
  font: inherit; font-size: 0.8rem; color: var(--muted); background: none; border: 0; cursor: pointer;
  display: flex; align-items: center; gap: 0.4rem; max-width: 16rem; overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
}
.status:hover { color: var(--ink); }
.led { width: 7px; height: 7px; border-radius: 50%; background: var(--line-strong); flex: none; }
.led.on { background: var(--ok); }
main { padding-block: 1.75rem 2.5rem; }
.credit { text-align: center; color: var(--muted); font-size: 0.8rem; padding: 0 1rem 2rem; }
.credit a { color: inherit; text-underline-offset: 2px; }
.credit a:hover { color: var(--accent); }
.credit span { margin: 0 0.4rem; }
.credit .gh { display: inline-flex; align-items: center; gap: 0.3rem; vertical-align: bottom; }
.toast {
  position: fixed; left: 50%; bottom: 1.5rem; transform: translateX(-50%); z-index: 20;
  padding: 0.6rem 1rem; border-radius: 8px; font-size: 0.9rem; max-width: calc(100vw - 2rem);
  background: var(--ink); color: var(--bg); box-shadow: 0 6px 24px rgb(0 0 0 / 0.2);
}
.toast.err { background: var(--err); color: #fff; }
.toast-enter-active, .toast-leave-active { transition: opacity 0.2s, transform 0.2s; }
.toast-enter-from, .toast-leave-to { opacity: 0; transform: translate(-50%, 0.5rem); }
@media (max-width: 560px) {
  .status { display: none; }
  .inner { gap: 0.75rem; }
}
</style>
