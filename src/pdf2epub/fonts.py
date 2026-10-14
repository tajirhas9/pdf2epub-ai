"""Embed fonts for scripts e-readers often lack (OFL-licensed, cached in .cache/fonts)."""

from __future__ import annotations

import logging
import shutil
import urllib.request
from pathlib import Path

from pdf2epub.ir import Document, Role

log = logging.getLogger(__name__)

# script -> (file name, URL). Google Fonts repo, SIL Open Font License.
FONTS = {
    "arabic": ("Amiri-Regular.ttf", "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/Amiri-Regular.ttf"),
}
LICENSE_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/OFL.txt"


def scripts_used(doc: Document) -> set[str]:
    from pdf2epub.textutil import dominant_script

    found = set()
    for p in doc.pages:
        for b in p.blocks:
            if b.role != Role.CONTENT:
                continue
            for s in b.spans:
                if s.dir == "rtl" and (sc := dominant_script(s.text)):
                    found.add(sc)
    return found


def provision_fonts(doc: Document, book_dir: Path, cache_dir: Path) -> list[str]:
    """Copy needed fonts into the book (downloaded once into the project cache). Best effort."""
    out = []
    for script in sorted(scripts_used(doc)):
        if script not in FONTS:
            continue
        name, url = FONTS[script]
        cached = cache_dir / "fonts" / name
        try:
            if not cached.exists():
                cached.parent.mkdir(parents=True, exist_ok=True)
                urllib.request.urlretrieve(url, cached)
                urllib.request.urlretrieve(LICENSE_URL, cached.parent / f"{cached.stem}-OFL.txt")
        except Exception as e:  # offline: readers fall back to their own fonts
            log.warning("could not download %s: %s", name, e)
            continue
        dest = book_dir / "fonts" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cached, dest)
        out.append(f"fonts/{name}")
    return out
