import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// The web app served by `pdf2epub ui` (Python serves the build and the /api endpoints).
// `npm run dev:ui` proxies /api to a running `pdf2epub ui` (PDF2EPUB_API, default http://127.0.0.1:8765).
export default defineConfig({
  plugins: [vue()],
  build: {
    rollupOptions: { input: 'ui.html' },
  },
  server: {
    proxy: { '/api': process.env.PDF2EPUB_API ?? 'http://127.0.0.1:8765' },
  },
})
