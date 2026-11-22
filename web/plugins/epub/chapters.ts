// Groups rendered pages into chapter documents. PDF pages are a dev unit only: a reader
// sees continuous chapters, with print page boundaries kept as pagebreak markers.
import type { Chapter } from './package.ts'
import { escAttr, fragmentToXhtml } from './xhtml.ts'

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
  const idFile = new Map<string, string>() // escaped element id -> chapter file

  for (const page of pages) {
    const toc: Chapter['toc'] = []
    const breaks: Chapter['pages'] = []
    const ids: string[] = []
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
        if (attrs.has('id')) ids.push(escAttr(attrs.get('id')!))
      },
    })
    const startsChapter = toc.some((t) => t.level === 1)
    if (!current || (startsChapter && pagesInChapter > 0) || pagesInChapter >= MAX_PAGES_PER_CHAPTER) {
      current = { file: `ch${String(chapters.length + 1).padStart(3, '0')}.xhtml`, title: '', body: '', toc: [], pages: [] }
      chapters.push(current)
      pagesInChapter = 0
    }
    for (const id of ids) idFile.set(id, current.file)
    current.body += body + '\n'
    current.toc.push(...toc)
    current.pages.push(...breaks)
    pagesInChapter++
  }
  for (const ch of chapters) {
    ch.title = ch.toc[0]?.text || fallbackTitle
    ch.body = resolveFragments(ch, idFile)
  }
  return chapters
}

// A link target can end up in another chapter file (e.g. a footnote marker merged onto the
// previous page, with the note on a page that opens a new chapter): point it at that file.
function resolveFragments(ch: Chapter, idFile: Map<string, string>): string {
  return ch.body.replace(/ href="#([^"]+)"/g, (whole, id: string) => {
    const file = idFile.get(id)
    return file && file !== ch.file ? ` href="${file}#${id}"` : whole
  })
}
