<script setup lang="ts">
import { onKeyStroke } from '@vueuse/core'
import { assetBase } from 'virtual:book'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useBook } from './store'

const props = defineProps<{ pageKey: string }>()
const book = useBook()
const router = useRouter()
const page = computed(() => book.byKey(props.pageKey))
const index = computed(() => book.pages.findIndex((p) => p.key === props.pageKey))
const showBoxes = ref(true)
const showHidden = ref(false)
const frames = ref<HTMLIFrameElement[]>([])

const DEVICES = [
  { id: 'phone', label: 'Phone 390×844', w: 390, h: 844, scale: 0.62 },
  { id: 'tablet', label: 'Tablet 820×1180', w: 820, h: 1180, scale: 0.44 },
  { id: 'spread', label: 'Two-page spread 1180×820', w: 1180, h: 820, scale: 0.44 },
] as const

const ROLE_COLOR: Record<string, string> = {
  content: '#1f5fa8',
  header: '#9a6a00',
  footer: '#9a6a00',
  page_number: '#9a6a00',
  decoration: '#b5532a',
  background: '#7a3fa0',
}

const blocks = computed(() =>
  (page.value?.blocks ?? []).filter((b) => showHidden.value || b.role === 'content'),
)

function go(delta: number) {
  const next = book.pages[index.value + delta]
  if (next) router.push(`/page/${next.key}`)
}
onKeyStroke('ArrowRight', () => go(1))
onKeyStroke('ArrowLeft', () => go(-1))

// Hover sync: original-image boxes <-> rendered blocks in every device frame.
function hover(bid: string | null) {
  book.hovered = bid
  for (const f of frames.value) f.contentWindow?.postMessage({ type: 'highlight', bid }, '*')
}
function onMessage(e: MessageEvent) {
  if (e.data?.type === 'hover') book.hovered = e.data.bid
}
onMounted(() => window.addEventListener('message', onMessage))
onBeforeUnmount(() => window.removeEventListener('message', onMessage))
watch(() => props.pageKey, () => hover(null))
</script>

<template>
  <div v-if="page" class="view">
    <nav class="bar">
      <button :disabled="index <= 0" @click="go(-1)">← prev</button>
      <strong>page {{ page.label }}</strong>
      <span class="muted">{{ page.kind }} · {{ page.flags.join(' · ') || 'no flags' }}</span>
      <label><input v-model="showBoxes" type="checkbox" /> boxes</label>
      <label><input v-model="showHidden" type="checkbox" /> non-content</label>
      <button :disabled="index >= book.pages.length - 1" @click="go(1)">next →</button>
    </nav>

    <div class="panes">
      <section class="pane original">
        <h3>Original</h3>
        <div class="paper">
          <img v-if="page.image" :src="assetBase + page.image.path" alt="original page" />
          <svg v-if="showBoxes" viewBox="0 0 1 1" preserveAspectRatio="none">
            <rect
              v-for="b in blocks"
              :key="b.id"
              :x="b.bbox.x0"
              :y="b.bbox.y0"
              :width="b.bbox.x1 - b.bbox.x0"
              :height="b.bbox.y1 - b.bbox.y0"
              :stroke="ROLE_COLOR[b.role] ?? '#888'"
              :class="{ hot: book.hovered === b.id, flagged: b.flags.length > 0 }"
              vector-effect="non-scaling-stroke"
              @mouseenter="hover(b.id)"
              @mouseleave="hover(null)"
            />
          </svg>
        </div>
      </section>

      <section v-for="d in DEVICES" :key="d.id" class="pane">
        <h3>{{ d.label }}</h3>
        <div class="device" :style="{ width: `${d.w * d.scale}px`, height: `${d.h * d.scale}px` }">
          <iframe
            ref="frames"
            :src="`/render/${page.key}?mode=${d.id}`"
            :style="{ width: `${d.w}px`, height: `${d.h}px`, transform: `scale(${d.scale})` }"
            :title="d.label"
          />
        </div>
      </section>
    </div>

    <section class="blocks">
      <h3>Blocks</h3>
      <table>
        <thead><tr><th>#</th><th>id</th><th>type</th><th>role</th><th>conf</th><th>flags</th><th>text</th></tr></thead>
        <tbody>
          <tr
            v-for="b in [...page.blocks].sort((a, c) => a.order - c.order)"
            :key="b.id"
            :class="{ hot: book.hovered === b.id, dim: b.role !== 'content' }"
            @mouseenter="hover(b.id)"
            @mouseleave="hover(null)"
          >
            <td>{{ b.role === 'content' ? b.order : '' }}</td>
            <td class="mono">{{ b.id }}</td>
            <td>{{ b.type }}</td>
            <td>{{ b.role }}</td>
            <td>{{ b.confidence.toFixed(2) }}</td>
            <td>{{ b.flags.join(', ') }}</td>
            <td class="text">{{ b.text }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
  <p v-else>Unknown page.</p>
</template>

<style scoped>
.bar { display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; margin-bottom: 0.8rem; }
.panes { display: flex; gap: 1rem; overflow-x: auto; align-items: flex-start; padding-bottom: 0.5rem; }
.pane h3, .blocks h3 { font-size: 0.8rem; color: var(--muted); font-weight: 500; margin: 0 0 0.3rem; }
.paper { position: relative; width: 420px; }
.paper img { width: 100%; display: block; }
.paper svg { position: absolute; inset: 0; width: 100%; height: 100%; }
rect { fill: transparent; stroke-width: 1.5; cursor: pointer; }
rect.flagged { stroke-dasharray: 4 2; }
rect.hot { fill: rgba(181, 83, 42, 0.18); stroke-width: 3; }
.device { overflow: hidden; border: 1px solid var(--line); border-radius: 10px; background: #fff; }
.device iframe { border: 0; transform-origin: 0 0; background: #fff; }
.blocks { margin-top: 1rem; }
table { border-collapse: collapse; width: 100%; font-size: 0.8rem; background: var(--panel); }
th, td { border-bottom: 1px solid var(--line); padding: 0.25rem 0.4rem; text-align: left; vertical-align: top; }
tr.hot { background: rgba(181, 83, 42, 0.12); }
tr.dim { color: var(--muted); }
.mono { font-family: ui-monospace, monospace; }
.text { max-width: 60ch; }
button { font: inherit; }
</style>
