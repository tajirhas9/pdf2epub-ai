"""Before/after images for the README's "Sample conversions" (docs/assets/samples/).

    uv run pdf2epub convert sample.pdf --no-llm
    uv run python scripts/readme_samples.py shots sample before    # phone renders without AI review
    uv run pdf2epub review --book sample
    uv run python scripts/readme_samples.py shots sample after     # ... and with it
    uv run python scripts/readme_samples.py compose sample.pdf sample 3 --top 0 --out docs/assets/samples/x.png

`shots` renders every generated page as a phone reader would show it (scrolling, full height) into
work/<book>/readme/<label>/. `compose` puts one page side by side: original | before | after, cropped
to the same phone screen, on a 16:10 canvas.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from pdf2epub.config import load_settings
from pdf2epub.screenshot import PHONE, build_previews

ROOT = Path(__file__).resolve().parent.parent
SCALE = 2  # device pixel ratio of the renders

_SCROLL_CSS = f"""
html {{ background: #fff; }}
body {{ box-sizing: border-box; padding: 1.2em; margin: 0; font-size: {PHONE.font_px}px; }}
"""

# 16:10 canvas, all sizes in output pixels
W, H = 1600, 1000
BG, CARD, INK, MUTED, ACCENT = "#f3f4f7", "#ffffff", "#1f2330", "#6b7180", "#7c3aed"
PANEL_H = 820
PHONE_W = round(PANEL_H * PHONE.width / PHONE.height)  # keeps the 390x844 phone ratio


def shots(book: str, label: str) -> None:
    book_dir = ROOT / "work" / book
    pages = build_previews(load_settings(), book_dir)
    out = book_dir / "readme" / label
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    asyncio.run(_render(sorted(pages.glob("*.xhtml")), out))
    print(f"{len(list(out.glob('*.png')))} renders in {out}")


async def _render(files: list[Path], out: Path) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(
            viewport={"width": PHONE.width, "height": PHONE.height}, device_scale_factor=SCALE
        )
        page = await ctx.new_page()
        for f in files:
            await page.goto(f.resolve().as_uri())
            await page.add_style_tag(content=_SCROLL_CSS)
            await page.wait_for_load_state("load")
            await page.screenshot(path=str(out / f"{f.stem}.png"), full_page=True)
        await browser.close()


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = (
        ["Helvetica Neue Bold", "HelveticaNeue.ttc", "Arial Bold.ttf", "DejaVuSans-Bold.ttf"]
        if bold
        else ["HelveticaNeue.ttc", "Arial.ttf", "DejaVuSans.ttf"]
    )
    for name in names:
        for base in ("/System/Library/Fonts/", "/Library/Fonts/", "/System/Library/Fonts/Supplemental/", ""):
            try:
                return ImageFont.truetype(base + name, size, index=1 if bold and name.endswith(".ttc") else 0)
            except OSError:
                continue
    return ImageFont.load_default(size)


def _phone_crop(png: Path, top: int) -> Image.Image:
    """One phone screen of a full-height render, starting `top` CSS px down."""
    im = Image.open(png).convert("RGB")
    y = min(top * SCALE, max(0, im.height - PHONE.height * SCALE))
    screen = Image.new("RGB", (PHONE.width * SCALE, PHONE.height * SCALE), "white")
    screen.paste(im.crop((0, y, im.width, min(im.height, y + PHONE.height * SCALE))), (0, 0))
    return screen.resize((PHONE_W, PANEL_H), Image.LANCZOS)


def _original(pdf: Path, number: int) -> Image.Image:
    page = pymupdf.open(pdf)[number - 1]
    zoom = PANEL_H * 2 / page.rect.height
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return im.resize((round(im.width * PANEL_H / im.height), PANEL_H), Image.LANCZOS)


def _rounded(im: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width - 1, im.height - 1), radius, fill=255)
    out = Image.new("RGBA", im.size)
    out.paste(im, (0, 0), mask)
    return out


def compose(pdf: Path, book: str, number: int, top: int, top_after: int | None, out: Path) -> None:
    renders = ROOT / "work" / book / "readme"
    name = f"p{number:04d}.png"
    panels = [
        ("Original PDF", _original(pdf, number)),
        ("Without AI review", _phone_crop(renders / "before" / name, top)),
        ("With AI review", _phone_crop(renders / "after" / name, top if top_after is None else top_after)),
    ]
    canvas = Image.new("RGBA", (W, H), BG)
    gap = 56
    total = sum(p.width for _, p in panels) + gap * (len(panels) - 1)
    x, y = (W - total) // 2, (H - PANEL_H) // 2 + 22
    label_font = _font(26, bold=True)
    draw = ImageDraw.Draw(canvas)
    for i, (label, im) in enumerate(panels):
        # soft shadow, then the panel with rounded corners
        shadow = Image.new("RGBA", (im.width + 60, im.height + 60), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle((30, 34, im.width + 30, im.height + 34), 18, fill=(20, 24, 40, 60))
        canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)), (x - 30, y - 30))
        canvas.alpha_composite(_rounded(im.convert("RGB"), 18), (x, y))
        color = ACCENT if i == len(panels) - 1 else MUTED if i == 1 else INK
        tw = draw.textlength(label, font=label_font)
        draw.text((x + (im.width - tw) / 2, y - 46), label, font=label_font, fill=color)
        x += im.width + gap
    out.parent.mkdir(parents=True, exist_ok=True)
    # mostly gray scans and text: a 256-colour palette looks the same at about half the size
    canvas.convert("RGB").quantize(256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(
        out, optimize=True
    )
    print(f"{out} {out.stat().st_size // 1024} KB")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("shots")
    s.add_argument("book")
    s.add_argument("label", choices=["before", "after"])
    c = sub.add_parser("compose")
    c.add_argument("pdf", type=Path)
    c.add_argument("book")
    c.add_argument("page", type=int, help="1-based page number")
    c.add_argument("--top", type=int, default=0, help="scroll offset (CSS px) of the phone screen")
    c.add_argument("--top-after", type=int, help="scroll offset for the AI-reviewed render, if different")
    c.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.cmd == "shots":
        shots(a.book, a.label)
    else:
        compose(a.pdf, a.book, a.page, a.top, a.top_after, a.out)


if __name__ == "__main__":
    main()
