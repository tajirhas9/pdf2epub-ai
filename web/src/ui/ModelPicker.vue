<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'

/** Text input with a filterable list of the server's models; any typed name is still accepted. */
const props = defineProps<{ id: string; models: string[]; loading?: boolean; placeholder?: string }>()
const model = defineModel<string>({ required: true })

const open = ref(false)
const typed = ref(false) // false: show the whole list (just focused); true: filter by what was typed
const active = ref(-1)
const input = ref<HTMLInputElement>()
const list = ref<HTMLUListElement>()

const words = computed(() => (typed.value ? model.value.toLowerCase().split(/\s+/).filter(Boolean) : []))
const shown = computed(() => {
  const w = words.value
  return w.length ? props.models.filter((m) => w.every((x) => m.toLowerCase().includes(x))) : props.models
})

/** Model name split into plain / matched runs for highlighting. */
function parts(name: string): { text: string; hit: boolean }[] {
  const low = name.toLowerCase()
  const hit = new Array<boolean>(name.length).fill(false)
  for (const w of words.value) {
    for (let i = low.indexOf(w); i !== -1; i = low.indexOf(w, i + 1)) hit.fill(true, i, i + w.length)
  }
  const out: { text: string; hit: boolean }[] = []
  for (let i = 0; i < name.length; i++) {
    const last = out[out.length - 1]
    if (last && last.hit === hit[i]) last.text += name[i]
    else out.push({ text: name[i]!, hit: hit[i]! })
  }
  return out
}

function show() {
  if (open.value) return
  open.value = true
  typed.value = false
  active.value = props.models.indexOf(model.value)
  scrollActive()
}
/** `value`: what the field ends up holding (the model prop only updates after the parent re-renders). */
function close(value = model.value) {
  open.value = false
  active.value = -1
  if (value.trim() !== model.value) model.value = value.trim()
}
function pick(name: string) {
  close(name)
}
function onInput() {
  open.value = true
  typed.value = true
  active.value = shown.value.length && words.value.length ? 0 : -1
}
function move(step: number) {
  if (!open.value) return show()
  const n = shown.value.length
  if (!n) return
  active.value = active.value < 0 ? (step > 0 ? 0 : n - 1) : (active.value + step + n) % n
  scrollActive()
}
function enter(e: KeyboardEvent) {
  if (!open.value) return
  e.preventDefault() // don't submit the surrounding form while choosing
  const name = shown.value[active.value]
  if (name) pick(name)
  else close()
}
function toggle() {
  if (open.value) close()
  else {
    show()
    input.value?.focus()
  }
}
function scrollActive() {
  nextTick(() => list.value?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' }))
}
watch(() => props.models, () => { if (open.value) active.value = Math.min(active.value, shown.value.length - 1) })
</script>

<template>
  <div class="picker" :class="{ open: open && (shown.length || loading) }">
    <input
      :id="id"
      ref="input"
      v-model="model"
      type="text"
      role="combobox"
      autocomplete="off"
      spellcheck="false"
      aria-autocomplete="list"
      :aria-expanded="open"
      :aria-controls="`${id}-list`"
      :aria-activedescendant="active >= 0 ? `${id}-opt-${active}` : undefined"
      :placeholder="placeholder"
      @focus="show"
      @click="show"
      @blur="close()"
      @input="onInput"
      @keydown.down.prevent="move(1)"
      @keydown.up.prevent="move(-1)"
      @keydown.enter="enter"
      @keydown.esc="open && ($event.stopPropagation(), close())"
    />
    <button
      v-if="models.length"
      type="button"
      class="chev"
      tabindex="-1"
      :aria-label="open ? 'Hide models' : 'Show models'"
      @mousedown.prevent="toggle"
    >
      <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" /></svg>
    </button>
    <div v-show="open && (shown.length || loading || (typed && models.length))" class="pop">
      <ul :id="`${id}-list`" ref="list" role="listbox" aria-label="Models on this server">
        <li
          v-for="(m, i) in shown"
          :id="`${id}-opt-${i}`"
          :key="m"
          role="option"
          :aria-selected="i === active"
          :class="{ current: m === model }"
          @mousedown.prevent="pick(m)"
          @mousemove="active = i"
        >
          <span class="name"><template v-for="(p, j) in parts(m)" :key="j"><mark v-if="p.hit">{{ p.text }}</mark><template v-else>{{ p.text }}</template></template></span>
          <svg v-if="m === model" class="tick" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" /></svg>
        </li>
      </ul>
      <p v-if="!shown.length && !loading" class="empty">No listed model matches — “{{ model.trim() }}” will be used as typed.</p>
      <p v-else-if="loading && !shown.length" class="empty">Loading models…</p>
      <p v-else class="foot">{{ shown.length }} of {{ models.length }} · ↑↓ to move · Enter to pick · Esc to close</p>
    </div>
  </div>
</template>

<style scoped>
.picker { position: relative; flex: 1; min-width: 0; }
.picker input { width: 100%; padding-right: 2rem; }
.chev {
  position: absolute;
  top: 50%;
  right: 0.35rem;
  transform: translateY(-50%);
  display: grid;
  place-items: center;
  width: 1.6rem;
  height: 1.6rem;
  border: 0;
  border-radius: 6px;
  background: none;
  color: var(--muted);
  cursor: pointer;
  transition: transform 0.15s;
}
.chev:hover { color: var(--ink); background: var(--sunken); }
.open .chev svg { transform: rotate(180deg); }
.pop {
  position: absolute;
  z-index: 20;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background: var(--panel);
  box-shadow: 0 8px 28px rgb(0 0 0 / 0.14);
  overflow: hidden;
}
ul { list-style: none; margin: 0; padding: 0.25rem; max-height: 16rem; overflow-y: auto; overscroll-behavior: contain; }
li {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.4rem 0.6rem;
  border-radius: 6px;
  cursor: pointer;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.84rem;
}
li[aria-selected='true'] { background: var(--accent-soft); }
li.current { font-weight: 600; }
.name { flex: 1; min-width: 0; overflow-wrap: anywhere; }
mark { background: none; color: var(--accent); font-weight: 700; }
.tick { flex: none; color: var(--accent); }
.empty, .foot { margin: 0; padding: 0.45rem 0.85rem; color: var(--muted); font-size: 0.78rem; }
.foot { border-top: 1px solid var(--line); }
@media (pointer: coarse) { .foot { display: none; } }
</style>
