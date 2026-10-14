"""pdf2epub command line."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from pdf2epub.config import Settings

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    rich_markup_mode="rich",
    help="Convert PDFs — scanned or digital — into clean, reflowable EPUB 3 books.",
    epilog=(
        "[dim]Quick start:[/]  pdf2epub init   →   pdf2epub run book.pdf -o ~/Books\n\n"
        "[dim]In the browser:[/]  pdf2epub ui   (setup, settings and conversions)\n\n"
        "[dim]Step by step:[/]  convert → dev (check pages in the browser) → review → build"
    ),
)
console = Console()


def _settings(
    langs: str | None = None,
    keep_work: bool | None = None,
    llm: bool | None = None,
    out_dir: Path | None = None,
) -> Settings:
    overrides: dict = {}
    if langs:
        overrides["langs"] = [x.strip() for x in langs.split(",") if x.strip()]
    if keep_work is not None:
        overrides["keep_work"] = keep_work
    if out_dir is not None:
        overrides["out_dir"] = out_dir.expanduser().resolve()
    settings = Settings(**overrides)
    if llm is not None:  # CLI flag wins over LLM_ENABLED
        settings.llm.enabled = llm
    return settings


LLM_OPT = typer.Option(
    "--llm/--no-llm",
    help="Have the LLM check every page against the scan (default: on, see `pdf2epub config`). "
    "--no-llm is fastest and fine for clean digital PDFs.",
    show_default=False,
)
OUTPUT_OPT = typer.Option(
    "-o",
    "--output",
    help="Folder for the EPUB (default: current folder; out/ in a git checkout). A path ending in .epub "
    "sets the exact file name.",
)


def _split_output(output: Path | None) -> tuple[Path | None, Path | None]:
    """-o value -> (exact epub path, output dir)."""
    if output is None:
        return None, None
    output = output.expanduser()
    return (output, None) if output.suffix.lower() == ".epub" else (None, output)


def _parse_pages(spec: str | None) -> set[int] | None:
    """'1-5,8' (1-based) -> {0,1,2,3,4,7}."""
    if not spec:
        return None
    out: set[int] = set()
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out.update(range(int(a) - 1, int(b or a)))
    return out


def _timings_table(stats: dict[str, float], pages: int) -> Table:
    from pdf2epub.ui import label

    t = Table(title="timings", show_header=True, header_style="dim", box=None)
    t.add_column("step")
    t.add_column("seconds", justify="right")
    t.add_column("ms/page", justify="right")
    for k, v in stats.items():
        t.add_row(label(k), f"{v:.2f}", f"{1000 * v / max(1, pages):.0f}")
    return t


VERBOSE = False


def _print_version(value: bool) -> None:
    if value:
        print(f"pdf2epub {_version()}")
        raise typer.Exit()


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Show library logs and timings")] = False,
    version: Annotated[
        bool, typer.Option("--version", callback=_print_version, is_eager=True, help="Show the version")
    ] = False,
) -> None:
    global VERBOSE
    VERBOSE = verbose
    if verbose:
        os.environ["PDF2EPUB_VERBOSE"] = "1"
        os.environ["GLOG_minloglevel"] = "0"
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("pdf2epub")
    except Exception:
        return "dev"


def _header(title: str, details: list[str]) -> None:
    console.print(f"[bold]pdf2epub[/] [dim]{_version()}[/]  {title}", highlight=False)
    if details:
        console.print("[dim]" + " · ".join(d for d in details if d) + "[/]", highlight=False)
    console.print()


def _llm_line(settings: Settings) -> str:
    if not settings.llm.enabled:
        return "LLM off"
    from urllib.parse import urlparse

    host = urlparse(settings.llm.base_url).netloc or settings.llm.base_url
    return f"LLM {settings.llm.model or 'server default'} @ {host}"


def _source_details(source: Path, settings: Settings) -> list[str]:
    from pdf2epub.ingest import open_document

    try:
        n = len(open_document(source).pages)
        pages = f"{n} page{'s' if n != 1 else ''}"
    except Exception:
        pages = ""
    langs = "languages " + ", ".join(settings.langs) if settings.langs else "languages auto"
    return [pages, langs, _llm_line(settings)]


@contextmanager
def _reporting():
    from pdf2epub.ui import make_reporter

    rep = make_reporter(console)
    try:
        yield rep
    finally:
        rep.stop()


def _convert_steps(settings: Settings, source: Path, pages: set[int] | None, from_stage: str | None, rep):
    from pdf2epub import app as core

    return core.convert_and_review(settings, source, pages, from_stage, rep)


def _finish(settings: Settings, book_dir: Path, res, t0: float) -> None:
    """Result panel + cleanup line (shared by build and run)."""
    from pdf2epub.app import epub_chapters, latest_doc
    from pdf2epub.cleanup import cleanup_after_build
    from pdf2epub.ui import attention_items, result_panel

    doc = latest_doc(settings, book_dir)
    attention = attention_items(doc) if doc else []
    pages = len(doc.pages) if doc else 0
    console.print()
    console.print(
        result_panel(res.epub, res.valid, time.perf_counter() - t0, epub_chapters(res.epub), pages, attention)
    )
    if res.valid is False:
        console.print(f"[dim]epubcheck details: {res.report}[/]")
    if VERBOSE and doc:
        stats = book_dir / "stats.json"
        if stats.exists():
            console.print(_timings_table(json.loads(stats.read_text()), pages))
    if cleanup_after_build(book_dir, res.valid is not False, settings.keep_work):
        console.print("[dim]intermediate files removed[/]")
    else:
        console.print(f"[dim]intermediate files kept: {book_dir}  (inspect with `pdf2epub dev`)[/]")
    if res.valid is False:
        raise typer.Exit(1)


def convert(
    source: Annotated[Path, typer.Argument(exists=True, metavar="PDF", help="PDF file (or a folder of page images)")],
    pages: Annotated[str | None, typer.Option(help="Only these pages, e.g. 1-5,9")] = None,
    from_stage: Annotated[str | None, typer.Option(help="Resume from this stage")] = None,
    langs: Annotated[str | None, typer.Option(help="Language hints, e.g. en,ar (scripts are auto-detected)")] = None,
    llm: Annotated[bool | None, LLM_OPT] = None,
) -> None:
    """Extract pages and generate one Vue page per PDF page (then LLM review); no EPUB yet."""
    settings = _settings(langs, llm=llm)
    _header(source.name, _source_details(source, settings))
    t = time.perf_counter()
    with _reporting() as rep:
        doc, ctx = _convert_steps(settings, source, _parse_pages(pages), from_stage, rep)
    console.print()
    console.print(
        f"[green]✓[/] Converted {len(doc.pages)} pages in {time.perf_counter() - t:.0f}s. Next:", highlight=False
    )
    console.print("  [bold]pdf2epub dev[/]     [dim]check pages side by side with the original[/]")
    console.print("  [bold]pdf2epub build[/]   [dim]make the EPUB[/]")
    if VERBOSE:
        console.print(_timings_table(ctx.stats, len(doc.pages)))


def review(
    book: Annotated[str | None, typer.Option(help="Book id or source path (default: latest)")] = None,
    pages: Annotated[str | None, typer.Option(help="Only these pages, e.g. 1-5")] = None,
    policy: Annotated[str | None, typer.Option(help="flagged | all")] = None,
) -> None:
    """LLM review: compares each page with its source image and applies validated fixes."""
    from pdf2epub import app as core

    settings = _settings()
    if policy:
        settings.llm.review_policy = policy
    book_dir = core.resolve_book(settings, book)
    _header(f"review {book_dir.name}", [_llm_line(settings)])
    with _reporting() as rep:
        summary = core.review(settings, book_dir, _parse_pages(pages), rep)
    console.print()
    console.print(f"[green]✓[/] {summary['text']}", highlight=False)


def _stop_group(proc: subprocess.Popen) -> None:
    import signal

    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, PermissionError):
        pass
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)


def _announce(url: str, port: int, open_browser: bool, title: str, note: str, query: str = "") -> None:
    """Print the address (on WSL: the one Windows can reach) and open the browser."""
    from rich.panel import Panel

    from pdf2epub.browser import is_wsl, open_url, windows_reachable, wsl_ip

    lines = []
    if is_wsl():
        ip = wsl_ip()
        candidates = [url] + ([f"http://{ip}:{port}/"] if ip else [])
        with console.status("checking which address Windows can reach…"):
            reachable = windows_reachable(candidates)
        if reachable:
            url = reachable
        alt = [c for c in candidates if c != url]
        lines.append(f"[bold]{url}{query}[/]")
        if reachable is None and alt:
            lines.append(f"[dim]if that doesn't load in Windows, try {alt[0]}{query}[/]")
        elif reachable and reachable != candidates[0]:
            lines.append("[dim]WSL: localhost isn't forwarded to Windows here, so this is the WSL address[/]")
    else:
        lines.append(f"[bold]{url}{query}[/]")
    opened = open_browser and open_url(url + query)
    lines.append(
        "[dim]"
        + ("opened in your browser · " if opened else "open this link in your browser · ")
        + f"{note} · Ctrl+C to stop[/]"
    )
    console.print(
        Panel("\n".join(lines), title=f"[green]{title}[/]", title_align="left", border_style="green", expand=False)
    )


def dev(
    book: Annotated[str | None, typer.Option(help="Book id or source path (default: latest)")] = None,
    port: Annotated[int, typer.Option(help="Preferred port (the next free one is used if busy)")] = 5173,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the workbench in your browser")] = True,
) -> None:
    from pdf2epub import app as core
    from pdf2epub.browser import bind_host, free_port, wait_until_up

    settings = _settings()
    book_dir = core.resolve_book(settings, book)
    _header(f"workbench · {book_dir.name}", [])
    with console.status("preparing the workbench…"):
        web = core.ensure_node_modules(settings)
    host = bind_host()
    port = free_port(port, "0.0.0.0" if host == "0.0.0.0" else "127.0.0.1")
    url = f"http://localhost:{port}/"
    proc = subprocess.Popen(
        [
            "npm",
            "run",
            "--silent",
            "dev",
            "--",
            "--host",
            host,
            "--port",
            str(port),
            "--strictPort",
            "--clearScreen",
            "false",
            "--logLevel",
            "warn",
        ],
        cwd=web,
        env=core.web_env(settings, book_dir),
        start_new_session=True,  # own process group: stopping it also stops vite under npm
        stdin=subprocess.DEVNULL,  # vite's key shortcuts would read the terminal and get SIGTTIN-stopped
    )
    try:
        with console.status("starting the dev server…"):
            up = wait_until_up(url, alive=lambda: proc.poll() is None)
        if not up:
            raise RuntimeError("the workbench server did not start (run with -v for details)")
        _announce(url, port, open_browser, "Workbench ready", "pages update live while convert/review run")
        proc.wait()
    except KeyboardInterrupt:
        console.print("[dim]workbench stopped[/]")
    finally:
        _stop_group(proc)


def ui(
    port: Annotated[int, typer.Option(help="Preferred port (the next free one is used if busy)")] = 8765,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open it in your browser")] = True,
    host: Annotated[
        str | None, typer.Option(help="Listen address (default: this computer only; 0.0.0.0 in Docker)")
    ] = None,
) -> None:
    """Web app: first-run setup, settings, upload a PDF and convert it (same engine as the CLI)."""
    import threading

    from pdf2epub.browser import bind_host, free_port
    from pdf2epub.webui.server import ensure_ui, make_server

    settings = _settings()
    _header("web app", [])
    with console.status("preparing the web app…"):
        dist = ensure_ui(settings)
    host = host or bind_host()
    port = free_port(port, host)
    server, web = make_server(host, port, dist, settings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _announce(
            f"http://localhost:{port}/",
            port,
            open_browser,
            "pdf2epub is running",
            "the link carries a private access key",
            f"?t={web.token}",
        )
        thread.join()
    except KeyboardInterrupt:
        busy = any(j["status"] in ("running", "cancelling") for j in web.runner.list())
        console.print(
            "[dim]stopped"
            + (" · the running conversion keeps its work files and resumes next time[/]" if busy else "[/]")
        )
    finally:
        server.shutdown()
        web.runner.clear_uploads()


def build(
    book: Annotated[str | None, typer.Option(help="Book id or source path (default: latest)")] = None,
    output: Annotated[Path | None, OUTPUT_OPT] = None,
    keep_work: Annotated[bool, typer.Option(help="Keep intermediates after a successful build")] = False,
) -> None:
    """Compile the Vue pages into an EPUB (vite build), validate with epubcheck, clean up."""
    from pdf2epub import app as core

    out, out_dir = _split_output(output)
    settings = _settings(keep_work=keep_work, out_dir=out_dir)
    book_dir = core.resolve_book(settings, book)
    _header(f"build {book_dir.name}", [])
    t = time.perf_counter()
    with _reporting() as rep:
        res = core.build(settings, book_dir, out, reporter=rep)
    _finish(settings, book_dir, res, t)


def run(
    source: Annotated[Path, typer.Argument(exists=True, metavar="PDF", help="PDF file (or a folder of page images)")],
    langs: Annotated[str | None, typer.Option(help="Language hints, e.g. en,ar")] = None,
    output: Annotated[Path | None, OUTPUT_OPT] = None,
    llm: Annotated[bool | None, LLM_OPT] = None,
    keep_work: Annotated[bool, typer.Option(help="Keep intermediates after a successful build")] = False,
) -> None:
    """One shot: convert → (LLM review) → build EPUB → validate → clean up."""
    from pdf2epub import app as core

    out, out_dir = _split_output(output)
    settings = _settings(langs, keep_work=keep_work, llm=llm, out_dir=out_dir)
    _header(source.name, _source_details(source, settings))
    t = time.perf_counter()
    with _reporting() as rep:
        _, ctx = _convert_steps(settings, source, None, None, rep)
        res = core.build(settings, ctx.book_dir, out, reporter=rep)
    _finish(settings, ctx.book_dir, res, t)


def clean(
    book: Annotated[str | None, typer.Argument(help="Book id (omit with --all)")] = None,
    all_: Annotated[bool, typer.Option("--all")] = False,
) -> None:
    """Delete intermediates of one book or all books."""
    import shutil

    from pdf2epub.cleanup import dir_size, human

    settings = _settings()
    targets = list(settings.work_dir.glob("*")) if all_ else ([settings.book_dir(book)] if book else [])
    if not targets:
        raise typer.BadParameter("give a book id or --all")
    for t in targets:
        if t.exists():
            size = dir_size(t)
            shutil.rmtree(t)
            console.print(f"removed {t} ({human(size)})")


def uninstall(yes: Annotated[bool, typer.Option("--yes", "-y")] = False) -> None:
    """Remove downloaded models/browser/epubcheck/node deps, work files, config and the stored API key."""
    from pdf2epub.cleanup import dir_size, human, remove, uninstall_targets
    from pdf2epub.config import keyring_delete
    from pdf2epub.paths import locations

    targets = uninstall_targets()
    for t in targets:
        console.print(f"  {t}  {human(dir_size(t))}")
    if not targets:
        console.print("nothing to remove")
        return
    if yes or typer.confirm("Delete all of the above?"):
        keyring_delete(_settings().llm.base_url)
        remove(targets)
        if locations().checkout:
            console.print("[green]removed[/]. Reinstall with scripts/setup.sh")
        else:
            console.print("[green]removed[/]. Remove the program with: uv tool uninstall pdf2epub  (or pipx uninstall)")


def schema(out: Path | None = None) -> None:
    """Print the IR JSON schema (used to generate TypeScript types for the workbench)."""
    from pdf2epub.ir import Document

    text = json.dumps(Document.model_json_schema(), indent=1)
    if out:
        out.write_text(text)
    else:
        print(text)


def eval_(
    llm: Annotated[bool, typer.Option(help="Include the LLM review (slower, costs tokens)")] = False,
    doc: Annotated[str | None, typer.Option(help="Only this corpus entry")] = None,
) -> None:
    """Run the evaluation corpus (eval/corpus/*.json) and report regressions vs the last run."""
    from pdf2epub.evaluate import regressions, run_eval

    results, prev = run_eval(_settings(), llm, doc)
    t = Table(title="eval")
    for col in ("doc", "pages", "epubcheck", "text_recall", "leaked", "spreads P/R", "rtl", "s/page"):
        t.add_column(col)
    for r in results:
        if "skipped" in r:
            t.add_row(r["doc"], r["skipped"], "", "", "", "", "", "")
            continue
        t.add_row(
            r["doc"],
            str(r["pages"]),
            str(r["epubcheck"]),
            str(r["text_recall"]),
            ", ".join(r["leaked"]) or "-",
            f"{r['spread_precision']}/{r['spread_recall']}",
            f"{r['rtl_blocks']} ({'ok' if r['rtl_ok'] else 'low'})",
            str(r["seconds_per_page"]),
        )
    console.print(t)
    for r in results:
        for m in r.get("missing_text", []):
            console.print(f"  [yellow]{r['doc']} missing:[/] {m}")
    regs = regressions(results, prev)
    if regs:
        console.print("[red]regressions vs previous run:[/]\n  " + "\n  ".join(regs))
        raise typer.Exit(1)
    console.print("[green]no regressions[/]" if prev else "baseline recorded")


def init(
    provider: Annotated[
        str | None, typer.Option(help="llamacpp|lmstudio|ollama|openai|gemini|openrouter|custom|none")
    ] = None,
    base_url: Annotated[str | None, typer.Option(help="Endpoint URL (default per provider)")] = None,
    model: Annotated[str | None, typer.Option(help="Model id (vision-capable)")] = None,
    api_key_env: Annotated[
        str | None, typer.Option(help="Read the API key from this env var (non-interactive)")
    ] = None,
    keyring: Annotated[bool, typer.Option(help="Store the API key in the OS keyring")] = True,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Non-interactive: accept defaults")] = False,
    check: Annotated[bool, typer.Option(help="Test the endpoint afterwards")] = True,
) -> None:
    """First-run setup: choose the LLM endpoint, model, API key and defaults; writes the user config."""
    import os

    from pdf2epub.setup_wizard import run_wizard

    key = os.environ.get(api_key_env) if api_key_env else None
    data = run_wizard(console, provider, base_url, model, key, keyring, yes)
    if check and data.get("llm", {}).get("enabled", True):
        llm_check()


config_app = typer.Typer(help="Show or change settings (flags and env vars still win).")


@config_app.command("path")
def config_path_cmd() -> None:
    """Print the config file location."""
    from pdf2epub.config_store import config_path

    print(config_path())


@config_app.command("show")
def config_show(all_: Annotated[bool, typer.Option("--all", help="Effective value of every setting")] = False) -> None:
    """Config file contents (secrets hidden), or with --all the effective settings from every source."""
    import tomli_w

    from pdf2epub import config_store
    from pdf2epub.paths import locations

    loc = locations()
    console.print(
        f"[dim]layout: {'git checkout' if loc.checkout else 'installed'} · config: {config_store.config_path()}[/]"
    )
    if not all_:
        data = config_store.redacted(config_store.read())
        print(tomli_w.dumps(data) if data else "(empty — run `pdf2epub init`)")
        return
    s = _settings()
    eff = s.model_dump(mode="json", exclude={"llm"})
    llm = s.llm.model_dump(mode="json")
    llm["api_key"] = "set" if s.llm.api_key else "none"
    eff["llm"] = llm
    print(tomli_w.dumps({k: v for k, v in eff.items() if v is not None}))


@config_app.command("set")
def config_set(key: str, value: str) -> None:
    """Set a value, e.g. `pdf2epub config set llm.model <model-id>` or `config set langs en,ar`."""
    from pdf2epub import config_store
    from pdf2epub.config import keyring_set

    if key == "llm.api_key":
        base = _settings().llm.base_url
        if keyring_set(base, value):
            console.print(f"API key for {base} stored in the OS keyring.")
            return
    try:
        path = config_store.set_value(key, value)
    except (KeyError, ValueError) as e:
        raise typer.BadParameter(str(e)) from e
    console.print(f"{key} updated in {path}")


@config_app.command("unset")
def config_unset(key: str) -> None:
    """Remove a value (falls back to the default)."""
    from pdf2epub import config_store
    from pdf2epub.config import keyring_delete

    if key == "llm.api_key":
        keyring_delete(_settings().llm.base_url)
    console.print(f"{key} removed from {config_store.unset_value(key)}")


def llm_check() -> None:
    """Verify the configured OpenAI-compatible endpoint (key, model, vision, JSON)."""
    import asyncio

    from pdf2epub.llm.probe import probe

    settings = _settings()
    _header("llm-check", [_llm_line(settings), "api key " + ("set" if settings.llm.api_key else "none")])
    with console.status("asking the model to read a test image…"):
        result = asyncio.run(probe(settings.llm))
    console.print(result)
    if result.startswith("[red]"):
        raise typer.Exit(1)


# Registration order = order in `pdf2epub --help` (panels: Convert, Setup, Maintenance, Development).
app.command(rich_help_panel="Convert", help="Convert a PDF to EPUB in one go: convert → review → build.")(run)
app.command(rich_help_panel="Convert", help="Open the web app: setup, settings, upload and convert in the browser.")(ui)
app.command(rich_help_panel="Convert", help="Prepare a PDF for checking: pages, text, pictures (no EPUB yet).")(convert)
app.command(rich_help_panel="Convert", help="Open the browser workbench: original, phone, tablet and two-page views.")(
    dev
)
app.command(rich_help_panel="Convert", help="Let the LLM compare each page with the scan and fix mistakes.")(review)
app.command(rich_help_panel="Convert", help="Make the EPUB from a converted book and validate it.")(build)
app.command(rich_help_panel="Setup", help="First-run setup: LLM provider, model, API key and defaults.")(init)
app.command(rich_help_panel="Setup", help="Test the LLM connection, image reading and structured output.")(llm_check)
app.add_typer(config_app, name="config", rich_help_panel="Setup")
app.command(rich_help_panel="Maintenance", help="Delete intermediate files of one book (or --all).")(clean)
app.command(rich_help_panel="Maintenance", help="Remove downloaded models, caches, settings and the stored API key.")(
    uninstall
)
app.command(
    name="eval", rich_help_panel="Development", help="Measure quality on the evaluation corpus; flags regressions."
)(eval_)
app.command(rich_help_panel="Development", help="Print the IR JSON schema (TypeScript types for the workbench).")(
    schema
)


# ---- entry point: friendly errors instead of tracebacks -------------------------------------


def entry() -> None:
    from pdf2epub.ui import error_panel

    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/] Nothing was written to the output; intermediate files are kept.")
        raise SystemExit(130) from None
    except Exception as exc:  # noqa: BLE001 - top-level presentation
        if VERBOSE:
            console.print_exception(show_locals=False)
        from pdf2epub.app import explain_error

        msg, hint = explain_error(exc)
        if VERBOSE and hint.startswith("Run with -v"):
            hint = ""
        console.print(error_panel(msg, hint))
        raise SystemExit(1) from None


if __name__ == "__main__":
    entry()
