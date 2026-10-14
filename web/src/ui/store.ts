import { reactive } from 'vue'
import { api, type State, type System, type Upload } from './api'

export const store = reactive({
  state: null as State | null,
  system: null as System | null,
  loadError: '',
  pending: null as Upload | null, // uploaded, not converted yet (survives tab switches)
  toast: null as { text: string; kind: 'ok' | 'err' } | null,
})

let toastTimer: ReturnType<typeof setTimeout> | undefined
export function toast(text: string, kind: 'ok' | 'err' = 'ok') {
  store.toast = { text, kind }
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => (store.toast = null), kind === 'err' ? 6000 : 3000)
}

export async function loadState() {
  try {
    store.state = await api.state()
    store.loadError = ''
  } catch (e) {
    store.loadError = (e as Error).message
  }
}

export async function loadSystem() {
  store.system = await api.system().catch(() => null)
}

export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}
