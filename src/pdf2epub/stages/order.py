"""Reading order (recursive XY-cut), cross-page paragraph continuation, footnote linking."""

from __future__ import annotations

import re

from pdf2epub.ir import Block, BlockType, Document, Page, Role, Span
from pdf2epub.pipeline import Context

MIN_GAP = 0.006  # page fraction
_TERMINAL = re.compile(r"[.!?:;…\"”’»)\]]\s*$")


class ReadingOrder:
    name = "order"

    def run(self, doc: Document, ctx: Context) -> Document:
        for page in doc.pages:
            rtl = page_is_rtl(page)
            ordered = xycut([b for b in page.blocks if b.role == Role.CONTENT], rtl)
            for i, b in enumerate(ordered):
                b.order = i
            merge_split_paragraphs(page)
            link_footnotes(page)
        mark_continuations(doc.pages)
        return doc


def page_is_rtl(page: Page) -> bool:
    spans = [s for b in page.blocks if b.role == Role.CONTENT for s in b.spans]
    rtl = sum(len(s.text) for s in spans if s.dir == "rtl")
    return rtl > 0.5 * max(1, sum(len(s.text) for s in spans))


def xycut(blocks: list[Block], rtl: bool = False) -> list[Block]:
    if len(blocks) <= 1:
        return list(blocks)
    for axis in ("y", "x"):
        groups = _split(blocks, axis)
        if len(groups) > 1:
            if axis == "x" and rtl:
                groups.reverse()
            return [b for g in groups for b in xycut(g, rtl)]
    return sorted(blocks, key=lambda b: (b.bbox.y0, -b.bbox.x0 if rtl else b.bbox.x0))


def _split(blocks: list[Block], axis: str) -> list[list[Block]]:
    lo = (lambda b: b.bbox.y0) if axis == "y" else (lambda b: b.bbox.x0)
    hi = (lambda b: b.bbox.y1) if axis == "y" else (lambda b: b.bbox.x1)
    items = sorted(blocks, key=lo)
    groups: list[list[Block]] = [[items[0]]]
    reach = hi(items[0])
    for b in items[1:]:
        if lo(b) - reach > MIN_GAP:
            groups.append([b])
        else:
            groups[-1].append(b)
        reach = max(reach, hi(b))
    return groups


def merge_split_paragraphs(page: Page) -> None:
    """Layout often cuts one paragraph where it wraps around a figure. Consecutive paragraph
    blocks (figures/captions may sit between) where the first doesn't end a sentence are one."""
    body = page.content_blocks
    prev: Block | None = None
    for b in body:
        if b.type != BlockType.PARAGRAPH:
            if b.type not in (BlockType.FIGURE, BlockType.CAPTION):
                prev = None
            continue
        if (
            prev is not None
            and prev.text.strip()
            and b.text.strip()
            and not _TERMINAL.search(prev.text)
            and not _LIST_START.match(b.text)
            and _same_lang(prev, b)
        ):
            sep = "" if prev.text.rstrip().endswith("-") and b.text.lstrip()[:1].islower() else " "
            if sep == "" and prev.spans:
                prev.spans[-1].text = prev.spans[-1].text.rstrip()[:-1]
            prev.spans += [Span(text=sep)] + b.spans
            prev.lines += b.lines
            prev.bbox = prev.bbox.union(b.bbox)
            prev.flags += [f for f in b.flags if f not in prev.flags]
            page.blocks.remove(b)
            continue
        prev = b
    for i, b in enumerate(page.content_blocks):
        b.order = i


_LIST_START = re.compile(r"^\s*(\d{1,2}[.)]|[a-z][.)]|[-•*])\s")


def _same_lang(a: Block, b: Block) -> bool:
    return {s.dir for s in a.spans} == {s.dir for s in b.spans}


def _first_para(page: Page) -> Block | None:
    for b in page.content_blocks:
        if b.type in (BlockType.FIGURE, BlockType.CAPTION, BlockType.FOOTNOTE, BlockType.TABLE):
            continue
        return b if b.type == BlockType.PARAGRAPH else None
    return None


def _last_para(page: Page) -> Block | None:
    body = [
        b
        for b in page.content_blocks
        if b.type not in (BlockType.FIGURE, BlockType.CAPTION, BlockType.FOOTNOTE, BlockType.TABLE)
    ]
    return body[-1] if body and body[-1].type == BlockType.PARAGRAPH else None


def mark_continuations(pages: list[Page]) -> None:
    for page in pages:
        for b in page.blocks:
            b.continues = b.continued = False
    for prev, nxt in zip(pages, pages[1:], strict=False):
        last, first = _last_para(prev), _first_para(nxt)
        if not last or not first or not last.text.strip() or not first.text.strip():
            continue
        if (
            not _TERMINAL.search(last.text)
            and not first.text.lstrip()[:1].isupper()
            or last.text.rstrip().endswith("-")
        ):
            last.continues = True
            first.continued = True


_SUP = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
_UNSUP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_LABEL_RE = re.compile(r"^\s*(\d{1,3}|[⁰¹²³⁴⁵⁶⁷⁸⁹]{1,3})[.)]?\s*")


def link_footnotes(page: Page) -> None:
    """Turn note markers glued to words in body text ("everybody.1") into note references."""
    body = [b for b in page.content_blocks if b.type in (BlockType.PARAGRAPH, BlockType.QUOTE, BlockType.LIST)]
    recover_note_numbers(page, body)
    notes = {}
    for b in page.blocks:
        if b.type == BlockType.FOOTNOTE and b.role == Role.CONTENT and (m := _LABEL_RE.match(b.text)):
            notes[m.group(1).translate(_UNSUP)] = b.id
    if not notes:
        return
    for label, note_id in notes.items():
        sup = label.translate(_SUP)
        # Superscript digits need no word boundary; plain digits must be glued to a word end.
        pattern = re.compile(
            rf"(?:(?:(?<=[^\W\d_])|(?<=[^\W\d_][.,;:!?\"”’)]))({label})(?=[\s.,;:]|$)|({sup})(?![⁰¹²³⁴⁵⁶⁷⁸⁹]))"
        )
        for b in body:
            if _split_marker(b, pattern, note_id):
                break


_MARKER_RE = re.compile(r"(?:(?<=[^\W\d_])|(?<=[^\W\d_][.,;:!?\"”’)]))(\d{1,2})(?=[\s.,;:]|$)")


def recover_note_numbers(page: Page, body: list[Block]) -> None:
    """OCR sometimes misses a small footnote number. Numbering is a sequence, so an unnumbered
    note right before note n is note n-1; a lone unnumbered note takes the only body marker that
    no numbered note claims. The recovered number is the one printed in the scan."""
    notes = sorted(
        (b for b in page.blocks if b.type == BlockType.FOOTNOTE and b.role == Role.CONTENT and b.text.strip()),
        key=lambda b: b.bbox.y0,
    )
    if not notes:
        return
    labels = [int(m.group(1).translate(_UNSUP)) if (m := _LABEL_RE.match(n.text)) else None for n in notes]
    used = {lab for lab in labels if lab is not None}
    for i, note in enumerate(notes):
        if labels[i] is not None:
            continue
        guess = None
        nxt = next((labels[j] for j in range(i + 1, len(labels)) if labels[j] is not None), None)
        prev = next((labels[j] for j in range(i - 1, -1, -1) if labels[j] is not None), None)
        if nxt is not None and nxt - 1 >= 1 and nxt - 1 not in used and (prev is None or prev < nxt - 1):
            guess = nxt - 1
        elif nxt is None and prev is None:
            markers = {
                int(m.group(1)) for b in body for sp in b.spans if not sp.note_ref for m in _MARKER_RE.finditer(sp.text)
            }
            free = [n for n in markers if n not in used and n < 20]
            if len(free) == 1:
                guess = free[0]
        if guess is not None and note.spans:
            note.spans[0].text = f"{guess} " + note.spans[0].text.lstrip()
            note.flags.append("note-number-inferred")
            labels[i] = guess
            used.add(guess)


def _split_marker(block: Block, pattern: re.Pattern, note_id: str) -> bool:
    for i, span in enumerate(block.spans):
        if span.note_ref:
            continue
        m = pattern.search(span.text)
        if not m:
            continue
        before, marker, after = span.text[: m.start()], m.group(0), span.text[m.end() :]
        new = [
            span.model_copy(update={"text": before}),
            Span(text=marker, note_ref=note_id, sup=True),
            span.model_copy(update={"text": after}),
        ]
        block.spans[i : i + 1] = [s for s in new if s.text]
        return True
    return False
