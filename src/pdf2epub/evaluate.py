"""Corpus evaluation: guards heuristics/prompts tuned on one sample against regressions elsewhere."""

from __future__ import annotations

import difflib
import json
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path

from pdf2epub.config import Settings
from pdf2epub.paths import REPO_ROOT

EVAL_DIR = REPO_ROOT / "eval"  # development tool: lives in the checkout


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def phrase_found(phrase: str, text: str, min_ratio: float = 0.9) -> bool:
    """Exact (normalized) substring, or a near match (OCR-level differences) in a sliding window."""
    p, t = _norm(phrase), _norm(text)
    if p in t:
        return True
    n = len(p)
    best = 0.0
    sm = difflib.SequenceMatcher(None, autojunk=False)
    sm.set_seq2(p)
    starts = [m.start() for m in re.finditer(re.escape(p[:6]), t)] or range(0, max(1, len(t) - n), max(1, n // 4))
    for s in starts:
        for extra in range(max(4, n // 10)):  # OCR may add characters (split words)
            sm.set_seq1(t[s : s + n + extra])
            best = max(best, sm.ratio())
    return best >= min_ratio


def epub_text(epub: Path) -> str:
    with zipfile.ZipFile(epub) as z:
        html = " ".join(z.read(n).decode() for n in z.namelist() if n.startswith("OEBPS/text/"))
    html = re.sub(r"<(aside)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL)  # footnotes counted separately
    return re.sub(r"<[^>]+>", " ", html)


def evaluate_one(spec_path: Path, settings: Settings, llm: bool) -> dict:
    import asyncio

    from pdf2epub import app as core
    from pdf2epub.ir import Role, SpreadStatus
    from pdf2epub.pipeline import load_checkpoint

    spec = json.loads(spec_path.read_text())
    pdf = REPO_ROOT / spec["pdf"]
    if not pdf.exists():
        return {"doc": spec_path.stem, "skipped": f"missing {spec['pdf']}"}
    s = settings.model_copy(deep=True)
    s.langs = spec.get("langs", [])
    s.work_dir = settings.work_dir / "_eval"
    t = time.perf_counter()
    doc, ctx = core.convert(s, pdf)
    if llm:
        from pdf2epub.review import run_review

        asyncio.run(run_review(s, ctx.book_dir, None))
        doc = load_checkpoint(ctx) or doc
    convert_s = time.perf_counter() - t
    res = core.build(s, ctx.book_dir, s.work_dir / f"{doc.id}.epub")
    text = epub_text(res.epub)

    expect = spec.get("expect_text", [])
    found = [p for p in expect if phrase_found(p, text)]
    leaks = [p for p in spec.get("forbid_text", []) if _norm(p) in _norm(text)]
    got_spreads = {
        (_label(doc.pages[sp.left_page]), _label(doc.pages[sp.right_page]))
        for sp in doc.spreads
        if sp.status in (SpreadStatus.MERGED, SpreadStatus.SIDE_BY_SIDE) and not sp.background
    }
    want_spreads = {tuple(x) for x in spec.get("spreads", [])}
    rtl_blocks = sum(
        1 for p in doc.pages for b in p.blocks if b.role == Role.CONTENT and any(sp.dir == "rtl" for sp in b.spans)
    )
    return {
        "doc": spec_path.stem,
        "pages": len(doc.pages),
        "epubcheck": res.valid,
        "text_recall": round(len(found) / len(expect), 3) if expect else None,
        "missing_text": [p for p in expect if p not in found],
        "leaked": leaks,
        "spread_precision": round(len(got_spreads & want_spreads) / len(got_spreads), 3) if got_spreads else None,
        "spread_recall": round(len(got_spreads & want_spreads) / len(want_spreads), 3) if want_spreads else None,
        "rtl_blocks": rtl_blocks,
        "rtl_ok": rtl_blocks >= spec.get("expect_rtl_blocks", 0),
        "seconds_per_page": round(convert_s / max(1, len(doc.pages)), 3),
        "stats": ctx.stats,
    }


def _label(page) -> int:
    return int(page.label) if page.label and page.label.isdigit() else page.index + 1


def run_eval(settings: Settings, llm: bool = False, only: str | None = None) -> tuple[list[dict], dict | None]:
    specs = sorted((EVAL_DIR / "corpus").glob("*.json"))
    if only:
        specs = [p for p in specs if p.stem == only]
    results = [evaluate_one(p, settings, llm) for p in specs]
    out_dir = EVAL_DIR / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    previous = sorted(out_dir.glob("*.json"))
    prev = json.loads(previous[-1].read_text()) if previous else None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"{stamp}.json").write_text(json.dumps({"llm": llm, "results": results}, indent=1, ensure_ascii=False))
    return results, prev


METRICS = ("epubcheck", "text_recall", "spread_precision", "spread_recall", "rtl_ok", "seconds_per_page")


def regressions(results: list[dict], prev: dict | None) -> list[str]:
    if not prev:
        return []
    before = {r["doc"]: r for r in prev.get("results", [])}
    out = []
    for r in results:
        b = before.get(r["doc"])
        if not b or "skipped" in r:
            continue
        for m in METRICS:
            x, y = r.get(m), b.get(m)
            if x is None or y is None:
                continue
            worse = (x > y * 1.25 + 0.05) if m == "seconds_per_page" else (x < y)
            if worse:
                out.append(f"{r['doc']}: {m} {y} -> {x}")
        if len(r.get("leaked", [])) > len(b.get("leaked", [])):
            out.append(f"{r['doc']}: leaked {b.get('leaked')} -> {r['leaked']}")
    return out
