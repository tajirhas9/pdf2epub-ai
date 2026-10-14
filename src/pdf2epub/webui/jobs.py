"""Conversion jobs for the web UI.

One worker thread runs jobs in order (OCR models and the GPU are shared), so the vision models stay
loaded between books. Uploading a file queues a warm-up so model loading overlaps with choosing options.
"""

from __future__ import annotations

import logging
import queue
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pdf2epub.config import Settings
from pdf2epub.ui import label

log = logging.getLogger(__name__)

REVIEW_POLICIES = ("flagged", "all")


class Cancelled(Exception):
    pass


@dataclass
class Upload:
    id: str
    path: Path
    name: str
    title: str
    pages: int

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "title": self.title, "pages": self.pages}


@dataclass
class Job:
    id: str
    upload: Upload
    options: dict
    status: str = "queued"  # queued | running | done | failed | cancelled
    steps: list[dict] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    result: dict | None = None
    error: dict | None = None
    cancel: bool = False

    def public(self, waiting: str | None = None) -> dict:
        end = self.finished or time.time()
        return {
            "id": self.id,
            "file": self.upload.public(),
            "options": self.options,
            "status": "cancelling" if self.cancel and self.status in ("queued", "running") else self.status,
            "steps": [dict(s) for s in self.steps],
            "created": self.created,
            "elapsed": round(end - self.started, 1) if self.started else 0,
            "result": self.result,
            "error": self.error,
            "waiting": waiting if self.status == "queued" else None,  # what the queue is busy with
        }


class JobReporter:
    """pdf2epub.ui.Reporter that records steps for the browser; raises Cancelled at the next tick."""

    def __init__(self, job: Job):
        self.job = job

    def _check(self) -> None:
        if self.job.cancel:
            raise Cancelled()

    def _step(self, key: str) -> dict:
        for s in self.job.steps:
            if s["key"] == key:
                return s
        s = {
            "key": key,
            "label": label(key),
            "status": "running",
            "done": 0,
            "total": None,
            "note": "",
            "t0": time.time(),
        }
        self.job.steps.append(s)
        return s

    def start(self, key, total=None, note=""):
        self._check()
        s = self._step(key)
        s.update(status="running", done=0, total=total, note=note, t0=time.time(), seconds=None)

    def advance(self, key, done, total=None, note=None):
        s = self._step(key)
        s["done"] = done
        if total is not None:
            s["total"] = total
        if note is not None:
            s["note"] = note
        self._check()

    def done(self, key, note=""):
        s = self._step(key)
        if s["total"]:
            s["done"] = s["total"]
        s.update(status="done", note=note, seconds=round(time.time() - s["t0"], 1))

    def fail(self, key, note=""):
        s = self._step(key)
        s.update(status="failed", note=note, seconds=round(time.time() - s["t0"], 1))

    def stop(self): ...


def safe_name(name: str) -> str:
    stem = re.sub(r"[^\w.\- ]+", "", Path(name).stem).strip() or "book"
    return stem[:120] + ".pdf"


def job_settings(options: dict) -> Settings:
    """Per-job settings: saved config + the options picked for this conversion."""
    overrides: dict[str, Any] = {"keep_work": bool(options.get("keep_work"))}
    if options.get("langs"):
        overrides["langs"] = [str(x).strip() for x in options["langs"] if str(x).strip()]
    for key in ("use_gpu", "ocr_det_model", "vector_dpi"):
        if options.get(key) is not None:
            overrides[key] = options[key]
    settings = Settings(**overrides)
    if options.get("llm") is not None:
        settings.llm.enabled = bool(options["llm"])
    if options.get("review_policy") in REVIEW_POLICIES:
        settings.llm.review_policy = options["review_policy"]
    return settings


def output_path(settings: Settings, options: dict, upload: Upload) -> Path | None:
    name = (options.get("output_name") or "").strip()
    if not name:
        return None
    name = re.sub(r'[\\/:*?"<>|]+', "", name).strip()
    if not name.lower().endswith(".epub"):
        name += ".epub"
    return settings.out_dir / name


class JobRunner:
    def __init__(self, upload_dir: Path):
        self.upload_dir = upload_dir
        self.clear_uploads()  # left over from an earlier run (nothing refers to them any more)
        self.uploads: dict[str, Upload] = {}
        self.jobs: dict[str, Job] = {}
        self._queue: queue.Queue = queue.Queue()
        self._warm: set[tuple] = set()
        self.preparing: str | None = None  # "models" | "download" while the OCR models load
        self._thread = threading.Thread(target=self._loop, name="pdf2epub-jobs", daemon=True)
        self._thread.start()

    # ---- uploads ----

    def add_upload(self, name: str, stream, length: int) -> Upload:
        from pdf2epub.ingest import open_document

        uid = uuid.uuid4().hex[:12]
        folder = self.upload_dir / uid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / safe_name(name)
        remaining = length
        with path.open("wb") as f:
            while remaining > 0:
                chunk = stream.read(min(1 << 20, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        try:
            if remaining or path.read_bytes()[:5] != b"%PDF-":
                raise ValueError("not a PDF file")
            doc = open_document(path)
        except Exception as e:
            shutil.rmtree(folder, ignore_errors=True)
            raise ValueError(f"Could not open {name}: {e}") from None
        up = Upload(uid, path, name, doc.title or path.stem, len(doc.pages))
        self.uploads[uid] = up
        self.warm_up()
        return up

    def remove_upload(self, uid: str) -> None:
        if any(j.upload.id == uid and j.status in ("queued", "running") for j in self.jobs.values()):
            return
        up = self.uploads.pop(uid, None)
        if up:
            shutil.rmtree(up.path.parent, ignore_errors=True)

    def clear_uploads(self) -> None:
        shutil.rmtree(self.upload_dir, ignore_errors=True)

    # ---- jobs ----

    def submit(self, upload_id: str, options: dict) -> Job:
        up = self.uploads.get(upload_id)
        if up is None:
            raise KeyError("upload not found; add the file again")
        job = Job(uuid.uuid4().hex[:12], up, options)
        self.jobs[job.id] = job
        self._queue.put(("job", job))
        return job

    def cancel(self, job_id: str) -> None:
        job = self.jobs[job_id]
        if job.status in ("queued", "running"):
            job.cancel = True

    def remove(self, job_id: str) -> None:
        job = self.jobs[job_id]
        if job.status in ("queued", "running"):
            raise ValueError("stop the conversion first")
        del self.jobs[job_id]
        if not any(j.upload.id == job.upload.id for j in self.jobs.values()):
            self.remove_upload(job.upload.id)

    def list(self) -> list[dict]:
        running = any(j.status == "running" for j in self.jobs.values())
        waiting = "conversion" if running else self.preparing
        return [j.public(waiting) for j in sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)]

    def warm_up(self) -> None:
        self._queue.put(("warm", None))

    # ---- worker ----

    def _loop(self) -> None:
        while True:
            kind, job = self._queue.get()
            try:
                if kind == "warm":
                    self._preload()
                else:
                    self._run(job)
            except Exception:  # noqa: BLE001 - the worker must survive anything
                log.exception("job worker")

    def _preload(self) -> None:
        settings = Settings()
        key = (settings.use_gpu, settings.ocr_scripts()[0], settings.ocr_det_model)
        if key in self._warm:
            return
        first = not (settings.cache_dir / "paddlex" / "official_models" / "PP-DocLayout_plus-L").exists()
        self.preparing = "download" if first else "models"
        try:
            from pdf2epub.engines import preload

            preload(*key)
            self._warm.add(key)
        except Exception as e:  # the job reports a real failure with a proper message later
            log.info("warm-up skipped: %s", e)
        finally:
            self.preparing = None

    def _run(self, job: Job) -> None:
        from pdf2epub import app as core
        from pdf2epub.app import epub_chapters, explain_error, latest_doc
        from pdf2epub.cleanup import cleanup_after_build
        from pdf2epub.ui import attention_items

        if job.cancel:
            job.status, job.finished = "cancelled", time.time()
            return
        job.status, job.started = "running", time.time()
        rep = JobReporter(job)
        try:
            settings = job_settings(job.options)
            _, ctx = core.convert_and_review(settings, job.upload.path, None, None, rep)
            res = core.build(settings, ctx.book_dir, output_path(settings, job.options, job.upload), reporter=rep)
            doc = latest_doc(settings, ctx.book_dir)
            removed = cleanup_after_build(ctx.book_dir, res.valid is not False, settings.keep_work)
            job.result = {
                "file": res.epub.name,
                "path": str(res.epub),
                "size": res.epub.stat().st_size,
                "valid": res.valid,
                "chapters": epub_chapters(res.epub),
                "pages": len(doc.pages) if doc else job.upload.pages,
                "attention": attention_items(doc) if doc else [],
                "work_dir": None if removed else str(ctx.book_dir),
                "epubcheck": res.epubcheck_output[-3000:] if res.valid is False else "",
                "note": res.epubcheck_output if res.valid is None else "",
            }
            job.status = "done"
            if removed:  # the PDF copy is only needed to resume a failed conversion
                self.uploads.pop(job.upload.id, None)
                shutil.rmtree(job.upload.path.parent, ignore_errors=True)
        except Cancelled:
            for s in job.steps:
                if s["status"] == "running" or s["status"] == "failed" and not s["note"]:
                    s.update(status="failed", note="stopped")
            job.status = "cancelled"
        except Exception as e:  # noqa: BLE001 - shown to the user
            log.info("job %s failed", job.id, exc_info=True)
            msg, hint = explain_error(e)
            job.error = {"message": msg, "hint": "" if hint.startswith("Run with -v") else hint}
            for s in job.steps:
                if s["status"] == "running":
                    s["status"] = "failed"
            job.status = "failed"
        finally:
            job.finished = time.time()
