"""Open a PDF or a folder of page images as an (empty) IR Document."""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf
from PIL import Image

from pdf2epub.ir import Document, Page, PageKind

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"}


def book_id_for(path: Path) -> str:
    stem = path.stem if path.is_file() else path.name
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "book"


def image_files(folder: Path) -> list[Path]:
    def natural(p: Path):
        return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)]

    return sorted((p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTS), key=natural)


def open_document(path: Path) -> Document:
    path = path.resolve()
    doc = Document(id=book_id_for(path), source=str(path))
    if path.is_dir():
        for i, f in enumerate(image_files(path)):
            with Image.open(f) as im:
                dpi = float((im.info.get("dpi") or (150, 150))[0]) or 150.0
                w, h = im.size
            doc.pages.append(Page(index=i, kind=PageKind.IMAGE, width_pt=w * 72 / dpi, height_pt=h * 72 / dpi, dpi=dpi))
        if not doc.pages:
            raise ValueError(f"no images found in {path}")
        doc.title = path.name
        return doc

    with pymupdf.open(path) as pdf:
        meta = pdf.metadata or {}
        doc.title = (meta.get("title") or "").strip() or path.stem
        doc.author = (meta.get("author") or "").strip()
        for i, page in enumerate(pdf):
            rect = page.rect
            doc.pages.append(Page(index=i, width_pt=rect.width, height_pt=rect.height))
    return doc
