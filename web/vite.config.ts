import { resolve } from 'node:path'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'
import { bookPlugin } from './plugins/vite-plugin-book.ts'
import { epubPlugin } from './plugins/vite-plugin-epub.ts'

// The Python CLI passes the book work dir (generated pages, images, book.json) via env.
const bookDir = resolve(process.env.PDF2EPUB_BOOK_DIR ?? '../work/_none')
const epubOut = process.env.PDF2EPUB_EPUB_OUT ?? resolve(bookDir, 'book.epub')

export default defineConfig({
  plugins: [
    vue({
      template: {
        compilerOptions: {
          // `epub:type` etc. are plain attributes; keep whitespace as authored in book text.
          whitespace: 'condense',
        },
      },
    }),
    bookPlugin({ bookDir }),
    epubPlugin({ bookDir, out: epubOut, cssFile: resolve(import.meta.dirname, 'src/book/book.css') }),
  ],
  resolve: {
    alias: {
      '@book-pages': resolve(bookDir, 'pages'),
      '@book': resolve(import.meta.dirname, 'src/book'),
    },
  },
  server: {
    fs: { allow: [import.meta.dirname, bookDir] },
  },
})
