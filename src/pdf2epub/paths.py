"""Where pdf2epub keeps things.

Two layouts:
- **checkout** (running from a git clone: pyproject.toml + web/ next to the package): everything
  stays inside the repo (.venv, .cache, work/, out/, .env/.env.local) — one folder to delete.
- **installed** (uv tool / pipx / Docker): per-user OS locations
    config  ~/.config/pdf2epub/config.toml        (macOS: ~/Library/Application Support/pdf2epub,
                                                   Windows: %APPDATA%\\pdf2epub)
    cache   ~/.cache/pdf2epub/                     models, browser, epubcheck, fonts, web deps, work/
    output  current directory
  `pdf2epub uninstall` removes cache + config (+ the stored API key).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

APP = "pdf2epub"
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[1]


def is_checkout() -> bool:
    if os.environ.get("PDF2EPUB_INSTALLED_LAYOUT"):  # force tool layout (tests, Docker)
        return False
    return (REPO_ROOT / "pyproject.toml").exists() and (REPO_ROOT / "web" / "package.json").exists()


@dataclass(frozen=True)
class Locations:
    checkout: bool
    config_dir: Path  # user config.toml lives here
    cache_dir: Path  # models, browsers, epubcheck, fonts, node deps
    work_dir: Path  # per-book intermediates
    out_dir: Path  # default EPUB output
    web_src: Path  # Vue/Vite sources shipped with the tool
    dotenv_files: tuple[Path, ...]  # .env files (checkout only)

    @property
    def config_file(self) -> Path:
        # checkout: gitignored file in the repo; installed: the user config file
        return self.config_dir / (".pdf2epub.toml" if self.checkout else "config.toml")

    @property
    def web_dir(self) -> Path:
        """Where `npm` runs: the repo's web/ in a checkout, a writable copy in the cache otherwise."""
        return self.web_src if self.checkout else self.cache_dir / "web"


@lru_cache(maxsize=1)
def locations() -> Locations:
    if is_checkout():
        return Locations(
            checkout=True,
            config_dir=REPO_ROOT,
            cache_dir=REPO_ROOT / ".cache",
            work_dir=REPO_ROOT / "work",
            out_dir=REPO_ROOT / "out",
            web_src=REPO_ROOT / "web",
            dotenv_files=(REPO_ROOT / ".env", REPO_ROOT / ".env.local"),
        )
    import platformdirs

    cache = Path(platformdirs.user_cache_dir(APP, appauthor=False))
    return Locations(
        checkout=False,
        config_dir=Path(platformdirs.user_config_dir(APP, appauthor=False)),
        cache_dir=cache,
        work_dir=cache / "work",
        out_dir=Path.cwd(),
        web_src=PACKAGE_DIR / "_web",
        dotenv_files=(),
    )


def project_config_file() -> Path:
    """Optional per-folder overrides: ./pdf2epub.toml in the current directory."""
    return Path.cwd() / "pdf2epub.toml"
