"""Before/after images for the README's "Sample conversions" (docs/assets/samples/).

    uv run pdf2epub convert sample.pdf --no-llm && uv run pdf2epub review --book sample --policy all
    uv run python scripts/readme_samples.py pages sample
    uv run python scripts/readme_samples.py compose sample.pdf sample 3 --out docs/assets/samples/x.webp

`pages` writes the book's pages twice, from the IR before the AI review (`glyphs` stage) and after it
(`review` stage), as standalone XHTML under work/<book>/readme/{before,after}/. `compose` renders one
page of both as a phone reader shows it, marks the words that differ (red: only without the review,
green: only with it), picks the phone screen with the most changes, and lays out original | before |
after on a 16:10 canvas.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import io
import re
import shutil
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from wordfreq import zipf_frequency

from pdf2epub.codegen.vue_page import VuePageWriter
from pdf2epub.config import load_settings
from pdf2epub.ir import Document
from pdf2epub.screenshot import PHONE, build_previews

ROOT = Path(__file__).resolve().parent.parent
SCALE = 2  # device pixel ratio of the renders
STAGES = {"before": "glyphs", "after": "review"}

_SCROLL_CSS = f"""
html {{ background: #fff; }}
body {{ box-sizing: border-box; padding: 1.2em; margin: 0; font-size: {PHONE.font_px}px; }}
mark.del {{ background: #fde2e1; color: #b42318; text-decoration: line-through #b4231880; border-radius: 3px; }}
mark.ins {{ background: #d9f5e3; color: #0b6b3a; border-radius: 3px; }}
"""

# Words in reading order (same walk for diffing and marking), and a function marking some of them.
_WORDS_JS = """() => {
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const out = []; let n;
  while ((n = w.nextNode())) for (const m of n.data.matchAll(/\\S+/g)) out.push(m[0]);
  return out;
}"""
_MARK_JS = """([flagged, cls]) => {
  const set = new Set(flagged), nodes = [], w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n, i = 0;
  while ((n = w.nextNode())) nodes.push(n);
  for (const node of nodes) {
    const parts = node.data.split(/(\\S+)/), frag = document.createDocumentFragment();
    let hit = false;
    for (const p of parts) {
      if (!p) continue;
      if (/^\\s+$/.test(p)) { frag.append(p); continue; }
      if (!set.has(i)) frag.append(p);
      else {
        const m = document.createElement('mark');
        m.className = cls; m.textContent = p; frag.append(m); hit = true;
      }
      i++;
    }
    if (hit) node.replaceWith(frag);
  }
  return [...document.querySelectorAll('mark')].map(m => m.getBoundingClientRect().top + scrollY);
}"""
_WORD_YS_JS = """() => {
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT), out = [], r = document.createRange();
  let n;
  while ((n = w.nextNode())) for (const m of n.data.matchAll(/\\S+/g)) {
    r.setStart(n, m.index); r.setEnd(n, m.index + 1); out.push(r.getBoundingClientRect().top + scrollY);
  }
  return out;
}"""

_BLOCK_TOPS_JS = """() => [...document.querySelectorAll('p, h1, h2, h3, h4, figure, img, table')]
  .map(e => e.getBoundingClientRect().top + scrollY)"""

# 16:10 canvas, all sizes in output pixels
W, H = 1600, 1000
BG, INK, MUTED, ACCENT = "#f3f4f7", "#1f2330", "#6b7180", "#7c3aed"
DEL, INS = ("#fde2e1", "#b42318"), ("#d9f5e3", "#0b6b3a")
PANEL_H = 800
SCREEN_H = 620  # CSS px of the phone screen shown: a bit less than a full screen, so text is larger
PHONE_W = round(PANEL_H * PHONE.width / SCREEN_H)


def pages(book: str) -> None:
    """Standalone XHTML of every page, before and after the AI review."""
    settings = load_settings()
    src = ROOT / "work" / book
    for label, stage in STAGES.items():
        tmp = ROOT / "work" / f"{book}--readme-{label}"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("readme", "build"))
        doc = Document.model_validate_json((src / "ir" / f"{stage}.json").read_text())
        VuePageWriter(doc, tmp).write_all()  # fonts and images come with the copy
        out = src / "readme" / label
        shutil.rmtree(out, ignore_errors=True)
        built = build_previews(settings, tmp)
        shutil.copytree(built, out)
        shutil.copy(built.parent / "book.css", out.parent)  # pages link ../book.css
        shutil.rmtree(tmp)
        print(f"{label}: {len(list(out.glob('*.xhtml')))} pages in {out}")


def _norm(word: str) -> str:
    return re.sub(r"[^\w]", "", word.lower())


def _is_word(w: str) -> bool:
    """A real word, not an OCR misreading (short rare tokens like `tet`, `ei` count as misreadings)."""
    return w.isdigit() or zipf_frequency(w, "en") >= (3.0 if len(w) <= 3 else 1.5)


async def _render(book: str, number: int, top: int | None) -> tuple[Image.Image, Image.Image, int]:
    """Phone screens of one page before and after the review, with differing words marked."""
    from playwright.async_api import async_playwright

    base = ROOT / "work" / book / "readme"
    files = {label: base / label / f"p{number:04d}.xhtml" for label in STAGES}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": PHONE.width, "height": SCREEN_H}, device_scale_factor=SCALE)
        tabs = {}
        for label, f in files.items():
            tab = tabs[label] = await ctx.new_page()
            await tab.goto(f.resolve().as_uri())
            await tab.add_style_tag(content=_SCROLL_CSS)
            await tab.wait_for_load_state("load")
        words = {label: await tab.evaluate(_WORDS_JS) for label, tab in tabs.items()}
        sm = difflib.SequenceMatcher(
            None, [_norm(w) for w in words["before"]], [_norm(w) for w in words["after"]], autojunk=False
        )
        # red: misread words that the review replaced (not a word at all, or swapped out in a short
        # local fix); green: words that only the reviewed text has. Text that merely moved or was
        # dropped as a duplicate stays unmarked.
        before_n, after_n = ([_norm(w) for w in words[label]] for label in STAGES)
        vocab_before, vocab_after = set(before_n), set(after_n)
        dels = {i for i, w in enumerate(before_n) if w and w not in vocab_after and not _is_word(w)}
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "replace" and i2 - i1 <= 4 and j2 - j1 <= 4:
                dels |= {i for i in range(i1, i2) if before_n[i] and before_n[i] not in vocab_after}
        ins = [j for j, w in enumerate(after_n) if w and w not in vocab_before]
        await tabs["before"].evaluate(_MARK_JS, [sorted(dels), "del"])
        ys = await tabs["after"].evaluate(_MARK_JS, [ins, "ins"])
        ys_before = await tabs["before"].evaluate(_WORD_YS_JS)
        ys_after = await tabs["after"].evaluate(_WORD_YS_JS)
        if top is None:  # the reviewed screen with the most corrections
            starts = range(0, max(1, int(max(ys, default=0))), 20)
            top = max(starts, key=lambda t: sum(t <= y < t + SCREEN_H - 40 for y in ys))
        # start at a paragraph, heading or picture, and show the same text at the top of the unreviewed screen
        blocks = await tabs["after"].evaluate(_BLOCK_TOPS_JS)
        top = max(0, int(max((y for y in blocks if y <= top + 1), default=0)) - 12)
        top_before = top
        pairs = ((i1 + d, j1 + d) for i1, j1, size in sm.get_matching_blocks() for d in range(size))
        for i, j in pairs:
            if ys_after[j] >= top:
                top_before = ys_before[i] - (ys_after[j] - top)
                break
        shots = []
        for label, offset in (("before", max(0, int(top_before))), ("after", top)):
            tab = tabs[label]
            await tab.evaluate(f"window.scrollTo(0, {offset})")
            height = await tab.evaluate("document.documentElement.scrollHeight")
            await tab.set_viewport_size({"width": PHONE.width, "height": max(SCREEN_H, height)})
            png = await tab.screenshot(clip={"x": 0, "y": offset, "width": PHONE.width, "height": SCREEN_H})
            shots.append(Image.open(io.BytesIO(png)).convert("RGB"))
        await browser.close()
    return shots[0], shots[1], top


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = (
        ["HelveticaNeue.ttc", "Arial Bold.ttf", "DejaVuSans-Bold.ttf"]
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


def _original(pdf: Path, number: int, width: int) -> Image.Image:
    page = pymupdf.open(pdf)[number - 1]
    zoom = width * 2 / page.rect.width
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    height = min(PANEL_H, round(im.height * width / im.width))
    return im.resize((width, round(im.height * width / im.width)), Image.LANCZOS).crop((0, 0, width, height))


def _rounded(im: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width - 1, im.height - 1), radius, fill=255)
    out = Image.new("RGBA", im.size)
    out.paste(im, (0, 0), mask)
    return out


def compose(pdf: Path, book: str, number: int, top: int | None, out: Path) -> None:
    before, after, top = asyncio.run(_render(book, number, top))
    gap, margin = 44, 48
    orig_w = W - 2 * margin - 2 * PHONE_W - 2 * gap
    panels = [
        ("Original PDF", _original(pdf, number, orig_w)),
        ("Without AI review", before.resize((PHONE_W, PANEL_H), Image.LANCZOS)),
        ("With AI review", after.resize((PHONE_W, PANEL_H), Image.LANCZOS)),
    ]
    canvas = Image.new("RGBA", (W, H), BG)
    draw = ImageDraw.Draw(canvas)
    label_font, legend_font = _font(26, bold=True), _font(20)
    x, y = margin, 92
    for i, (label, im) in enumerate(panels):
        py = y + (PANEL_H - im.height) // 2
        shadow = Image.new("RGBA", (im.width + 60, im.height + 60), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle((30, 34, im.width + 30, im.height + 34), 18, fill=(20, 24, 40, 60))
        canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)), (x - 30, py - 30))
        canvas.alpha_composite(_rounded(im, 18), (x, py))
        color = (INK, MUTED, ACCENT)[i]
        tw = draw.textlength(label, font=label_font)
        draw.text((x + (im.width - tw) / 2, y - 52), label, font=label_font, fill=color)
        x += im.width + gap
    # legend under the two phone panels
    lx, ly = margin + orig_w + gap, y + PANEL_H + 26
    for (bg, fg), text in ((DEL, "OCR errors, removed by the review"), (INS, "corrected by the review")):
        draw.rounded_rectangle((lx, ly + 2, lx + 34, ly + 24), 5, fill=bg)
        draw.text((lx + 6, ly), "ab", font=legend_font, fill=fg)
        draw.text((lx + 46, ly), text, font=legend_font, fill=MUTED)
        lx += 46 + draw.textlength(text, font=legend_font) + 40
    out.parent.mkdir(parents=True, exist_ok=True)
    # lossless WebP: sharp text and smooth shadows at about half the size of a PNG
    canvas.convert("RGB").save(out, lossless=True, method=6)
    print(f"{out} {out.stat().st_size // 1024} KB (top {top})")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("pages")
    s.add_argument("book")
    c = sub.add_parser("compose")
    c.add_argument("pdf", type=Path)
    c.add_argument("book")
    c.add_argument("page", type=int, help="1-based page number")
    c.add_argument("--top", type=int, help="scroll offset (CSS px) of the screen; default: most changes")
    c.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.cmd == "pages":
        pages(a.book)
    else:
        compose(a.pdf, a.book, a.page, a.top, a.out)


if __name__ == "__main__":
    main()
