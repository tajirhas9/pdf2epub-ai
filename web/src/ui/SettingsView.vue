<script setup lang="ts">
import { computed } from 'vue'
import { onBeforeRouteLeaveLike } from './leave'
import { useDraft } from './draft'
import LangPicker from './LangPicker.vue'
import LlmForm from './LlmForm.vue'
import { errorText, store, toast } from './store'
import Switch from './Switch.vue'
import SystemCheck from './SystemCheck.vue'

const emit = defineEmits<{ setup: [] }>()
const d = useDraft()
const v = d.values
const st = computed(() => store.state!)
const pinned = (key: string) => st.value.env_overrides.includes(key)
onBeforeRouteLeaveLike(() => d.dirty.value)

async function save() {
  try {
    await d.save()
    toast('Settings saved')
  } catch (e) {
    toast(errorText(e), 'err')
  }
}
</script>

<template>
  <form class="stack" @submit.prevent="save">
    <div class="intro">
      <h1>Settings</h1>
      <p class="muted small">Defaults for every conversion. The command line (<code>pdf2epub config</code>) reads and writes the same file.</p>
    </div>

    <p v-if="st.env_overrides.length" class="notice warn">
      Some settings are set by environment variables and override what you save here:
      <code>{{ st.env_overrides.join(', ') }}</code>
    </p>

    <section class="card">
      <header><h2>AI page review</h2></header>
      <div class="stack">
        <Switch
          v-model="v['llm.enabled']"
          label="Review pages with an AI model"
          hint="A vision model compares each page with the original and fixes mistakes. Slower; best for scans."
        />
        <template v-if="v['llm.enabled']">
          <LlmForm
            v-model:base-url="v['llm.base_url']"
            v-model:model="v['llm.model']"
            v-model:api-key="d.apiKey.value"
            v-model:clear-key="d.clearKey.value"
            :providers="st.providers"
            :api-key-set="st.api_key_set"
            :keyring="st.keyring"
            :env-overrides="st.env_overrides"
            :extra-body="v['llm.extra_body']"
            :extra-headers="v['llm.extra_headers']"
          />
          <details class="more">
            <summary>Advanced model options</summary>
            <div class="body stack">
              <div class="field">
                <span class="label" id="policy-label">Pages to review</span>
                <div class="segmented" role="radiogroup" aria-labelledby="policy-label">
                  <label><input v-model="v['llm.review_policy']" type="radio" value="flagged" />Only pages that need it</label>
                  <label><input v-model="v['llm.review_policy']" type="radio" value="all" />Every page</label>
                </div>
                <span class="hint">“Only pages that need it” skips pages the converter is already sure about — much faster.</span>
              </div>
              <div class="grid-2">
                <div class="field">
                  <label for="s-conc">Parallel requests</label>
                  <input id="s-conc" v-model.number="v['llm.concurrency']" type="number" min="1" max="64" />
                  <span class="hint">Use 1 for a local server with one slot.</span>
                </div>
                <div class="field">
                  <label for="s-max">Max reply length (tokens)</label>
                  <input id="s-max" v-model.number="v['llm.max_output_tokens']" type="number" min="256" step="256" />
                  <span class="hint">Stops runaway answers from small models.</span>
                </div>
                <div class="field">
                  <label for="s-timeout">Timeout per request (s)</label>
                  <input id="s-timeout" v-model.number="v['llm.timeout_s']" type="number" min="10" step="10" />
                </div>
              </div>
              <div class="field">
                <label for="s-body">Extra request fields (JSON)</label>
                <textarea id="s-body" v-model="d.json.body" spellcheck="false" :aria-invalid="d.jsonErrors.value.body"
                  placeholder='{"chat_template_kwargs": {"enable_thinking": false}}' />
                <span v-if="d.jsonErrors.value.body" class="hint err">Not a valid JSON object.</span>
                <span v-else class="hint">Provider-specific options, e.g. turn off “thinking” on local Qwen models, or <code>{"reasoning_effort": "low"}</code>.</span>
              </div>
              <div class="field">
                <label for="s-headers">Extra HTTP headers (JSON)</label>
                <textarea id="s-headers" v-model="d.json.headers" spellcheck="false" :aria-invalid="d.jsonErrors.value.headers"
                  placeholder='{"HTTP-Referer": "https://example.com"}' />
                <span v-if="d.jsonErrors.value.headers" class="hint err">Not a valid JSON object.</span>
              </div>
            </div>
          </details>
        </template>
      </div>
    </section>

    <section class="card">
      <header><h2>Conversion defaults</h2></header>
      <div class="stack">
        <div class="field">
          <label for="s-langs">Book languages</label>
          <LangPicker id="s-langs" v-model="v.langs" :languages="st.languages" />
          <span class="hint">Faster text recognition when set. Leave empty to detect automatically.</span>
        </div>
        <div class="field">
          <label for="s-out">Save EPUBs to</label>
          <input id="s-out" v-model.trim="v.out_dir" type="text" spellcheck="false" />
          <span v-if="pinned('out_dir')" class="hint warn">Set by PDF2EPUB_OUT_DIR in the environment.</span>
        </div>
        <Switch v-model="v.keep_work" label="Keep intermediate files" hint="For inspecting pages with `pdf2epub dev` after converting. Uses disk space." />
        <details class="more">
          <summary>Advanced conversion options</summary>
          <div class="body stack">
            <Switch v-model="v.use_gpu" label="Use the graphics card (GPU)" hint="Falls back to the CPU automatically when no NVIDIA GPU is available." />
            <div class="grid-2">
              <div class="field">
                <label for="s-det">Text line detector</label>
                <select id="s-det" v-model="v.ocr_det_model">
                  <option value="auto">Automatic (recommended)</option>
                  <option value="PP-OCRv5_mobile_det">Fast — mobile model</option>
                  <option value="PP-OCRv5_server_det">Accurate — server model</option>
                </select>
                <span class="hint">Automatic picks accurate on GPU and fast on CPU.</span>
              </div>
              <div class="field">
                <label for="s-dpi">Page render resolution (DPI)</label>
                <input id="s-dpi" v-model.number="v.vector_dpi" type="number" min="72" max="600" step="10" />
                <span class="hint">For digital PDFs. Higher is sharper but slower.</span>
              </div>
            </div>
          </div>
        </details>
      </div>
    </section>

    <section class="card">
      <header><h2>This computer</h2></header>
      <SystemCheck />
    </section>

    <section class="card">
      <header><h2>Settings file</h2></header>
      <div class="row wrap">
        <code class="path">{{ st.config_path }}</code>
        <span class="spacer" />
        <button type="button" class="btn" @click="emit('setup')">Run first-time setup again</button>
      </div>
    </section>

    <div class="savebar" :class="{ show: d.dirty.value }" role="region" aria-label="Unsaved changes">
      <span>You have unsaved changes</span>
      <span class="spacer" />
      <button type="button" class="btn ghost" @click="d.reset()">Discard</button>
      <button type="submit" class="btn primary" :disabled="!d.valid.value || d.saving.value">
        <span v-if="d.saving.value" class="spinner" /> Save changes
      </button>
    </div>
  </form>
</template>

<style scoped>
.intro { display: grid; gap: 0.3rem; }
.path { word-break: break-all; color: var(--muted); }
.savebar {
  position: sticky;
  bottom: 1rem;
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 0.7rem 0.8rem 0.7rem 1.1rem;
  border-radius: var(--radius);
  background: var(--ink);
  color: var(--bg);
  box-shadow: 0 6px 24px rgb(0 0 0 / 0.18);
  transform: translateY(150%);
  opacity: 0;
  pointer-events: none;
  transition: transform 0.2s, opacity 0.2s;
  font-size: 0.9rem;
}
.savebar.show { transform: none; opacity: 1; pointer-events: auto; }
.savebar .btn.ghost { color: var(--bg); opacity: 0.8; }
.savebar .btn.ghost:hover { background: transparent; opacity: 1; color: var(--bg); }
</style>
