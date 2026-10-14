// Assembles chapters + assets into an EPUB 3 container (zip with stored `mimetype` first).
import { randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { extname, join } from 'node:path'
import JSZip from 'jszip'
import { escAttr, escText, xhtmlDocument } from './xhtml.ts'

export interface BookManifest {
  id: string
  title: string
  author?: string
  language: string
  identifier?: string
  cover?: string | null
  assets: string[] // book-dir relative paths (images, fonts)
}

export interface Chapter {
  file: string // e.g. ch001.xhtml
  title: string
  body: string // XHTML body content
  toc: { id: string; level: number; text: string }[]
  pages: { id: string; label: string }[]
}

const MEDIA: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
  '.ttf': 'font/ttf',
  '.otf': 'font/otf',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
}

const manifestId = (path: string) => 'a-' + path.replace(/[^A-Za-z0-9]/g, '-')

export async function buildEpub(opts: {
  book: BookManifest
  bookDir: string
  chapters: Chapter[]
  css: string
}): Promise<Uint8Array> {
  const { book, bookDir, chapters, css } = opts
  const zip = new JSZip()
  zip.file('mimetype', 'application/epub+zip', { compression: 'STORE' })
  zip.file(
    'META-INF/container.xml',
    `<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
`,
  )

  for (const ch of chapters) {
    zip.file(`OEBPS/text/${ch.file}`, xhtmlDocument(ch.title, book.language, ch.body))
  }
  zip.file('OEBPS/styles/book.css', css)
  zip.file('OEBPS/nav.xhtml', navDocument(book, chapters))

  const items: string[] = []
  for (const asset of book.assets) {
    const media = MEDIA[extname(asset).toLowerCase()]
    if (!media) continue
    zip.file(`OEBPS/${asset}`, await readFile(join(bookDir, asset)))
    const props = asset === book.cover ? ' properties="cover-image"' : ''
    items.push(`<item id="${manifestId(asset)}" href="${escAttr(asset)}" media-type="${media}"${props}/>`)
  }

  const identifier = book.identifier ?? `urn:uuid:${randomUUID()}`
  const modified = new Date().toISOString().replace(/\.\d+Z$/, 'Z')
  const rtl = /^(ar|fa|ur|he|yi|ps|sd|ug)\b/.test(book.language)
  zip.file(
    'OEBPS/content.opf',
    `<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="${escAttr(book.language)}">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">${escText(identifier)}</dc:identifier>
    <dc:title>${escText(book.title)}</dc:title>
    <dc:language>${escText(book.language)}</dc:language>
${book.author ? `    <dc:creator>${escText(book.author)}</dc:creator>\n` : ''}    <meta property="dcterms:modified">${modified}</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="css" href="styles/book.css" media-type="text/css"/>
${chapters.map((c) => `    <item id="${c.file.replace('.xhtml', '')}" href="text/${c.file}" media-type="application/xhtml+xml"/>`).join('\n')}
${items.map((i) => '    ' + i).join('\n')}
  </manifest>
  <spine${rtl ? ' page-progression-direction="rtl"' : ''}>
${chapters.map((c) => `    <itemref idref="${c.file.replace('.xhtml', '')}"/>`).join('\n')}
  </spine>
</package>
`,
  )
  return zip.generateAsync({ type: 'uint8array', compression: 'DEFLATE', mimeType: 'application/epub+zip' })
}

function navDocument(book: BookManifest, chapters: Chapter[]): string {
  // Nested TOC from heading levels; falls back to one entry per chapter.
  const entries = chapters.flatMap((c) =>
    c.toc.length ? c.toc.map((t) => ({ ...t, href: `text/${c.file}#${t.id}` })) : [{ id: '', level: 1, text: c.title, href: `text/${c.file}` }],
  )
  type TocNode = { text: string; href: string; children: TocNode[] }
  const root: TocNode[] = []
  const stack: { level: number; node: TocNode }[] = []
  for (const e of entries) {
    const node: TocNode = { text: e.text || book.title, href: e.href, children: [] }
    while (stack.length && stack[stack.length - 1]!.level >= e.level) stack.pop()
    ;(stack.length ? stack[stack.length - 1]!.node.children : root).push(node)
    stack.push({ level: e.level, node })
  }
  const render = (nodes: TocNode[]): string =>
    '<ol>' +
    nodes
      .map((n) => `<li><a href="${escAttr(n.href)}">${escText(n.text)}</a>${n.children.length ? render(n.children) : ''}</li>`)
      .join('') +
    '</ol>'
  const html = render(root)

  const pages = chapters.flatMap((c) => c.pages.map((p) => ({ ...p, href: `text/${c.file}#${p.id}` })))
  const pageList = pages.length
    ? `<nav epub:type="page-list" hidden=""><h2>Pages</h2><ol>${pages
        .map((p) => `<li><a href="${escAttr(p.href)}">${escText(p.label)}</a></li>`)
        .join('')}</ol></nav>`
    : ''
  return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="${escAttr(book.language)}" lang="${escAttr(book.language)}">
<head><meta charset="UTF-8"/><title>${escText(book.title)}</title></head>
<body>
<nav epub:type="toc" id="toc"><h1>${escText(book.title)}</h1>${html}</nav>
${pageList}
</body>
</html>
`
}
