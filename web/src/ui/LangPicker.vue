<script setup lang="ts">
import { computed } from 'vue'
import type { Language } from './api'

const props = defineProps<{ languages: Language[]; id?: string }>()
const model = defineModel<string[]>({ required: true })

const names = computed(() => Object.fromEntries(props.languages.map((l) => [l.code, l.name])))
const available = computed(() =>
  props.languages.filter((l) => !model.value.includes(l.code)).sort((a, b) => a.name.localeCompare(b.name)),
)

function add(e: Event) {
  const el = e.target as HTMLSelectElement
  if (el.value) model.value = [...model.value, el.value]
  el.value = ''
}
function remove(code: string) {
  model.value = model.value.filter((c) => c !== code)
}
</script>

<template>
  <div class="langs">
    <span v-for="code in model" :key="code" class="chip">
      {{ names[code] ?? code }}
      <button type="button" :aria-label="`Remove ${names[code] ?? code}`" @click="remove(code)">×</button>
    </span>
    <span v-if="!model.length" class="auto">Auto-detect</span>
    <select :id="id" class="add" aria-label="Add a language" @change="add">
      <option value="">+ Add language</option>
      <option v-for="l in available" :key="l.code" :value="l.code">{{ l.name }} ({{ l.code }})</option>
    </select>
  </div>
</template>

<style scoped>
.langs {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.4rem;
  min-height: 2.4rem;
  padding: 0.3rem;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background: var(--panel);
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 0.2rem;
  padding: 0.15rem 0.25rem 0.15rem 0.6rem;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 0.85rem;
  font-weight: 560;
}
.chip button {
  border: 0;
  background: none;
  color: inherit;
  cursor: pointer;
  font-size: 1rem;
  line-height: 1;
  padding: 0 0.3rem;
  border-radius: 999px;
}
.chip button:hover { background: rgb(0 0 0 / 0.08); }
.auto { color: var(--muted); font-size: 0.88rem; padding-left: 0.4rem; }
.add { width: auto; border: 0; background: transparent; color: var(--muted); font-size: 0.85rem; padding: 0.2rem 0.4rem; cursor: pointer; }
</style>
