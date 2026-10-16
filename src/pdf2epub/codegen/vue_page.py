"""IR -> one `.vue` file per PDF page (+ viewer meta JSON + book.json for the EPUB build).

Pages are always generated from IR; the LLM never writes them. A hash header detects hand edits:
an edited page is "locked" and not overwritten.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pdf2epub.ir import Block, BlockType, Document, Page, Role, Span
from pdf2epub.textutil import NOTE_LABEL_RE, UNSUP

HEADER_RE = re.compile(r"^<!-- pdf2epub:sha=([0-9a-f]{16}) -->\n")

_TEXT_ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", "{": "&#123;", "}": "&#125;"}
_ATTR_ESC = {**_TEXT_ESC, '"': "&quot;"}


def esc(text: str) -> str:
    return "".join(_TEXT_ESC.get(c, c) for c in text)


def esc_attr(text: str) -> str:
    return "".join(_ATTR_ESC.get(c, c) for c in text)


def page_key(page: Page) -> str:
    return f"p{page.index + 1:04d}"


def page_label(page: Page) -> str:
    return page.label or str(page.index + 1)


class VuePageWriter:
    def __init__(self, doc: Document, book_dir: Path):
        self.doc = doc
        self.book_dir = book_dir
        self.pages_dir = book_dir / "pages"
        self.meta_dir = book_dir / "meta"
        # Footnotes some text marker links to (only those get a back-link).

    def _linked_notes(self) -> set[str]:
        """Footnotes a marker in the text points to (computed now: LLM edits may change it)."""
        return {
            s.note_ref
            for p in self.doc.pages
            for b in p.blocks
            if b.role == Role.CONTENT
            for s in b.spans
            if s.note_ref
        }

    def _note_ids(self) -> set[str]:
        """Footnotes that get rendered: a marker to anything else would be a dangling link."""
        return {b.id for p in self.doc.pages for b in p.content_blocks if b.type == BlockType.FOOTNOTE}

    # ---- public -------------------------------------------------------------------------

    def write_all(self, pages: list[Page] | None = None) -> dict[str, list[str]]:
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        written, locked = [], []
        for page in pages or self.doc.pages:
            (written if self.write_page(page) else locked).append(page_key(page))
        self.write_book_json()
        return {"written": written, "locked": locked}

    def write_page(self, page: Page) -> bool:
        path = self.pages_dir / f"{page_key(page)}.vue"
        locked = is_hand_edited(path)
        if not locked:
            body = self.render(page)
            sha = hashlib.sha256(body.encode()).hexdigest()[:16]
            path.write_text(f"<!-- pdf2epub:sha={sha} -->\n{body}")
        self.write_meta(page, locked)
        return not locked

    def render(self, page: Page) -> str:
        lines: list[str] = []
        blocks = page.content_blocks
        prev = self._prev_page(page)
        continued_from_prev = bool(
            blocks and blocks[0].continued and prev and _last_para(prev) and _last_para(prev).continues
        )
        if not continued_from_prev:
            lines.append(f'<PageBreak label="{esc_attr(page_label(page))}" />')

        captions = {b.caption_of: b for b in blocks if b.type == BlockType.CAPTION and b.caption_of}
        notes: list[Block] = []
        prev_block = _last_body_block(prev) if prev else None
        for i, b in enumerate(blocks):
            if i == 0 and continued_from_prev:
                continue  # emitted by the previous page, inside its last paragraph
            if b.type == BlockType.FOOTNOTE:
                notes.append(b)
            elif b.type == BlockType.CAPTION and b.caption_of in {x.id for x in blocks}:
                continue
            else:
                lines.append(self._block(b, page, captions, prev_block))
                prev_block = b
        if notes:
            lines.append('<section class="notes">')
            lines.extend(
                f'  <Footnote id="{esc_attr(n.id)}" bid="{esc_attr(n.id)}"{_lang_attrs(n)}'
                f' label="{esc_attr(_note_label(n))}"{" back" if n.id in self._linked_notes() else ""}>'
                f"{self._inline(n.spans, n, strip_label=True)}</Footnote>"
                for n in notes
            )
            lines.append("</section>")
        inner = "\n".join("    " + line for line in lines)
        return (
            f'<template>\n  <section class="page" data-page="{page.index + 1}">\n{inner}\n  </section>\n</template>\n'
        )

    # ---- blocks -------------------------------------------------------------------------

    def _block(self, b: Block, page: Page, captions: dict[str, Block], prev: Block | None = None) -> str:
        bid = f' bid="{esc_attr(b.id)}"'
        if b.type in (BlockType.TITLE, BlockType.HEADING):
            level = _heading_level(b)
            # Chapter titles and section headings open a new reader page, unless they directly
            # follow another heading (title + subtitle, chapter + first section stay together).
            newpage = " newpage" if level <= 2 and not (prev and _heading_level(prev)) else ""
            return (
                f'<BookHeading :level="{level}"{bid}{newpage}{_lang_attrs(b)}>'
                f"{self._inline(b.spans, b)}</BookHeading>"
            )
        if b.type in (BlockType.FIGURE, BlockType.TABLE, BlockType.FORMULA) and b.image:
            cap = captions.get(b.id)
            caption = self._inline(cap.spans, cap) if cap else ""
            attrs = (
                f' src="{esc_attr(b.image.path)}" alt="{esc_attr(b.alt or "")}"'
                f' layout="{(b.layout.value if b.layout else "full")}"'
                f' :width="{b.image.width}" :height="{b.image.height}"'
            )
            return f"<BookFigure{attrs}{bid}>{caption}</BookFigure>"
        kind = {BlockType.QUOTE: "quote", BlockType.CAPTION: "caption", BlockType.LIST: "list"}.get(b.type)
        extra = (" cont" if b.continued else "") + (f' kind="{kind}"' if kind else "")
        text = self._inline(b.spans, b)
        if b.continues and (nxt := self._next_page(page)):
            first = next(iter(nxt.content_blocks), None)
            if first is not None and first.continued:
                text += f'<PageBreak label="{esc_attr(page_label(nxt))}" />' + self._join_text(b, first)
        return f"<BookPara{bid}{_lang_attrs(b)}{extra}>{text}</BookPara>"

    def _join_text(self, b: Block, nxt: Block) -> str:
        text = self._inline(nxt.spans, nxt)
        # Hyphenated word split across the page boundary.
        if b.text.endswith("-") and nxt.text[:1].islower():
            return text
        return " " + text.lstrip()

    def _inline(self, spans: list[Span], block: Block, strip_label: bool = False) -> str:
        out: list[str] = []
        for i, s in enumerate(spans):
            text = s.text
            if strip_label and i == 0:
                text = NOTE_LABEL_RE.sub("", text, count=1)
            if s.glyph and (g := self.doc.glyphs.get(s.glyph)) and g.render != "text" and g.image:
                # Unverified special mark: keep the printed glyph rather than risk wrong text.
                out.append(
                    f'<BookGlyph src="{esc_attr(g.image.path)}" :width="{g.image.width}" :height="{g.image.height}" />'
                )
                continue
            if s.note_ref and s.note_ref in self._note_ids():
                out.append(f'<NoteRef to="{esc_attr(s.note_ref)}" label="{esc_attr(text.strip())}" />')
                continue
            piece = esc(text)
            if s.bold:
                piece = f"<b>{piece}</b>"
            if s.italic:
                piece = f"<i>{piece}</i>"
            if s.sup:
                piece = f"<sup>{piece}</sup>"
            if s.lang and s.lang != (_block_lang(block) or self.doc.language):
                dir_attr = f' dir="{s.dir}"' if s.dir else ""
                piece = f'<Lang lang="{esc_attr(s.lang)}"{dir_attr}>{piece}</Lang>'
            out.append(piece)
        return "".join(out).strip()

    # ---- neighbours ---------------------------------------------------------------------

    def _prev_page(self, page: Page) -> Page | None:
        return self.doc.pages[page.index - 1] if page.index > 0 else None

    def _next_page(self, page: Page) -> Page | None:
        return self.doc.pages[page.index + 1] if page.index + 1 < len(self.doc.pages) else None

    # ---- meta ---------------------------------------------------------------------------

    def write_meta(self, page: Page, locked: bool) -> None:
        meta = {
            "key": page_key(page),
            "index": page.index,
            "label": page_label(page),
            "kind": page.kind.value,
            "image": page.image.model_dump() if page.image else None,
            "locked": locked,
            "flags": page.flags,
            "timings": page.timings,
            "textLayerQuality": page.text_layer_quality,
            "blocks": [
                {
                    "id": b.id,
                    "type": b.type.value,
                    "role": b.role.value,
                    "bbox": b.bbox.model_dump(),
                    "text": b.text[:300],
                    "source": b.source.value,
                    "confidence": round(b.confidence, 3),
                    "flags": b.flags,
                    "order": b.order,
                }
                for b in page.blocks
            ],
        }
        (self.meta_dir / f"{page_key(page)}.json").write_text(json.dumps(meta, ensure_ascii=False))

    def write_book_json(self) -> None:
        assets: list[str] = []
        for page in self.doc.pages:
            for b in page.content_blocks:
                if b.image and b.type != BlockType.CAPTION:
                    assets.append(b.image.path)
        used = {s.glyph for p in self.doc.pages for b in p.content_blocks for s in b.spans if s.glyph}
        assets += [g.image.path for k, g in self.doc.glyphs.items() if k in used and g.render != "text" and g.image]
        fonts_dir = self.book_dir / "fonts"
        if fonts_dir.exists():
            assets += [f"fonts/{f.name}" for f in sorted(fonts_dir.iterdir())]
        book = {
            "id": self.doc.id,
            "title": self.doc.title,
            "author": self.doc.author,
            "language": self.doc.language,
            "identifier": self.doc.meta.get("identifier"),
            "cover": self.doc.meta.get("cover"),
            "assets": list(dict.fromkeys(assets)),
            "pages": [{"key": page_key(p), "label": page_label(p)} for p in self.doc.pages],
        }
        (self.book_dir / "book.json").write_text(json.dumps(book, ensure_ascii=False, indent=1))


def is_hand_edited(path: Path) -> bool:
    if not path.exists():
        return False
    raw = path.read_text()
    m = HEADER_RE.match(raw)
    if not m:
        return True  # no header: authored by hand
    return hashlib.sha256(raw[m.end() :].encode()).hexdigest()[:16] != m.group(1)


def _last_para(page: Page) -> Block | None:
    paras = [b for b in page.content_blocks if b.type == BlockType.PARAGRAPH]
    return paras[-1] if paras else None


def _heading_level(b: Block) -> int:
    """Rendered heading level, 0 for non-headings."""
    if b.type == BlockType.TITLE:
        return 1
    if b.type == BlockType.HEADING:
        return b.level or 2
    return 0


def _last_body_block(page: Page) -> Block | None:
    """Last block rendered in the page body (footnotes go to the notes section)."""
    body = [b for b in page.content_blocks if b.type != BlockType.FOOTNOTE]
    return body[-1] if body else None


def _block_lang(b: Block) -> str | None:
    langs = {s.lang for s in b.spans if s.text.strip()}
    return langs.pop() if len(langs) == 1 else None


def _lang_attrs(b: Block) -> str:
    lang = _block_lang(b)
    if not lang:
        return ""
    d = next((s.dir for s in b.spans if s.dir), None)
    return f' lang="{esc_attr(lang)}"' + (f' dir="{d}"' if d else "")


def _note_label(b: Block) -> str:
    m = NOTE_LABEL_RE.match(b.text)
    return m.group(1).translate(UNSUP) if m else ""
