<script setup lang="ts">
import { computed, onMounted, reactive, ref, toRef, watch } from 'vue'
import { api, formatSize, hostOf, type ConvertOptions } from './api'
import { refreshJobs } from './jobs'
import LangPicker from './LangPicker.vue'
import { errorText, loadSystem, store, toast } from './store'
import Switch from './Switch.vue'

const emit = defineEmits<{ settings: []; created: [id: string] }>()
const st = computed(() => store.state!)

// OCR engine choices: Apple Vision only where macOS provides it; "auto" resolves to the recommended one.
const engines = computed(() => store.system?.ocr_engines)
const appleAvailable = computed(() => !!engines.value?.available.includes('apple'))
const recommendedLabel = computed(() => (engines.value?.recommended === 'apple' ? 'Apple Vision' : 'PaddleOCR'))
const usesPaddle = (choice: string) => (choice === 'auto' ? engines.value?.recommended !== 'apple' : choice === 'paddle')

// ---- file ----
const file = toRef(store, 'pending')
const uploading = ref<{ name: string; size: number; progress: number } | null>(null)
const dragging = ref(false)
const picker = ref<HTMLInputElement>()

async function take(files: FileList | null | undefined) {
  const f = files?.[0]
  if (!f) return
  if (!/\.pdf$/i.test(f.name) && f.type !== 'application/pdf') {
    toast(`${f.name} is not a PDF`, 'err')
    return
  }
  const previous = file.value
  file.value = null
  uploading.value = { name: f.name, size: f.size, progress: 0 }
  try {
    file.value = await api.upload(f, (p) => uploading.value && (uploading.value.progress = p))
    if (previous) api.removeUpload(previous.id).catch(() => {})
    options.output_name = ''
  } catch (e) {
    toast(errorText(e), 'err')
  } finally {
    uploading.value = null
    if (picker.value) picker.value.value = ''
  }
}
function onDrop(e: DragEvent) {
  dragging.value = false
  take(e.dataTransfer?.files)
}
function clearFile() {
  if (file.value) api.removeUpload(file.value.id).catch(() => {})
  file.value = null
}

// ---- options (start from the saved defaults) ----
const v = st.value.values
const options = reactive<ConvertOptions>({
  langs: [...v.langs],
  llm: v['llm.enabled'],
  review_policy: v['llm.review_policy'] === 'all' ? 'all' : 'flagged',
  output_name: '',
  keep_work: v.keep_work,
  use_gpu: v.use_gpu,
  ocr_det_model: v.ocr_det_model,
  ocr_engine: v.ocr_engine,
  vector_dpi: v.vector_dpi,
})
const defaultName = computed(() =>
  file.value ? file.value.name.replace(/\.pdf$/i, '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') + '.epub' : 'book.epub',
)
// The chosen file and options survive a page refresh (this browser tab only; the PDF stays on the server).
const DRAFT_KEY = 'pdf2epub.convert'
function saveDraft() {
  try {
    if (file.value) sessionStorage.setItem(DRAFT_KEY, JSON.stringify({ upload_id: file.value.id, options }))
    else sessionStorage.removeItem(DRAFT_KEY)
  } catch { /* storage unavailable: only the refresh convenience is lost */ }
}
watch([file, options], saveDraft, { deep: true })
onMounted(async () => {
  if (file.value) return
  let draft: { upload_id: string; options: ConvertOptions } | null = null
  try {
    draft = JSON.parse(sessionStorage.getItem(DRAFT_KEY) ?? 'null')
  } catch { /* ignore */ }
  if (!draft?.upload_id) return
  try {
    const up = await api.getUpload(draft.upload_id)
    if (!file.value) {
      Object.assign(options, draft.options)
      file.value = up
    }
  } catch {
    saveDraft() // gone (server restarted or converted elsewhere): forget it
  }
})

const llmLine = computed(() => `${v['llm.model'] || 'server default model'} at ${hostOf(v['llm.base_url'])}`)

const submitting = ref(false)
async function convert() {
  if (!file.value) return
  submitting.value = true
  try {
    const job = await api.convert(file.value.id, { ...options, langs: [...options.langs] })
    file.value = null
    await refreshJobs()
    emit('created', job.id)
  } catch (e) {
    toast(errorText(e), 'err')
  } finally {
    submitting.value = false
  }
}

onMounted(() => { if (!store.system) loadSystem() }) // OCR engines on this computer
</script>

<template>
  <div class="stack">
    <section class="card">
      <header><h2>1. Choose a PDF</h2></header>

      <div
        v-if="!file"
        class="drop"
        :class="{ dragging, busy: uploading }"
        @dragover.prevent="dragging = true"
        @dragleave.prevent="dragging = false"
        @drop.prevent="onDrop"
      >
        <template v-if="uploading">
          <p><b>{{ uploading.name }}</b> <span class="muted">· {{ formatSize(uploading.size) }}</span></p>
          <div class="bar up" role="progressbar" :aria-valuenow="Math.round(uploading.progress * 100)" aria-valuemin="0" aria-valuemax="100">
            <span :style="{ width: uploading.progress * 100 + '%' }" />
          </div>
          <p class="muted small">{{ uploading.progress < 1 ? 'Uploading…' : 'Reading the PDF…' }}</p>
        </template>
        <template v-else>
          <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
            <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5M12 17v-6M9.5 13.5 12 11l2.5 2.5" />
          </svg>
          <p><b>Drop a PDF here</b> or <button type="button" class="link-btn" @click="picker?.click()">browse your files</button></p>
          <p class="muted small">Scanned or digital · stays on this computer</p>
        </template>
        <input ref="picker" type="file" accept="application/pdf,.pdf" class="sr-only" tabindex="-1" @change="take(($event.target as HTMLInputElement).files)" />
      </div>

      <div v-else class="picked" @dragover.prevent @drop.prevent="onDrop">
        <span class="doc" aria-hidden="true">PDF</span>
        <div class="meta">
          <b :title="file.name">{{ file.name }}</b>
          <span class="muted small">{{ file.pages }} page{{ file.pages === 1 ? '' : 's' }}<template v-if="file.title && file.title !== file.name.replace(/\.pdf$/i, '')"> · “{{ file.title }}”</template></span>
        </div>
        <button type="button" class="btn ghost" @click="picker?.click()">Replace</button>
        <button type="button" class="btn ghost" aria-label="Remove file" @click="clearFile">✕</button>
        <input ref="picker" type="file" accept="application/pdf,.pdf" class="sr-only" tabindex="-1" @change="take(($event.target as HTMLInputElement).files)" />
      </div>
    </section>

    <section class="card">
      <header><h2>2. Options</h2><p>Defaults come from Settings.</p></header>
      <div class="stack">
        <div class="field">
          <label for="c-langs">Languages in the book</label>
          <LangPicker id="c-langs" v-model="options.langs" :languages="st.languages" />
          <span class="hint">Faster and more accurate when set. Leave empty to detect automatically.</span>
        </div>

        <div class="ai">
          <Switch v-model="options.llm" label="AI page review" :hint="options.llm ? `Uses ${llmLine}` : 'Off — fastest; fine for clean digital PDFs.'" />
          <div v-if="options.llm" class="ai-more">
            <div class="segmented" role="radiogroup" aria-label="Pages to review">
              <label><input v-model="options.review_policy" type="radio" value="flagged" />Pages that need it</label>
              <label><input v-model="options.review_policy" type="radio" value="all" />Every page</label>
            </div>
            <button type="button" class="link-btn small" @click="emit('settings')">Change model</button>
          </div>
        </div>

        <div class="field">
          <label for="c-name">File name</label>
          <input id="c-name" v-model.trim="options.output_name" type="text" :placeholder="defaultName" spellcheck="false" />
          <span class="hint">Saved to <code>{{ v.out_dir }}</code>. You can also download it here when it's done.</span>
        </div>

        <details class="more">
          <summary>Advanced</summary>
          <div class="body stack">
            <Switch v-model="options.use_gpu" label="Use the graphics card (GPU)" hint="NVIDIA GPUs only; falls back to the CPU. Apple Vision uses the Mac's GPU by itself." />
            <Switch v-model="options.keep_work" label="Keep intermediate files" hint="To inspect pages afterwards with `pdf2epub dev`." />
            <div class="grid-2">
              <div class="field">
                <label for="c-ocr">Text recognition (OCR)</label>
                <select id="c-ocr" v-model="options.ocr_engine">
                  <option value="auto">Automatic — {{ recommendedLabel }}</option>
                  <option v-if="appleAvailable" value="apple">Apple Vision</option>
                  <option value="paddle">PaddleOCR</option>
                </select>
              </div>
              <div v-if="usesPaddle(options.ocr_engine)" class="field">
                <label for="c-det">Text line detector</label>
                <select id="c-det" v-model="options.ocr_det_model">
                  <option value="auto">Automatic</option>
                  <option value="PP-OCRv5_mobile_det">Fast — mobile model</option>
                  <option value="PP-OCRv5_server_det">Accurate — server model</option>
                </select>
              </div>
              <div class="field">
                <label for="c-dpi">Render resolution (DPI)</label>
                <input id="c-dpi" v-model.number="options.vector_dpi" type="number" min="72" max="600" step="10" />
              </div>
            </div>
          </div>
        </details>
      </div>

      <div class="go">
        <button type="button" class="btn primary big" :disabled="!file || submitting" @click="convert">
          <span v-if="submitting" class="spinner" /> Convert to EPUB
        </button>
        <span v-if="!file" class="hint">Choose a PDF first.</span>
      </div>
    </section>
  </div>
</template>

<style scoped>
.drop {
  display: grid;
  justify-items: center;
  gap: 0.4rem;
  padding: 2.5rem 1rem;
  border: 2px dashed var(--line-strong);
  border-radius: var(--radius);
  text-align: center;
  color: var(--muted);
  transition: border-color 0.15s, background 0.15s;
}
.drop p { color: var(--ink); }
.drop p.muted { color: var(--muted); }
.drop.dragging { border-color: var(--accent); background: var(--accent-soft); }
.drop .up { width: min(320px, 80%); margin: 0.4rem 0; }
.picked { display: flex; align-items: center; gap: 0.75rem; }
.doc {
  flex: none; width: 2.6rem; height: 3.1rem; border-radius: 6px; display: grid; place-items: center;
  background: var(--accent-soft); color: var(--accent); font-size: 0.7rem; font-weight: 700; letter-spacing: 0.04em;
}
.meta { flex: 1; min-width: 0; display: grid; }
.meta b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ai { display: grid; gap: 0.75rem; }
.ai-more .link-btn { font-size: 0.85rem; }
.ai-more { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; padding-left: 3rem; }
.go { display: flex; align-items: center; gap: 1rem; margin-top: 1.5rem; padding-top: 1.25rem; border-top: 1px solid var(--line); }
@media (max-width: 520px) { .ai-more { padding-left: 0; } }
</style>
