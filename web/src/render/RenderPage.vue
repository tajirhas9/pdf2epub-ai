<script setup lang="ts">
// Bare page as an e-reader would show it: book.css only, reader-like margins, paginated columns.
import { assetBase } from 'virtual:book'
import { defineAsyncComponent, onBeforeUnmount, onMounted, provide, shallowRef, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ASSET_BASE } from '@book/components'
import '@book/book.css'
import { loaderFor } from '../viewer/pages'

const props = defineProps<{ pageKey: string }>()
const route = useRoute()
provide(ASSET_BASE, assetBase)

const Page = shallowRef<unknown>(null)
watch(
  () => props.pageKey,
  (key) => {
    const loader = loaderFor(key)
    Page.value = loader ? defineAsyncComponent(loader) : null
  },
  { immediate: true },
)

const mode = (route.query.mode as string) ?? 'phone'
document.documentElement.dataset.mode = mode

let lastHot: Element | null = null
function onMessage(e: MessageEvent) {
  if (e.data?.type !== 'highlight') return
  lastHot?.classList.remove('wb-hot')
  lastHot = e.data.bid ? document.querySelector(`[data-bid="${CSS.escape(e.data.bid)}"]`) : null
  lastHot?.classList.add('wb-hot')
  lastHot?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
}
function onOver(e: MouseEvent) {
  const el = (e.target as Element).closest('[data-bid]')
  window.parent.postMessage({ type: 'hover', bid: el?.getAttribute('data-bid') ?? null }, '*')
}
onMounted(() => {
  window.addEventListener('message', onMessage)
  document.addEventListener('mouseover', onOver)
})
onBeforeUnmount(() => {
  window.removeEventListener('message', onMessage)
  document.removeEventListener('mouseover', onOver)
})
</script>

<template>
  <div class="reader">
    <component :is="Page" v-if="Page" />
    <p v-else>No generated page {{ pageKey }}.</p>
  </div>
</template>

<style>
html { background: #fff; color: #111; }
html[data-mode] body { margin: 0; }
/* Reader emulation: fixed-height viewport paginated with CSS columns, like EPUB reading systems. */
.reader {
  height: 100vh;
  box-sizing: border-box;
  padding: 1.2em 1.2em;
  column-gap: 2.4em;
  column-fill: auto;
  overflow-x: auto;
  font-size: 17px;
}
html[data-mode='phone'] .reader { column-width: calc(100vw - 2.4em); }
html[data-mode='tablet'] .reader { column-width: calc(100vw - 2.4em); font-size: 19px; }
html[data-mode='spread'] .reader { column-count: 2; font-size: 18px; }
.wb-hot { outline: 3px solid rgba(181, 83, 42, 0.8); outline-offset: 2px; }
</style>
