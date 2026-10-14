// Groups rendered pages into chapter documents. PDF pages are a dev unit only: a reader
// sees continuous chapters, with print page boundaries kept as pagebreak markers.
import type { Chapter } from './package.ts'
import { fragmentToXhtml } from './xhtml.ts'

export interface RenderedPage {
  key: string
  html: string
}

const MAX_PAGES_PER_CHAPTER = 40

export function toChapters(pages: RenderedPage[], fallbackTitle: string): Chapter[] {
  const chapters: Chapter[] = []
  let current: Chapter | null = null
  let pagesInChapter = 0
  let headingSeq = 0

  for (const page of pages) {
    const toc: Chapter['toc'] = []
    const breaks: Chapter['pages'] = []
    const body = fragmentToXhtml(page.html, {
      onElement(tag, attrs, text) {
        const m = /^h([1-3])$/.exec(tag)
        if (m) {
          if (!attrs.has('id')) attrs.set('id', `h${++headingSeq}`)
          toc.push({ id: attrs.get('id')!, level: Number(m[1]), text: text().replace(/\s+/g, ' ').trim() })
        }
        if (attrs.get('epub:type') === 'pagebreak' && attrs.has('id')) {
          breaks.push({ id: attrs.get('id')!, label: attrs.get('aria-label') ?? '' })
        }
      },
    })
    const startsChapter = toc.some((t) => t.level === 1)
    if (!current || (startsChapter && pagesInChapter > 0) || pagesInChapter >= MAX_PAGES_PER_CHAPTER) {
      current = { file: `ch${String(chapters.length + 1).padStart(3, '0')}.xhtml`, title: '', body: '', toc: [], pages: [] }
      chapters.push(current)
      pagesInChapter = 0
    }
    current.body += body + '\n'
    current.toc.push(...toc)
    current.pages.push(...breaks)
    pagesInChapter++
  }
  for (const ch of chapters) ch.title = ch.toc[0]?.text || fallbackTitle
  return chapters
}
