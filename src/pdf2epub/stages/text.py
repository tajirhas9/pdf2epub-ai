"""Text for every text-bearing block.

Visible, good-quality text layer -> used directly (no OCR, fastest path).
Otherwise -> OCR: line detection once per page, recognition per line with the primary script
model, low-confidence lines retried with other script models; a hidden/poor text layer is used
only as a hint to repair unknown words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
from PIL import Image

from pdf2epub.engines import OcrEngine
from pdf2epub.ir import BBox, Block, BlockType, Document, GlyphHit, Page, PageKind, Source, Span, Word
from pdf2epub.pipeline import Context
from pdf2epub.stages.layout import load_page_array
from pdf2epub.textutil import (
    direction,
    dominant_script,
    fix_punct_spacing,
    is_known,
    join_lines,
    lang_for_script,
    normalize,
    text_layer_quality,
)

TEXT_TYPES = {
    BlockType.TITLE,
    BlockType.HEADING,
    BlockType.PARAGRAPH,
    BlockType.CAPTION,
    BlockType.FOOTNOTE,
    BlockType.LIST,
    BlockType.QUOTE,
}
LOW_CONF = 0.80  # retry other scripts / flag below this
EDGE = 0.04  # page fraction: lines this close to the left/right edge may be cut at the gutter


@dataclass
class Line:
    text: str
    score: float
    bbox: BBox
    script: str


class Text:
    name = "text"
    counted = True

    def __init__(self, ocr: OcrEngine, scripts: list[str]):
        self.ocr = ocr
        self.scripts = scripts  # first = primary; others tried on low-confidence lines

    def run(self, doc: Document, ctx: Context) -> Document:
        pages = ctx.selected(doc)
        for n, page in enumerate(pages, 1):
            self.process(page, ctx, doc.language)
            ctx.progress(self.name, n, len(pages))
        return doc

    def process(self, page: Page, ctx: Context, lang: str) -> None:
        page.text_layer_quality = text_layer_quality(page.words, lang)
        visible = [w for w in page.words if not w.invisible]
        use_layer = (
            page.kind == PageKind.VECTOR
            and page.text_layer_quality is not None
            and page.text_layer_quality >= ctx.settings.text_layer_min_quality
            and len(visible) >= 0.9 * len(page.words)
        )
        if use_layer:
            lines = lines_from_words(page.words)
        else:
            lines = self._ocr_lines(page, ctx)
            if page.words:
                repair_with_hint(lines, page.words, lang)
            promote_text_figures(page, lines, ctx)
        for line in lines:
            if line.script == "latin":
                line.text = split_glued_words(fix_punct_spacing(line.text), lang)
        page.edge_lines = [
            Word(text=ln.text, bbox=ln.bbox) for ln in lines if ln.bbox.x0 < EDGE or ln.bbox.x1 > 1 - EDGE
        ]
        assign_lines(page, lines, ctx.settings.langs)
        page.flags = [f for f in page.flags if f != "ocr"] + ([] if use_layer else ["ocr"])

    def _ocr_lines(self, page: Page, ctx: Context) -> list[Line]:
        img = load_page_array(page, ctx)
        H, W = img.shape[:2]
        boxes = self.ocr.detect_lines(img)
        scale = max(1.0, ctx.settings.ocr_min_ppi / page.dpi) if page.dpi else 1.0
        crops = [_crop(img, b, scale) for b in boxes]
        primary = self.scripts[0]
        results = self.ocr.recognize(crops, primary)
        best = [(t, s, primary) for t, s in results]
        for script in self.scripts[1:]:
            retry = [i for i, (_, s, _) in enumerate(best) if s < LOW_CONF]
            if not retry:
                break
            for i, (t, s) in zip(retry, self.ocr.recognize([crops[i] for i in retry], script), strict=False):
                if dominant_script(t) != script:
                    continue  # the other model did not see its own script either
                cur_text, cur_score, _ = best[i]
                implausible = _too_few_letters(cur_text, boxes[i])
                if s > cur_score or (implausible and s > 0.3):
                    best[i] = (t, s, script)
        lines = []
        for (x0, y0, x1, y1), (text, score, script) in zip(boxes, best, strict=False):
            text = normalize(text).strip()
            if not text:
                continue
            lines.append(
                Line(
                    text=text,
                    score=score,
                    bbox=BBox(x0=x0 / W, y0=y0 / H, x1=x1 / W, y1=y1 / H),
                    script=dominant_script(text) or script,
                )
            )
        return lines


def _too_few_letters(text: str, box) -> bool:
    """A recognized line with far fewer letters than its width allows is not real text in this script."""
    x0, y0, x1, y1 = box
    expected = (x1 - x0) / max(1.0, (y1 - y0)) * 1.5  # ~1.5 characters per line-height of width
    return sum(c.isalnum() for c in text) < 0.25 * expected


def promote_text_figures(page: Page, lines: list[Line], ctx: Context) -> None:
    """Layout models often label decorative/calligraphic text lines (e.g. scripture in another
    script) as figures. A non-photographic 'figure' mostly covered by text lines is text."""
    from pdf2epub.stages.figures import _is_photo, crop_bbox

    img = None
    for b in page.blocks:
        if b.type != BlockType.FIGURE:
            continue
        inside = [ln for ln in lines if ln.bbox.overlap_ratio(b.bbox) > 0.6]
        covered = sum(ln.bbox.area for ln in inside) / (b.bbox.area or 1)
        if len(inside) == 0 or covered < 0.45:
            continue
        if img is None:
            img = Image.open(ctx.book_dir / page.image.path).convert("RGB")
        if _is_photo(crop_bbox(img, b.bbox)):
            continue
        b.type = BlockType.PARAGRAPH
        b.flags.append("figure-text")


def _crop(img: np.ndarray, box, scale: float, pad: int = 2) -> np.ndarray:
    H, W = img.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    crop = img[max(0, y0 - pad) : min(H, y1 + pad), max(0, x0 - pad) : min(W, x1 + pad)]
    if scale > 1.05 and crop.size:
        im = Image.fromarray(crop)
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
        crop = np.asarray(im)
    return crop


def lines_from_words(words: list[Word]) -> list[Line]:
    by_line: dict[int, list[Word]] = {}
    for w in words:
        by_line.setdefault(w.line, []).append(w)
    lines = []
    for ws in by_line.values():
        ws.sort(key=lambda w: w.bbox.x0)
        text = normalize(" ".join(w.text for w in ws))
        bbox = ws[0].bbox
        for w in ws[1:]:
            bbox = bbox.union(w.bbox)
        lines.append(Line(text=text, score=1.0, bbox=bbox, script=dominant_script(text) or "latin"))
    return lines


def repair_with_hint(lines: list[Line], words: list[Word], lang: str) -> None:
    """Replace OCR tokens unknown to the dictionary by the text-layer reading at the same spot,
    when that reading (with spurious inner spaces removed) is a known word."""
    for line in lines:
        if line.script != "latin" or is_known("the", lang) is None:
            continue
        hint = [w for w in words if w.bbox.overlap_ratio(line.bbox) > 0.5]
        if not hint:
            continue
        tokens = line.text.split(" ")
        n = len(line.text) or 1
        pos = 0
        for i, tok in enumerate(tokens):
            start, end = pos / n, (pos + len(tok)) / n
            pos += len(tok) + 1
            core = re.sub(r"^\W+|\W+$", "", tok)
            if len(core) < 3 or is_known(core, lang):
                continue
            x0 = line.bbox.x0 + start * line.bbox.width
            x1 = line.bbox.x0 + end * line.bbox.width
            near = [w for w in hint if w.bbox.cx >= x0 - 0.01 and w.bbox.cx <= x1 + 0.01]
            cand = re.sub(r"^\W+|\W+$", "", "".join(w.text for w in sorted(near, key=lambda w: w.bbox.x0)))
            if cand and cand.lower() != core.lower() and is_known(cand, lang):
                tokens[i] = tok.replace(core, cand)
        line.text = " ".join(tokens)


def split_glued_words(text: str, lang: str) -> str:
    """OCR often drops spaces ('hadgreat', 'Ismaelwasthirteen'). Split an unknown token into known
    words when a dictionary for `lang` is available. Proper nouns and real words are left alone."""
    if is_known("the", lang) is None:
        return text
    out = []
    for tok in text.split(" "):
        m = re.fullmatch(r"(\W*)([^\W\d_]{5,})(\W*)", tok)
        if not m or _zipf(m.group(2), lang) >= 2.5:
            out.append(tok)
            continue
        parts = _camel_split(m.group(2), lang) or _segment(m.group(2), lang)
        out.append(m.group(1) + " ".join(parts) + m.group(3) if parts else tok)
    return " ".join(out)


def _zipf(word: str, lang: str) -> float:
    from pdf2epub.textutil import _wordfreq

    return _wordfreq().zipf_frequency(word.lower(), lang)


def _camel_split(word: str, lang: str) -> list[str] | None:
    """'ofQahtan' -> ['of', 'Qahtan']: a common word glued to a capitalized name."""
    m = re.fullmatch(r"([^\W\d_]+?)([A-Z][^\W\d_]{2,})", word)
    if m and m.group(1).islower() and _zipf(m.group(1), lang) >= 4.0:
        return [m.group(1), m.group(2)]
    return None


def _segment(word: str, lang: str) -> list[str] | None:
    """Best split into 2-3 parts, each a common word."""
    from pdf2epub.textutil import _wordfreq

    wf = _wordfreq()
    zipf = lambda w: wf.zipf_frequency(w.lower(), lang)  # noqa: E731
    best: tuple[float, list[str]] | None = None
    n = len(word)
    floor = 4.0 if n < 7 else 3.0  # short tokens: only split into very common words
    for i in range(2, n - 1):
        a, rest = word[:i], word[i:]
        if zipf(a) < floor:  # names are never split: every part must be a common word
            continue
        candidates = [[rest]] + [[rest[:j], rest[j:]] for j in range(2, len(rest) - 1)]
        for tail in candidates:
            if all(zipf(t) >= floor or (len(t) == 1 and t.lower() in "ai") for t in tail):
                score = min(zipf(x) for x in [a, *tail]) - 0.5 * len(tail)
                if best is None or score > best[0]:
                    best = (score, [a, *tail])
    return best[1] if best else None


def assign_lines(page: Page, lines: list[Line], lang_hints: list[str]) -> None:
    lines = merge_row_fragments(lines)  # before assignment: a split-off fragment may sit in a neighbour's box
    text_blocks = [b for b in page.blocks if b.type in TEXT_TYPES]
    owned: dict[str, list[Line]] = {b.id: [] for b in text_blocks}
    orphans: list[Line] = []
    for line in lines:
        best, best_ov = None, 0.0
        for b in text_blocks:
            ov = line.bbox.overlap_ratio(b.bbox)
            if ov > best_ov:
                best, best_ov = b, ov
        if best is not None and best_ov >= 0.5:
            owned[best.id].append(line)
        elif (neighbour := _row_neighbour(line, text_blocks, owned, lines)) is not None:
            # a fragment just outside its block's box (e.g. a footnote number in the margin)
            owned[neighbour.id].append(line)
        elif not any(line.bbox.overlap_ratio(b.bbox) > 0.5 for b in page.blocks):
            orphans.append(line)
    for b in text_blocks:
        owned[b.id] = merge_row_fragments(owned[b.id])
    page.glyph_hits = []
    for b in text_blocks:
        if b.type != BlockType.FOOTNOTE:
            page.glyph_hits += glyph_hits_for(b.id, owned[b.id])
    for b in text_blocks:
        if b.type == BlockType.FOOTNOTE:
            _split_footnote(page, b, owned[b.id], lang_hints)
        else:
            set_block_text(b, owned[b.id], lang_hints)
    if orphans:
        page.flags.append(f"orphan-lines:{len(orphans)}")
        # Text the layout model missed: keep it rather than lose content.
        for group in _group_orphans(orphans):
            box = group[0].bbox
            for ln in group[1:]:
                box = box.union(ln.bbox)
            b = Block(
                id=page.new_block_id(), type=BlockType.PARAGRAPH, bbox=box, source=Source.OCR, order=len(page.blocks)
            )
            b.flags.append("layout-missed")
            set_block_text(b, group, lang_hints)
            page.blocks.append(b)


_NOTE_START = re.compile(r"^\s*(\d{1,3}|[¹²³⁴⁵⁶⁷⁸⁹⁰]+)[.)]?\s*\S")


def _split_footnote(page: Page, block: Block, lines: list[Line], lang_hints: list[str]) -> None:
    """One layout region often holds several notes ('1 …', '2 …'): one block per note."""
    lines = sorted(lines, key=lambda ln: (ln.bbox.y0, ln.bbox.x0))
    groups: list[list[Line]] = []
    for ln in lines:
        if not groups or _NOTE_START.match(ln.text):
            groups.append([ln])
        else:
            groups[-1].append(ln)
    set_block_text(block, groups[0] if groups else [], lang_hints)
    for group in groups[1:]:
        box = group[0].bbox
        for ln in group[1:]:
            box = box.union(ln.bbox)
        nb = Block(id=page.new_block_id(), type=BlockType.FOOTNOTE, bbox=box, source=Source.OCR, order=block.order)
        set_block_text(nb, group, lang_hints)
        page.blocks.append(nb)
    if groups:
        first = groups[0]
        box = first[0].bbox
        for ln in first[1:]:
            box = box.union(ln.bbox)
        block.bbox = box


def _group_orphans(lines: list[Line]) -> list[list[Line]]:
    lines = sorted(lines, key=lambda ln: (ln.bbox.y0, ln.bbox.x0))
    groups: list[list[Line]] = []
    for ln in lines:
        last = groups[-1][-1] if groups else None
        if last and ln.bbox.y0 - last.bbox.y1 < 1.5 * last.bbox.height and abs(ln.bbox.x0 - last.bbox.x0) < 0.1:
            groups[-1].append(ln)
        else:
            groups.append([ln])
    return groups


def _row_neighbour(
    line: Line, blocks: list[Block], owned: dict[str, list[Line]], all_lines: list[Line]
) -> Block | None:
    """Text block owning a line on the same visual row, within a couple of line-heights."""
    best, best_gap = None, 2.5 * line.bbox.height
    for b in blocks:
        for other in all_lines:
            if other is line or other.bbox.overlap_ratio(b.bbox) < 0.5:
                continue
            overlap = min(other.bbox.y1, line.bbox.y1) - max(other.bbox.y0, line.bbox.y0)
            if overlap < 0.6 * min(other.bbox.height, line.bbox.height):
                continue
            gap = max(other.bbox.x0 - line.bbox.x1, line.bbox.x0 - other.bbox.x1, 0.0)
            if gap < best_gap:
                best, best_gap = b, gap
    return best


def merge_row_fragments(lines: list[Line], max_gap: float = 2.5) -> list[Line]:
    """Detectors may split one printed line at a wide gap (e.g. a footnote number and its text).
    Fragments on the same visual row and close together (gap < max_gap line-heights; columns stay
    apart) are joined back into one line, in reading direction."""
    if len(lines) < 2:
        return lines
    rows: list[list[Line]] = []
    for ln in sorted(lines, key=lambda ln: (ln.bbox.cy, ln.bbox.x0)):
        placed = False
        for row in rows[-4:]:  # rows near this height
            ref = row[0].bbox
            overlap = min(ref.y1, ln.bbox.y1) - max(ref.y0, ln.bbox.y0)
            if overlap < 0.6 * min(ref.height, ln.bbox.height):
                continue
            h = max(ref.height, ln.bbox.height)
            if any(max(o.bbox.x0 - ln.bbox.x1, ln.bbox.x0 - o.bbox.x1, 0.0) < max_gap * h for o in row):
                row.append(ln)
                placed = True
                break
        if not placed:
            rows.append([ln])
    out = []
    for row in rows:
        if len(row) == 1:
            out.append(row[0])
            continue
        rtl = sum(ln.script in ("arabic", "hebrew") for ln in row) > len(row) / 2
        row.sort(key=lambda ln: -ln.bbox.x0 if rtl else ln.bbox.x0)
        box = row[0].bbox
        for ln in row[1:]:
            box = box.union(ln.bbox)
        script = max(set(ln.script for ln in row), key=lambda sc: sum(len(ln.text) for ln in row if ln.script == sc))
        out.append(
            Line(text=" ".join(ln.text for ln in row), score=min(ln.score for ln in row), bbox=box, script=script)
        )
    return out


def _reading_sorted(lines: list[Line]) -> list[Line]:
    rtl_majority = sum(ln.script in ("arabic", "hebrew") for ln in lines) > len(lines) / 2
    return sorted(lines, key=lambda ln: (round(ln.bbox.cy * 200), -ln.bbox.x0 if rtl_majority else ln.bbox.x0))


def glyph_hits_for(block_id: str, lines: list[Line]) -> list[GlyphHit]:
    """Suspicious inline marks in OCR lines, with their occurrence index in the block text."""
    from pdf2epub.stages.glyphs import approx_char_bbox, find_hits

    hits: list[GlyphHit] = []
    counts: dict[str, int] = {}
    for ln in _reading_sorted(lines):
        if ln.script != "latin":
            continue
        found = {i: sym for sym, i in find_hits(ln.text)}
        for i, ch in enumerate(ln.text):
            if i in found:
                hits.append(
                    GlyphHit(
                        block_id=block_id,
                        symbol=ch,
                        ordinal=counts.get(ch, 0),
                        bbox=approx_char_bbox(ln.bbox, ln.text, i),
                    )
                )
            counts[ch] = counts.get(ch, 0) + 1
    return hits


def set_block_text(block: Block, lines: list[Line], lang_hints: list[str]) -> None:
    if not lines:
        block.flags.append("no-text")
        block.spans = []
        return
    lines = _reading_sorted(lines)
    block.lines = [Word(text=ln.text, bbox=ln.bbox) for ln in lines]
    # Consecutive lines of the same script form one span.
    spans: list[Span] = []
    run: list[Line] = []
    for ln in lines + [None]:
        if run and (ln is None or ln.script != run[-1].script):
            script = run[-1].script
            text = join_lines([r.text for r in run])
            spans.append(Span(text=text, lang=lang_for_script(script, lang_hints), dir=direction(script)))
            run = []
        if ln is not None:
            run.append(ln)
    for i in range(1, len(spans)):
        spans[i].text = " " + spans[i].text
    block.spans = spans
    block.confidence = min(ln.score for ln in lines)
    block.source = Source.PDF if all(ln.score == 1.0 for ln in lines) else Source.OCR
    if block.confidence < LOW_CONF:
        block.flags.append("low-confidence")
    if any(s.dir == "rtl" for s in spans):
        block.flags.append("rtl")
        if block.confidence < 0.9:
            # Vowelled / calligraphic RTL text at scan resolution: OCR is a draft, the LLM transcribes.
            block.flags.append("needs-transcription")
