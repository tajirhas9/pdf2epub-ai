"""A/B check of page-review request variants against hand-corrected pages (eval/ground_truth/).

    uv run python eval/ab_review.py run baseline-1 --pages 103,117,…
    uv run python eval/ab_review.py run lines --set review_payload=lines --pages …
    uv run python eval/ab_review.py score baseline-1 baseline-2 lines

`run` reviews each page (1-based numbers) from the book's `ir/review-pre.json` with the configured LLM plus the
`--set` overrides of [llm] settings, with its own reply cache, and saves the reviewed pages, op logs and usage under
eval/ab/<name>/. `score` compares runs: character error rate on the ground-truth pages, running heads / side tabs
left as content, footnote markers kept, ops rejected, failed pages, time per page.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
import unicodedata
from pathlib import Path

from pdf2epub.config import load_settings
from pdf2epub.evaluate import phrase_found
from pdf2epub.ir import PLACEHOLDER, BlockType, Document, Page, Role
from pdf2epub.llm.client import LLMError, LLMUnavailable, OpenAICompatClient

ROOT = Path(__file__).resolve().parent
GROUND_TRUTH = ROOT / "ground_truth" / "sealed-nectar.json"
OUT = ROOT / "ab"

_HONORIFICS = re.compile(r"[﵀-﵏ﷰ-﷿]|عليه السلام|رضي الله عنه[ما]?|رحمه الله|صلى الله عليه وسلم")
_ARABIC_MARKS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ࣓-ࣿـ]")
_ARABIC_DIGITS_AND_BRACKETS = re.compile(r"[٠-٩۰-۹﴾﴿{}*]")
_QUOTES = str.maketrans({c: "'" for c in '`´‘’“”"«»'})


def norm(text: str) -> str:
    """Compare what a reader sees: honorifics, Arabic vowel marks / verse numbers, quote styles and spacing
    before punctuation are not counted (the scan's quote marks are ambiguous; verified marks are locked)."""
    text = unicodedata.normalize("NFC", text).replace(PLACEHOLDER, " ")
    text = _HONORIFICS.sub(" ", text)
    text = _ARABIC_MARKS.sub("", text)
    text = _ARABIC_DIGITS_AND_BRACKETS.sub(" ", text)
    text = re.sub("[ٱأإآ]", "ا", text).translate(_QUOTES).replace("''", "'")
    text = re.sub(r"\s+([,.;:!?)\]])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def content_text(page: Page) -> str:
    """Content blocks in reading order, as text: no verified marks, no footnote marker digits."""
    parts = []
    for b in sorted((b for b in page.blocks if b.role == Role.CONTENT), key=lambda b: b.order):
        if b.type in (BlockType.FIGURE, BlockType.TABLE, BlockType.FORMULA):
            continue
        text = "".join(s.text for s in b.spans if not s.glyph and not s.note_ref)
        if b.type == BlockType.FOOTNOTE:
            text = re.sub(r"^\s*\d+\s+", "", text)
        parts.append(text)
    return " ".join(parts)


def note_refs(page: Page) -> int:
    return sum(1 for b in page.blocks for s in b.spans if s.note_ref)


# ---- run ------------------------------------------------------------------------------------


async def _run(name: str, pages: list[int], overrides: dict[str, str]) -> None:
    from pdf2epub.review import review_page
    from pdf2epub.screenshot import Renderer

    gt = json.loads(GROUND_TRUTH.read_text())
    book_dir = (ROOT.parent / gt["book"]).resolve()
    settings = load_settings()
    for key, value in overrides.items():
        field = type(settings.llm).model_fields[key]
        value = _parse(value, field.annotation)
        if isinstance(value, dict):  # merged into the configured value (e.g. extra_body)
            value = {**getattr(settings.llm, key), **value}
        settings.llm = settings.llm.model_copy(update={key: value})
    out = OUT / name
    (out / "pages").mkdir(parents=True, exist_ok=True)
    (out / "settings.json").write_text(json.dumps({"pages": pages, "overrides": overrides}, indent=1))
    doc = Document.model_validate_json((book_dir / "ir" / "review-pre.json").read_text())
    client = OpenAICompatClient(settings.llm, cache_dir=out / "cache", usage_log=out / "usage.jsonl")
    async with Renderer(out / "shots") as renderer:
        for n in pages:
            target = out / "pages" / f"p{n:04d}.json"
            if target.exists():
                continue
            page = doc.pages[n - 1].model_copy(deep=True)
            t = time.perf_counter()
            try:
                log = await review_page(client, page, settings, book_dir, renderer)
                error = ""
            except LLMUnavailable:
                raise  # server gone: stop (pages already done are kept; rerun continues from here)
            except LLMError as e:
                log, error = [], str(e)
            seconds = time.perf_counter() - t
            target.write_text(json.dumps({"page": page.model_dump(mode="json"), "log": log, "error": error,
                                          "seconds": seconds}, ensure_ascii=False))  # fmt: skip
            print(f"{name} p{n}: {seconds:.0f}s, {len(log)} ops{' FAILED ' + error if error else ''}", flush=True)
    await client.close()
    print(client.usage_summary())


def _parse(value: str, annotation):
    if annotation is bool:
        return value.lower() in ("1", "true", "yes", "on")
    if annotation is int:
        return int(value)
    if annotation is dict:
        return json.loads(value)
    return value


# ---- score ----------------------------------------------------------------------------------


def replay(pre_page: Page, log: list[dict]) -> tuple[Page, list[dict]]:
    """The run's page rebuilt from the model's logged ops with the current validators (OpApplier), so runs
    made with different code versions are scored under the same checks."""
    from pdf2epub.llm.ops import OpApplier

    page = pre_page.model_copy(deep=True)
    ops = [x["op"] for x in log]
    results = OpApplier(page).apply_all(ops)
    return page, [{"status": r.status, "reason": r.reason, "op": r.op} for r in results if r.status != "skipped"]


def score(name: str, replay_ops: bool = False) -> dict:
    gt = json.loads(GROUND_TRUTH.read_text())
    book_dir = ROOT.parent / gt["book"]
    pre = Document.model_validate_json((book_dir / "ir" / "review-pre.json").read_text())
    runs = {int(p.stem[1:]): json.loads(p.read_text()) for p in sorted((OUT / name / "pages").glob("p*.json"))}
    if name == "pre-review":  # the IR before any review: how much the review adds
        runs = {int(n): {"page": pre.pages[int(n) - 1].model_dump(mode="json"), "log": [], "error": ""}
                for n in gt["pages"]}  # fmt: skip
    if replay_ops:
        for n, r in runs.items():
            page, r["log"] = replay(pre.pages[n - 1], r["log"])
            r["page"] = {**page.model_dump(mode="json"), "flags": r["page"].get("flags", [])}
    cer, leaks, per_page = [], 0, {}
    for n, spec in gt["pages"].items():
        if int(n) not in runs:
            continue
        page = Page.model_validate(runs[int(n)]["page"])
        truth, got = norm(" ".join(spec["content"])), norm(content_text(page))
        c = levenshtein(truth, got) / max(1, len(truth))
        leak = sum(phrase_found(x, got, 0.85) for x in spec["not_content"])
        cer.append(c)
        leaks += leak
        per_page[n] = round(c * 100, 2)
    lost_refs = sum(
        max(0, note_refs(pre.pages[n - 1]) - note_refs(Page.model_validate(r["page"]))) for n, r in runs.items()
    )
    logs = [x for r in runs.values() for x in r["log"]]
    seconds = [r["seconds"] for r in runs.values() if "seconds" in r and not r["error"]]
    return {
        "name": name,
        "pages": len(runs),
        "cer_%": round(statistics.mean(cer) * 100, 2) if cer else None,
        "cer_by_page_%": per_page,
        "heads_left_as_content": leaks,
        "note_refs_lost": lost_refs,
        "ops_applied": sum(x["status"] == "applied" for x in logs),
        "ops_rejected": sum(x["status"] == "rejected" for x in logs),
        "pages_failed": sum(bool(r["error"]) for r in runs.values()),
        "needs_human": sum("review:needs_human" in r["page"].get("flags", []) for r in runs.values()),
        "s_per_page": round(statistics.mean(seconds), 1) if seconds else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("name")
    r.add_argument("--pages", required=True, help="1-based page numbers, comma separated")
    r.add_argument("--set", action="append", default=[], help="llm setting override, e.g. review_payload=lines")
    s = sub.add_parser("score")
    s.add_argument("names", nargs="+")
    s.add_argument("--replay", action="store_true", help="re-apply each run's ops with the current validators")
    args = ap.parse_args()
    if args.cmd == "run":
        overrides = dict(kv.split("=", 1) for kv in args.set)
        asyncio.run(_run(args.name, [int(x) for x in args.pages.split(",")], overrides))
    else:
        rows = [score(n, args.replay) for n in args.names]
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=1)
        print()


if __name__ == "__main__":
    main()
