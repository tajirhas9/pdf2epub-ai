// Dev integration: exposes the book work dir to the viewer and pushes pipeline progress over HMR.
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { extname, join, normalize, relative } from 'node:path'
import type { Plugin, ViteDevServer } from 'vite'

const VIRTUAL = 'virtual:book'
const RESOLVED = '\0' + VIRTUAL
const ASSET_PREFIX = '/book-assets/'

const MIME: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.json': 'application/json',
  '.ttf': 'font/ttf',
  '.woff2': 'font/woff2',
}

export function bookPlugin(opts: { bookDir: string }): Plugin {
  const { bookDir } = opts

  function readJson(path: string): unknown {
    return existsSync(path) ? JSON.parse(readFileSync(path, 'utf8')) : null
  }

  function bookState() {
    const metaDir = join(bookDir, 'meta')
    const metas = existsSync(metaDir)
      ? readdirSync(metaDir)
          .filter((f) => f.endsWith('.json'))
          .sort()
          .map((f) => readJson(join(metaDir, f)))
      : []
    return {
      book: readJson(join(bookDir, 'book.json')),
      progress: readJson(join(bookDir, 'progress.json')),
      pages: metas,
    }
  }

  return {
    name: 'pdf2epub:book',
    resolveId: (id) => (id === VIRTUAL ? RESOLVED : undefined),
    load(id) {
      if (id !== RESOLVED) return
      return `export const assetBase = ${JSON.stringify(ASSET_PREFIX)}\nexport default ${JSON.stringify(bookState())}`
    },
    configureServer(server: ViteDevServer) {
      // Serve book images/fonts read-only from the work dir.
      server.middlewares.use(ASSET_PREFIX, async (req, res, next) => {
        const rel = normalize(decodeURIComponent((req.url ?? '').split('?')[0] ?? '')).replace(/^([/\\])+/, '')
        const file = join(bookDir, rel)
        if (relative(bookDir, file).startsWith('..') || !existsSync(file)) return next()
        res.setHeader('Content-Type', MIME[extname(file).toLowerCase()] ?? 'application/octet-stream')
        res.setHeader('Cache-Control', 'no-cache')
        res.end(await readFile(file))
      })
      server.middlewares.use('/api/book', (_req, res) => {
        res.setHeader('Content-Type', 'application/json')
        res.end(JSON.stringify(bookState()))
      })

      // Pipeline writes meta/progress JSON; push it to the viewer and refresh the virtual module.
      server.watcher.add([join(bookDir, 'meta'), join(bookDir, 'progress.json'), join(bookDir, 'book.json')])
      const onChange = (file: string) => {
        if (!file.startsWith(bookDir) || !file.endsWith('.json')) return
        const mod = server.moduleGraph.getModuleById(RESOLVED)
        if (mod) server.moduleGraph.invalidateModule(mod)
        server.ws.send({ type: 'custom', event: 'book:update', data: bookState() })
      }
      server.watcher.on('change', onChange)
      server.watcher.on('add', onChange)
    },
  }
}
