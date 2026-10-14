import type { Component } from 'vue'

// Generated book pages (lazy: the dev server compiles a page only when it is opened).
export const pageLoaders = import.meta.glob<{ default: Component }>('@book-pages/*.vue')

export function loaderFor(key: string) {
  const entry = Object.entries(pageLoaders).find(([path]) => path.endsWith(`/${key}.vue`))
  return entry?.[1]
}
