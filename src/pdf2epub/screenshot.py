"""Reader-emulated renders of each page (from the built XHTML + EPUB CSS) for the LLM review.

One headless Chromium, one context per device, pages rendered in parallel.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pdf2epub.config import Settings


@dataclass(frozen=True)
class Device:
    name: str
    width: int
    height: int
    font_px: int
    columns: int = 1


PHONE = Device("phone", 390, 844, 17)
SPREAD = Device("spread", 1180, 820, 18, columns=2)

# Paginated like an e-reader: fixed viewport height, content flows into columns.
_READER_CSS = """
html {{ height: 100vh; overflow: hidden; background: #fff; }}
body {{ height: 100vh; box-sizing: border-box; padding: 1.2em; margin: 0;
       column-count: {columns}; column-gap: 2.4em; column-fill: auto; font-size: {font}px; }}
"""


def build_previews(settings: Settings, book_dir: Path) -> Path:
    """Render every generated page to standalone XHTML (vite build --mode pages, ~1-2 s)."""
    from pdf2epub.app import ensure_node_modules, web_env

    web = ensure_node_modules(settings)
    subprocess.run(
        ["npm", "run", "--silent", "build:pages", "--", "--logLevel", "error"],
        cwd=web,
        env=web_env(settings, book_dir),
        check=True,
        capture_output=True,
    )
    return book_dir / "build" / "pages"


class Renderer:
    def __init__(self, out_dir: Path, devices: tuple[Device, ...] = (PHONE,)):
        self.out_dir = out_dir
        self.devices = devices
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> Renderer:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def render(self, xhtml: Path, device: Device = PHONE) -> Path:
        """First reader 'screen' of the page as an e-reader would show it."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        out = self.out_dir / f"{xhtml.stem}-{device.name}.png"
        ctx = await self._browser.new_context(viewport={"width": device.width, "height": device.height})
        try:
            page = await ctx.new_page()
            await page.goto(xhtml.resolve().as_uri())
            await page.add_style_tag(content=_READER_CSS.format(columns=device.columns, font=device.font_px))
            await page.wait_for_load_state("load")
            await page.screenshot(path=str(out))
        finally:
            await ctx.close()
        return out
