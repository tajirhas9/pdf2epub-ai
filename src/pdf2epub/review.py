"""Step 5: LLM page review — concurrent, cached, every change validated before it touches the IR."""

from __future__ import annotations

import asyncio
import json
import time
from importlib import resources
from pathlib import Path

from PIL import Image

from pdf2epub.codegen.vue_page import VuePageWriter
from pdf2epub.config import Settings
from pdf2epub.ir import BlockType, Document, Page, Role
from pdf2epub.llm.client import ImagePart, LLMError, OpenAICompatClient
from pdf2epub.llm.ops import OPS_SCHEMA, OpApplier, payload_text
from pdf2epub.pipeline import Context, load_checkpoint, save_checkpoint
from pdf2epub.stages.order import mark_continuations

SYSTEM = resources.files("pdf2epub.llm.prompts").joinpath("page_review.md").read_text()
SPREAD_SYSTEM = resources.files("pdf2epub.llm.prompts").joinpath("spread_check.md").read_text()
GLYPH_SYSTEM = resources.files("pdf2epub.llm.prompts").joinpath("glyph_resolve.md").read_text()
GLYPH_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["honorific", "ligature", "letter", "symbol", "noise", "same", "unknown"],
        },
        "text": {"type": "string"},
        "lang": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["kind", "text", "lang", "confidence"],
}
SPREAD_SCHEMA = {
    "type": "object",
    "properties": {
        "same_picture": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["same_picture", "confidence", "reason"],
}
CROP_FLAGS = {"needs-transcription", "low-confidence", "rtl", "no-text", "layout-missed", "figure-text"}


def needs_review(page: Page, policy: str) -> bool:
    if policy == "all":
        return True
    if policy == "none":
        return False
    return bool(
        page.flags
        or any(b.flags for b in page.blocks)
        or any(b.type == BlockType.FIGURE and b.role == Role.CONTENT for b in page.blocks)
    )


def page_payload(page: Page) -> dict:
    return {
        "page": page.label or page.index + 1,
        "blocks": [
            {
                "id": b.id,
                "type": b.type.value,
                "role": b.role.value,
                "order": b.order if b.role == Role.CONTENT else None,
                "bbox": [round(v, 3) for v in (b.bbox.x0, b.bbox.y0, b.bbox.x1, b.bbox.y1)],
                "text": payload_text(b),
                **({"layout": b.layout.value} if b.layout else {}),
                **({"flags": b.flags} if b.flags else {}),
            }
            for b in sorted(page.blocks, key=lambda b: (b.role != Role.CONTENT, b.order))
        ],
    }


def page_images(
    page: Page, book_dir: Path, max_images: int, max_side: int, render: Path | None = None
) -> list[ImagePart]:
    with Image.open(book_dir / page.image.path) as im:
        img = im.convert("RGB")
    parts = [ImagePart.from_pil(img, "page", max_side)]
    if render is not None and render.exists() and max_images > 2:
        parts.append(ImagePart.from_path(render, "current EPUB rendering on a phone (first screen)", max_side))
    W, H = img.size
    crops = [b for b in page.blocks if b.role == Role.CONTENT and CROP_FLAGS & set(b.flags)]
    crops.sort(key=lambda b: ("needs-transcription" not in b.flags, b.confidence))
    for b in crops[: max(0, max_images - 1)]:
        pad = 0.01
        box = (
            round(max(0, b.bbox.x0 - pad) * W),
            round(max(0, b.bbox.y0 - pad) * H),
            round(min(1, b.bbox.x1 + pad) * W),
            round(min(1, b.bbox.y1 + pad) * H),
        )
        crop = img.crop(box)
        if crop.width < 900:  # zoom small crops so thin strokes/diacritics are legible
            f = 900 / crop.width
            crop = crop.resize((round(crop.width * f), round(crop.height * f)), Image.LANCZOS)
        parts.append(ImagePart.from_pil(crop, f"block {b.id}", max_side))
    return parts


async def review_page(
    client: OpenAICompatClient, page: Page, settings: Settings, book_dir: Path, renderer=None
) -> list[dict]:
    render = None
    if renderer is not None:
        xhtml = book_dir / "build" / "pages" / f"p{page.index + 1:04d}.xhtml"
        if xhtml.exists():
            try:
                render = await renderer.render(xhtml)
            except Exception:  # rendering is an aid, never a blocker
                render = None
    images = page_images(page, book_dir, settings.llm.max_images_per_call, settings.llm.image_max_side, render)
    text = "Current interpretation of this page:\n" + json.dumps(page_payload(page), ensure_ascii=False)
    reply = await client.complete_json(SYSTEM, text, images, OPS_SCHEMA, "page_review")
    ops = reply.get("ops") if isinstance(reply.get("ops"), list) else []
    log = []
    for r in OpApplier(page).apply_all(ops):
        log.append({"page": page.index + 1, "status": r.status, "reason": r.reason, "op": r.op})
    page.flags = [f for f in page.flags if not f.startswith("review")] + [f"review:{reply.get('verdict', '?')}"]
    return log


TEXT_KINDS = {"honorific", "ligature", "letter", "symbol"}
MIN_GLYPH_CONFIDENCE = 0.8


async def resolve_glyphs(client: OpenAICompatClient, doc: Document, settings: Settings, book_dir: Path) -> list[dict]:
    """Verify each distinct printed mark once. Only confident readings become text; everything
    else keeps the printed glyph (image). Applied to every occurrence of that mark."""
    from pdf2epub.ir import PLACEHOLDER
    from pdf2epub.stages.glyphs import refresh_glyph_spans

    log = []
    for g in doc.glyphs.values():
        if g.resolved or not g.image or g.sample_bbox is None:
            continue
        page = doc.pages[g.sample_page]
        b = g.sample_bbox
        with Image.open(book_dir / page.image.path) as im:
            W, H = im.size
            rgb = im.convert("RGB")
            mark = rgb.crop((round(b.x0 * W), round(b.y0 * H), round(b.x1 * W), round(b.y1 * H)))
            pad = 0.22
            context = rgb.crop(
                (
                    round(max(0.0, b.x0 - pad) * W),
                    round(max(0.0, b.y0 - 0.01) * H),
                    round(min(1.0, b.x1 + pad) * W),
                    round(min(1.0, b.y1 + 0.01) * H),
                )
            )
        f = max(4, round(160 / max(1, mark.height)))
        mark = mark.resize((mark.width * f, mark.height * f), Image.LANCZOS)
        context = context.resize((context.width * 3, context.height * 3), Image.LANCZOS)
        ocr = "nothing (OCR missed it)" if g.symbol == PLACEHOLDER else repr(g.symbol)
        try:
            reply = await client.complete_json(
                GLYPH_SYSTEM,
                f"OCR read this mark as: {ocr}",
                [ImagePart.from_pil(mark, "the mark (zoomed)"), ImagePart.from_pil(context, "the line around it")],
                GLYPH_SCHEMA,
                "glyph_resolve",
            )
        except LLMError as e:
            log.append(
                {"page": page.index + 1, "status": "rejected", "reason": f"glyph {g.key}: {e}", "op": {"op": "glyph"}}
            )
            continue  # stays an image
        kind = str(reply.get("kind") or "unknown")
        text = str(reply.get("text") or "").strip()[:40]
        try:
            conf = float(reply.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        g.kind, g.confidence, g.resolved = kind, conf, True
        if conf >= MIN_GLYPH_CONFIDENCE and kind in TEXT_KINDS and text:
            g.render, g.text, g.lang = "text", text, (reply.get("lang") or None)
        elif conf >= MIN_GLYPH_CONFIDENCE and kind == "same" and g.symbol != PLACEHOLDER:
            g.render, g.text, g.lang = "text", g.symbol, None
        elif conf >= MIN_GLYPH_CONFIDENCE and kind == "noise":
            g.render, g.text = "text", ""
        else:
            g.render = "image"  # unsure: keep the printed glyph
        log.append(
            {
                "page": page.index + 1,
                "status": "applied",
                "reason": f"glyph {g.key} x{g.occurrences}: {kind} {text!r} conf={conf:.2f} -> {g.render}",
                "op": {"op": "glyph"},
            }
        )
    from pdf2epub.stages.glyphs import harmonize

    harmonized = harmonize(doc, book_dir)
    if harmonized:
        log.append(
            {"page": 0, "status": "applied", "reason": f"{harmonized} glyph clusters harmonized", "op": {"op": "glyph"}}
        )
    changed = refresh_glyph_spans(doc)
    if changed:
        log.append(
            {"page": 0, "status": "applied", "reason": f"{changed} glyph spans updated", "op": {"op": "glyph-apply"}}
        )
    return log


async def verify_inserted_marks(
    client: OpenAICompatClient, doc: Document, settings: Settings, book_dir: Path
) -> list[dict]:
    """Marks the page review added where OCR saw nothing: locate on the scan, match to known marks,
    verify new ones; what can't be located is removed (missing is better than wrong)."""
    from pdf2epub.engines import PaddleOcr
    from pdf2epub.ir import PLACEHOLDER
    from pdf2epub.stages.glyphs import GlyphBook, apply_glyphs, placeholder_hits, strip_placeholders

    book = GlyphBook(doc, book_dir, ocr=PaddleOcr(settings.use_gpu, settings.ocr_det_model))
    found = 0
    for page in doc.pages:
        blocks = [b for b in page.blocks if any(PLACEHOLDER in s.text for s in b.spans)]
        if not blocks:
            continue
        with Image.open(book_dir / page.image.path) as im:
            img = im.convert("RGB")
        hits = placeholder_hits(blocks, img)
        page.glyph_hits = [h for h in hits if book.assign(img, h, page.index)]
        found += len(page.glyph_hits)
    log = await resolve_glyphs(client, doc, settings, book_dir) if found else []
    apply_glyphs(doc)
    dropped = strip_placeholders(doc)
    if found or dropped:
        log.append(
            {
                "page": 0,
                "status": "applied",
                "reason": f"inserted marks: {found} located, {dropped} dropped",
                "op": {"op": "glyph-insert"},
            }
        )
    return log


def _crop_block(book_dir: Path, page: Page, block) -> Image.Image:
    with Image.open(book_dir / page.image.path) as im:
        W, H = im.size
        b = block.bbox
        return im.convert("RGB").crop((round(b.x0 * W), round(b.y0 * H), round(b.x1 * W), round(b.y1 * H)))


async def check_spreads(client: OpenAICompatClient, doc: Document, settings: Settings, book_dir: Path) -> list[dict]:
    """Page-pair check for spread candidates the image analysis could not decide alone."""
    from pdf2epub.ir import SpreadStatus
    from pdf2epub.stages.spreads import apply_spread

    log = []
    for sp in doc.spreads:
        if sp.status != SpreadStatus.CANDIDATE or not sp.image:
            continue
        left, right = doc.pages[sp.left_page], doc.pages[sp.right_page]
        side = settings.llm.image_max_side
        lb0, rb0 = left.block(sp.left_block), right.block(sp.right_block)
        if not lb0 or not rb0:
            continue
        images = [
            ImagePart.from_pil(_crop_block(book_dir, left, lb0), "left half (left page, at the gutter)", side),
            ImagePart.from_pil(_crop_block(book_dir, right, rb0), "right half (right page, at the gutter)", side),
            ImagePart.from_path(book_dir / sp.image.path, "proposed joined picture", side),
        ]
        try:
            reply = await client.complete_json(
                SPREAD_SYSTEM, "Is this one picture across both pages?", images, SPREAD_SCHEMA, "spread_check"
            )
        except LLMError as e:
            log.append(
                {
                    "page": left.index + 1,
                    "status": "rejected",
                    "reason": f"spread check failed: {e}",
                    "op": {"op": "spread"},
                }
            )
            continue
        lb, rb = left.block(sp.left_block), right.block(sp.right_block)
        same = bool(reply.get("same_picture")) and float(reply.get("confidence") or 0) >= 0.6
        if same and lb and rb:
            sp.status = SpreadStatus.SIDE_BY_SIDE
            apply_spread(left, right, sp, lb, rb)
        else:
            sp.status = SpreadStatus.REJECTED
            for b in (lb, rb):
                if b:
                    b.flags = [f for f in b.flags if f != "spread-candidate"]
        log.append(
            {
                "page": left.index + 1,
                "status": "applied",
                "reason": reply.get("reason", ""),
                "op": {"op": "spread", "same_picture": same},
            }
        )
    return log


async def _start_renderer(settings: Settings, book_dir: Path, rep):
    """Phone renders of the current pages, if Node + Playwright are available."""
    from pdf2epub.screenshot import Renderer, build_previews

    try:
        await asyncio.to_thread(build_previews, settings, book_dir)
        try:
            return await Renderer(book_dir / "build" / "shots").__aenter__()
        except Exception as e:
            if "Executable doesn't exist" not in str(e):
                raise
            from pdf2epub.runtime import ensure_browser

            if rep:
                rep.start("browser", note="one-time download")
            await asyncio.to_thread(ensure_browser)
            if rep:
                rep.done("browser")
            return await Renderer(book_dir / "build" / "shots").__aenter__()
    except Exception as e:
        if rep:  # renders are an aid; the review continues without them
            rep.start("renders")
            rep.done("renders", f"skipped ({e.__class__.__name__})")
        return None


async def run_review(settings: Settings, book_dir: Path, pages: set[int] | None, rep=None) -> dict:
    ctx = Context(settings=settings, book_dir=book_dir, source=Path(""), pages=pages)
    doc: Document | None = load_checkpoint(ctx)
    if doc is None:
        raise FileNotFoundError("no IR checkpoint — run `pdf2epub convert` first")
    todo = [p for p in ctx.selected(doc) if needs_review(p, settings.llm.review_policy)]
    client = OpenAICompatClient(settings.llm, cache_dir=book_dir / "llm" / "cache")
    sem = asyncio.Semaphore(max(1, settings.llm.concurrency))
    logs: list[dict] = []
    failures: list[str] = []
    done = 0
    t0 = time.perf_counter()
    writer = VuePageWriter(doc, book_dir)

    try:
        await client.ping()
    except LLMError:
        await client.close()
        raise
    renderer = await _start_renderer(settings, book_dir, rep)
    from pdf2epub.ir import SpreadStatus

    def _count(log: list[dict]) -> tuple[int, int]:
        ok = sum(1 for x in log if x["status"] == "applied")
        return ok, len(log) - ok

    async def one(page: Page) -> None:
        nonlocal done
        async with sem:
            t = time.perf_counter()
            try:
                logs.extend(await review_page(client, page, settings, book_dir, renderer))
            except LLMError as e:
                failures.append(f"p{page.index + 1}: {e}")
                page.flags.append("review:failed")
            page.timings["review"] = round(time.perf_counter() - t, 2)
            writer.write_page(page)  # live update in the workbench via HMR
            done += 1
            if rep:
                ok, bad = _count([x for x in logs if x["op"].get("op") not in ("spread", "glyph", "glyph-apply")])
                note = (
                    f"{ok} fixes"
                    + (f" · {bad} rejected" if bad else "")
                    + (f" · {len(failures)} failed" if failures else "")
                )
                rep.advance("review", done, len(todo), note)

    try:
        if settings.llm.review_policy != "none":
            candidates = [sp for sp in doc.spreads if sp.status == SpreadStatus.CANDIDATE]
            if candidates:
                rep and rep.start("spread_check", len(candidates))
                entries = await check_spreads(client, doc, settings, book_dir)
                logs.extend(entries)
                joined = sum(1 for x in entries if x["op"].get("same_picture"))
                rep and rep.done("spread_check", f"{joined} joined, {len(candidates) - joined} kept apart")
            pending = [g for g in doc.glyphs.values() if not g.resolved]
            if pending:
                rep and rep.start("glyph_check", len(pending))
                logs.extend(await resolve_glyphs(client, doc, settings, book_dir))
                rep and rep.done("glyph_check", _glyph_note(doc))
            writer.write_all()
        rep and rep.start("review", len(todo))
        await asyncio.gather(*(one(p) for p in todo))
        if rep:
            ok, bad = _count([x for x in logs if x["op"].get("op") not in ("spread", "glyph", "glyph-apply")])
            rep.done("review", f"{ok} fixes applied" + (f", {bad} rejected by checks" if bad else ""))
        before = len(logs)
        logs.extend(await verify_inserted_marks(client, doc, settings, book_dir))
        if rep and len(logs) > before:
            rep.start("inserted_marks")
            rep.done("inserted_marks", logs[-1]["reason"].replace("inserted marks: ", ""))
    finally:
        await client.close()
        if renderer is not None:
            await renderer.__aexit__(None, None, None)
    mark_continuations(doc.pages)
    save_checkpoint(doc, ctx, "review")
    writer.write_all()
    (book_dir / "llm").mkdir(parents=True, exist_ok=True)
    with (book_dir / "llm" / "ops-log.jsonl").open("a") as f:
        for entry in logs:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    applied = sum(1 for x in logs if x["status"] == "applied")
    rejected = len(logs) - applied
    seconds = time.perf_counter() - t0
    text = f"Reviewed {len(todo)} pages in {seconds:.0f}s: {applied} changes applied, {rejected} rejected by checks"
    if failures:
        text += f"; {len(failures)} pages failed ({failures[0]})"
    return {
        "pages": len(todo),
        "applied": applied,
        "rejected": rejected,
        "failures": failures,
        "seconds": seconds,
        "text": text,
    }


def _glyph_note(doc: Document) -> str:
    text = sum(1 for g in doc.glyphs.values() if g.render == "text")
    image = sum(1 for g in doc.glyphs.values() if g.render != "text")
    parts = []
    if text:
        parts.append(f"{text} verified as text")
    if image:
        parts.append(f"{image} kept as printed image")
    return ", ".join(parts)
