"""Removal of intermediates after a successful build, and full uninstall of heavy local deps."""

from __future__ import annotations

import shutil
from pathlib import Path


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def cleanup_after_build(book_dir: Path, success: bool, keep_work: bool) -> bool:
    """Delete the book work dir only when the build succeeded and the user didn't ask to keep it.

    On failure the work dir stays so the next run resumes from cached stages.
    Returns True when the directory was removed.
    """
    if not success or keep_work or not book_dir.exists():
        return False
    shutil.rmtree(book_dir)
    return True


def uninstall_targets() -> list[Path]:
    """Everything pdf2epub downloaded or created.

    checkout: inside the repo (.venv, .cache, node_modules, work/, local config).
    installed: the user cache dir (models, browser, epubcheck, node deps, work/) and config dir.
    The program itself is removed by the installer (`uv tool uninstall pdf2epub` / `pipx uninstall`).
    """
    from pdf2epub.paths import locations

    loc = locations()
    if loc.checkout:
        root = loc.config_dir
        candidates = (root / ".venv", loc.cache_dir, root / "web" / "node_modules", loc.work_dir, loc.config_file)
    else:
        candidates = (loc.cache_dir, loc.config_dir)
    out = [p for p in candidates if p.exists()]
    # Paddle creates ~/.cache/paddle/dataset (empty) on import, hard-coded to $HOME; remove it only if
    # it holds no files (never touch data another Paddle user put there).
    stray = Path.home() / ".cache" / "paddle"
    if stray.is_dir() and not any(f.is_file() for f in stray.rglob("*")):
        out.append(stray)
    return out


def remove(paths: list[Path]) -> None:
    for p in paths:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
