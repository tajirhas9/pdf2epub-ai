// Typed client for the `pdf2epub ui` JSON API (src/pdf2epub/webui).

export type Values = {
  'llm.enabled': boolean
  'llm.base_url': string
  'llm.model': string
  'llm.review_policy': string
  'llm.concurrency': number
  'llm.max_output_tokens': number
  'llm.timeout_s': number
  'llm.extra_body': Record<string, unknown>
  'llm.extra_headers': Record<string, string>
  langs: string[]
  out_dir: string
  keep_work: boolean
  use_gpu: boolean
  ocr_det_model: string
  vector_dpi: number
}

export interface Provider { id: string; label: string; base_url: string; needs_key: boolean; hint: string }
export interface Language { code: string; name: string }

export interface State {
  configured: boolean
  config_path: string
  layout: 'checkout' | 'installed'
  values: Values
  api_key_set: boolean
  keyring: boolean
  env_overrides: string[]
  providers: Provider[]
  languages: Language[]
}

export interface System {
  node: string | null
  node_ok: boolean
  node_min: string
  java: boolean
  models: boolean
  paddle: boolean
  gpu: boolean
}

export interface Upload { id: string; name: string; title: string; pages: number }

export interface ConvertOptions {
  langs: string[]
  llm: boolean
  review_policy: string
  output_name: string
  keep_work: boolean
  use_gpu: boolean
  ocr_det_model: string
  vector_dpi: number
}

export interface Step {
  key: string
  label: string
  status: 'running' | 'done' | 'failed' | 'paused'
  done: number
  total: number | null
  note: string
  seconds?: number | null
}

export interface Job {
  id: string
  file: Upload
  options: ConvertOptions
  status: 'queued' | 'running' | 'pausing' | 'paused' | 'cancelling' | 'done' | 'failed' | 'cancelled'
  /** while paused: 'paused' (by hand), 'interrupted' (app stopped), 'llm-unreachable: …' */
  reason: string
  steps: Step[]
  created: number
  elapsed: number
  result: {
    file: string
    path: string
    size: number
    valid: boolean | null
    chapters: number
    pages: number
    attention: string[]
    work_dir: string | null
    epubcheck: string
    note: string
  } | null
  error: { message: string; hint: string } | null
  /** while queued: what runs first (another conversion, or the OCR models loading/downloading) */
  waiting: 'conversion' | 'models' | 'download' | null
}

export interface LlmFields { base_url: string; model: string; api_key?: string; extra_body?: unknown; extra_headers?: unknown }
export interface Check { level: 'ok' | 'warn' | 'error'; message: string }

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api/${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await res.json().catch(() => ({ error: res.statusText }))
  if (!res.ok) throw new ApiError(data.error ?? res.statusText, res.status)
  return data as T
}

export const api = {
  state: () => call<State>('GET', 'state'),
  system: () => call<System>('GET', 'system'),
  saveConfig: (changes: { set?: Partial<Values>; unset?: string[]; api_key?: string; clear_api_key?: boolean }) =>
    call<State>('PUT', 'config', changes),
  models: (f: LlmFields) => call<{ models: string[] }>('POST', 'models', f),
  checkLlm: (f: LlmFields) => call<Check>('POST', 'llm-check', f),
  jobs: () => call<Job[]>('GET', 'jobs'),
  convert: (upload_id: string, options: ConvertOptions) => call<Job>('POST', 'jobs', { upload_id, options }),
  cancel: (id: string) => call('POST', `jobs/${id}/cancel`),
  pause: (id: string) => call('POST', `jobs/${id}/pause`),
  resume: (id: string) => call('POST', `jobs/${id}/resume`),
  removeJob: (id: string) => call('DELETE', `jobs/${id}`),
  getUpload: (id: string) => call<Upload>('GET', `uploads/${id}`),
  removeUpload: (id: string) => call('DELETE', `uploads/${id}`),
  epubUrl: (id: string) => `/api/jobs/${id}/epub`,

  /** Upload with progress (fetch has no upload progress). */
  upload(file: File, onProgress: (fraction: number) => void): Promise<Upload> {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      xhr.open('POST', `/api/uploads?name=${encodeURIComponent(file.name)}`)
      xhr.setRequestHeader('Content-Type', 'application/pdf')
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total)
      xhr.onload = () => {
        let data: { error?: string } = {}
        try {
          data = JSON.parse(xhr.responseText)
        } catch { /* empty */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data as Upload)
        else reject(new ApiError(data.error ?? `upload failed (${xhr.status})`, xhr.status))
      }
      xhr.onerror = () => reject(new ApiError('upload failed: the server is not reachable', 0))
      xhr.send(file)
    })
  },
}

export function formatSize(n: number): string {
  const units = ['B', 'KB', 'MB', 'GB']
  let i = 0
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i++
  }
  return `${i ? n.toFixed(1) : n} ${units[i]}`
}

export function formatSeconds(s: number): string {
  if (s < 60) return `${Math.round(s)}s`
  const m = Math.floor(s / 60)
  return m < 60 ? `${m}m ${Math.round(s % 60)}s` : `${Math.floor(m / 60)}h ${m % 60}m`
}

export function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}
