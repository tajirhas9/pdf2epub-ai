"""Pause/resume: crash-safe files, per-page progress in page stages and the LLM review, the run
manifest, and the web app bringing unfinished jobs back."""

import asyncio
import json
import time
from pathlib import Path

import pymupdf
import pytest

from pdf2epub import app as core
from pdf2epub.config import LLMSettings, Settings
from pdf2epub.ir import Document
from pdf2epub.llm.client import LLMUnavailable, OpenAICompatClient
from pdf2epub.pipeline import Context, PageStage, Pipeline, load_checkpoint, save_checkpoint
from pdf2epub.resume import (
    OptionsChanged,
    PagePartials,
    Paused,
    atomic_write_text,
    fingerprint,
    read_run,
    resumable,
    run_options,
    write_run,
)
from tests.helpers import block, page


class Counting(PageStage):
    """Marks each page it processes; `seen` lists the pages (serial runs only)."""

    parallel = False

    def __init__(self, name="count"):
        self.name = name
        self.seen: list[int] = []

    def process(self, p, ctx):
        self.seen.append(p.index)
        p.flags = [*p.flags, f"by:{self.name}"]
        return p


class Slow(PageStage):
    name = "slow"

    def process(self, p, ctx):
        time.sleep(0.3)
        return p


def _doc(n=6) -> Document:
    return Document(id="b", source="b.pdf", pages=[page(i, [block(f"b{i}", "text")]) for i in range(n)])


def _ctx(tmp_path, **kw) -> Context:
    s = Settings(work_dir=tmp_path / "work", workers=kw.pop("workers", 1))
    return Context(settings=s, book_dir=tmp_path / "work" / "b", source=Path("b.pdf"), **kw)


def _pause_after(n: int):
    calls = {"n": 0}

    def requested() -> bool:
        calls["n"] += 1
        return calls["n"] >= n

    return requested


# ---- files ----


def test_atomic_write_and_corrupt_checkpoint_is_ignored(tmp_path):
    f = tmp_path / "a" / "x.json"
    atomic_write_text(f, '{"a": 1}')
    atomic_write_text(f, '{"a": 2}')
    assert json.loads(f.read_text()) == {"a": 2}
    assert [p.name for p in f.parent.iterdir()] == ["x.json"]  # no temp files left behind

    ctx = _ctx(tmp_path)
    save_checkpoint(_doc(2), ctx, "text")
    assert load_checkpoint(ctx).pages[1].index == 1
    (ctx.ir_dir / "text.json").write_text('{"id": "b", "pag')  # cut short by a power cut
    assert load_checkpoint(ctx) is None
    (ctx.ir_dir / "latest.json").write_text("{")
    assert load_checkpoint(ctx) is None


# ---- page stages ----


def test_page_stage_saves_each_page_and_resumes_the_rest(tmp_path):
    ctx = _ctx(tmp_path, pause_requested=_pause_after(2))
    stage = Counting()
    with pytest.raises(Paused):
        stage.run(_doc(), ctx)
    assert stage.seen == [0, 1]
    assert sorted(PagePartials(ctx.ir_dir, "count").load()) == [0, 1]

    ctx.resume, ctx.pause_requested = True, lambda: False
    again = Counting()
    doc = again.run(_doc(), ctx)
    assert again.seen == [2, 3, 4, 5]  # the saved pages are not redone
    assert all("by:count" in p.flags for p in doc.pages)


def test_pausing_a_parallel_stage_does_not_wait_for_every_page(tmp_path):
    ctx = _ctx(tmp_path, workers=2, pause_requested=lambda: True)
    t = time.perf_counter()
    with pytest.raises(Paused):
        Slow().run(_doc(12), ctx)
    assert time.perf_counter() - t < 12 * 0.3 / 2  # remaining pages were cancelled, not run
    saved = PagePartials(ctx.ir_dir, "slow").load()
    assert 1 <= len(saved) < 12  # finished pages (incl. the ones in flight) are kept


def test_pipeline_resumes_after_the_last_checkpoint(tmp_path):
    a, b = Counting("a"), Counting("b")
    pipe = Pipeline([a, b])
    ctx = _ctx(tmp_path)
    assert pipe.resume_point(ctx) is None
    ctx.pause_requested = _pause_after(2 + 6)  # stage a (6 pages + check after it), then 1 page of b
    with pytest.raises(Paused):
        pipe.run(_doc(), ctx)
    assert pipe.resume_point(ctx) == "b"
    assert not PagePartials(ctx.ir_dir, "a").dir.exists()  # cleared once its checkpoint exists

    ctx.resume, ctx.pause_requested = True, lambda: False
    a2, b2 = Counting("a"), Counting("b")
    doc = Pipeline([a2, b2]).run(load_checkpoint(ctx), ctx, from_stage="b")
    assert a2.seen == [] and len(b2.seen) < 6
    assert all({"by:a", "by:b"} <= set(p.flags) for p in doc.pages)
    assert Pipeline([a2, b2]).resume_point(ctx) == ""


def test_fresh_run_clears_stale_progress(tmp_path):
    ctx = _ctx(tmp_path)
    PagePartials(ctx.ir_dir, "count").save(page(0, []))
    PagePartials(ctx.ir_dir, "review").save(page(0, []))
    stage = Counting()
    Pipeline([stage]).run(_doc(2), ctx)
    assert stage.seen == [0, 1]
    assert not (ctx.ir_dir / "partial").exists()


# ---- whole conversion (fake pipeline) ----


def _pdf(path: Path, n=5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for _ in range(n):
        doc.new_page()
    doc.save(path)
    return path


@pytest.fixture
def fake_convert(monkeypatch):
    stages: list[Counting] = []

    def build(settings):
        stages[:] = [Counting("a"), Counting("b")]
        return Pipeline(stages)

    monkeypatch.setattr(core, "build_pipeline", build)
    monkeypatch.setattr(core, "preload_models", lambda *a, **k: None)
    monkeypatch.setattr(core, "write_pages", lambda doc, ctx: None)
    return stages


def test_convert_pauses_and_resumes_where_it_stopped(tmp_path, fake_convert):
    pdf = _pdf(tmp_path / "Book.pdf")
    settings = Settings(work_dir=tmp_path / "work", workers=1, langs=["en"])
    settings.llm.enabled = False
    with pytest.raises(Paused):
        core.convert_and_review(settings, pdf, pause_requested=_pause_after(5 + 1 + 2))
    book = settings.book_dir("book")
    run = read_run(book)
    assert run["status"] == "paused" and run["phase"] == "b" and run["done"] == 2 and run["total"] == 5

    seen = []
    doc, _ = core.convert_and_review(settings, pdf, on_resume=seen.append)
    assert seen and seen[0]["status"] == "paused"
    a, b = fake_convert
    assert a.seen == [] and b.seen == [2, 3, 4]
    assert read_run(book)["status"] == "converted"
    assert all({"by:a", "by:b"} <= set(p.flags) for p in doc.pages)

    # converted: rerunning goes straight to the end; --restart redoes everything
    core.convert_and_review(settings, pdf)
    assert fake_convert[0].seen == [] and fake_convert[1].seen == []
    core.convert_and_review(settings, pdf, resume=False)
    assert fake_convert[0].seen == [0, 1, 2, 3, 4]


def test_resume_needs_the_same_file_and_options(tmp_path, fake_convert):
    pdf = _pdf(tmp_path / "Book.pdf")
    settings = Settings(work_dir=tmp_path / "work", workers=1, langs=["en"])
    settings.llm.enabled = False
    with pytest.raises(Paused):
        core.convert_and_review(settings, pdf, pause_requested=lambda: True)
    book = settings.book_dir("book")
    assert resumable(settings, pdf, book) is not None
    copy = tmp_path / "upload" / "Book.pdf"  # the same content elsewhere (e.g. uploaded again) resumes
    copy.parent.mkdir()
    copy.write_bytes(pdf.read_bytes())
    assert resumable(settings, copy, book) is not None
    with pytest.raises(OptionsChanged):
        resumable(Settings(work_dir=tmp_path / "work", langs=["ar"]), pdf, book)
    with pytest.raises(OptionsChanged):  # a different OCR engine would mix two engines' text in one book
        other = "paddle" if settings.ocr_engine != "paddle" else "apple"
        resumable(Settings(work_dir=tmp_path / "work", workers=1, langs=["en"], ocr_engine=other), pdf, book)
    _pdf(pdf, 6)  # the PDF changed: its old progress doesn't apply
    assert resumable(settings, pdf, book) is None


# ---- LLM review ----


class FakeClient:
    def __init__(self, *a, **k): ...

    async def ping(self): ...

    async def close(self): ...


class FakeWriter:
    def __init__(self, *a, **k): ...

    def write_page(self, page):
        return True

    def write_all(self):
        return {"written": [], "locked": []}


@pytest.fixture
def fake_review(monkeypatch):
    from pdf2epub import review as rv

    calls: list[int] = []
    state = {"down_at": None}

    async def review_page(client, p, settings, book_dir, renderer=None):
        if p.index == state["down_at"]:
            raise LLMUnavailable("page_review: cannot reach http://localhost:8080/v1")
        calls.append(p.index)
        p.flags = [*p.flags, "review:ok"]
        return [{"page": p.index + 1, "status": "applied", "reason": "fix", "op": {"op": "text"}}]

    async def none(*a, **k):
        return None

    async def no_log(*a, **k):
        return []

    monkeypatch.setattr(rv, "OpenAICompatClient", FakeClient)
    monkeypatch.setattr(rv, "VuePageWriter", FakeWriter)
    monkeypatch.setattr(rv, "review_page", review_page)
    monkeypatch.setattr(rv, "_start_renderer", none)
    monkeypatch.setattr(rv, "verify_inserted_marks", no_log)
    return calls, state


def test_review_pauses_when_the_llm_goes_away_and_resumes(tmp_path, fake_review):
    from pdf2epub.review import run_review

    calls, state = fake_review
    settings = Settings(work_dir=tmp_path / "work")
    settings.llm.review_policy, settings.llm.concurrency = "all", 1
    ctx = _ctx(tmp_path)
    save_checkpoint(_doc(5), ctx, "glyphs")

    state["down_at"] = 2
    with pytest.raises(Paused) as e:
        asyncio.run(run_review(settings, ctx.book_dir, None))
    assert e.value.reason.startswith("llm-unreachable")
    assert calls == [0, 1]
    assert sorted(PagePartials(ctx.ir_dir, "review").load()) == [0, 1]

    state["down_at"] = None
    summary = asyncio.run(run_review(settings, ctx.book_dir, None))
    assert calls == [0, 1, 2, 3, 4]  # pages 1-2 were not reviewed twice
    assert summary["pages"] == 5 and summary["applied"] == 5
    log = (ctx.book_dir / "llm" / "ops-log.jsonl").read_text().splitlines()
    assert sorted(json.loads(x)["page"] for x in log) == [1, 2, 3, 4, 5]
    doc = load_checkpoint(ctx)
    assert all("review:ok" in p.flags for p in doc.pages)
    assert not (ctx.ir_dir / "partial").exists() and not (ctx.ir_dir / "review-pre.json").exists()


def test_review_pause_lets_the_current_page_finish(tmp_path, fake_review):
    from pdf2epub.review import run_review

    calls, _ = fake_review
    settings = Settings(work_dir=tmp_path / "work")
    settings.llm.review_policy, settings.llm.concurrency = "all", 1
    ctx = _ctx(tmp_path)
    save_checkpoint(_doc(4), ctx, "glyphs")
    flag = {"on": False}

    def requested():
        on = flag["on"]
        flag["on"] = True  # asked while page 1 is being reviewed
        return on

    with pytest.raises(Paused):
        asyncio.run(run_review(settings, ctx.book_dir, None, pause_requested=requested))
    assert calls == [0]
    asyncio.run(run_review(settings, ctx.book_dir, None))
    assert calls == [0, 1, 2, 3]


def test_failed_pages_are_recorded_and_retried(tmp_path, fake_review, monkeypatch):
    from pdf2epub import review as rv
    from pdf2epub.llm.client import LLMError
    from pdf2epub.review import review_failures, run_review

    calls, _ = fake_review
    ok_review = rv.review_page
    broken = {1, 3}

    async def flaky(client, p, *a, **k):
        if p.index in broken:
            raise LLMError("page_review: model did not return JSON")
        return await ok_review(client, p, *a, **k)

    monkeypatch.setattr(rv, "review_page", flaky)
    settings = Settings(work_dir=tmp_path / "work")
    settings.llm.review_policy, settings.llm.concurrency = "all", 1
    ctx = _ctx(tmp_path)
    save_checkpoint(_doc(5), ctx, "glyphs")
    notes = []

    class Rep:
        def start(self, *a, **k): ...
        def advance(self, key, done, total=None, note=None):
            notes.append(note)

        def done(self, key, note=""):
            notes.append(note)

    summary = asyncio.run(run_review(settings, ctx.book_dir, None, Rep()))
    assert summary["failed_pages"] == [1, 3]
    assert review_failures(ctx.book_dir) == {
        1: "page_review: model did not return JSON",
        3: "page_review: model did not return JSON",
    }
    assert "2 failed (p2: page_review: model did not return JSON)" in notes[-1]
    doc = load_checkpoint(ctx)
    assert [i for i, p in enumerate(doc.pages) if "review:failed" in p.flags] == [1, 3]

    broken.discard(1)  # page 2 works now, page 4 still fails
    summary = asyncio.run(run_review(settings, ctx.book_dir, {1, 3}))
    assert summary["pages"] == 2 and summary["failed_pages"] == [3]
    assert review_failures(ctx.book_dir) == {3: "page_review: model did not return JSON"}
    doc = load_checkpoint(ctx)
    assert "review:failed" not in doc.pages[1].flags and doc.pages[3].flags.count("review:failed") == 1


def test_unreachable_server_is_llm_unavailable():
    s = LLMSettings(base_url="http://127.0.0.1:9/v1", model="m", max_retries=0, timeout_s=3)

    async def go():
        client = OpenAICompatClient(s)
        try:
            await client.complete_json("sys", "hi", [], {"type": "object"}, "t")
        finally:
            await client.close()

    with pytest.raises(LLMUnavailable):
        asyncio.run(go())


# ---- web app ----


def _web_run(tmp_path, status="running", reason="") -> tuple[Path, Path]:
    work = tmp_path / "work"
    pdf = _pdf(work / "_uploads" / "u1" / "Book.pdf")
    book = work / "book"
    book.mkdir(parents=True)
    write_run(
        book,
        source=str(pdf),
        fingerprint=fingerprint(pdf),
        options=run_options(Settings()),
        status=status,
        reason=reason,
        phase="review",
        done=3,
        total=5,
        web={"job_id": "j1", "name": "Book.pdf", "title": "Book", "pages": 5, "upload": str(pdf), "options": {}},
    )
    return work, pdf


def test_web_app_brings_back_unfinished_jobs(tmp_path, monkeypatch):
    from pdf2epub.webui.jobs import JobRunner

    monkeypatch.setattr(JobRunner, "warm_up", lambda self: None)
    work, pdf = _web_run(tmp_path)
    orphan = work / "_uploads" / "old"
    orphan.mkdir()
    runner = JobRunner(work / "_uploads")
    job = runner.jobs["j1"]
    assert job.status == "paused" and job.reason == "interrupted"  # was running when the app stopped
    assert runner.list()[0]["reason"] == "interrupted"
    assert pdf.exists() and not orphan.exists()  # the upload it needs is kept, the rest removed

    runner.remove("j1")  # discards the saved progress too
    assert not (work / "book").exists()


def test_web_pause_and_resume(tmp_path, monkeypatch):
    from pdf2epub.webui.jobs import JobRunner

    monkeypatch.setattr(JobRunner, "warm_up", lambda self: None)
    ran = []
    monkeypatch.setattr(JobRunner, "_run", lambda self, job: ran.append(job.id))
    work, _ = _web_run(tmp_path, "paused", "llm-unreachable: page_review: cannot reach")
    runner = JobRunner(work / "_uploads")
    job = runner.jobs["j1"]
    assert job.reason.startswith("llm-unreachable")
    with pytest.raises(ValueError):
        runner.pause("j1")  # already paused
    runner.resume("j1")
    assert job.status == "queued" and job.reason == ""
    for _ in range(50):
        if ran:
            break
        time.sleep(0.02)
    assert ran == ["j1"]
    job.status = "running"
    runner.pause("j1")
    assert runner.list()[0]["status"] == "pausing"
    with pytest.raises(ValueError):
        runner.resume("j1")


def test_web_retry_review_of_failed_pages(tmp_path, monkeypatch):
    from pdf2epub.webui.jobs import JobRunner

    monkeypatch.setattr(JobRunner, "warm_up", lambda self: None)
    ran = []
    monkeypatch.setattr(JobRunner, "_retry", lambda self, job: ran.append(("retry", job.id)))
    monkeypatch.setattr(JobRunner, "_run", lambda self, job: ran.append(("run", job.id)))
    work, _ = _web_run(tmp_path, "paused", "paused")
    runner = JobRunner(work / "_uploads")
    job = runner.jobs["j1"]
    job.status = "done"
    job.result = {"review_failed": [], "work_dir": str(work / "book")}
    with pytest.raises(ValueError):
        runner.retry_review("j1")  # nothing failed
    job.result["review_failed"] = [{"page": 2, "reason": "x"}]
    runner.retry_review("j1")
    assert job.status == "queued" and job.retrying
    for _ in range(50):
        if ran:
            break
        time.sleep(0.02)
    assert ran == [("retry", "j1")]
    job.status = "paused"  # paused during the retry: Resume continues the retry, not a full conversion
    runner.resume("j1")
    for _ in range(50):
        if len(ran) == 2:
            break
        time.sleep(0.02)
    assert ran[-1] == ("retry", "j1")


# ---- retry from a step ---------------------------------------------------------------------------

STAGES = ("a", "b", "c")


def _book(tmp_path) -> Path:
    """A finished, reviewed book: a checkpoint per stage plus the review's files."""
    book = tmp_path / "book"
    for name in (*STAGES, "review"):
        atomic_write_text(book / "ir" / f"{name}.json", "{}")
    atomic_write_text(book / "ir" / "latest.json", json.dumps({"stage": "review"}))
    atomic_write_text(book / "ir" / "partial" / "c" / "p0001.json", "{}")
    for name in ("llm/ops-log.jsonl", "llm/review-failures.json", "llm/cache/x.json", "book.json"):
        atomic_write_text(book / name, "{}")
    write_run(book, status="done")
    return book


def test_retry_points_follow_the_files_on_disk(tmp_path):
    from pdf2epub.resume import retry_points

    book = _book(tmp_path)
    assert retry_points(book, STAGES, llm=True) == [*STAGES, "codegen", "review", "build", "validate"]
    assert "review" not in retry_points(book, STAGES, llm=False)
    (book / "ir" / "a.json").unlink()
    (book / "book.json").unlink()
    assert retry_points(book, STAGES, llm=False) == ["a", "c", "codegen"]
    assert retry_points(tmp_path / "gone", STAGES, llm=True) == []


def test_rewind_to_a_stage_keeps_the_stages_before_it(tmp_path):
    from pdf2epub.resume import latest_stage, rewind

    book = _book(tmp_path)
    rewind(book, "b", STAGES)
    assert sorted(f.stem for f in (book / "ir").glob("*.json")) == ["a", "latest"]
    assert latest_stage(book) == "a"
    assert not (book / "ir" / "partial").exists()
    assert not (book / "llm" / "ops-log.jsonl").exists() and not (book / "llm" / "review-failures.json").exists()
    assert (book / "llm" / "cache" / "x.json").exists()  # free, fast rerun of the review
    run = read_run(book)
    assert run["status"] == "paused" and run["redo"] == "b"


def test_rewind_to_the_review_or_the_build(tmp_path):
    from pdf2epub.resume import latest_stage, rewind

    book = _book(tmp_path)
    rewind(book, "build", STAGES)  # the IR stays; only the EPUB is left to build
    assert latest_stage(book) == "review" and read_run(book)["status"] == "converted"
    rewind(book, "review", STAGES)
    assert latest_stage(book) == "c" and not (book / "ir" / "review.json").exists()
    rewind(book, "a", STAGES)
    assert latest_stage(book) is None and not list((book / "ir").glob("*.json"))
    with pytest.raises(ValueError):  # the input of "b" is gone now
        rewind(book, "b", STAGES)


def test_convert_redoes_from_a_stage(tmp_path, fake_convert, monkeypatch):
    monkeypatch.setattr(core, "STAGE_NAMES", ("a", "b"))
    monkeypatch.setattr(core, "RETRY_STEPS", ("a", "b", "codegen", "build"))
    pdf = _pdf(tmp_path / "Book.pdf")
    settings = Settings(work_dir=tmp_path / "work", workers=1, langs=["en"])
    settings.llm.enabled = False
    core.convert_and_review(settings, pdf)
    book = settings.book_dir("book")
    seen = []
    doc, _ = core.convert_and_review(settings, pdf, None, "b", on_resume=seen.append)
    a, b = fake_convert
    assert a.seen == [] and b.seen == [0, 1, 2, 3, 4]
    assert seen[0]["redo"] == "b" and read_run(book)["status"] == "converted" and not read_run(book)["redo"]
    assert all({"by:a", "by:b"} <= set(p.flags) for p in doc.pages)
    with pytest.raises(ValueError):
        core.convert_and_review(settings, pdf, None, "nope")


def test_resume_true_never_starts_over(tmp_path, fake_convert):
    pdf = _pdf(tmp_path / "Book.pdf")
    settings = Settings(work_dir=tmp_path / "work", workers=1, langs=["en"])
    settings.llm.enabled = False
    book = settings.book_dir("book")
    atomic_write_text(book / "keep.txt", "x")
    with pytest.raises(ValueError):
        core.convert_and_review(settings, pdf, resume=True)
    assert (book / "keep.txt").exists()
