"""First-use provisioning of runtime pieces into the cache dir (installed layout friendly):
Node deps for the Vite build, epubcheck, the headless browser for review renders."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from pdf2epub.config import Settings
from pdf2epub.paths import locations

EPUBCHECK_VERSION = "5.4.0"
NODE_MIN = (22, 12)


class RuntimeMissing(RuntimeError):
    pass


def node_version() -> tuple[int, int] | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, check=True).stdout.strip()
        major, minor, *_ = out.lstrip("v").split(".")
        return int(major), int(minor)
    except Exception:
        return None


def require_node() -> None:
    v = node_version()
    if v is None or v < NODE_MIN:
        have = f"found {v[0]}.{v[1]}" if v else "not found"
        raise RuntimeMissing(
            f"Node.js >= {NODE_MIN[0]}.{NODE_MIN[1]} is needed to build EPUBs ({have}). "
            "Install it from https://nodejs.org (or use the Docker image)."
        )


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(root.rglob("*")):
        if f.is_file() and "node_modules" not in f.parts:
            h.update(str(f.relative_to(root)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:16]


def ensure_web(settings: Settings) -> Path:
    """Vite project ready to run: the repo's web/ (checkout) or a cached copy of the packaged sources."""
    require_node()
    loc = locations()
    web = settings.web_dir
    if not loc.checkout:
        src = loc.web_src
        if not (src / "package.json").exists():
            raise RuntimeMissing(f"web sources missing from the installation ({src}); reinstall pdf2epub")
        stamp = web / ".pdf2epub-src"
        want = _tree_hash(src)
        if not stamp.exists() or stamp.read_text() != want:
            if web.exists():
                for item in web.iterdir():  # keep node_modules (slow to reinstall)
                    if item.name != "node_modules":
                        shutil.rmtree(item) if item.is_dir() else item.unlink()
            shutil.copytree(src, web, dirs_exist_ok=True)
            (web / "node_modules" / ".package-lock.json").unlink(missing_ok=True)  # force npm ci check
            stamp.write_text(want)
    if not (web / "node_modules" / ".package-lock.json").exists():
        # quiet unless it fails (npm prints update notices and progress otherwise)
        proc = subprocess.run(
            ["npm", "ci", "--no-audit", "--no-fund", "--loglevel=error"], cwd=web, capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise RuntimeMissing(
                "installing the EPUB build tools (npm ci) failed — is the internet reachable?\n"
                + (proc.stdout + proc.stderr).strip()[-1500:]
            )
    return web


def epubcheck_jar(settings: Settings) -> Path | None:
    jars = sorted((settings.cache_dir / "epubcheck").glob("epubcheck-*/epubcheck.jar"))
    return jars[-1] if jars else None


def ensure_epubcheck(settings: Settings) -> Path | None:
    """Download epubcheck once (needs Java to run; without Java EPUBs are built but not validated)."""
    if (jar := epubcheck_jar(settings)) is not None:
        return jar
    if not shutil.which("java"):
        return None
    dest = settings.cache_dir / "epubcheck"
    dest.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/w3c/epubcheck/releases/download/v{EPUBCHECK_VERSION}/epubcheck-{EPUBCHECK_VERSION}.zip"
    tmp = dest / "epubcheck.zip"
    try:
        urllib.request.urlretrieve(url, tmp)
        with zipfile.ZipFile(tmp) as z:
            z.extractall(dest)
    except Exception:
        return None
    finally:
        tmp.unlink(missing_ok=True)
    return epubcheck_jar(settings)


def ensure_browser() -> None:
    """Headless Chromium for reader-emulated renders (downloaded once into the cache)."""
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium-headless-shell"],
        check=True,
        capture_output=True,
    )
