<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useDraft } from './draft'
import LangPicker from './LangPicker.vue'
import LlmForm from './LlmForm.vue'
import { errorText, store, toast } from './store'
import SystemCheck from './SystemCheck.vue'

const emit = defineEmits<{ done: [] }>()
const d = useDraft()
const st = computed(() => store.state!)

const steps = computed(() => [
  { id: 'system', title: 'Check this computer' },
  { id: 'mode', title: 'AI page review' },
  ...(d.values['llm.enabled'] ? [{ id: 'llm', title: 'Connect a model' }] : []),
  { id: 'defaults', title: 'Defaults' },
])
const index = ref(0)
const step = computed(() => steps.value[index.value]!)
const last = computed(() => index.value === steps.value.length - 1)
// OCR engine: preselect the best one for this computer (Apple Vision on a Mac); the user may switch.
const engines = computed(() => store.system?.ocr_engines)
const engineChoice = computed(() => (engines.value?.available.length ?? 0) > 1)
watch(
  engines,
  (e) => { if (e && d.values.ocr_engine === 'auto') d.values.ocr_engine = e.recommended },
  { immediate: true },
)
const blocked = computed(() => step.value.id === 'system' && store.system !== null && (!store.system.node_ok || !store.system.paddle))

async function finish(skip = false) {
  try {
    await d.save(skip ? ['llm.enabled'] : ['llm.enabled', 'llm.base_url', 'llm.model', 'langs', 'ocr_engine'])
    toast(skip ? 'Setup skipped — you can change everything in Settings.' : 'All set. Add a PDF to convert it.')
    emit('done')
  } catch (e) {
    toast(errorText(e), 'err')
  }
}
</script>

<template>
  <div class="wizard">
    <div class="intro">
      <h1>Welcome to pdf2epub</h1>
      <p class="muted">A few choices and you're ready to turn PDFs into EPUB books. You can change them later in Settings.</p>
    </div>

    <ol class="progress" aria-label="Setup steps">
      <li v-for="(s, i) in steps" :key="s.id" :class="{ active: i === index, done: i < index }" :aria-current="i === index ? 'step' : undefined">
        <span class="dot">{{ i < index ? '✓' : i + 1 }}</span><span class="t">{{ s.title }}</span>
      </li>
    </ol>

    <section class="card">
      <template v-if="step.id === 'system'">
        <header><h2>Check this computer</h2></header>
        <SystemCheck />
        <div v-if="engineChoice" class="engine">
          <h3>Text recognition engine</h3>
          <div class="choices" role="radiogroup">
            <label class="choice" :class="{ on: d.values.ocr_engine === 'apple' }">
              <input v-model="d.values.ocr_engine" type="radio" value="apple" name="ocr" />
              <b>{{ engines!.label || 'Apple Vision' }} <span class="badge accent">best for this Mac</span></b>
              <span class="muted small">Built into macOS, nothing to download. Uses the Neural Engine and GPU on
                Apple silicon. Scripts it can't read (Hindi, Tamil, Greek…) still go to PaddleOCR.</span>
            </label>
            <label class="choice" :class="{ on: d.values.ocr_engine === 'paddle' }">
              <input v-model="d.values.ocr_engine" type="radio" value="paddle" name="ocr" />
              <b>PaddleOCR</b>
              <span class="muted small">The engine used on Windows and Linux. Runs on the CPU on a Mac (slower).</span>
            </label>
          </div>
        </div>
        <p v-if="blocked" class="notice err" style="margin-top: 1rem">Fix the items marked ✕ first, then click <b>Check again</b>.</p>
      </template>

      <template v-else-if="step.id === 'mode'">
        <header><h2>Should an AI model check the pages?</h2></header>
        <div class="choices" role="radiogroup">
          <label class="choice" :class="{ on: d.values['llm.enabled'] }">
            <input v-model="d.values['llm.enabled']" type="radio" :value="true" name="mode" />
            <b>Yes, review with AI <span class="badge accent">best quality</span></b>
            <span class="muted small">A vision model compares every page with the original and fixes OCR mistakes,
              joins two-page pictures and reads special marks. Works with local servers (llama.cpp, LM Studio, Ollama)
              or cloud APIs (OpenAI, Gemini, OpenRouter, DeepSeek…).</span>
          </label>
          <label class="choice" :class="{ on: !d.values['llm.enabled'] }">
            <input v-model="d.values['llm.enabled']" type="radio" :value="false" name="mode" />
            <b>No, convert without AI <span class="badge">fastest</span></b>
            <span class="muted small">Good for clean, born-digital PDFs. You can turn AI review on per conversion later.</span>
          </label>
        </div>
      </template>

      <template v-else-if="step.id === 'llm'">
        <header><h2>Connect a model</h2><p>Pick where the model runs, then test the connection.</p></header>
        <LlmForm
          v-model:base-url="d.values['llm.base_url']"
          v-model:model="d.values['llm.model']"
          v-model:api-key="d.apiKey.value"
          :providers="st.providers"
          :api-key-set="st.api_key_set"
          :keyring="st.keyring"
          :env-overrides="st.env_overrides"
          :extra-body="d.values['llm.extra_body']"
        />
      </template>

      <template v-else>
        <header><h2>Defaults</h2><p>Used for every conversion unless you change them there.</p></header>
        <div class="stack">
          <div class="field">
            <label for="setup-langs">Book languages</label>
            <LangPicker id="setup-langs" v-model="d.values.langs" :languages="st.languages" />
            <span class="hint">Picking the languages you usually convert makes text recognition faster. Leave empty to detect automatically.</span>
          </div>
          <div class="field">
            <label for="setup-out">Save EPUBs to</label>
            <input id="setup-out" v-model.trim="d.values.out_dir" type="text" spellcheck="false" />
            <span class="hint">A folder on this computer. Each book can also be downloaded from the browser.</span>
          </div>
        </div>
      </template>

      <footer class="nav">
        <button v-if="index > 0" type="button" class="btn" @click="index--">Back</button>
        <button v-else type="button" class="btn ghost" :disabled="d.saving.value" @click="finish(true)">Skip setup</button>
        <span class="spacer" />
        <button v-if="!last" type="button" class="btn primary" :disabled="blocked" @click="index++">Continue</button>
        <button v-else type="button" class="btn primary" :disabled="d.saving.value" @click="finish()">
          <span v-if="d.saving.value" class="spinner" /> Finish setup
        </button>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.wizard { display: grid; gap: 1.25rem; }
.intro { display: grid; gap: 0.35rem; }
.progress { list-style: none; display: flex; gap: 0.5rem; padding: 0; margin: 0; flex-wrap: wrap; }
.progress li { display: flex; align-items: center; gap: 0.45rem; color: var(--muted); font-size: 0.85rem; }
.progress li + li::before { content: ''; width: 1.5rem; height: 1px; background: var(--line-strong); margin-right: 0.1rem; }
.dot {
  width: 1.4rem; height: 1.4rem; border-radius: 50%; display: grid; place-items: center;
  border: 1px solid var(--line-strong); font-size: 0.75rem; font-weight: 600;
}
.progress li.active { color: var(--ink); font-weight: 560; }
.progress li.active .dot { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.progress li.done .dot { background: var(--accent-soft); border-color: var(--accent-soft); color: var(--accent); }
.choices { display: grid; gap: 0.75rem; }
.choice {
  display: grid; gap: 0.3rem; padding: 0.9rem 1rem 0.9rem 2.6rem; position: relative;
  border: 1px solid var(--line-strong); border-radius: var(--radius); cursor: pointer;
}
.choice.on { border-color: var(--accent); background: color-mix(in srgb, var(--accent-soft) 45%, transparent); }
.choice input { position: absolute; left: 1rem; top: 1.05rem; accent-color: var(--accent); }
.choice b { display: flex; align-items: center; gap: 0.5rem; }
.engine { margin-top: 1.25rem; display: grid; gap: 0.6rem; }
.engine h3 { margin: 0; font-size: 0.95rem; }
.nav { display: flex; gap: 0.6rem; margin-top: 1.5rem; padding-top: 1rem; border-top: 1px solid var(--line); }
@media (max-width: 560px) { .progress .t { display: none; } .progress li.active .t { display: inline; } }
</style>
