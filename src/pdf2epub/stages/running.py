"""Running elements (headers, footers, page numbers, side tabs) by cross-page repetition.

Book-level pass over IR — cheap, no images. Also derives printed page labels and left/right sides.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from pdf2epub.ir import Block, BlockType, Document, Page, Role
from pdf2epub.pipeline import Context

TOP, BOTTOM, SIDE = "top", "bottom", "side"
_NUM_RE = re.compile(r"^\W*(\d{1,4}|[ivxlcdm]{1,7})\W*$", re.IGNORECASE)


def zone(b: Block) -> str | None:
    if b.bbox.cy < 0.12 and b.bbox.height < 0.08:
        return TOP
    if b.bbox.cy > 0.88 and b.bbox.height < 0.08:
        return BOTTOM
    if (b.bbox.cx < 0.08 or b.bbox.cx > 0.92) and b.bbox.width < 0.08:
        return SIDE
    return None


def norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", re.sub(r"\d+", "#", text.lower()))


class RunningElements:
    name = "running"

    def run(self, doc: Document, ctx: Context) -> Document:
        pages = doc.pages
        threshold = max(2, math.ceil(ctx.settings.running_min_repeat * len(pages)))

        counts: Counter[tuple[str, str]] = Counter()
        for page in pages:
            seen = {(z, norm(b.text)) for b in page.blocks if (z := zone(b)) and norm(b.text)}
            counts.update(seen)

        for page in pages:
            for b in page.blocks:
                z = zone(b)
                text = b.text.strip()
                if z in (TOP, BOTTOM) and _NUM_RE.match(text):
                    b.role = Role.PAGE_NUMBER
                elif z and norm(text) and counts[(z, norm(text))] >= threshold:
                    b.role = {TOP: Role.HEADER, BOTTOM: Role.FOOTER, SIDE: Role.DECORATION}[z]
                elif b.role in (Role.HEADER, Role.FOOTER, Role.PAGE_NUMBER) and not z:
                    # Layout model said running element, but it sits in the body: keep as content.
                    b.role = Role.CONTENT
                elif "aside" in b.flags and z:
                    b.role = Role.DECORATION
                if b.role != Role.CONTENT and b.type in (BlockType.TITLE, BlockType.HEADING):
                    b.type = BlockType.PARAGRAPH
        assign_labels(pages)
        return doc


def assign_labels(pages: list[Page]) -> None:
    """Printed page labels from detected page numbers; gaps filled by the dominant offset."""
    found: dict[int, tuple[int, float]] = {}
    for page in pages:
        for b in page.blocks:
            if b.role == Role.PAGE_NUMBER and (m := re.search(r"\d{1,4}", b.text)):
                found[page.index] = (int(m.group(0)), b.bbox.cx)
                break
    offsets = Counter(num - idx for idx, (num, _) in found.items())
    if not offsets:
        return
    offset, votes = offsets.most_common(1)[0]
    if votes < max(2, len(found) // 3):
        return
    for page in pages:
        num = page.index + offset
        if num >= 1:
            page.label = str(num)
    # Which parity sits on the right-hand side? Decide from where page numbers are printed.
    side_votes: dict[int, list[float]] = defaultdict(list)
    for idx, (num, cx) in found.items():
        if num - idx == offset:
            side_votes[num % 2].append(cx)
    odd = side_votes.get(1, [])
    even = side_votes.get(0, [])
    if odd and even and abs(sum(odd) / len(odd) - sum(even) / len(even)) > 0.2:
        odd_right = sum(odd) / len(odd) > sum(even) / len(even)
        for page in pages:
            if page.label:
                is_odd = int(page.label) % 2 == 1
                page.side = "right" if is_odd == odd_right else "left"
