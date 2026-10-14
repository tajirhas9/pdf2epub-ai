"""Step 1: page image + PDF text layer.

Scanned page (one raster covering the page) -> the embedded raster is extracted as-is (no
resampling). Vector page -> rendered at `vector_dpi`. Image input -> copied.
"""

from __future__ import annotations

import pymupdf
from PIL import Image

from pdf2epub.ingest import image_files
from pdf2epub.ir import BBox, ImageRef, Page, PageKind, Word
from pdf2epub.pipeline import Context, PageStage

SCAN_COVERAGE = 0.9


class Rasterize(PageStage):
    name = "rasterize"

    def process(self, page: Page, ctx: Context) -> Page:
        out = ctx.img_dir / "pages" / f"p{page.index + 1:04d}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        src = ctx.source
        if page.kind == PageKind.IMAGE:
            with Image.open(image_files(src)[page.index]) as im:
                img = im.convert("RGB")
        else:
            with pymupdf.open(src) as pdf:
                pdf_page = pdf[page.index]
                img, page.kind, page.dpi = _page_image(pdf, pdf_page, ctx.settings.vector_dpi)
                page.words = _words(pdf_page)
        if page.kind == PageKind.IMAGE:
            page.dpi = page.dpi or 150.0
        img.save(out, compress_level=1)
        page.image = ImageRef(path=str(out.relative_to(ctx.book_dir)), width=img.width, height=img.height)
        return page


def _page_image(pdf: pymupdf.Document, page: pymupdf.Page, vector_dpi: int):
    rect = page.rect
    page_area = rect.width * rect.height
    infos = page.get_image_info(xrefs=True)
    scans = [
        i
        for i in infos
        if i.get("xref") and pymupdf.Rect(i["bbox"]).intersect(rect).get_area() >= SCAN_COVERAGE * page_area
    ]
    if scans:
        info = max(scans, key=lambda i: i["width"] * i["height"])
        ppi = info["width"] / (pymupdf.Rect(info["bbox"]).width / 72)
        a, b, c, d, _, _ = info["transform"]
        axis_aligned = abs(b) < 1e-6 and abs(c) < 1e-6 and a > 0 and d > 0
        if axis_aligned and pymupdf.Rect(info["bbox"]).contains(rect) and len(scans) == 1:
            img = _native(pdf, info["xref"])
            if img is not None:
                bbox = pymupdf.Rect(info["bbox"])
                if abs(bbox.width - rect.width) > 1 or abs(bbox.height - rect.height) > 1:
                    sx, sy = img.width / bbox.width, img.height / bbox.height
                    img = img.crop(
                        (
                            round((rect.x0 - bbox.x0) * sx),
                            round((rect.y0 - bbox.y0) * sy),
                            round((rect.x1 - bbox.x0) * sx),
                            round((rect.y1 - bbox.y0) * sy),
                        )
                    )
                return img, PageKind.SCANNED, round(ppi, 1)
        # Rotated / clipped / composite scans: render at the scan's native resolution.
        return _render(page, ppi), PageKind.SCANNED, round(ppi, 1)
    return _render(page, vector_dpi), PageKind.VECTOR, float(vector_dpi)


def _native(pdf: pymupdf.Document, xref: int) -> Image.Image | None:
    try:
        pix = pymupdf.Pixmap(pdf, xref)
        if pix.alpha or pix.colorspace is None or pix.colorspace.n not in (1, 3):
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        mode = "L" if pix.n == 1 else "RGB"
        return Image.frombytes(mode, (pix.width, pix.height), pix.samples).convert("RGB")
    except Exception:
        return None


def _render(page: pymupdf.Page, dpi: float) -> Image.Image:
    pix = page.get_pixmap(dpi=round(dpi), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _words(page: pymupdf.Page) -> list[Word]:
    W, H = page.rect.width, page.rect.height
    fonts: list[tuple[pymupdf.Rect, str, float]] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                fonts.append((pymupdf.Rect(span["bbox"]), span["font"], span["size"]))
    invisible = _invisible_rects(page)
    words: list[Word] = []
    line_ids: dict[tuple[int, int], int] = {}
    for x0, y0, x1, y1, text, bno, lno, _ in page.get_text("words"):
        r = pymupdf.Rect(x0, y0, x1, y1)
        c = (r.tl + r.br) / 2
        font, size = next(((f, s) for fr, f, s in fonts if c in fr), ("", 0.0))
        lid = line_ids.setdefault((bno, lno), len(line_ids))
        words.append(
            Word(
                text=text,
                bbox=BBox(x0=x0 / W, y0=y0 / H, x1=x1 / W, y1=y1 / H),
                font=font,
                size=size,
                line=lid,
                invisible=any(c in ir for ir in invisible),
            )
        )
    return words


def _invisible_rects(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Text the reader never sees: render mode 3, or text painted *under* a later image
    (both are typical for OCR layers placed behind/over scans)."""
    try:
        log = page.get_bboxlog()
    except Exception:
        return []
    hidden: list[pymupdf.Rect] = []
    pending: list[pymupdf.Rect] = []
    for kind, bbox in log:
        r = pymupdf.Rect(bbox)
        if kind == "ignore-text":
            hidden.append(r)
        elif kind.endswith("-text"):
            pending.append(r)
        elif kind == "fill-image":
            still = []
            for t in pending:
                (hidden if r.contains(t) else still).append(t)
            pending = still
    return hidden
