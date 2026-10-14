import { computed, reactive, ref, watch } from 'vue'
import { api, type Values } from './api'
import { store } from './store'

/** Editable copy of the saved settings; `save()` sends only what changed. */
export function useDraft() {
  const values = reactive({} as Values)
  const apiKey = ref('')
  const clearKey = ref(false)
  const json = reactive({ body: '', headers: '' })
  const saving = ref(false)

  function reset() {
    const v = store.state!.values
    Object.assign(values, JSON.parse(JSON.stringify(v)) as Values) // plain JSON values; proxies can't be structuredClone'd
    apiKey.value = ''
    clearKey.value = false
    json.body = Object.keys(v['llm.extra_body']).length ? JSON.stringify(v['llm.extra_body'], null, 2) : ''
    json.headers = Object.keys(v['llm.extra_headers']).length ? JSON.stringify(v['llm.extra_headers'], null, 2) : ''
  }
  reset()

  function parse(text: string): Record<string, unknown> | null {
    if (!text.trim()) return {}
    try {
      const v = JSON.parse(text)
      return v && typeof v === 'object' && !Array.isArray(v) ? v : null
    } catch {
      return null
    }
  }
  const jsonErrors = computed(() => ({ body: parse(json.body) === null, headers: parse(json.headers) === null }))
  watch(
    () => json.body,
    (t) => { const v = parse(t); if (v) values['llm.extra_body'] = v },
  )
  watch(
    () => json.headers,
    (t) => { const v = parse(t); if (v) values['llm.extra_headers'] = v as Record<string, string> },
  )

  const changed = computed(() => {
    const saved = store.state!.values as Record<string, unknown>
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(values)) if (JSON.stringify(v) !== JSON.stringify(saved[k])) out[k] = v
    return out as Partial<Values>
  })
  const dirty = computed(() => Object.keys(changed.value).length > 0 || !!apiKey.value || clearKey.value)
  const valid = computed(() => !jsonErrors.value.body && !jsonErrors.value.headers)

  /** `keys`: also write these even if unchanged (first-run setup records its choices explicitly). */
  async function save(keys: (keyof Values)[] = []) {
    saving.value = true
    try {
      store.state = await api.saveConfig({
        set: { ...Object.fromEntries(keys.map((k) => [k, values[k]])), ...changed.value },
        api_key: apiKey.value || undefined,
        clear_api_key: clearKey.value || undefined,
      })
      reset()
    } finally {
      saving.value = false
    }
  }

  return { values, apiKey, clearKey, json, jsonErrors, dirty, valid, saving, reset, save }
}
