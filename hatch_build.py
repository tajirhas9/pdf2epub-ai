"""Wheel build hook: ship the web app (`pdf2epub ui`) already built, so it starts without Node.js.

Node is still needed at run time to build EPUBs, but the setup wizard can now tell the user so.
Without Node at build time the wheel is built anyway (warning) and `pdf2epub ui` builds the page on
first start instead. Set PDF2EPUB_REQUIRE_UI_BUILD=1 to make that an error (releases, Docker).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class WebUiBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            return
        web = Path(self.root) / "web"
        out = web / "dist-ui"
        try:
            npm = shutil.which("npm")
            if not npm or not (web / "package.json").exists():
                raise RuntimeError("npm not found")
            if not (web / "node_modules" / ".package-lock.json").exists():
                self._run([npm, "ci", "--no-audit", "--no-fund", "--loglevel=error"], web)
            self._run([npm, "run", "--silent", "build:ui", "--", "--outDir", str(out), "--emptyOutDir"], web)
        except Exception as e:  # noqa: BLE001
            if os.environ.get("PDF2EPUB_REQUIRE_UI_BUILD"):
                raise
            print(f"warning: web app not prebuilt ({e}); `pdf2epub ui` will build it on first start", file=sys.stderr)
            return
        build_data["force_include"][str(out)] = "pdf2epub/webui/dist"

    @staticmethod
    def _run(cmd: list[str], cwd: Path) -> None:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:3])} failed: {(proc.stdout + proc.stderr)[-2000:]}")
