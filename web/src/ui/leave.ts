import { onBeforeUnmount, onMounted } from 'vue'

// Unsaved-changes guard for the tab switch in App.vue and for closing the browser tab.
const guards = new Set<() => boolean>()

export function onBeforeRouteLeaveLike(isDirty: () => boolean) {
  const warn = (e: BeforeUnloadEvent) => {
    if (isDirty()) e.preventDefault()
  }
  onMounted(() => {
    guards.add(isDirty)
    window.addEventListener('beforeunload', warn)
  })
  onBeforeUnmount(() => {
    guards.delete(isDirty)
    window.removeEventListener('beforeunload', warn)
  })
}

export function canLeave(): boolean {
  return [...guards].every((g) => !g()) || window.confirm('Leave without saving your changes?')
}
