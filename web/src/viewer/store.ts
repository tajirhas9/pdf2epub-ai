import { defineStore } from 'pinia'
import initial from 'virtual:book'
import type { BookState, PageMeta } from './types'

export const useBook = defineStore('book', {
  state: (): BookState & { hovered: string | null } => ({ ...initial, hovered: null }),
  getters: {
    byKey: (s) => (key: string): PageMeta | undefined => s.pages.find((p) => p.key === key),
  },
  actions: {
    replace(state: BookState) {
      this.book = state.book
      this.progress = state.progress
      this.pages = state.pages
    },
  },
})

/** Live updates pushed by the dev plugin whenever the pipeline writes meta/progress. */
export function subscribeBookUpdates(store: ReturnType<typeof useBook>): void {
  if (import.meta.hot) {
    import.meta.hot.on('book:update', (state: BookState) => store.replace(state))
  }
}
