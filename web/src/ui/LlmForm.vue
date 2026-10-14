<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api, type Check, type Provider } from './api'
import { errorText } from './store'

const props = defineProps<{
  providers: Provider[]
  apiKeySet: boolean
  keyring: boolean
  envOverrides: string[]
  extraBody?: unknown
  extraHeaders?: unknown
}>()
const baseUrl = defineModel<string>('baseUrl', { required: true })
const model = defineModel<string>('model', { required: true })
const apiKey = defineModel<string>('apiKey', { required: true })
const clearKey = defineModel<boolean>('clearKey', { default: false })

const presets = computed(() => props.providers.filter((p) => p.id !== 'none'))
const providerId = ref('custom')
const provider = computed(() => presets.value.find((p) => p.id === providerId.value))
const keyHint = computed(() =>
  provider.value?.needs_key ? 'Required by this provider.' : 'Usually not needed for servers on your computer.',
)

function syncProvider() {
  providerId.value = presets.value.find((p) => p.base_url && p.base_url === baseUrl.value)?.id ?? 'custom'
}
onMounted(syncProvider)
watch(baseUrl, syncProvider)

function pickProvider(e: Event) {
  const id = (e.target as HTMLSelectElement).value
  providerId.value = id
  const p = presets.value.find((x) => x.id === id)
  if (p?.base_url) baseUrl.value = p.base_url
  check.value = null
}

const pinned = (key: string) => props.envOverrides.includes(key)
const ENV = { 'llm.base_url': 'LLM_BASE_URL', 'llm.model': 'LLM_MODEL', 'llm.api_key': 'LLM_API_KEY' } as const

// models offered by the endpoint
const models = ref<string[]>([])
const loadingModels = ref(false)
const modelsLoaded = ref(false)
function fields() {
  return {
    base_url: baseUrl.value,
    model: model.value,
    api_key: apiKey.value || undefined,
    extra_body: props.extraBody,
    extra_headers: props.extraHeaders,
  }
}
async function loadModels() {
  if (!baseUrl.value) return
  loadingModels.value = true
  try {
    models.value = (await api.models(fields())).models
  } catch {
    models.value = []
  } finally {
    loadingModels.value = false
    modelsLoaded.value = true
  }
}
let timer: ReturnType<typeof setTimeout> | undefined
watch([baseUrl, apiKey], () => {
  modelsLoaded.value = false
  clearTimeout(timer)
  timer = setTimeout(loadModels, 600)
})
onMounted(loadModels)

// connection test
const check = ref<Check | null>(null)
const testing = ref(false)
async function test() {
  testing.value = true
  check.value = null
  try {
    check.value = await api.checkLlm(fields())
  } catch (e) {
    check.value = { level: 'error', message: errorText(e) }
  } finally {
    testing.value = false
  }
}
watch([baseUrl, model, apiKey], () => (check.value = null))
</script>

<template>
  <div class="stack">
    <div class="grid-2">
      <div class="field">
        <label for="llm-provider">Provider</label>
        <select id="llm-provider" :value="providerId" @change="pickProvider">
          <option v-for="p in presets" :key="p.id" :value="p.id">{{ p.label }}</option>
        </select>
        <span v-if="provider?.hint" class="hint">{{ provider.hint }}</span>
      </div>
      <div class="field">
        <label for="llm-url">Server address</label>
        <input id="llm-url" v-model.trim="baseUrl" type="url" placeholder="https://…/v1" spellcheck="false" />
        <span v-if="pinned('llm.base_url')" class="hint warn">Set by {{ ENV['llm.base_url'] }} in the environment, which wins over this value.</span>
        <span v-else class="hint">Any OpenAI-compatible endpoint.</span>
      </div>
    </div>

    <div class="field">
      <label for="llm-key">API key</label>
      <div class="row">
        <input
          id="llm-key"
          v-model="apiKey"
          type="password"
          autocomplete="off"
          :placeholder="apiKeySet && !clearKey ? '•••••••• saved — type to replace' : 'Paste your key'"
        />
        <button v-if="apiKeySet && !clearKey && !apiKey" type="button" class="btn ghost" @click="clearKey = true">Remove</button>
        <button v-if="clearKey" type="button" class="btn ghost" @click="clearKey = false">Undo</button>
      </div>
      <span v-if="pinned('llm.api_key')" class="hint warn">Set by {{ ENV['llm.api_key'] }} in the environment.</span>
      <span v-else-if="clearKey" class="hint warn">The saved key will be removed when you save.</span>
      <span v-else class="hint">
        {{ keyHint }}
        {{ keyring ? 'Stored in your system keychain, never in plain text.' : 'No system keychain found: stored in the settings file (readable only by you).' }}
      </span>
    </div>

    <div class="field">
      <label for="llm-model">Model</label>
      <div class="row">
        <input id="llm-model" v-model.trim="model" type="text" list="llm-models" placeholder="Server default" spellcheck="false" />
        <button type="button" class="btn" :disabled="loadingModels || !baseUrl" @click="loadModels">
          <span v-if="loadingModels" class="spinner" />{{ loadingModels ? 'Loading' : 'Refresh list' }}
        </button>
      </div>
      <datalist id="llm-models">
        <option v-for="m in models" :key="m" :value="m" />
      </datalist>
      <span v-if="pinned('llm.model')" class="hint warn">Set by {{ ENV['llm.model'] }} in the environment, which wins over this value.</span>
      <span v-else-if="modelsLoaded && models.length" class="hint">
        {{ models.length }} model{{ models.length === 1 ? '' : 's' }} available — pick one that can read images.
      </span>
      <span v-else-if="modelsLoaded" class="hint">The server didn't list models. Type the model name, or leave empty for the server's default.</span>
      <span v-else class="hint">Must be able to read images (vision model).</span>
    </div>

    <div class="row wrap">
      <button type="button" class="btn" :disabled="testing || !baseUrl" @click="test">
        <span v-if="testing" class="spinner" />{{ testing ? 'Testing — reading a test image…' : 'Test connection' }}
      </button>
      <span class="hint">Sends one small test image to check the key, the model and image reading.</span>
    </div>
    <p v-if="check" class="notice" :class="{ ok: check.level === 'ok', warn: check.level === 'warn', err: check.level === 'error' }" role="status">
      {{ check.level === 'ok' ? '✓ ' : check.level === 'warn' ? '! ' : '✕ ' }}{{ check.message }}
    </p>
  </div>
</template>
