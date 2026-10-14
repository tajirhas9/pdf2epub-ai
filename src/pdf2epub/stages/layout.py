"""Layout regions -> typed IR blocks (runs before OCR so OCR can work per region)."""

from __future__ import annotations

import numpy as np
from PIL import Image

from pdf2epub.engines import LayoutEngine, Region
from pdf2epub.ir import BBox, Block, BlockType, Document, Page, Role, Source
from pdf2epub.pipeline import Context

_TYPE = {
    "title": (BlockType.TITLE, Role.CONTENT),
    "heading": (BlockType.HEADING, Role.CONTENT),
    "text": (BlockType.PARAGRAPH, Role.CONTENT),
    "list": (BlockType.LIST, Role.CONTENT),
    "figure": (BlockType.FIGURE, Role.CONTENT),
    "caption": (BlockType.CAPTION, Role.CONTENT),
    "table": (BlockType.TABLE, Role.CONTENT),
    "formula": (BlockType.FORMULA, Role.CONTENT),
    "footnote": (BlockType.FOOTNOTE, Role.CONTENT),
    "header": (BlockType.PARAGRAPH, Role.HEADER),
    "footer": (BlockType.PARAGRAPH, Role.FOOTER),
    "page_number": (BlockType.PARAGRAPH, Role.PAGE_NUMBER),
    "aside": (BlockType.PARAGRAPH, Role.DECORATION),
}

MIN_SCORE = 0.4
BATCH = 4


class Layout:
    name = "layout"
    counted = True

    def __init__(self, engine: LayoutEngine):
        self.engine = engine

    def run(self, doc: Document, ctx: Context) -> Document:
        pages = ctx.selected(doc)
        for start in range(0, len(pages), BATCH):
            batch = pages[start : start + BATCH]
            images = [load_page_array(p, ctx) for p in batch]
            for page, regions in zip(batch, self.engine.detect(images), strict=False):
                page.blocks = regions_to_blocks(page, regions)
            ctx.progress(self.name, min(start + BATCH, len(pages)), len(pages))
        return doc


def load_page_array(page: Page, ctx: Context) -> np.ndarray:
    with Image.open(ctx.book_dir / page.image.path) as im:
        return np.asarray(im.convert("RGB"))


def regions_to_blocks(page: Page, regions: list[Region]) -> list[Block]:
    W, H = page.image.width, page.image.height
    blocks: list[Block] = []
    for r in sorted(regions, key=lambda r: (r.box[1], r.box[0])):
        if r.score < MIN_SCORE:
            continue
        btype, role = _TYPE.get(r.label, (BlockType.PARAGRAPH, Role.CONTENT))
        x0, y0, x1, y1 = r.box
        bbox = BBox(x0=max(0, x0 / W), y0=max(0, y0 / H), x1=min(1, x1 / W), y1=min(1, y1 / H))
        if bbox.area <= 0:
            continue
        block = Block(
            id=f"p{page.index + 1:04d}-b{len(blocks):03d}",
            type=btype,
            role=role,
            bbox=bbox,
            source=Source.OCR,
            confidence=r.score,
            order=len(blocks),
        )
        if r.label == "aside":
            block.flags.append("aside")
        blocks.append(block)
    return _drop_nested(blocks)


def _drop_nested(blocks: list[Block]) -> list[Block]:
    """Remove text boxes almost fully inside another text box of the same kind (duplicate detections)."""
    keep: list[Block] = []
    for i, b in enumerate(blocks):
        dup = any(
            j != i
            and o.type == b.type
            and (o.type != BlockType.FIGURE or o.bbox.overlap_ratio(b.bbox) > 0.9)
            and b.bbox.overlap_ratio(o.bbox) > 0.9
            and (o.bbox.area > b.bbox.area or (o.bbox.area == b.bbox.area and j < i))
            for j, o in enumerate(blocks)
        )
        if not dup:
            keep.append(b)
    return keep
