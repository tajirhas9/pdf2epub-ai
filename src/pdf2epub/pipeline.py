"""Stage protocol + runner with per-stage IR checkpoints and timings."""

from __future__ import annotations

import json
import logging
import signal
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pdf2epub.config import Settings
from pdf2epub.ir import Document, Page
from pdf2epub.resume import PagePartials, Paused, atomic_write_text, clear_partials, read_json

log = logging.getLogger(__name__)


@dataclass
class Context:
    settings: Settings
    book_dir: Path
    source: Path
    pages: set[int] | None = None  # restrict page-local work to these indices (None = all)
    progress: Callable[[str, int, int], None] = lambda stage, done, total: None
    stats: dict[str, float] = field(default_factory=dict)
    reporter: object = None  # pdf2epub.ui.Reporter (None = silent)
    resume: bool = False  # continue from saved per-page progress (pdf2epub.resume)
    pause_requested: Callable[[], bool] = lambda: False  # checked between pages

    @property
    def ir_dir(self) -> Path:
        return self.book_dir / "ir"

    @property
    def img_dir(self) -> Path:
        return self.book_dir / "img"

    def selected(self, doc: Document) -> list[Page]:
        return [p for p in doc.pages if self.pages is None or p.index in self.pages]

    def workers(self) -> int:
        from pdf2epub.hardware import auto_workers

        return self.settings.workers or auto_workers()

    def check_pause(self) -> None:
        if self.pause_requested():
            raise Paused()

    def __getstate__(self):
        # The progress callback may be a closure; worker processes don't need it.
        state = dict(self.__dict__)
        state["progress"] = _noop_progress
        state["reporter"] = None
        state["pause_requested"] = _never
        return state


def _noop_progress(stage: str, done: int, total: int) -> None:
    pass


def _never() -> bool:
    return False


class Stage(Protocol):
    name: str

    def run(self, doc: Document, ctx: Context) -> Document: ...


class PageStage:
    """Base for stages whose work is independent per page; runs pages in parallel processes.

    Subclasses implement `process(page, ctx) -> Page` as a picklable pure function of the page.
    """

    name = "page-stage"
    parallel = True
    counted = True  # reports per-page progress

    def process(self, page: Page, ctx: Context) -> Page:  # pragma: no cover - abstract
        raise NotImplementedError

    def run(self, doc: Document, ctx: Context) -> Document:
        partials = PagePartials(ctx.ir_dir, self.name)
        results = {i: page for i, (page, _) in partials.load().items()} if ctx.resume else {}
        pages = [p for p in ctx.selected(doc) if p.index not in results]
        total = len(pages) + len(results)
        done = len(results)

        def finished(page: Page) -> None:
            nonlocal done
            results[page.index] = page
            partials.save(page)
            done += 1
            ctx.progress(self.name, done, total)
            ctx.check_pause()

        if done:
            ctx.progress(self.name, done, total)
        if self.parallel and len(pages) > 1 and ctx.workers() > 1:
            pool = ProcessPoolExecutor(max_workers=min(ctx.workers(), len(pages)), initializer=_ignore_sigint)
            futures = []
            try:
                futures = [pool.submit(_timed_process, self, p, ctx) for p in pages]
                for fut in as_completed(futures):
                    finished(fut.result())
            except BaseException:
                # pause / stop / Ctrl+C: don't start the remaining pages; keep the ones that finished
                pool.shutdown(wait=True, cancel_futures=True)
                for fut in futures:
                    if fut.done() and not fut.cancelled() and fut.exception() is None:
                        page = fut.result()
                        if page.index not in results:
                            partials.save(page)
                raise
            pool.shutdown()
        else:
            for p in pages:
                finished(_timed_process(self, p, ctx))
        doc.pages = [results.get(p.index, p) for p in doc.pages]
        return doc


def _ignore_sigint() -> None:
    # Ctrl+C is for the main process (pause, then stop); workers finish their page and are shut down.
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _timed_process(stage: PageStage, page: Page, ctx: Context) -> Page:
    t = time.perf_counter()
    page = stage.process(page, ctx)
    page.timings[stage.name] = round(time.perf_counter() - t, 4)
    return page


class Pipeline:
    def __init__(self, stages: list[Stage]):
        self.stages = stages

    def names(self) -> list[str]:
        return [s.name for s in self.stages]

    def resume_point(self, ctx: Context) -> str | None:
        """First stage still to run after the latest checkpoint ("" = all done, None = from the start)."""
        latest = read_json(ctx.ir_dir / "latest.json") or {}
        stage = latest.get("stage")
        names = self.names()
        if stage not in names or not (ctx.ir_dir / f"{stage}.json").exists():
            return "" if stage in ("review-pre", "review") else None
        i = names.index(stage) + 1
        return names[i] if i < len(names) else ""

    def run(self, doc: Document, ctx: Context, from_stage: str | None = None) -> Document:
        start = 0
        if from_stage:
            start = self.names().index(from_stage)
            if start > 0:
                doc = load_checkpoint(ctx, self.stages[start - 1].name) or doc
        if not ctx.resume:  # saved pages of the stages about to rerun (and of the review) are stale
            clear_partials(ctx.ir_dir)
        from pdf2epub.ui import findings

        rep = ctx.reporter
        for stage in self.stages[start:]:
            t = time.perf_counter()
            log.info("stage %s", stage.name)
            if rep:
                rep.start(stage.name, len(ctx.selected(doc)) if getattr(stage, "counted", False) else None)
            try:
                doc = stage.run(doc, ctx)
            except Paused:
                if rep:
                    rep.fail(stage.name, "paused")
                raise
            except Exception:
                if rep:
                    rep.fail(stage.name)
                raise
            ctx.stats[stage.name] = round(time.perf_counter() - t, 3)
            save_checkpoint(doc, ctx, stage.name)
            PagePartials(ctx.ir_dir, stage.name).clear()
            if rep:
                rep.done(stage.name, findings(stage.name, doc))
            ctx.check_pause()
        return doc


def save_checkpoint(doc: Document, ctx: Context, stage: str) -> Path:
    ctx.ir_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.ir_dir / f"{stage}.json"
    atomic_write_text(path, doc.model_dump_json(exclude_defaults=True))
    atomic_write_text(ctx.ir_dir / "latest.json", json.dumps({"stage": stage}))
    return path


def load_checkpoint(ctx: Context, stage: str | None = None) -> Document | None:
    if stage is None:
        stage = (read_json(ctx.ir_dir / "latest.json") or {}).get("stage")
        if not stage:
            return None
    path = ctx.ir_dir / f"{stage}.json"
    try:
        return Document.model_validate_json(path.read_text())
    except (OSError, ValueError):  # missing, or cut short by a crash
        return None
