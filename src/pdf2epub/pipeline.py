"""Stage protocol + runner with per-stage IR checkpoints and timings."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pdf2epub.config import Settings
from pdf2epub.ir import Document, Page

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

    @property
    def ir_dir(self) -> Path:
        return self.book_dir / "ir"

    @property
    def img_dir(self) -> Path:
        return self.book_dir / "img"

    def selected(self, doc: Document) -> list[Page]:
        return [p for p in doc.pages if self.pages is None or p.index in self.pages]

    def workers(self) -> int:
        return self.settings.workers or os.cpu_count() or 4

    def __getstate__(self):
        # The progress callback may be a closure; worker processes don't need it.
        state = dict(self.__dict__)
        state["progress"] = _noop_progress
        state["reporter"] = None
        return state


def _noop_progress(stage: str, done: int, total: int) -> None:
    pass


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
        pages = ctx.selected(doc)
        total = len(pages)
        if self.parallel and total > 1 and ctx.workers() > 1:
            with ProcessPoolExecutor(max_workers=min(ctx.workers(), total)) as pool:
                futures = [pool.submit(_timed_process, self, p, ctx) for p in pages]
                results = {}
                for done, fut in enumerate(as_completed(futures), 1):
                    page = fut.result()
                    results[page.index] = page
                    ctx.progress(self.name, done, total)
        else:
            results = {}
            for done, p in enumerate(pages, 1):
                results[p.index] = _timed_process(self, p, ctx)
                ctx.progress(self.name, done, total)
        doc.pages = [results.get(p.index, p) for p in doc.pages]
        return doc


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

    def run(self, doc: Document, ctx: Context, from_stage: str | None = None) -> Document:
        start = 0
        if from_stage:
            start = self.names().index(from_stage)
            if start > 0:
                doc = load_checkpoint(ctx, self.stages[start - 1].name) or doc
        from pdf2epub.ui import findings

        rep = ctx.reporter
        for stage in self.stages[start:]:
            t = time.perf_counter()
            log.info("stage %s", stage.name)
            if rep:
                rep.start(stage.name, len(ctx.selected(doc)) if getattr(stage, "counted", False) else None)
            try:
                doc = stage.run(doc, ctx)
            except Exception:
                if rep:
                    rep.fail(stage.name)
                raise
            ctx.stats[stage.name] = round(time.perf_counter() - t, 3)
            save_checkpoint(doc, ctx, stage.name)
            if rep:
                rep.done(stage.name, findings(stage.name, doc))
        return doc


def save_checkpoint(doc: Document, ctx: Context, stage: str) -> Path:
    ctx.ir_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.ir_dir / f"{stage}.json"
    path.write_text(doc.model_dump_json(exclude_defaults=True))
    (ctx.ir_dir / "latest.json").write_text(json.dumps({"stage": stage}))
    return path


def load_checkpoint(ctx: Context, stage: str | None = None) -> Document | None:
    if stage is None:
        latest = ctx.ir_dir / "latest.json"
        if not latest.exists():
            return None
        stage = json.loads(latest.read_text())["stage"]
    path = ctx.ir_dir / f"{stage}.json"
    return Document.model_validate_json(path.read_text()) if path.exists() else None
