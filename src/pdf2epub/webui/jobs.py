"""Conversion jobs for the web UI.

One worker thread runs jobs in order (OCR models and the GPU are shared), so the vision models stay
loaded between books. Uploading a file queues a warm-up so model loading overlaps with choosing options.

Jobs can be paused and resumed. Each running job records itself in its book's run.json, so a job cut
off by a restart or a power cut comes back as "paused" the next time the app starts.
"""

from __future__ import annotations

import logging
import queue
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pdf2epub.app import STAGE_NAMES
from pdf2epub.config import Settings
from pdf2epub.resume import BUILD_STEPS
from pdf2epub.ui import label

log = logging.getLogger(__name__)

REVIEW_POLICIES = ("flagged", "all")
PIPELINE_STEPS = STAGE_NAMES  # shown as pending before they run


def planned_steps(settings: Settings) -> list[str]:
    """Steps every conversion runs, shown as pending up front. Occasional ones appear when they start."""
    llm = settings.llm.enabled
    return [
        *(["llm_connect"] if llm else []),
        "models",
        *PIPELINE_STEPS,
        "codegen",
        *(["review"] if llm else []),
        *BUILD_STEPS,
    ]


RETRYABLE = ("paused", "done", "failed", "cancelled")  # job states "Retry from here" is offered in


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
    status: str = "queued"  # queued | running | paused | done | failed | cancelled
    steps: list[dict] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    result: dict | None = None
    error: dict | None = None
    cancel: bool = False
    pause: bool = False
    reason: str = ""  # why it is paused: "paused" | "interrupted" | "llm-unreachable: …"
    retrying: bool = False  # re-reviewing the pages whose LLM call failed (Resume continues that)

    def book_dir(self, settings: Settings | None = None) -> Path:
        from pdf2epub.ingest import book_id_for

        return (settings or job_settings(self.options)).book_dir(book_id_for(self.upload.path))

    def retry_steps(self) -> list[str]:
        """Steps this job can be redone from: its work folder still holds their input."""
        from pdf2epub.resume import retry_points

        if self.status not in RETRYABLE or not self.upload.path.is_file():
            return []
        settings = job_settings(self.options)
        return retry_points(self.book_dir(settings), STAGE_NAMES, settings.llm.enabled)

    def public(self, waiting: str | None = None) -> dict:
        end = self.finished or time.time()
        status = self.status
        if self.cancel and status in ("queued", "running"):
            status = "cancelling"
        elif self.pause and status == "running":
            status = "pausing"
        return {
            "id": self.id,
            "file": self.upload.public(),
            "options": self.options,
            "status": status,
            "reason": self.reason if self.status == "paused" else "",
            "steps": [dict(s) for s in self.steps],
            "created": self.created,
            "elapsed": round(end - self.started, 1) if self.started else 0,
            "result": self.result,
            "error": self.error,
            "waiting": waiting if self.status == "queued" else None,  # what the queue is busy with
            "retry_steps": self.retry_steps(),
        }


class JobReporter:
    """pdf2epub.ui.Reporter that records steps for the browser; raises Cancelled at the next tick."""

    def __init__(self, job: Job):
        self.job = job

    def pause_requested(self) -> bool:
        return self.job.pause

    def _check(self) -> None:
        if self.job.cancel:
            raise Cancelled()

    def seed(self, keys: list[str]) -> None:
        """List the steps to come as pending (a resumed job keeps the steps it already has)."""
        if not self.job.steps:
            self.job.steps.extend(_new_step(k, "pending") for k in keys)

    def _step(self, key: str) -> dict:
        steps = self.job.steps
        for s in steps:
            if s["key"] == key:
                return s
        s = _new_step(key, "running")
        # an occasional step goes before the ones still to come
        at = next((i for i, x in enumerate(steps) if x["status"] == "pending"), len(steps))
        steps.insert(at, s)
        return s

    def start(self, key, total=None, note=""):
        self._check()
        s = self._step(key)
        s.update(status="running", done=0, total=total, note=note, t0=time.time(), seconds=None)
        for x in self.job.steps[: self.job.steps.index(s)]:
            if x["status"] == "pending":  # not needed this time (e.g. already done before a restart)
                x["status"] = "skipped"

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


def _new_step(key: str, status: str) -> dict:
    return {
        "key": key,
        "label": label(key),
        "status": status,
        "done": 0,
        "total": None,
        "note": "",
        "t0": time.time(),
        "seconds": None,
    }


def safe_name(name: str) -> str:
    stem = re.sub(r"[^\w.\- ]+", "", Path(name).stem).strip() or "book"
    return stem[:120] + ".pdf"


def job_settings(options: dict) -> Settings:
    """Per-job settings: saved config + the options picked for this conversion."""
    overrides: dict[str, Any] = {"keep_work": bool(options.get("keep_work"))}
    if options.get("langs"):
        overrides["langs"] = [str(x).strip() for x in options["langs"] if str(x).strip()]
    for key in ("use_gpu", "ocr_det_model", "ocr_engine", "vector_dpi"):
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
        self.uploads: dict[str, Upload] = {}
        self.jobs: dict[str, Job] = {}
        self.restore()
        self.clear_uploads()  # left over from an earlier run and not needed to resume a job
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
        """Remove upload folders except the ones unfinished jobs need to resume."""
        keep = {up.path.parent.resolve() for up in self.uploads.values()}
        if not self.upload_dir.is_dir():
            return
        for folder in self.upload_dir.iterdir():
            if folder.resolve() not in keep:
                shutil.rmtree(folder, ignore_errors=True) if folder.is_dir() else folder.unlink(missing_ok=True)

    def restore(self) -> None:
        """Jobs of an earlier app run that didn't finish (paused, stopped by a restart or a power cut)."""
        from pdf2epub.resume import RESUMABLE, read_run

        for run_file in sorted(self.upload_dir.parent.glob("*/run.json")):
            run = read_run(run_file.parent)
            web = (run or {}).get("web")
            if not web or run.get("status") not in RESUMABLE or run.get("status") == "converted":
                continue
            path = Path(web.get("upload", ""))
            if not path.is_file() or not path.is_relative_to(self.upload_dir):
                continue
            up = Upload(path.parent.name, path, web["name"], web["title"], int(web["pages"]))
            reason = (run["status"] == "paused" and run.get("reason")) or "interrupted"
            job = Job(web["job_id"], up, dict(web.get("options") or {}), status="paused", reason=reason)
            job.created = float(web.get("created") or run.get("updated") or time.time())
            self.uploads[up.id] = up
            self.jobs[job.id] = job

    # ---- jobs ----

    def submit(self, upload_id: str, options: dict) -> Job:
        from pdf2epub.paddle_install import INSTALLER

        if INSTALLER.running:
            raise ValueError("PaddlePaddle (the OCR engine) is still installing — try again in a minute")
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
        elif job.status == "paused":
            job.status, job.finished = "cancelled", time.time()

    def pause(self, job_id: str) -> None:
        """Running: stop after the pages in progress (saved). Waiting: just don't start it yet."""
        job = self.jobs[job_id]
        if job.status == "running":
            job.pause = True
        elif job.status == "queued":  # its queue entry is skipped; Resume queues it again
            job.status, job.reason, job.finished = "paused", "paused", time.time()
        else:
            raise ValueError("only a waiting or running conversion can be paused")

    def resume(self, job_id: str) -> None:
        job = self.jobs[job_id]
        if job.status != "paused":
            raise ValueError("this conversion is not paused")
        job.pause, job.cancel, job.reason = False, False, ""
        job.status, job.finished, job.error = "queued", None, None
        self._queue.put(("retry" if job.retrying else "job", job))

    def retry_review(self, job_id: str) -> None:
        """Review again the pages whose LLM call failed, then rebuild the EPUB."""
        job = self.jobs[job_id]
        if job.status != "done" or not (job.result or {}).get("review_failed"):
            raise ValueError("this conversion has no failed review pages to retry")
        if not Path((job.result or {}).get("work_dir") or "").is_dir():
            raise ValueError("the work folder of this conversion is gone; convert the file again")
        job.status, job.finished, job.error, job.cancel, job.pause = "queued", None, None, False, False
        job.retrying = True
        self._queue.put(("retry", job))

    def retry_from(self, job_id: str, step: str) -> None:
        """Redo `step` and every step after it (after a fix), keeping the work of the steps before it."""
        from pdf2epub.resume import rewind

        job = self.jobs[job_id]
        if job.status not in RETRYABLE:
            raise ValueError("stop or pause the conversion first")
        if step not in job.retry_steps():
            raise ValueError(f"can't redo from {label(step)!r}: its input is gone; convert the file again")
        rewind(job.book_dir(), step, STAGE_NAMES)
        keys = [s["key"] for s in job.steps]
        at = keys.index(step) if step in keys else len(keys)
        planned = planned_steps(job_settings(job.options))
        later = planned[planned.index(step) :] if step in planned else []
        job.steps[at:] = [_new_step(k, "pending") for k in later]  # occasional steps reappear as they run
        self.uploads.setdefault(job.upload.id, job.upload)
        job.status, job.reason, job.finished, job.result, job.error = "queued", "", None, None, None
        job.pause, job.cancel, job.retrying = False, False, False
        self._queue.put(("job", job))

    def remove(self, job_id: str) -> None:
        from pdf2epub.ingest import book_id_for
        from pdf2epub.resume import read_run

        job = self.jobs[job_id]
        if job.status in ("queued", "running"):
            raise ValueError("stop the conversion first")
        del self.jobs[job_id]
        book_dir = self.upload_dir.parent / book_id_for(job.upload.path)  # uploads live in work_dir/_uploads
        if job.status != "done" and ((read_run(book_dir) or {}).get("web") or {}).get("job_id") == job.id:
            shutil.rmtree(book_dir, ignore_errors=True)  # its saved progress: nothing can resume it now
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
                elif kind == "retry":
                    self._retry(job)
                else:
                    self._run(job)
            except Exception:  # noqa: BLE001 - the worker must survive anything
                log.exception("job worker")

    def _preload(self) -> None:
        settings = Settings()
        key = (settings.use_gpu, settings.ocr_scripts()[0], settings.ocr_det_model, settings.ocr_engine)
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
        from pdf2epub.resume import OptionsChanged

        def work(rep: JobReporter, settings: Settings) -> Path:
            web = {
                "job_id": job.id,
                "name": job.upload.name,
                "title": job.upload.title,
                "pages": job.upload.pages,
                "upload": str(job.upload.path),
                "options": job.options,
                "created": job.created,
            }
            convert = core.convert_and_review
            try:
                _, ctx = convert(settings, job.upload.path, None, None, rep, None, rep.pause_requested, web)
            except OptionsChanged:  # new options for a book with unfinished progress: start over
                job.steps.clear()
                rep.seed(planned_steps(settings))
                _, ctx = convert(settings, job.upload.path, None, None, rep, False, rep.pause_requested, web)
            return ctx.book_dir

        self._execute(job, work)

    def _retry(self, job: Job) -> None:
        from pdf2epub import app as core

        book_dir = Path(job.result["work_dir"])
        pages = {int(f["page"]) - 1 for f in job.result["review_failed"]}

        def work(rep: JobReporter, settings: Settings) -> Path:
            for s in job.steps:  # the review and everything after it run again
                if s["key"] in ("review", "inserted_marks") or s["key"] in BUILD_STEPS:
                    s.update(_new_step(s["key"], "pending"))
            core.review(settings, book_dir, pages, rep, rep.pause_requested)
            return book_dir

        self._execute(job, work)

    def _execute(self, job: Job, work: Callable[[JobReporter, Settings], Path]) -> None:
        """Run `work` (convert/review; returns the book dir), then build the EPUB and record the result."""
        from pdf2epub import app as core
        from pdf2epub.app import epub_chapters, explain_error, latest_doc
        from pdf2epub.cleanup import cleanup_after_build
        from pdf2epub.resume import Paused
        from pdf2epub.review import review_failures
        from pdf2epub.ui import attention_items

        if job.cancel:
            job.status, job.finished = "cancelled", time.time()
            return
        if job.status != "queued":  # paused while waiting (and maybe queued again by Resume)
            return
        job.status, job.started = "running", time.time()
        rep = JobReporter(job)
        try:
            settings = job_settings(job.options)
            rep.seed(planned_steps(settings))
            book_dir = work(rep, settings)
            res = core.build(settings, book_dir, output_path(settings, job.options, job.upload), reporter=rep)
            doc = latest_doc(settings, book_dir)
            failed = review_failures(book_dir) if settings.llm.enabled else {}
            # failed review pages: keep the work dir so they can be retried
            removed = cleanup_after_build(book_dir, res.valid is not False and not failed, settings.keep_work)
            job.result = {
                "file": res.epub.name,
                "path": str(res.epub),
                "size": res.epub.stat().st_size,
                "valid": res.valid,
                "chapters": epub_chapters(res.epub),
                "pages": len(doc.pages) if doc else job.upload.pages,
                "attention": attention_items(doc) if doc else [],
                "review_failed": [{"page": i + 1, "reason": r} for i, r in sorted(failed.items())],
                "work_dir": None if removed else str(book_dir),
                "epubcheck": res.epubcheck_output[-3000:] if res.valid is False else "",
                "note": res.epubcheck_output if res.valid is None else "",
            }
            for s in job.steps:
                if s["status"] == "pending":
                    s["status"] = "skipped"
            job.status, job.retrying = "done", False
            if removed:  # the PDF copy is only needed to resume a failed conversion
                self.uploads.pop(job.upload.id, None)
                shutil.rmtree(job.upload.path.parent, ignore_errors=True)
        except Paused as e:
            for s in job.steps:
                if s["status"] in ("running", "failed") and s["note"] in ("", "paused"):
                    s.update(status="paused", note="paused")
            job.status, job.reason, job.pause = "paused", e.reason, False
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
