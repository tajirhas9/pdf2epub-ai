// Vue SSR emits HTML5; EPUB needs well-formed XHTML. Re-serialize through a real HTML parser.
import { parseFragment } from 'parse5'

type Node = {
  nodeName: string
  tagName?: string
  value?: string
  data?: string
  attrs?: { name: string; value: string }[]
  childNodes?: Node[]
  content?: Node
}

const VOID = new Set([
  'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr',
])
const DROP_ATTR = /^(data-v-|data-bid$|data-page$)/

const escText = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
const escAttr = (s: string) => escText(s).replace(/"/g, '&quot;')

export interface XhtmlOptions {
  /** Called for each element; may return extra/overridden attributes (e.g. heading ids). */
  onElement?: (tag: string, attrs: Map<string, string>, text: () => string) => void
}

export function fragmentToXhtml(html: string, opts: XhtmlOptions = {}): string {
  const root = parseFragment(html) as unknown as Node
  return serialize(root.childNodes ?? [], opts)
}

function textOf(node: Node): string {
  if (node.nodeName === '#text') return node.value ?? ''
  return (node.childNodes ?? []).map(textOf).join('')
}

function serialize(nodes: Node[], opts: XhtmlOptions): string {
  let out = ''
  for (const n of nodes) {
    if (n.nodeName === '#text') out += escText(n.value ?? '')
    else if (n.nodeName === '#comment') continue
    else if (n.tagName) {
      const attrs = new Map<string, string>()
      for (const a of n.attrs ?? []) if (!DROP_ATTR.test(a.name)) attrs.set(a.name, a.value)
      if (attrs.get('class') === '') attrs.delete('class')
      opts.onElement?.(n.tagName, attrs, () => textOf(n))
      const attrStr = [...attrs].map(([k, v]) => ` ${k}="${escAttr(v)}"`).join('')
      const children = n.tagName === 'template' ? n.content?.childNodes : n.childNodes
      if (VOID.has(n.tagName)) out += `<${n.tagName}${attrStr}/>`
      else out += `<${n.tagName}${attrStr}>${serialize(children ?? [], opts)}</${n.tagName}>`
    }
  }
  return out
}

export function xhtmlDocument(title: string, lang: string, body: string, css = '../styles/book.css'): string {
  return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="${escAttr(lang)}" lang="${escAttr(lang)}">
<head>
<meta charset="UTF-8"/>
<title>${escText(title)}</title>
<link rel="stylesheet" type="text/css" href="${css}"/>
</head>
<body>
${body}
</body>
</html>
`
}

export { escAttr, escText }
