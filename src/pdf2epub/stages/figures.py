"""Figure crops, caption association, background/decoration detection, reflow layout choice."""

from __future__ import annotations

import numpy as np
import pymupdf
from PIL import Image

from pdf2epub.ir import BBox, Block, BlockType, FigureLayout, ImageRef, Page, PageKind, Role
from pdf2epub.pipeline import Context, PageStage

VISUAL_TYPES = {BlockType.FIGURE, BlockType.TABLE, BlockType.FORMULA}
TEXT_TYPES = {BlockType.PARAGRAPH, BlockType.HEADING, BlockType.TITLE, BlockType.LIST, BlockType.QUOTE}
VECTOR_FIGURE_DPI = 300


class Figures(PageStage):
    name = "figures"

    def process(self, page: Page, ctx: Context) -> Page:
        figs = [b for b in page.blocks if b.type in VISUAL_TYPES and b.role == Role.CONTENT]
        texts = [b for b in page.blocks if b.type in TEXT_TYPES and b.role == Role.CONTENT and b.text.strip()]
        for fig in figs:
            covered = sum(fig.bbox.intersection(t.bbox) for t in texts) / (fig.bbox.area or 1)
            if covered > 0.25 and fig.bbox.area > 0.1:
                fig.role = Role.BACKGROUND  # photo printed under body text
            elif fig.bbox.area < 0.002:
                fig.role = Role.DECORATION  # ornaments, bullets, tiny logos
        figs = [f for f in figs if f.role == Role.CONTENT]
        if not figs:
            return page
        img = Image.open(ctx.book_dir / page.image.path).convert("RGB")
        pdf = pymupdf.open(ctx.source) if page.kind == PageKind.VECTOR else None
        try:
            for fig in figs:
                fig.image = save_crop(page, fig, mask_text(img, fig, texts), pdf, ctx)
                fig.layout = choose_layout(fig, texts)
        finally:
            if pdf is not None:
                pdf.close()
        attach_captions(page, figs)
        return page


def mask_text(img: Image.Image, fig: Block, texts: list[Block]) -> Image.Image:
    """Body text that intrudes into a figure's box (tilted photos, text wrapping close) is not part
    of the picture: paint it with the page background so it doesn't show up in the EPUB image."""
    from PIL import ImageDraw

    intruders = [t for t in texts if fig.bbox.intersection(t.bbox) > 0 and t.bbox.overlap_ratio(fig.bbox) < 0.9]
    if not intruders:
        return img
    W, H = img.size
    arr = np.asarray(img)
    paper = tuple(int(v) for v in np.median(arr[: max(1, H // 50)].reshape(-1, 3), axis=0))  # top margin colour
    out = img.copy()
    draw = ImageDraw.Draw(out)
    for t in intruders:
        x0, y0 = max(t.bbox.x0, fig.bbox.x0), max(t.bbox.y0, fig.bbox.y0)
        x1, y1 = min(t.bbox.x1, fig.bbox.x1), min(t.bbox.y1, fig.bbox.y1)
        draw.rectangle((x0 * W, y0 * H, x1 * W, y1 * H), fill=paper)
    return out


def save_crop(page: Page, fig: Block, img: Image.Image, pdf, ctx: Context) -> ImageRef:
    out_dir = ctx.book_dir / "img" / "figs"
    out_dir.mkdir(parents=True, exist_ok=True)
    b = fig.bbox
    if pdf is not None:
        p = pdf[page.index]
        clip = pymupdf.Rect(b.x0 * p.rect.width, b.y0 * p.rect.height, b.x1 * p.rect.width, b.y1 * p.rect.height)
        pix = p.get_pixmap(dpi=VECTOR_FIGURE_DPI, clip=clip, alpha=False)
        crop = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    else:
        W, H = img.size
        crop = img.crop((round(b.x0 * W), round(b.y0 * H), round(b.x1 * W), round(b.y1 * H)))
    return save_image(crop, out_dir, fig.id, ctx)


def save_image(crop: Image.Image, out_dir, name: str, ctx: Context) -> ImageRef:
    # Few colours (line art, diagrams, tables) -> PNG; photos -> JPEG.
    is_photo = _is_photo(crop)
    path = out_dir / f"{name}.{'jpg' if is_photo else 'png'}"
    if is_photo:
        crop.save(path, quality=90, optimize=True)
    else:
        crop.save(path, optimize=True)
    return ImageRef(path=str(path.relative_to(ctx.book_dir)), width=crop.width, height=crop.height)


def _is_photo(im: Image.Image) -> bool:
    small = np.asarray(im.convert("RGB").resize((64, 64)))
    unique = len({tuple(px) for px in small.reshape(-1, 3) // 8})
    return unique > 300


def choose_layout(fig: Block, texts: list[Block]) -> FigureLayout:
    """Narrow figure with text beside it -> float on that side; otherwise full width."""
    if fig.bbox.width > 0.6:
        return FigureLayout.FULL
    beside = [
        t
        for t in texts
        if min(t.bbox.y1, fig.bbox.y1) - max(t.bbox.y0, fig.bbox.y0) > 0.3 * fig.bbox.height
        and (t.bbox.x1 <= fig.bbox.x0 + 0.02 or t.bbox.x0 >= fig.bbox.x1 - 0.02)
    ]
    if not beside:
        return FigureLayout.FULL
    text_left = sum(t.bbox.cx < fig.bbox.cx for t in beside) >= len(beside) / 2
    return FigureLayout.FLOAT_RIGHT if text_left else FigureLayout.FLOAT_LEFT


def attach_captions(page: Page, figs: list[Block]) -> None:
    captions = [b for b in page.blocks if b.type == BlockType.CAPTION and b.role == Role.CONTENT]
    for cap in captions:
        best, best_d = None, 0.08  # max vertical gap (page fraction)
        for fig in figs:
            h_overlap = min(cap.bbox.x1, fig.bbox.x1) - max(cap.bbox.x0, fig.bbox.x0)
            if h_overlap < 0.3 * min(cap.bbox.width, fig.bbox.width):
                continue
            d = min(abs(cap.bbox.y0 - fig.bbox.y1), abs(fig.bbox.y0 - cap.bbox.y1))
            if fig.bbox.intersection(cap.bbox) > 0:
                d = 0
            if d < best_d:
                best, best_d = fig, d
        if best is not None:
            cap.caption_of = best.id


def crop_bbox(img: Image.Image, b: BBox) -> Image.Image:
    W, H = img.size
    return img.crop((round(b.x0 * W), round(b.y0 * H), round(b.x1 * W), round(b.y1 * H)))
