// SSR entry used only by `vite build --mode epub`: renders each generated page component to HTML.
import { createSSRApp, type Component } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { ASSET_BASE, installBook } from '@book/components'

const modules = import.meta.glob<{ default: Component }>('@book-pages/*.vue', { eager: true })

export const pageKeys: string[] = Object.keys(modules).sort()

export async function renderPage(key: string, assetBase: string): Promise<string> {
  const mod = modules[key]
  if (!mod) throw new Error(`unknown page ${key}`)
  const app = createSSRApp(mod.default)
  installBook(app)
  app.provide(ASSET_BASE, assetBase)
  return renderToString(app)
}
