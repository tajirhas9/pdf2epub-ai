"""Two-page spreads: one picture printed across the gutter of facing pages.

1. Pair facing pages (left/right from page labels; consecutive pairs when unknown).
2. Candidates: visual blocks touching the gutter on both pages with overlapping heights.
3. Register the right half to the left half by *continuity across the seam* (spreads rarely
   share duplicated content, so feature matching is not enough): search vertical shift, scale and
   horizontal overlap maximizing normalized cross-correlation of the seam columns; estimate
   residual rotation from shift drift along the seam and correct it.
4. QA: seam NCC, shift drift along the seam, edge (line) continuity. Pass -> stitched image with
   photometric matching; fail -> halves side by side (flagged); no continuity -> not a spread.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

from pdf2epub.ir import BBox, Block, BlockType, Document, FigureLayout, Page, Role, Source, Spread, SpreadStatus
from pdf2epub.pipeline import Context
from pdf2epub.stages.figures import save_image
from pdf2epub.stages.order import page_is_rtl

VISUAL = {BlockType.FIGURE, BlockType.TABLE}
# Band agreement estimates the vertical alignment but cannot tell one picture from two
# (horizons/frames of unrelated photos line up too). So without proven seam continuity a pair is
# only a *candidate*, decided by the LLM page-pair check; left unconfirmed, the halves stay separate.
REJECT_BAND = 0.2
EDGE_AGREE = 0.02  # top/bottom edges of the two halves within this page fraction
GAP_PX = 4
SEAM_STRONG = 0.6


@dataclass
class Registration:
    dy: float  # right half shift (px, in left-page pixels) to align with left
    scale: float
    overlap: int  # columns of the right half duplicated in the left half
    angle: float  # degrees, rotation applied to the right half
    ncc: float  # seam continuity (exact join quality)
    drift: float  # px variation of best shift along the seam
    continuity: float  # mean px mismatch of edges crossing the seam
    band_ncc: float = 0.0  # agreement of horizontal structure on both sides (works across a gap)


class Spreads:
    name = "spreads"

    def run(self, doc: Document, ctx: Context) -> Document:
        doc.spreads = []
        rtl_book = sum(page_is_rtl(p) for p in doc.pages) > len(doc.pages) / 2
        for a, b in zip(doc.pages, doc.pages[1:], strict=False):
            if not facing(a, b):
                continue
            left, right = (b, a) if rtl_book else (a, b)
            for lb, rb in candidates(left, right, ctx.settings.spread_gutter_margin):
                spread = Spread(left_page=left.index, right_page=right.index, left_block=lb.id, right_block=rb.id)
                if Role.BACKGROUND in (lb.role, rb.role):
                    spread.background = True
                    lb.role = rb.role = Role.BACKGROUND
                    spread.status = SpreadStatus.MERGED
                    doc.spreads.append(spread)
                    continue
                self._merge(spread, left, right, lb, rb, ctx)
                if spread.status != SpreadStatus.REJECTED:
                    doc.spreads.append(spread)
        return doc

    def _merge(self, spread: Spread, left: Page, right: Page, lb: Block, rb: Block, ctx: Context) -> None:
        L = _load(left, ctx)
        R = _load(right, ctx)
        # Normalize the right page to the left page's scale (scans differ by a few pixels).
        if R.shape[0] != L.shape[0]:
            f = L.shape[0] / R.shape[0]
            R = cv2.resize(R, (round(R.shape[1] * f), L.shape[0]), interpolation=cv2.INTER_LANCZOS4)
        y0 = round(min(lb.bbox.y0, rb.bbox.y0) * L.shape[0])
        y1 = round(max(lb.bbox.y1, rb.bbox.y1) * L.shape[0])
        lx0 = round(lb.bbox.x0 * L.shape[1])
        rx1 = round(rb.bbox.x1 * R.shape[1])
        le = content_edge(L, y0, y1, from_right=True)
        rs = content_edge(R, y0, y1, from_right=False)
        reg = register(L[:, :le], R[:, rs:], y0, y1)
        spread.qa = {
            "ncc": round(reg.ncc, 3),
            "drift_px": round(reg.drift, 2),
            "continuity_px": round(reg.continuity, 2),
            "dy": round(reg.dy, 2),
            "scale": round(reg.scale, 4),
            "overlap": reg.overlap,
            "angle": round(reg.angle, 3),
            "band_ncc": round(reg.band_ncc, 3),
        }
        spread.score = max(reg.ncc, reg.band_ncc)
        cut = cut_labels(left, right, lb, rb, ctx.settings.langs[0] if ctx.settings.langs else "en")
        spread.qa["cut_labels"] = cut
        s = ctx.settings
        seamless = (
            reg.ncc >= s.spread_min_ncc
            and reg.drift <= s.spread_max_seam_px
            and reg.continuity <= s.spread_max_seam_px * 1.5
        )
        edges_agree = abs(lb.bbox.y0 - rb.bbox.y0) <= EDGE_AGREE and abs(lb.bbox.y1 - rb.bbox.y1) <= EDGE_AGREE
        left_part = L[y0:y1, lx0:le]
        right_full = R[:, rs:rx1]
        if seamless:
            img, spread.status = stitch(left_part, right_full, reg, y0, y1), SpreadStatus.MERGED
        elif cut >= 1 and (reg.band_ncc >= REJECT_BAND or edges_agree):
            # Labels cut at the gutter: the picture continues. Gutter content was lost in the scan:
            # aligned halves with a thin gap (nothing invented).
            img, spread.status = aligned_gap(left_part, right_full, reg, y0, y1), SpreadStatus.SIDE_BY_SIDE
        elif reg.band_ncc >= REJECT_BAND or edges_agree:
            # Gutter content possibly lost in the scan: aligned halves with a thin gap (nothing invented).
            img, spread.status = aligned_gap(left_part, right_full, reg, y0, y1), SpreadStatus.CANDIDATE
        else:
            spread.status = SpreadStatus.REJECTED
            return
        spread.block_id = left.new_block_id()
        spread.image = save_image(Image.fromarray(img), ctx.book_dir / "img" / "figs", spread.block_id, ctx)
        if spread.status == SpreadStatus.CANDIDATE:
            for part in (lb, rb):
                part.flags.append("spread-candidate")  # LLM page-pair check decides; halves stay separate until then
            return
        apply_spread(left, right, spread, lb, rb)


def apply_spread(left: Page, right: Page, spread: Spread, lb: Block, rb: Block) -> Block:
    """Replace the two halves by one figure on the left page (halves become non-content)."""
    merged = Block(
        id=spread.block_id,
        type=BlockType.FIGURE,
        bbox=BBox(x0=lb.bbox.x0, y0=lb.bbox.y0, x1=1.0, y1=lb.bbox.y1),
        source=Source.MERGED,
        confidence=spread.score,
        layout=FigureLayout.SPREAD,
        image=spread.image,
        order=lb.order,
        flags=["spread"] + ([] if spread.status == SpreadStatus.MERGED else ["spread-gap"]),
    )
    left.blocks.append(merged)
    for part in (lb, rb):
        part.role = Role.DECORATION
        part.flags = [f for f in part.flags if f != "spread-candidate"] + [f"spread-part:{merged.id}"]
    for page in (left, right):
        for c in page.blocks:
            if c.caption_of in (lb.id, rb.id):
                c.caption_of = merged.id if page is left else None
    return merged


def _fragment(token: str, lang: str) -> bool:
    from pdf2epub.textutil import is_known

    core = "".join(c for c in token if c.isalpha())
    if not core:
        return False
    known = is_known(core, lang)
    return (known is False) or len(core) <= 2


def cut_labels(left: Page, right: Page, lb: Block, rb: Block, lang: str) -> int:
    """Count text lines cut by the gutter inside the candidate: a left-page line ending at the
    right edge with a word fragment ('Gulf of Ade'), or a right-page line starting at the left edge
    with a fragment / lowercase start in a capitalized label ('ian Peninsula Physical')."""
    count = 0
    y0, y1 = min(lb.bbox.y0, rb.bbox.y0) - 0.02, max(lb.bbox.y1, rb.bbox.y1) + 0.04
    for w in left.edge_lines:
        toks = w.text.split()
        if toks and w.bbox.x1 > 0.96 and y0 <= w.bbox.cy <= y1 and _fragment(toks[-1], lang):
            count += 1
    for w in right.edge_lines:
        toks = w.text.split()
        if not toks or w.bbox.x0 > 0.04 or not (y0 <= w.bbox.cy <= y1):
            continue
        lower_start = toks[0][:1].islower() and len(toks) > 1 and toks[1][:1].isupper()
        if lower_start or _fragment(toks[0], lang):
            count += 1
    return count


def facing(a: Page, b: Page) -> bool:
    if a.side and b.side:
        return {a.side, b.side} == {"left", "right"} and a.side != b.side
    return True  # unknown sides: let seam continuity decide


def candidates(left: Page, right: Page, margin: float) -> list[tuple[Block, Block]]:
    lc = [
        b
        for b in left.blocks
        if b.type in VISUAL and b.role in (Role.CONTENT, Role.BACKGROUND) and b.bbox.x1 >= 1 - margin
    ]
    rc = [
        b
        for b in right.blocks
        if b.type in VISUAL and b.role in (Role.CONTENT, Role.BACKGROUND) and b.bbox.x0 <= margin
    ]
    pairs = []
    for lb in lc:
        for rb in rc:
            ov = min(lb.bbox.y1, rb.bbox.y1) - max(lb.bbox.y0, rb.bbox.y0)
            if ov > 0.5 * min(lb.bbox.height, rb.bbox.height):
                pairs.append((lb, rb))
    # One partner per block: keep best vertical agreement.
    pairs.sort(key=lambda p: abs(p[0].bbox.cy - p[1].bbox.cy))
    used: set[str] = set()
    out = []
    for lb, rb in pairs:
        if lb.id not in used and rb.id not in used:
            out.append((lb, rb))
            used |= {lb.id, rb.id}
    return out


def _load(page: Page, ctx: Context) -> np.ndarray:
    with Image.open(ctx.book_dir / page.image.path) as im:
        return np.asarray(im.convert("RGB"))


def content_edge(img: np.ndarray, y0: int, y1: int, from_right: bool, max_trim: float = 0.03) -> int:
    """Index just past the last picture column at the gutter side, skipping blank/binding columns."""
    band = img[y0:y1].astype(np.float32)
    W = band.shape[1]
    limit = max(1, int(W * max_trim))
    cols = range(W - 1, W - 1 - limit, -1) if from_right else range(limit)
    for x in cols:
        col = band[:, x]
        if col.std() > 12 and abs(col.mean() - 255) > 8 and col.mean() > 12:
            return x + 1 if from_right else x
    return W if from_right else 0


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    d = math.sqrt(float((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / d) if d > 1e-6 else 0.0


def _best_shift(lcol: np.ndarray, rcol: np.ndarray, max_dy: int) -> tuple[int, float]:
    h = len(lcol)
    best = (0, -1.0)
    for dy in range(-max_dy, max_dy + 1):
        a = lcol[max(0, dy) : h + min(0, dy)]
        b = rcol[max(0, -dy) : h + min(0, -dy)]
        if len(a) < h // 2:
            continue
        s = _ncc(a, b)
        if s > best[1]:
            best = (dy, s)
    return best


def _profile(gray: np.ndarray, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
    """Column-band profile along the seam, high-passed so lines/edges dominate smooth shading."""
    col = gray[y0:y1, x0:x1].mean(axis=1)
    return col - cv2.GaussianBlur(col.reshape(-1, 1), (1, 31), 0).ravel()


def _left_profile(Lg: np.ndarray, ov: int, y0: int, y1: int, band: int) -> np.ndarray:
    """Left-half columns that should match the right half's first columns under overlap `ov`."""
    W = Lg.shape[1]
    if ov <= 0:
        return _profile(Lg, y0, y1, W - 1, W)  # butt join: last column vs first column
    k = min(band, ov)
    return _profile(Lg, y0, y1, W - ov, W - ov + k)


def _right_profile(Rg: np.ndarray, ov: int, y0: int, y1: int, band: int) -> np.ndarray:
    k = 1 if ov <= 0 else min(band, ov)
    return _profile(Rg, y0, y1, 0, k)


def _band_profile(gray: np.ndarray, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
    """Row profile of a wide band, high-passed: horizontal structure (frames, coastlines,
    baselines) that continues across the gutter even when the gutter itself was lost."""
    rows = np.abs(np.diff(gray[y0:y1, x0:x1], axis=0)).mean(axis=1)
    return rows - cv2.GaussianBlur(rows.reshape(-1, 1), (1, 41), 0).ravel()


def register(
    L: np.ndarray, R: np.ndarray, y0: int, y1: int, band: int = 3, max_overlap: int = 40, wide: int = 40
) -> Registration:
    """Align R (right half, starting at its gutter edge) to L (left half, ending at its gutter edge).

    1. Vertical shift + scale from wide bands on both sides (robust to lost gutter content).
    2. Exact seam continuity (overlap search) around that shift decides seamless vs. gap.
    """
    Lg = cv2.cvtColor(L, cv2.COLOR_RGB2GRAY).astype(np.float32)
    max_dy = max(4, round((y1 - y0) * 0.04))
    w = min(wide, L.shape[1] // 2, R.shape[1] // 2)
    lband = _band_profile(Lg, y0, y1, L.shape[1] - w, L.shape[1])
    best = Registration(0, 1.0, 0, 0.0, -1.0, 0.0, 0.0, -1.0)
    for scale in (0.985, 0.99, 0.995, 1.0, 1.005, 1.01, 1.015):
        Rg = cv2.cvtColor(_transform(R, scale, 0.0), cv2.COLOR_RGB2GRAY).astype(np.float32)
        dy, bs = _best_shift(lband, _band_profile(Rg, y0, y1, 0, w), max_dy)
        # Seam: exact continuity at that shift (± a little), over plausible overlaps.
        seam = -1.0
        best_ov, best_dy = 0, dy
        for ov in range(min(max_overlap, L.shape[1] - band)):
            sdy, ss = _best_shift(_left_profile(Lg, ov, y0, y1, band), _right_profile(Rg, ov, y0, y1, band), max_dy)
            if abs(sdy - dy) <= 2 and ss > seam + 1e-4:
                seam, best_ov, best_dy = ss, ov, sdy
        # A strong seam (exact continuity) wins; otherwise trust the band. Small prior for scale 1.
        strong = seam >= SEAM_STRONG
        score = (1.0 + seam if strong else bs) - 2.0 * abs(scale - 1.0)
        cand = Registration(best_dy if strong else dy, scale, best_ov, 0.0, seam, 0.0, 0.0, bs)
        best_score = (1.0 + best.ncc if best.ncc >= SEAM_STRONG else best.band_ncc) - 2.0 * abs(best.scale - 1.0)
        if best.band_ncc < 0 or score > best_score + 1e-4:
            best = cand
    # Residual rotation: best shift per seam segment; linear drift -> angle.
    drift, slope = _drift(Lg, R, best, y0, y1, band)
    if drift > 1.0:
        angle = math.degrees(math.atan(slope))
        Rg = cv2.cvtColor(_transform(R, best.scale, angle), cv2.COLOR_RGB2GRAY).astype(np.float32)
        dy, s = _best_shift(
            _left_profile(Lg, best.overlap, y0, y1, band), _right_profile(Rg, best.overlap, y0, y1, band), max_dy
        )
        rotated = Registration(dy, best.scale, best.overlap, angle, s, 0.0, 0.0, best.band_ncc)
        d2, _ = _drift(Lg, R, rotated, y0, y1, band)
        if d2 < drift and s >= best.ncc - 0.05:
            best, drift = rotated, d2
    best.drift = drift
    best.continuity = _continuity(Lg, R, best, y0, y1)
    return best


def _transform(R: np.ndarray, scale: float, angle: float) -> np.ndarray:
    if scale == 1.0 and angle == 0.0:
        return R
    # Rotate/scale about the gutter-side centre so the seam stays put.
    M = cv2.getRotationMatrix2D((0.0, R.shape[0] / 2), angle, scale)
    return cv2.warpAffine(R, M, (R.shape[1], R.shape[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def _drift(Lg: np.ndarray, R: np.ndarray, reg: Registration, y0: int, y1: int, band: int) -> tuple[float, float]:
    Rg = cv2.cvtColor(_transform(R, reg.scale, reg.angle), cv2.COLOR_RGB2GRAY).astype(np.float32)
    seg = (y1 - y0) // 3
    if seg < 40:
        return 0.0, 0.0
    lp = _left_profile(Lg, reg.overlap, y0, y1, band)
    rp = _right_profile(Rg, reg.overlap, 0, Rg.shape[0], band)
    centers, shifts = [], []
    for k in range(3):
        a, b = k * seg, (k + 1) * seg
        lcol = lp[a:b]
        # right profile shifted by the global dy, then searched locally
        start = y0 + a - round(reg.dy)
        rcol = rp[max(0, start) : max(0, start) + (b - a)]
        if len(rcol) != len(lcol):
            continue
        dy, s = _best_shift(lcol, rcol, 6)
        if s > 0.3:
            centers.append((a + b) / 2)
            shifts.append(reg.dy + dy)
    if len(shifts) < 2:
        return 0.0, 0.0
    slope = float(np.polyfit(centers, shifts, 1)[0])
    return float(max(shifts) - min(shifts)), slope


def _continuity(Lg: np.ndarray, R: np.ndarray, reg: Registration, y0: int, y1: int) -> float:
    """Mean |Δy| between strong transitions on both sides of the seam (lines crossing it)."""
    Rg = cv2.cvtColor(_transform(R, reg.scale, reg.angle), cv2.COLOR_RGB2GRAY).astype(np.float32)
    W = Lg.shape[1]
    lx = W - reg.overlap if reg.overlap else W - 1
    lcol = Lg[y0:y1, lx]
    # Aligned right column: row j of the left page shows right-page row j - dy.
    rcol = np.roll(Rg[:, 0], round(reg.dy))[y0:y1]
    le = _edges(lcol)
    re_ = _edges(rcol)
    if len(le) == 0 or len(re_) == 0:
        return 0.0
    d = [min(abs(e - r) for r in re_) for e in le]
    d = [v for v in d if v <= 8]  # unmatched edges: content legitimately different
    return float(np.mean(d)) if d else 0.0


def _edges(col: np.ndarray) -> np.ndarray:
    g = np.abs(np.diff(cv2.GaussianBlur(col.reshape(-1, 1), (1, 5), 0).ravel()))
    thr = max(15.0, float(np.percentile(g, 97)))
    idx = np.where((g[1:-1] > thr) & (g[1:-1] >= g[:-2]) & (g[1:-1] >= g[2:]))[0] + 1
    return idx


def stitch(left: np.ndarray, right_full: np.ndarray, reg: Registration, y0: int, y1: int) -> np.ndarray:
    R = _transform(right_full, reg.scale, reg.angle)
    M = np.float32([[1, 0, 0], [0, 1, reg.dy]])
    R = cv2.warpAffine(R, M, (R.shape[1], R.shape[0]), borderMode=cv2.BORDER_REPLICATE)[y0:y1]
    if reg.overlap:
        R = R[:, reg.overlap :]
    # Photometric match on the seam band (avoid a brightness/colour jump).
    band = 8
    lb = left[:, -band:].reshape(-1, 3).astype(np.float32).mean(axis=0)
    rb = R[:, :band].reshape(-1, 3).astype(np.float32).mean(axis=0)
    gain = np.clip((lb + 1) / (rb + 1), 0.85, 1.18)
    R = np.clip(R.astype(np.float32) * gain, 0, 255).astype(np.uint8)
    out = np.concatenate([left, R], axis=1)
    # Feather a few columns across the seam.
    seam, w = left.shape[1], 3
    if seam - w > 0 and seam + w < out.shape[1]:
        for i in range(-w, w):
            t = (i + w) / (2 * w)
            src_l = out[:, seam - 1].astype(np.float32)
            src_r = out[:, seam].astype(np.float32)
            if abs(i) < w:
                out[:, seam + i] = np.clip(out[:, seam + i] * 0.5 + ((1 - t) * src_l + t * src_r) * 0.5, 0, 255)
    return out


def aligned_gap(left: np.ndarray, right_full: np.ndarray, reg: Registration, y0: int, y1: int) -> np.ndarray:
    R = _transform(right_full, reg.scale, reg.angle)
    M = np.float32([[1, 0, 0], [0, 1, reg.dy]])
    R = cv2.warpAffine(R, M, (R.shape[1], R.shape[0]), borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))[
        y0:y1
    ]
    return side_by_side(left, R, gap=GAP_PX)


def side_by_side(left: np.ndarray, right: np.ndarray, gap: int = 6) -> np.ndarray:
    h = max(left.shape[0], right.shape[0])
    pad = lambda a: np.pad(a, ((0, h - a.shape[0]), (0, 0), (0, 0)), constant_values=255)  # noqa: E731
    return np.concatenate([pad(left), np.full((h, gap, 3), 255, np.uint8), pad(right)], axis=1)
