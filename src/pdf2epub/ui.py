"""Terminal presentation: a calm live checklist of stages, a result panel, friendly errors.

The pipeline only talks to the `Reporter` protocol; the CLI picks the implementation:
- RichReporter  — interactive terminal: live rows (spinner -> ✓), bars with page counts and ETA,
                  one-line findings per stage.
- PlainReporter — pipes/CI/NO_COLOR logs: one line per finished stage, no animation.
- NullReporter  — library use and tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import BarColumn, Progress, ProgressColumn, SpinnerColumn, Task, TaskID, TextColumn
from rich.table import Table
from rich.text import Text

# Stage key -> label the user reads.
LABELS = {
    "llm_connect": "Connect to LLM",
    "models": "Load vision models",
    "rasterize": "Read pages",
    "layout": "Detect layout",
    "text": "Recognize text",
    "running": "Remove headers & page numbers",
    "figures": "Extract figures",
    "spreads": "Join two-page spreads",
    "order": "Reading order & footnotes",
    "glyphs": "Find special marks",
    "codegen": "Write pages",
    "spread_check": "LLM: check spreads",
    "glyph_check": "LLM: verify special marks",
    "review": "LLM: review pages",
    "inserted_marks": "LLM: verify added marks",
    "browser": "Download page renderer",
    "renders": "Page renders for the LLM",
    "build": "Build EPUB",
    "validate": "Validate (epubcheck)",
}


def label(key: str) -> str:
    return LABELS.get(key, key.replace("_", " ").capitalize())


class Reporter(Protocol):
    def start(self, key: str, total: int | None = None, note: str = "") -> None: ...
    def advance(self, key: str, done: int, total: int | None = None, note: str | None = None) -> None: ...
    def done(self, key: str, note: str = "") -> None: ...
    def fail(self, key: str, note: str = "") -> None: ...
    def stop(self) -> None: ...


class NullReporter:
    def start(self, key, total=None, note=""): ...
    def advance(self, key, done, total=None, note=None): ...
    def done(self, key, note=""): ...
    def fail(self, key, note=""): ...
    def stop(self): ...


def _secs(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    m, s = divmod(int(s), 60)
    return f"{m}m {s:02d}s" if m < 60 else f"{m // 60}h {m % 60:02d}m"


class _Status(ProgressColumn):
    """Spinner while running, ✓ when done, ✗ when failed."""

    def __init__(self):
        super().__init__()
        self._spin = SpinnerColumn(style="cyan")

    def render(self, task: Task):
        if task.fields.get("failed"):
            return Text("✗", style="red")
        if task.finished:
            return Text("✓", style="green")
        return self._spin.render(task)


class _Bar(ProgressColumn):
    """Bar only while a counted stage runs; finished rows stay uncluttered."""

    def __init__(self):
        super().__init__()
        self._bar = BarColumn(bar_width=22, complete_style="cyan", finished_style="cyan", pulse_style="cyan")

    def render(self, task: Task):
        if task.finished or task.total is None:
            return Text(" " * 22)
        return self._bar.render(task)


class _Count(ProgressColumn):
    def render(self, task: Task):
        if task.total is None or task.fields.get("counted") is False:
            return Text("")
        return Text(f"{int(task.completed)}/{int(task.total)}", style="dim" if task.finished else "")


class _Time(ProgressColumn):
    """Elapsed for finished rows; elapsed + estimate for running counted rows."""

    def render(self, task: Task):
        elapsed = task.finished_time if task.finished else task.elapsed
        if elapsed is None:
            return Text("")
        if not task.finished and task.total and 0 < task.completed < task.total and elapsed > 3:
            remaining = elapsed / task.completed * (task.total - task.completed)
            return Text(f"{_secs(elapsed)}  ~{_secs(remaining)} left", style="dim")
        return Text(_secs(elapsed), style="dim")


class RichReporter:
    def __init__(self, console: Console):
        self.console = console
        self.progress = Progress(
            _Status(),
            TextColumn("{task.description}", style="bold", table_column=None),
            _Bar(),
            _Count(),
            _Time(),
            TextColumn("[dim]{task.fields[note]}[/]"),
            console=console,
            transient=False,
            refresh_per_second=8,
        )
        self.tasks: dict[str, TaskID] = {}
        self._started = False

    def _ensure(self):
        if not self._started:
            self.progress.start()
            self._started = True

    def start(self, key, total=None, note=""):
        self._ensure()
        if key in self.tasks:
            self.progress.reset(self.tasks[key], total=total, completed=0, note=note)
            return
        self.tasks[key] = self.progress.add_task(label(key), total=total, note=note, failed=False)

    def advance(self, key, done, total=None, note=None):
        if key not in self.tasks:
            self.start(key, total)
        kw = {"completed": done}
        if total is not None:
            kw["total"] = total
        if note is not None:
            kw["note"] = note
        self.progress.update(self.tasks[key], **kw)

    def done(self, key, note=""):
        if key not in self.tasks:
            self.start(key, None)
        tid = self.tasks[key]
        task = self.progress.tasks[tid]
        if task.total is None:  # uncounted stage: finish it so the spinner becomes ✓ (count column stays empty)
            self.progress.update(tid, total=1, completed=1, note=note, counted=False)
        else:
            self.progress.update(tid, completed=task.total, note=note)
        self.progress.stop_task(tid)

    def fail(self, key, note=""):
        if key not in self.tasks:
            self.start(key)
        tid = self.tasks[key]
        self.progress.update(tid, failed=True, description=f"[red]{label(key)}[/]", note=note)
        self.progress.stop_task(tid)

    def stop(self):
        if self._started:
            self.progress.stop()
            self._started = False


class PlainReporter:
    """No animation: one line per finished stage (logs, CI, pipes)."""

    def __init__(self, console: Console):
        self.console = console
        self.t0: dict[str, float] = {}
        self.totals: dict[str, int | None] = {}

    def start(self, key, total=None, note=""):
        self.t0[key] = time.perf_counter()
        self.totals[key] = total

    def advance(self, key, done, total=None, note=None):
        if key not in self.t0:
            self.start(key, total)
        if total is not None:
            self.totals[key] = total

    def done(self, key, note=""):
        t = time.perf_counter() - self.t0.get(key, time.perf_counter())
        n = self.totals.get(key)
        count = f"{n} pages, " if n else ""
        line = f"✓ {label(key)} ({count}{_secs(t)})" + (f"  {note}" if note else "")
        self.console.print(line, highlight=False, soft_wrap=True)  # logs: never truncate

    def fail(self, key, note=""):
        self.console.print(f"✗ {label(key)}" + (f" — {note}" if note else ""), highlight=False, soft_wrap=True)

    def stop(self): ...


def make_reporter(console: Console, quiet: bool = False) -> Reporter:
    if quiet:
        return NullReporter()
    return RichReporter(console) if console.is_terminal else PlainReporter(console)


class Fanout:
    """Reporter + extra listeners (e.g. progress.json for the live workbench)."""

    def __init__(self, reporter: Reporter, on_progress: Callable[[str, int, int], None] | None = None):
        self.reporter = reporter
        self.on_progress = on_progress

    def start(self, key, total=None, note=""):
        self.reporter.start(key, total, note)

    def advance(self, key, done, total=None, note=None):
        self.reporter.advance(key, done, total, note)
        if self.on_progress and total:
            self.on_progress(key, done, total)

    def done(self, key, note=""):
        self.reporter.done(key, note)

    def fail(self, key, note=""):
        self.reporter.fail(key, note)

    def stop(self):
        self.reporter.stop()


# ---- findings per stage (one short line each) -------------------------------------------------


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def findings(stage: str, doc) -> str:
    from pdf2epub.ir import BlockType, Role, SpreadStatus

    pages = doc.pages
    if stage == "rasterize":
        scanned = sum(p.kind.value == "scanned" for p in pages)
        if scanned == len(pages):
            return f"scanned, {int(pages[0].dpi)} ppi" if pages else ""
        return "born-digital" if scanned == 0 else f"{scanned} scanned, {len(pages) - scanned} born-digital"
    if stage == "layout":
        n = sum(len(p.blocks) for p in pages)
        return f"{_plural(n, 'region')}"
    if stage == "text":
        ocr = sum("ocr" in p.flags for p in pages)
        rtl = sum(1 for p in pages for b in p.blocks if "rtl" in b.flags)
        parts = [
            "text layer used" if ocr == 0 else ("OCR on all pages" if ocr == len(pages) else f"OCR on {ocr} pages")
        ]
        if rtl:
            parts.append(f"{_plural(rtl, 'right-to-left block')}")
        return ", ".join(parts)
    if stage == "running":
        n = sum(1 for p in pages for b in p.blocks if b.role in (Role.HEADER, Role.FOOTER, Role.PAGE_NUMBER))
        d = sum(1 for p in pages for b in p.blocks if b.role == Role.DECORATION)
        labels = [p.label for p in pages if p.label]
        out = f"{n} removed" + (f", {d} decorations" if d else "")
        if labels:
            out += f" · printed pages {labels[0]}–{labels[-1]}"
        return out
    if stage == "figures":
        figs = sum(1 for p in pages for b in p.blocks if b.type == BlockType.FIGURE and b.role == Role.CONTENT)
        bg = sum(1 for p in pages for b in p.blocks if b.role == Role.BACKGROUND)
        out = _plural(figs, "figure") if figs else "no figures"
        return out + (f", {bg} background images skipped" if bg else "")
    if stage == "spreads":
        joined = sum(sp.status in (SpreadStatus.MERGED, SpreadStatus.SIDE_BY_SIDE) for sp in doc.spreads)
        cand = sum(sp.status == SpreadStatus.CANDIDATE for sp in doc.spreads)
        if not doc.spreads:
            return "none found"
        return f"{joined} joined" + (f", {cand} uncertain (LLM decides)" if cand else "")
    if stage == "order":
        notes = sum(1 for p in pages for b in p.blocks for s in b.spans if s.note_ref)
        return f"{_plural(notes, 'footnote')} linked" if notes else "no footnote markers"
    if stage == "glyphs":
        occ = sum(g.occurrences for g in doc.glyphs.values())
        return "none" if not occ else f"{occ} found ({_plural(len(doc.glyphs), 'distinct mark')})"
    return ""


# ---- result + error panels ------------------------------------------------------------------


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def result_panel(epub, valid: bool | None, seconds: float, chapters: int, pages: int, attention: list[str]) -> Panel:
    rows = Table.grid(padding=(0, 2))
    rows.add_column(style="dim", justify="right")
    rows.add_column()
    rows.add_row("file", f"[bold]{epub.name}[/]  [dim]{_size(epub.stat().st_size)}[/]")
    rows.add_row("where", str(epub.parent))
    rows.add_row("content", f"{_plural(pages, 'page')} · {_plural(chapters, 'chapter')}")
    if valid is True:
        rows.add_row("check", "[green]valid EPUB 3[/] [dim](epubcheck)[/]")
    elif valid is False:
        rows.add_row("check", "[red]epubcheck reported problems[/] [dim](see report)[/]")
    else:
        rows.add_row("check", "[yellow]not validated[/] [dim](install Java to enable epubcheck)[/]")
    rows.add_row("time", _secs(seconds))
    body = [rows]
    if attention:
        body.append(Text(""))
        body.append(Text("worth a look", style="yellow"))
        for a in attention[:6]:
            body.append(Text(f"  • {a}", style="dim"))
        if len(attention) > 6:
            body.append(Text(f"  • … {len(attention) - 6} more in the report", style="dim"))
    color = "green" if valid is not False else "red"
    title = "Done" if valid is not False else "Built, but validation failed"
    return Panel(Group(*body), title=f"[{color}]{title}[/]", title_align="left", border_style=color, expand=False)


def error_panel(message: str, hint: str = "") -> Panel:
    body = Text(message)
    if hint:
        body.append("\n\n")
        body.append(hint, style="dim")
    return Panel(body, title="[red]Error[/]", title_align="left", border_style="red", expand=False)


def attention_items(doc) -> list[str]:
    """Things a careful reader may want to check in the workbench (short, actionable)."""
    from pdf2epub.ir import SpreadStatus

    out = []
    for sp in doc.spreads:
        a, b = doc.pages[sp.left_page], doc.pages[sp.right_page]
        pair = f"{a.label or a.index + 1}–{b.label or b.index + 1}"
        if sp.status == SpreadStatus.CANDIDATE:
            other = any(
                o is not sp and o.left_page == sp.left_page and o.status != SpreadStatus.CANDIDATE for o in doc.spreads
            )
            what = "another pair of pictures" if other else "two pictures"
            out.append(f"pages {pair}: {what} at the gutter might be one picture (kept separate)")
        elif sp.status == SpreadStatus.SIDE_BY_SIDE:
            out.append(f"pages {pair}: two-page picture joined with a thin gap (gutter lost in the scan)")
    images = [g for g in doc.glyphs.values() if g.render != "text" and g.occurrences]
    if images:
        n = sum(g.occurrences for g in images)
        out.append(f"{_plural(n, 'special mark')} kept as printed images (not verified as text)")
    for p in doc.pages:
        if "review:needs_human" in p.flags:
            out.append(f"page {p.label or p.index + 1}: the reviewer asked for a human check")
        if "review:failed" in p.flags:
            out.append(f"page {p.label or p.index + 1}: LLM review failed")
    return out
