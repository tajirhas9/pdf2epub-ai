/// <reference types="vite/client" />

declare module 'virtual:book' {
  import type { BookState } from './src/viewer/types'
  export const assetBase: string
  const state: BookState
  export default state
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<object, object, unknown>
  export default component
}
