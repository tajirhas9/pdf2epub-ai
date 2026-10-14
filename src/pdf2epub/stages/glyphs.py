"""Inline special marks (honorific ligatures, dingbats, PUA font glyphs).

OCR misreads them ('Ibrahim&to') or drops them. Policy: a mark ends up either as *verified*
Unicode text or as the *original printed glyph* (inline image) — never as unverified text,
because a confidently wrong honorific is worse than the printed one.

- Each occurrence is located tightly on the scan (ink segmentation of its text line).
- Occurrences are grouped only when their ink shapes match closely (normalized correlation),
  so visually different honorifics never share a decision.
- Each group is verified once (LLM, see review.resolve_glyphs); unsure -> stays an image.
"""

from __future__ import annotations

import re

import cv2
import numpy as np
from PIL import Image

from pdf2epub.ir import PLACEHOLDER, BBox, Block, Document, Glyph, GlyphHit, ImageRef, Role, Source, Span, Word
from pdf2epub.pipeline import Context

# Symbols that rarely occur inside running prose; stray ones are usually misread glyphs.
SUSPECT = re.compile(
    r"(?<=[^\W\d_])\s?([&#*%@~^§¤¶†‡|]|[-])(?=\s?[^\W\d_])"
    r"|(?<=[^\W\d_])\s([&#*%@~^§¤¶†‡|]|[-])(?=[\s,.;:)])"
)
MATCH = 0.80  # descriptor correlation to count as the same printed mark (same cluster)
FAMILY = 0.50  # looser: clusters this similar must agree on their text (see harmonize)
DESC = 32


def find_hits(line_text: str) -> list[tuple[str, int]]:
    """(symbol, char index in line) for suspicious marks."""
    return [(m.group(m.lastindex), m.start(m.lastindex)) for m in SUSPECT.finditer(line_text)]


# ---- locating marks on the scan ------------------------------------------------------------


def _line_gray(img: Image.Image, line: BBox, pad: float = 0.15) -> tuple[np.ndarray, int, int]:
    W, H = img.size
    h = line.height
    x0, y0 = round(line.x0 * W), round(max(0.0, line.y0 - pad * h) * H)
    x1, y1 = round(line.x1 * W), round(min(1.0, line.y1 + pad * h) * H)
    gray = np.asarray(img.convert("L").crop((x0, y0, x1, y1)))
    return gray, x0, y0


def ink_segments(gray: np.ndarray, min_gap: int = 2) -> list[tuple[int, int]]:
    """Horizontal ink runs separated by blank columns (>= min_gap px)."""
    if gray.size == 0:
        return []
    _, ink = cv2.threshold(gray, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    cols = ink.sum(axis=0) > 0
    segs: list[tuple[int, int]] = []
    start, gap = None, 0
    for x, on in enumerate(cols):
        if on:
            if start is None:
                start = x
            gap = 0
        elif start is not None:
            gap += 1
            if gap >= min_gap:
                segs.append((start, x - gap + 1))
                start, gap = None, 0
    if start is not None:
        segs.append((start, len(cols) - gap))
    return [s for s in segs if s[1] - s[0] >= 2]


def _seg_bbox(img: Image.Image, gray: np.ndarray, ox: int, oy: int, seg: tuple[int, int]) -> BBox:
    W, H = img.size
    _, ink = cv2.threshold(gray[:, seg[0] : seg[1]], 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    rows = np.where(ink.sum(axis=1) > 0)[0]
    r0, r1 = (int(rows[0]), int(rows[-1]) + 1) if len(rows) else (0, gray.shape[0])
    m = 2
    return BBox(
        x0=max(0, ox + seg[0] - m) / W,
        y0=max(0, oy + r0 - m) / H,
        x1=min(W, ox + seg[1] + m) / W,
        y1=min(H, oy + r1 + m) / H,
    )


def word_gap_threshold(segs: list[tuple[int, int]]) -> float | None:
    """This line's letter-gap / word-gap boundary: Otsu split of the *log* gap sizes (letter and word
    gaps differ by a ratio; works whether letters touch or not). None when not clearly bimodal."""
    gaps = np.array([b[0] - a[1] for a, b in zip(segs, segs[1:], strict=False)], dtype=np.float64)
    if len(gaps) < 3:
        return None
    lg = np.log(gaps)
    best_t, best_var = None, 0.0
    for t in np.unique(lg)[1:]:
        lo, hi = lg[lg < t], lg[lg >= t]
        var = len(lo) * len(hi) * (hi.mean() - lo.mean()) ** 2
        if var > best_var:
            best_t, best_var = t, var
    if best_t is None:
        return None
    lo, hi = gaps[lg < best_t], gaps[lg >= best_t]
    if hi.min() < 1.5 * lo.max() and hi.mean() < 2.0 * lo.mean():
        return None  # not clearly two kinds of gaps
    return float((lo.max() + hi.min()) / 2)


def locate_between(img: Image.Image, line: Word, text_before: str, text_after: str) -> BBox | None:
    """Find the mark between the words `text_before` and `text_after` of a line.

    Primary: group ink into words using this line's own word-gap size; the first/last N word
    groups are the known words, the rest is the mark. Fallback: char-width consistency.

    The line is over-segmented at every blank column run; the boundaries are chosen so that the
    Latin text on both sides has the same average character width (same font), preferring real
    gaps. No font-size or resolution assumptions.
    """
    gray, ox, oy = _line_gray(img, line.bbox)
    segs = ink_segments(gray)
    if not segs:
        return None
    nwb, nwa = len(text_before.split()), len(text_after.split())
    t = word_gap_threshold(segs)
    if t is not None:
        words: list[tuple[int, int]] = [segs[0]]
        for sg in segs[1:]:
            if sg[0] - words[-1][1] >= t:
                words.append(sg)
            else:
                words[-1] = (words[-1][0], sg[1])
        if len(words) >= nwb + nwa + 1:
            mid = words[nwb : len(words) - nwa]
            return _seg_bbox(img, gray, ox, oy, (mid[0][0], mid[-1][1]))
    left, right = segs[0][0], segs[-1][1]
    nb, na = len(text_before.strip()), len(text_after.strip())
    h = gray.shape[0]
    # candidate boundaries: (position, gap size) at every gap, plus the line ends
    ends = [(s[1], (segs[i + 1][0] - s[1]) if i + 1 < len(segs) else h) for i, s in enumerate(segs)]
    starts = [(s[0], (s[0] - segs[i - 1][1]) if i > 0 else h) for i, s in enumerate(segs)]
    best, best_score = None, float("inf")
    for b1, g1 in [(left, h)] if nb == 0 else ends:
        for b2, g2 in [(right, h)] if na == 0 else starts:
            if b2 - b1 < 0.25 * h:  # the mark needs some width
                continue
            cw_l = (b1 - left) / nb if nb else None
            cw_r = (right - b2) / na if na else None
            if cw_l is not None and cw_r is not None:
                score = abs(cw_l - cw_r) / max(1e-6, (cw_l + cw_r) / 2)
            else:  # mark at a line end: only one side to calibrate against
                cw = cw_l or cw_r or 1.0
                score = abs((b2 - b1) - 2.5 * cw) / (2.5 * cw) * 0.5
            score -= 0.02 * min(g1, g2) / max(1, h)  # prefer real (wide) gaps
            if score < best_score:
                best, best_score = (b1, b2), score
    if best is None:
        return None
    # tighten to the ink actually inside the chosen range
    inside = [sg for sg in segs if sg[0] >= best[0] - 1 and sg[1] <= best[1] + 1]
    if not inside:
        return None
    return _seg_bbox(img, gray, ox, oy, (inside[0][0], inside[-1][1]))


def _proportional(img: Image.Image, line: Word, x_frac: float) -> BBox | None:
    gray, ox, oy = _line_gray(img, line.bbox)
    segs = ink_segments(gray)
    if not segs:
        return None
    x = x_frac * gray.shape[1]
    seg = min(segs, key=lambda s: 0 if s[0] <= x <= s[1] else min(abs(x - s[0]), abs(x - s[1])))
    return _seg_bbox(img, gray, ox, oy, seg)


def plausible_mark(box: BBox | None, line: Word, img: Image.Image) -> BBox | None:
    """An inline mark is about one text line tall and a few letters wide; anything else means the
    locator grabbed words (or two lines) — reject rather than verify the wrong thing."""
    if box is None:
        return None
    W, H = img.size
    lh = line.bbox.height * H
    w, h = box.width * W, box.height * H
    if h > 1.6 * lh or w > 3.5 * lh or w < 0.3 * lh:
        return None
    if box.cy < line.bbox.y0 or box.cy > line.bbox.y1:
        return None
    return box


def locate_by_char(img: Image.Image, line: Word, index: int) -> BBox | None:
    """Mark that OCR read as the character at `index` of the line text (e.g. 'Ibrahim&to')."""
    box = locate_between(img, line, line.text[:index], line.text[index + 1 :])
    box = box or _proportional(img, line, (index + 0.5) / max(1, len(line.text)))
    return plausible_mark(box, line, img)


def locate_after_word(img: Image.Image, line: Word, word_index: int) -> BBox | None:
    """Mark printed right after the `word_index`-th word of the line (OCR dropped it)."""
    words = line.text.split()
    if word_index >= len(words):
        return None
    # Punctuation OCR glued to the word ('Ibrahim!') is printed *after* the mark.
    m = re.match(r"^(.*?[^\W_])([^\w\s]+)$", words[word_index])
    before = " ".join(words[:word_index] + [m.group(1) if m else words[word_index]])
    after = " ".join(([m.group(2)] if m else []) + words[word_index + 1 :])
    box = locate_between(img, line, before, after)
    if box is None:
        end_char = len(" ".join(words[: word_index + 1]))
        box = _proportional(img, line, (end_char + 1) / max(1, len(line.text)))
    return plausible_mark(box, line, img)


# ---- comparing marks -------------------------------------------------------------------------


def descriptor(crop: Image.Image) -> np.ndarray | None:
    """Ink shape normalized for position/scale: tight ink box, square padded, 32x32, zero-mean."""
    g = np.asarray(crop.convert("L"))
    if g.size == 0:
        return None
    _, ink = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ys, xs = np.where(ink > 0)
    if len(xs) < 4:
        return None
    ink = ink[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    h, w = ink.shape
    side = max(h, w)
    sq = np.zeros((side, side), np.uint8)
    sq[(side - h) // 2 : (side - h) // 2 + h, (side - w) // 2 : (side - w) // 2 + w] = ink
    d = cv2.resize(sq, (DESC, DESC), interpolation=cv2.INTER_AREA).astype(np.float32)
    d = cv2.GaussianBlur(d, (3, 3), 0)  # tolerate small print/scan differences
    d -= d.mean()
    n = np.linalg.norm(d)
    if n < 1e-6:
        return None
    aspect = np.float32(w / h)
    return np.append((d / n).ravel(), aspect)


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    if abs(float(a[-1]) - float(b[-1])) > 0.35 * max(float(a[-1]), float(b[-1])):
        return 0.0  # clearly different proportions
    return float(np.dot(a[:-1], b[:-1]))


def crop(img: Image.Image, b: BBox) -> Image.Image:
    W, H = img.size
    return img.crop((round(b.x0 * W), round(b.y0 * H), round(b.x1 * W), round(b.y1 * H)))


class GlyphBook:
    """Prototype registry: assigns occurrences to clusters of the same printed mark."""

    def __init__(self, doc: Document, book_dir, ocr=None):
        self.doc = doc
        self.book_dir = book_dir
        self.ocr = ocr  # optional OcrEngine: rejects crops that are actually Latin words
        self.out_dir = book_dir / "img" / "glyphs"
        self.protos: list[tuple[str, np.ndarray]] = []
        for g in doc.glyphs.values():
            if g.image and (p := book_dir / g.image.path).exists() and (d := descriptor(Image.open(p))) is not None:
                self.protos.append((g.key, d))

    def assign(self, img: Image.Image, hit: GlyphHit, page_index: int = 0) -> str | None:
        c = crop(img, hit.bbox)
        d = descriptor(c)
        if d is None or self._is_word(c):
            return None
        best = max(((similarity(d, pd), k) for k, pd in self.protos), default=(0.0, None))
        if best[0] >= MATCH:
            key = best[1]
        else:
            key = f"g{len(self.doc.glyphs):03d}"
            while key in self.doc.glyphs:
                key = f"g{int(key[1:]) + 1:03d}"
            self.out_dir.mkdir(parents=True, exist_ok=True)
            path = self.out_dir / f"{key}.png"
            c.save(path)
            self.doc.glyphs[key] = Glyph(
                key=key,
                image=ImageRef(path=str(path.relative_to(self.book_dir)), width=c.width, height=c.height),
                symbol=hit.symbol,
                sample_page=page_index,
                sample_bbox=hit.bbox,
            )
            self.protos.append((key, d))
        hit.key = key
        self.doc.glyphs[key].occurrences += 1
        return key

    def _is_word(self, c: Image.Image) -> bool:
        if self.ocr is None:
            return False
        try:
            [(text, score)] = self.ocr.recognize([np.asarray(c.convert("RGB"))], "latin")
        except Exception:
            return False
        letters = sum(ch.isascii() and ch.isalpha() for ch in text)
        # Even a single Latin letter read confidently means the crop is text, not a special mark.
        return (letters >= 2 and score >= 0.85) or (letters == 1 and score >= 0.9)


def harmonize(doc: Document, book_dir) -> int:
    """The same printed mark must get the same text. Clusters that look alike (>= FAMILY) form a
    family; if their verified texts disagree, a 2/3 majority (by occurrences) wins, otherwise the
    whole family is shown as printed images. Returns the number of clusters changed."""
    from collections import Counter

    keys = [k for k, g in doc.glyphs.items() if g.image and g.resolved]
    desc = {}
    for k in keys:
        path = book_dir / doc.glyphs[k].image.path
        if path.exists() and (dk := descriptor(Image.open(path))) is not None:
            desc[k] = dk
    keys = [k for k in keys if k in desc]
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            if similarity(desc[a], desc[b]) >= FAMILY:
                parent[find(a)] = find(b)
    families: dict[str, list[str]] = {}
    for k in keys:
        families.setdefault(find(k), []).append(k)
    changed = 0
    for members in families.values():
        votes = Counter()
        for k in members:
            g = doc.glyphs[k]
            votes[g.text if g.render == "text" else None] += g.occurrences
        if len(votes) <= 1:
            continue
        winner, n = votes.most_common(1)[0]
        total = sum(votes.values())
        ref = next((doc.glyphs[k] for k in members if doc.glyphs[k].text == winner), None)
        for k in members:
            g = doc.glyphs[k]
            if winner is not None and n >= 2 / 3 * total and ref is not None:
                new = ("text", ref.text, ref.lang)
            else:
                new = ("image", g.text, g.lang)
            if (g.render, g.text) != new[:2]:
                g.render, g.text, g.lang = new
                g.kind = (g.kind or "") + "+harmonized"
                changed += 1
    return changed


class Glyphs:
    name = "glyphs"

    def __init__(self, ocr=None):
        self.ocr = ocr

    def run(self, doc: Document, ctx: Context) -> Document:
        doc.glyphs = {}
        book = GlyphBook(doc, ctx.book_dir, self.ocr)
        for page in doc.pages:
            content = {b.id for b in page.blocks if b.role == Role.CONTENT}
            page.glyph_hits = [h for h in page.glyph_hits if h.block_id in content]  # not headers/decor
            if not page.glyph_hits:
                continue
            img = Image.open(ctx.book_dir / page.image.path).convert("RGB")
            kept = []
            for hit in page.glyph_hits:
                block = page.block(hit.block_id)
                tight = _tighten(img, block, hit)
                if tight is None:
                    continue
                hit.bbox = tight
                if book.assign(img, hit, page.index):
                    kept.append(hit)
            page.glyph_hits = kept
        apply_glyphs(doc)  # unverified marks become the printed image (better than '&')
        return doc


def _tighten(img: Image.Image, block: Block | None, hit: GlyphHit) -> BBox | None:
    """Refine the approximate box from the text stage to the mark's own ink run."""
    if block is None:
        return None
    for line in block.lines:
        if line.bbox.intersection(hit.bbox) > 0:
            idx = _char_index_near(line, hit)
            if idx is not None:
                return locate_by_char(img, line, idx)
    return None


def _char_index_near(line: Word, hit: GlyphHit) -> int | None:
    positions = [i for i, ch in enumerate(line.text) if ch == hit.symbol]
    if not positions:
        return None
    cx = (hit.bbox.cx - line.bbox.x0) / max(1e-6, line.bbox.width) * len(line.text)
    return min(positions, key=lambda i: abs(i - cx))


# ---- applying decisions to the text ----------------------------------------------------------


def apply_glyphs(doc: Document) -> int:
    """Replace each mark occurrence by a glyph span: verified text, or the printed image."""
    done = 0
    for page in doc.pages:
        by_block: dict[str, list[GlyphHit]] = {}
        for hit in page.glyph_hits:
            if hit.key in doc.glyphs:
                by_block.setdefault(hit.block_id, []).append(hit)
        for bid, hits in by_block.items():
            block = page.block(bid)
            if block is None:
                continue
            for hit in sorted(hits, key=lambda h: (h.symbol, -h.ordinal)):
                if _replace(block, hit, doc.glyphs[hit.key]):
                    done += 1
        page.glyph_hits = []
    return done


def glyph_span(glyph: Glyph) -> Span | None:
    if glyph.render == "text":
        if not glyph.text:
            return None  # verified noise: drop
        rtl = any("֐" <= c <= "ࣿ" or "יִ" <= c <= "﻿" for c in glyph.text)
        return Span(text=glyph.text, lang=glyph.lang, dir="rtl" if rtl else None, glyph=glyph.key)
    return Span(text="", glyph=glyph.key)  # printed mark kept as an inline image


def _replace(block: Block, hit: GlyphHit, glyph: Glyph) -> bool:
    seen = 0
    for i, span in enumerate(block.spans):
        if span.glyph:
            continue
        start = 0
        while (j := span.text.find(hit.symbol, start)) != -1:
            if seen == hit.ordinal:
                before = span.text[:j].rstrip(" ")
                after = span.text[j + len(hit.symbol) :].lstrip(" ")
                g = glyph_span(glyph)
                joiner = "" if not after or after.startswith((",", ".", ";", ":", ")", "!", "?")) else " "
                parts = [span.model_copy(update={"text": before + (" " if before else "")})]
                if g is not None:
                    parts.append(g)
                    parts.append(span.model_copy(update={"text": joiner + after}))
                else:
                    parts[0] = span.model_copy(update={"text": before + joiner + after})
                block.spans[i : i + 1] = [p for p in parts if p.text or p.glyph]
                return True
            seen += 1
            start = j + 1
    return False


def refresh_glyph_spans(doc: Document) -> int:
    """After verification, update every span of a glyph (image -> text, or drop noise)."""
    changed = 0
    for page in doc.pages:
        for b in page.blocks:
            new: list[Span] = []
            for s in b.spans:
                g = doc.glyphs.get(s.glyph) if s.glyph else None
                if g is None:
                    new.append(s)
                    continue
                gs = glyph_span(g)
                if gs is None:
                    changed += 1
                    continue
                if gs.text != s.text:
                    changed += 1
                    if gs.text and b.source == Source.PDF:
                        b.source = Source.LLM
                new.append(gs)
            b.spans = new
    return changed


def approx_char_bbox(line: BBox, text: str, index: int) -> BBox:
    """Rough box of a character by its proportional position (refined later on the scan)."""
    n = max(1, len(text))
    w = line.width / n
    cx = line.x0 + (index + 0.5) * w
    return BBox(x0=max(0.0, cx - 1.5 * w), y0=line.y0, x1=min(1.0, cx + 1.5 * w), y1=line.y1)


def placeholder_hits(page_blocks: list[Block], img: Image.Image) -> list[GlyphHit]:
    """Marks the LLM inserted (PLACEHOLDER in text + proposed text): locate each on the scan."""
    hits = []
    for b in page_blocks:
        ordinal = 0
        for span in b.spans:
            if span.glyph:
                continue
            for j, ch in enumerate(span.text):
                if ch != PLACEHOLDER:
                    continue
                prev_word = _prev_word(span.text[:j])
                box = _locate_inserted(img, b, prev_word, ordinal)
                if box is not None:
                    hits.append(GlyphHit(block_id=b.id, symbol=PLACEHOLDER, ordinal=ordinal, bbox=box))
                ordinal += 1
    return hits


def _prev_word(text: str) -> str:
    words = re.findall(r"[^\W\d_]+", text)
    return words[-1] if words else ""


def _locate_inserted(img: Image.Image, block: Block, prev_word: str, ordinal: int) -> BBox | None:
    if not prev_word:
        return None
    candidates = []
    for line in block.lines:
        words = line.text.split()
        for k, w in enumerate(words):
            if re.sub(r"\W", "", w).lower() == prev_word.lower():
                candidates.append((line, k))
    if not candidates:
        return None
    line, k = candidates[min(ordinal, len(candidates) - 1)]
    return locate_after_word(img, line, k)


def strip_placeholders(doc: Document) -> int:
    """Placeholders that could not be located/verified: remove (missing beats wrong)."""
    n = 0
    for page in doc.pages:
        for b in page.blocks:
            for s in b.spans:
                if PLACEHOLDER in s.text:
                    n += s.text.count(PLACEHOLDER)
                    s.text = re.sub(rf"\s?{PLACEHOLDER}", "", s.text)
    return n
