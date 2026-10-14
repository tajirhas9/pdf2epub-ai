"""Composition root: wires stages, engines and settings. The CLI only talks to this module."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from pdf2epub.codegen.vue_page import VuePageWriter
from pdf2epub.config import Settings
from pdf2epub.ingest import book_id_for, open_document
from pdf2epub.ir import Document
from pdf2epub.pipeline import Context, Pipeline, load_checkpoint


def build_pipeline(settings: Settings) -> Pipeline:
    # Imported here so `pdf2epub --help` doesn't pay for loading vision libraries.
    from pdf2epub.engines import PaddleLayout, PaddleOcr
    from pdf2epub.stages.figures import Figures
    from pdf2epub.stages.glyphs import Glyphs
    from pdf2epub.stages.layout import Layout
    from pdf2epub.stages.order import ReadingOrder
    from pdf2epub.stages.rasterize import Rasterize
    from pdf2epub.stages.running import RunningElements
    from pdf2epub.stages.spreads import Spreads
    from pdf2epub.stages.text import Text

    ocr = PaddleOcr(settings.use_gpu, settings.ocr_det_model)
    return Pipeline(
        [
            Rasterize(),
            Layout(PaddleLayout(settings.use_gpu)),
            Text(ocr, settings.ocr_scripts()),
            RunningElements(),
            Figures(),
            Spreads(),
            ReadingOrder(),
            Glyphs(ocr),
        ]
    )


def make_context(
    settings: Settings, source: Path, book_id: str, pages: set[int] | None = None, reporter=None
) -> Context:
    book_dir = settings.book_dir(book_id)
    book_dir.mkdir(parents=True, exist_ok=True)
    progress_file = book_dir / "progress.json"

    def progress(stage: str, done: int, total: int) -> None:
        # live workbench (vite plugin watches this file) + terminal
        progress_file.write_text(json.dumps({"stage": stage, "done": done, "total": total, "t": time.time()}))
        if reporter is not None:
            reporter.advance(stage, done, total)

    return Context(
        settings=settings, book_dir=book_dir, source=source.resolve(), pages=pages, progress=progress, reporter=reporter
    )


def preload_models(settings: Settings, reporter=None) -> None:
    """Load the vision models up front, as their own visible step (first run downloads them)."""
    from pdf2epub.engines import preload

    if reporter is not None:
        first = not (settings.cache_dir / "paddlex" / "official_models" / "PP-DocLayout_plus-L").exists()
        reporter.start("models", note="first run: downloading ~250 MB, one time" if first else "")
    preload(settings.use_gpu, settings.ocr_scripts()[0], settings.ocr_det_model)
    if reporter is not None:
        reporter.done("models", _device_note(settings))


def _device_note(settings: Settings) -> str:
    from pdf2epub.engines import _device

    return "GPU" if _device(settings.use_gpu).startswith("gpu") else "CPU"


def convert(
    settings: Settings,
    source: Path,
    pages: set[int] | None = None,
    from_stage: str | None = None,
    reporter=None,
) -> tuple[Document, Context]:
    doc = open_document(source)
    doc.language = settings.langs[0] if settings.langs else "en"
    ctx = make_context(settings, source, doc.id, pages, reporter)
    if from_stage or pages is not None:
        doc = load_checkpoint(ctx) or doc
    pipeline = build_pipeline(settings)
    names = pipeline.names()
    if from_stage is None or names.index(from_stage) <= names.index("glyphs"):
        preload_models(settings, reporter)
    doc = pipeline.run(doc, ctx, from_stage=from_stage)
    if not settings.langs:
        doc.language = detect_language(doc)
    write_pages(doc, ctx)
    return doc, ctx


def llm_preflight(settings: Settings, rep=None) -> str:
    """Check the LLM endpoint first: a wrong URL/key fails in a second, not after the conversion."""
    import asyncio

    from pdf2epub.llm.client import LLMError, OpenAICompatClient

    async def check() -> str:
        client = OpenAICompatClient(settings.llm)
        try:
            await client.ping()
            return await client._resolve_model()
        finally:
            await client.close()

    if rep:
        rep.start("llm_connect")
    try:
        model = asyncio.run(check())
    except LLMError:
        if rep:
            rep.fail("llm_connect")
        raise
    if rep:
        rep.done("llm_connect", model)
    return model


def review(settings: Settings, book_dir: Path, pages: set[int] | None = None, rep=None) -> dict:
    import asyncio

    from pdf2epub.review import run_review

    return asyncio.run(run_review(settings, book_dir, pages, rep))


def convert_and_review(
    settings: Settings, source: Path, pages: set[int] | None = None, from_stage: str | None = None, reporter=None
) -> tuple[Document, Context]:
    """convert, then the LLM review when enabled (the CLI's `convert`/`run` and the web UI share this)."""
    if settings.llm.enabled:
        llm_preflight(settings, reporter)
    doc, ctx = convert(settings, source, pages, from_stage, reporter=reporter)
    if settings.llm.enabled:
        review(settings, ctx.book_dir, pages, reporter)
    save_stats(ctx)
    return doc, ctx


def latest_doc(settings: Settings, book_dir: Path) -> Document | None:
    return load_checkpoint(Context(settings=settings, book_dir=book_dir, source=Path("")))


def explain_error(exc: BaseException) -> tuple[str, str]:
    """(message, hint) for errors users can act on."""
    from pdf2epub.llm.client import LLMError
    from pdf2epub.runtime import RuntimeMissing

    if isinstance(exc, LLMError):
        return str(exc), "Check the endpoint with `pdf2epub llm-check`, or skip the review with --no-llm."
    if isinstance(exc, BuildError):
        return str(exc), exc.output.strip()[-1500:] or "Run with -v for details."
    if isinstance(exc, RuntimeMissing | FileNotFoundError):
        return str(exc), ""
    if isinstance(exc, subprocess.CalledProcessError):
        cmd = " ".join(map(str, exc.cmd[:3])) if isinstance(exc.cmd, list | tuple) else str(exc.cmd)
        return f"`{cmd}` failed (exit {exc.returncode})", "Run with -v for details."
    if isinstance(exc, RuntimeError) and "PaddlePaddle is not installed" in str(exc):
        return str(exc), ""
    return f"{type(exc).__name__}: {exc}", "Run with -v for the full traceback."


def detect_language(doc: Document) -> str:
    """Book language = language of most content text (span langs come from script detection)."""
    from collections import Counter

    from pdf2epub.ir import Role

    weight: Counter[str] = Counter()
    for page in doc.pages:
        for b in page.blocks:
            if b.role == Role.CONTENT:
                for s in b.spans:
                    weight[s.lang or doc.language] += len(s.text)
    return weight.most_common(1)[0][0] if weight else doc.language


def write_pages(doc: Document, ctx: Context) -> dict[str, list[str]]:
    from pdf2epub.fonts import provision_fonts

    t = time.perf_counter()
    rep = ctx.reporter
    if rep:
        rep.start("codegen", len(doc.pages))
    provision_fonts(doc, ctx.book_dir, ctx.settings.cache_dir)
    result = VuePageWriter(doc, ctx.book_dir).write_all()
    ctx.stats["codegen"] = round(time.perf_counter() - t, 3)
    if rep:
        locked = len(result["locked"])
        rep.done("codegen", f"{locked} hand-edited pages kept" if locked else "")
    (ctx.book_dir / "progress.json").write_text(json.dumps({"stage": "done", "done": 1, "total": 1, "t": time.time()}))
    return result


def resolve_book(settings: Settings, book: str | None) -> Path:
    """Book work dir from an id, a source path, or the most recently touched book."""
    if book:
        candidate = settings.book_dir(book_id_for(Path(book)) if Path(book).exists() else book)
        if candidate.exists():
            return candidate
        raise FileNotFoundError(
            f"No converted book called {book!r} (it may have been cleaned up after a successful build). "
            "Convert it first: pdf2epub convert <file.pdf>"
        )
    books = [d for d in settings.work_dir.glob("*") if (d / "book.json").exists()]
    if not books:
        raise FileNotFoundError("No converted books yet. Start with: pdf2epub convert <file.pdf>")
    return max(books, key=lambda d: (d / "book.json").stat().st_mtime)


def web_env(settings: Settings, book_dir: Path, epub_out: Path | None = None) -> dict[str, str]:
    env = dict(os.environ, PDF2EPUB_BOOK_DIR=str(book_dir))
    if epub_out:
        env["PDF2EPUB_EPUB_OUT"] = str(epub_out)
    return env


def ensure_node_modules(settings: Settings) -> Path:
    from pdf2epub.runtime import ensure_web

    return ensure_web(settings)


@dataclass
class BuildResult:
    epub: Path
    report: Path
    valid: bool | None  # None: built but not validated (no Java)
    epubcheck_output: str
    seconds: float


def epubcheck_jar(settings: Settings) -> Path | None:
    from pdf2epub.runtime import epubcheck_jar as jar

    return jar(settings)


def run_epubcheck(settings: Settings, epub: Path) -> tuple[bool | None, str]:
    from pdf2epub.runtime import ensure_epubcheck

    if not shutil.which("java"):
        return None, "Java not found: EPUB built but not validated (install a JRE to enable epubcheck)"
    jar = ensure_epubcheck(settings)
    if not jar:
        return None, "epubcheck could not be downloaded: EPUB built but not validated"
    # -Duser.home: without a passwd entry (docker --user) Java falls back to "?" and writes a cache dir there.
    cmd = ["java", f"-Duser.home={Path.home()}", "-jar", str(jar), str(epub)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


class BuildError(RuntimeError):
    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


def build(settings: Settings, book_dir: Path, out: Path | None = None, reporter=None) -> BuildResult:
    t = time.perf_counter()
    book = json.loads((book_dir / "book.json").read_text())
    out = (out.expanduser() if out else settings.out_dir / f"{book['id']}.epub").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    if reporter:
        reporter.start("build")
    web = ensure_node_modules(settings)
    proc = subprocess.run(
        ["npm", "run", "--silent", "build:epub", "--", "--logLevel", "error"],
        cwd=web,
        env={**web_env(settings, book_dir, out), "VITE_CONFIG_NATIVE_IGNORE_WARNING": "true"},
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not out.exists():
        if reporter:
            reporter.fail("build")
        raise BuildError("the EPUB build (vite) failed", (proc.stdout + proc.stderr)[-4000:])
    if reporter:
        reporter.done("build", _chapters_note(out))
        reporter.start("validate")
    valid, output = run_epubcheck(settings, out)
    if reporter:
        if valid is None:
            reporter.done("validate", "skipped: " + output.split(":")[0].lower())
        elif valid:
            reporter.done("validate", "valid EPUB 3")
        else:
            reporter.fail("validate", "problems found (see report)")
    report = out.with_suffix(".report.json")
    report.write_text(json.dumps(build_report(book_dir, valid, output), indent=1, ensure_ascii=False))
    return BuildResult(out, report, valid, output, round(time.perf_counter() - t, 2))


def epub_chapters(epub: Path) -> int:
    import zipfile

    with zipfile.ZipFile(epub) as z:
        return sum(1 for n in z.namelist() if n.startswith("OEBPS/text/") and n.endswith(".xhtml"))


def _chapters_note(epub: Path) -> str:
    n = epub_chapters(epub)
    return f"{n} chapter{'s' if n != 1 else ''}"


def build_report(book_dir: Path, valid: bool | None, epubcheck_output: str) -> dict:
    report: dict = {"epubcheck": {"valid": valid, "output": epubcheck_output[-4000:]}}
    stats = book_dir / "stats.json"
    if stats.exists():
        report["timings"] = json.loads(stats.read_text())
    ops = book_dir / "llm" / "ops-log.jsonl"
    if ops.exists():
        lines = [json.loads(line) for line in ops.read_text().splitlines() if line.strip()]
        report["llm"] = {
            "applied": sum(1 for x in lines if x.get("status") == "applied"),
            "rejected": sum(1 for x in lines if x.get("status") == "rejected"),
        }
    flags: dict[str, int] = {}
    for meta in sorted((book_dir / "meta").glob("*.json")):
        for f in json.loads(meta.read_text()).get("flags", []):
            key = f.split(":")[0]
            flags[key] = flags.get(key, 0) + 1
    report["page_flags"] = flags
    return report


def save_stats(ctx: Context) -> None:
    (ctx.book_dir / "stats.json").write_text(json.dumps(ctx.stats, indent=1))
