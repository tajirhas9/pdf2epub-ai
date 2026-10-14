"""Pause/resume: crash-safe writes, per-page progress inside a stage, and the run manifest.

A conversion can stop at any moment (Pause, Ctrl+C, the LLM server going away, a power cut). Every
finished page is saved as it completes, so the next run continues from the last finished page.

Invariant: the saved pages of stage S are valid only while no earlier stage reruns; a fresh run
clears them for every stage it runs (including the LLM review).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from pdf2epub.ir import Page

RUN_FILE = "run.json"
# Options a resumed run must share with the run that saved the progress.
RUN_OPTIONS = ("langs", "use_gpu", "ocr_det_model", "vector_dpi")
LLM_OPTIONS = ("enabled", "review_policy")
# running = stopped without notice (crash, power cut); converted = only the EPUB build is left
RESUMABLE = ("running", "paused", "converted")
SAMPLE = 4 << 20  # bytes hashed from each end of the source


class Paused(Exception):
    """The conversion stopped at a page boundary; its progress is saved and it can be resumed."""

    def __init__(self, reason: str = "paused"):
        super().__init__(reason)
        self.reason = reason


class OptionsChanged(ValueError):
    pass


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a synced temp file + rename: a power cut leaves the old file or the new one, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path) -> dict | None:
    """None when missing or unreadable (e.g. truncated by a crash before atomic writes existed)."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


class PagePartials:
    """Finished pages of one stage, one file each: ir/partial/<stage>/p0001.json."""

    def __init__(self, ir_dir: Path, stage: str):
        self.dir = ir_dir / "partial" / stage

    def _path(self, index: int) -> Path:
        return self.dir / f"p{index + 1:04d}.json"

    def save(self, page: Page, extra: list | None = None) -> None:
        data = {"page": page.model_dump(mode="json", exclude_defaults=True), "extra": extra or []}
        atomic_write_text(self._path(page.index), json.dumps(data, ensure_ascii=False))

    def load(self) -> dict[int, tuple[Page, list]]:
        out: dict[int, tuple[Page, list]] = {}
        if not self.dir.is_dir():
            return out
        for f in sorted(self.dir.glob("p*.json")):
            data = read_json(f)
            if data is None or "page" not in data:
                continue
            try:
                page = Page.model_validate(data["page"])
            except ValueError:
                continue
            out[page.index] = (page, list(data.get("extra") or []))
        return out

    def clear(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        try:
            self.dir.parent.rmdir()  # partial/ itself, once no stage has saved pages
        except OSError:
            pass


def clear_partials(ir_dir: Path) -> None:
    shutil.rmtree(ir_dir / "partial", ignore_errors=True)


# ---- run manifest -------------------------------------------------------------------------------


def fingerprint(source: Path) -> dict:
    """Identifies the content, not the path: a fresh upload of the same PDF continues its progress."""
    if source.is_dir():  # folder of page images: names and sizes
        files = sorted(p for p in source.iterdir() if p.is_file())
        return {"files": [[p.name, p.stat().st_size] for p in files]}
    size = source.stat().st_size
    h = hashlib.sha256()
    with source.open("rb") as f:  # head + tail: fast on big files, and changes when the PDF does
        h.update(f.read(SAMPLE))
        if size > 2 * SAMPLE:
            f.seek(size - SAMPLE)
        h.update(f.read(SAMPLE))
    return {"size": size, "sha256": h.hexdigest()}


def run_options(settings) -> dict:
    opts = {k: getattr(settings, k) for k in RUN_OPTIONS}
    opts.update({f"llm.{k}": getattr(settings.llm, k) for k in LLM_OPTIONS})
    return json.loads(json.dumps(opts))  # normalised like a round-trip through run.json


def read_run(book_dir: Path) -> dict | None:
    return read_json(book_dir / RUN_FILE)


def write_run(book_dir: Path, **fields) -> dict:
    run = read_run(book_dir) or {}
    run.update(fields, updated=time.time())
    atomic_write_text(book_dir / RUN_FILE, json.dumps(run, ensure_ascii=False, indent=1))
    return run


def resumable(settings, source: Path, book_dir: Path) -> dict | None:
    """The unfinished run to continue for this source, or None to start fresh.

    Raises OptionsChanged when the saved progress was made with different options."""
    run = read_run(book_dir)
    if not run or run.get("status") not in RESUMABLE:
        return None
    if run.get("fingerprint") != fingerprint(source):
        return None  # a different/changed file under the same name: its progress doesn't apply
    if run.get("options") != run_options(settings):
        raise OptionsChanged(
            "an unfinished conversion of this book used different options — "
            "rerun with the same options, or pass --restart to start over"
        )
    return run


def describe(run: dict) -> str:
    """'stopped during LLM: review pages (143/400 pages done)' for the resume line."""
    from pdf2epub.ui import label

    if run.get("status") == "converted":
        return "already converted, only the EPUB is left to build (--restart converts it again)"
    phase = run.get("phase") or ""
    text = f"stopped during {label(phase).lower()}" if phase else "stopped"
    if run.get("total"):
        text += f" ({run.get('done', 0)}/{run['total']} pages done)"
    if run.get("reason", "").startswith("llm-unreachable"):
        text += ", AI server was unreachable"
    return text


def latest_stage(book_dir: Path) -> str | None:
    return (read_json(book_dir / "ir" / "latest.json") or {}).get("stage")


def reset(book_dir: Path) -> None:
    """--restart: drop all progress of a book. Keeps the LLM reply cache (harmless and free speed) and
    the generated pages (hand-edited ones stay locked); both are rewritten as the run goes."""
    shutil.rmtree(book_dir / "ir", ignore_errors=True)
    for name in (RUN_FILE, "progress.json", "stats.json", "llm/ops-log.jsonl"):
        (book_dir / name).unlink(missing_ok=True)
