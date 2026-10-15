import { reactive } from 'vue'
import { api, type Job } from './api'
import { toast } from './store'

// The conversions, polled for the whole app (fast while something runs) so status toasts show on any page.
export const jobs = reactive({
  list: [] as Job[],
  loaded: false,
})

const ACTIVE: Job['status'][] = ['queued', 'running', 'pausing', 'cancelling']
export const isActive = (j: Job) => ACTIVE.includes(j.status)

let timer: ReturnType<typeof setTimeout> | undefined
export async function refreshJobs() {
  clearTimeout(timer)
  try {
    const before = new Map(jobs.list.map((j) => [j.id, j.status]))
    jobs.list = await api.jobs()
    jobs.loaded = true
    for (const j of jobs.list) {
      const was = before.get(j.id)
      if (!was || was === j.status) continue
      if (j.status === 'done') toast(`${j.file.name} is ready`)
      if (j.status === 'failed') toast(`${j.file.name} failed`, 'err')
      if (j.status === 'paused' && j.reason.startsWith('llm-unreachable'))
        toast(`${j.file.name} paused: the AI review stopped (see the reason)`, 'err')
    }
  } catch { /* server restarting: keep the last view */ }
  timer = setTimeout(refreshJobs, jobs.list.some(isActive) ? 800 : 4000)
}

const STATUS = {
  queued: { text: 'Waiting', cls: '' },
  running: { text: 'Converting', cls: 'accent' },
  pausing: { text: 'Pausing…', cls: 'warn' },
  paused: { text: 'Paused', cls: 'warn' },
  cancelling: { text: 'Stopping…', cls: 'warn' },
  done: { text: 'Done', cls: 'ok' },
  failed: { text: 'Failed', cls: 'err' },
  cancelled: { text: 'Stopped', cls: '' },
} as const

export const isPreparing = (j: Job) => j.status === 'queued' && (j.waiting === 'models' || j.waiting === 'download')
export const statusOf = (j: Job): { text: string; cls: string } => (isPreparing(j) ? { text: 'Preparing', cls: 'accent' } : STATUS[j.status])
