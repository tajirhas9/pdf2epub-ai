// `vite build --mode epub`: SSR-build the book pages, then render + package them into an EPUB.
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { transform } from 'lightningcss'
import type { Plugin } from 'vite'
import { toChapters } from './epub/chapters.ts'
import { fragmentToXhtml, xhtmlDocument } from './epub/xhtml.ts'
import { buildEpub, type BookManifest } from './epub/package.ts'

// Old WebKit/Blink engines found in e-readers (Kobo, older Apple Books/Android readers).
const READER_TARGETS = { safari: 9 << 16, ios_saf: 9 << 16, chrome: 60 << 16, firefox: 60 << 16 }

export interface EpubPluginOptions {
  bookDir: string
  out: string
  cssFile: string
}

export function epubPlugin(opts: EpubPluginOptions): Plugin {
  const ssrOut = join(opts.bookDir, 'build', 'ssr')
  let onlyPages = false
  return {
    name: 'pdf2epub:epub',
    apply: (_config, env) => env.mode === 'epub' || env.mode === 'pages',
    config(_config, env) {
      onlyPages = env.mode === 'pages'
      return {
        build: {
          ssr: 'src/epub/entry-server.ts',
          outDir: ssrOut,
          emptyOutDir: true,
          minify: false,
          rollupOptions: { output: { format: 'es', entryFileNames: 'entry-server.mjs' } },
        },
        ssr: { noExternal: true },
      }
    },
    async closeBundle() {
      const t0 = performance.now()
      const book = JSON.parse(await readFile(join(opts.bookDir, 'book.json'), 'utf8')) as BookManifest
      const entry = (await import(pathToFileURL(join(ssrOut, 'entry-server.mjs')).href)) as {
        pageKeys: string[]
        renderPage: (key: string, base: string) => Promise<string>
      }
      // Chapters live in OEBPS/text/, assets in OEBPS/<book-relative path>.
      const pages = await Promise.all(
        entry.pageKeys.map(async (key) => ({ key, html: await entry.renderPage(key, '../') })),
      )
      // Embedded fonts (fonts/<Family>-Regular.ttf) are declared for the EPUB only.
      const fontFaces = book.assets
        .filter((a) => a.startsWith('fonts/'))
        .map((a) => {
          const family = a.split('/').pop()!.replace(/-[^-]*\.\w+$/, '')
          return `@font-face{font-family:"${family}";src:url("../${a}");}`
        })
        .join('\n')
      const { code } = transform({
        filename: 'book.css',
        code: Buffer.from(fontFaces + '\n' + (await readFile(opts.cssFile, 'utf8'))),
        minify: true,
        targets: READER_TARGETS,
      })
      // Standalone XHTML per PDF page: used for reader-emulated screenshots in the LLM review.
      const previewDir = join(opts.bookDir, 'build', 'pages')
      await mkdir(previewDir, { recursive: true })
      await writeFile(join(opts.bookDir, 'build', 'book.css'), code)
      await Promise.all(
        entry.pageKeys.map(async (key) => {
          const name = key.split('/').pop()!.replace('.vue', '')
          const html = await entry.renderPage(key, '../../')
          await writeFile(join(previewDir, `${name}.xhtml`), xhtmlDocument(name, book.language, fragmentToXhtml(html), '../book.css'))
        }),
      )
      if (onlyPages) {
        console.log(`[pdf2epub] page previews in ${previewDir} (${Math.round(performance.now() - t0)} ms)`)
        return
      }
      const chapters = toChapters(pages, book.title)
      const epub = await buildEpub({ book, bookDir: opts.bookDir, chapters, css: code.toString() })
      const out = resolve(opts.out)
      await mkdir(dirname(out), { recursive: true })
      await writeFile(out, epub)
      const ms = Math.round(performance.now() - t0)
      console.log(`[pdf2epub] wrote ${out} — ${pages.length} pages, ${chapters.length} chapters in ${ms} ms`)
    },
  }
}
