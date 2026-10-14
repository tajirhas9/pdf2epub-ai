"""Local HTTP server for `pdf2epub ui`: JSON API + the built Vue app (no extra Python dependencies).

Access needs a per-run token (in the opened URL, then a SameSite=Strict cookie), because the server
can change settings and, on WSL, listens on all interfaces so Windows can reach it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import secrets
import shutil
import subprocess
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from pdf2epub.config import Settings
from pdf2epub.webui import settings_api
from pdf2epub.webui.jobs import JobRunner

log = logging.getLogger(__name__)
COOKIE = "pdf2epub_ui"
MAX_UPLOAD = 2 << 30  # 2 GB


# ---- the Vue app (web/src/ui) ------------------------------------------------------------------


def _ui_sources(web: Path) -> list[Path]:
    files = [web / "ui.html", web / "vite.ui.config.ts", web / "package-lock.json"]
    return files + sorted(p for p in (web / "src" / "ui").rglob("*") if p.is_file())


PACKAGED_UI = Path(__file__).parent / "dist"  # built into the wheel by hatch_build.py


def ensure_ui(settings: Settings) -> Path:
    """The built web app: the copy shipped in the package (no Node needed), or built from the sources
    (git checkout, or a wheel built without Node), rebuilt when they change."""
    from pdf2epub.app import ensure_node_modules
    from pdf2epub.paths import locations

    if not locations().checkout and (PACKAGED_UI / "ui.html").exists():
        return PACKAGED_UI
    web = ensure_node_modules(settings)
    h = hashlib.sha256()
    for f in _ui_sources(web):
        if f.exists():
            h.update(str(f.relative_to(web)).encode())
            h.update(f.read_bytes())
    want = h.hexdigest()[:16]
    dist = settings.cache_dir / "ui"
    stamp = dist / ".pdf2epub-src"
    if stamp.exists() and stamp.read_text() == want and (dist / "ui.html").exists():
        return dist
    shutil.rmtree(dist, ignore_errors=True)
    proc = subprocess.run(
        ["npm", "run", "--silent", "build:ui", "--", "--outDir", str(dist), "--emptyOutDir", "--logLevel", "error"],
        cwd=web,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not (dist / "ui.html").exists():
        from pdf2epub.app import BuildError

        raise BuildError("building the web UI (vite) failed", (proc.stdout + proc.stderr)[-4000:])
    stamp.write_text(want)
    return dist


# ---- HTTP ---------------------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class App:
    def __init__(self, dist: Path, settings: Settings):
        self.dist = dist.resolve()
        self.token = secrets.token_urlsafe(18)
        self.runner = JobRunner(settings.work_dir / "_uploads")


class Handler(BaseHTTPRequestHandler):
    server_version = "pdf2epub"
    app: App  # set on the subclass made by make_server

    def log_message(self, fmt, *args):  # quiet: the terminal shows the URL, not access logs
        log.debug(fmt, *args)

    # ---- plumbing ----

    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data).encode(), "application/json", {"Cache-Control": "no-store"})

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1 << 20:
            raise ApiError(413, "request too large")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            raise ApiError(400, "invalid JSON") from None

    def _authorized(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        got = cookie[COOKIE].value if COOKIE in cookie else ""
        return secrets.compare_digest(got, self.app.token)

    # ---- routing ----

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        try:
            if url.path.startswith("/api/"):
                if not self._authorized():
                    raise ApiError(401, "Open the link printed by `pdf2epub ui` (it carries the access key).")
                self._api(method, url.path[5:].strip("/"), parse_qs(url.query))
            elif method in ("GET", "HEAD"):
                self._static(url.path, parse_qs(url.query))
            else:
                raise ApiError(405, "method not allowed")
        except ApiError as e:
            self._json({"error": str(e)}, e.status)
        except (KeyError, ValueError) as e:
            self._json({"error": str(e.args[0]) if e.args else type(e).__name__}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            log.exception("request failed")
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _static(self, path: str, query: dict) -> None:
        token = (query.get("t") or [""])[0]
        if token:  # first visit: trade the token in the URL for a cookie, then drop it from the address bar
            if not secrets.compare_digest(token, self.app.token):
                raise ApiError(403, "This link is from an earlier run. Use the link printed by `pdf2epub ui`.")
            cookie = f"{COOKIE}={self.app.token}; Path=/; HttpOnly; SameSite=Strict"
            self._send(302, b"", "text/plain", {"Location": path or "/", "Set-Cookie": cookie})
            return
        rel = path.lstrip("/") or "ui.html"
        file = (self.app.dist / rel).resolve()
        if not file.is_relative_to(self.app.dist) or not file.is_file():
            file = self.app.dist / "ui.html"  # SPA fallback
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        cache = "public, max-age=31536000, immutable" if "/assets/" in str(file) else "no-cache"
        self._send(200, file.read_bytes(), ctype, {"Cache-Control": cache})

    def _api(self, method: str, route: str, query: dict) -> None:
        runner = self.app.runner
        parts = route.split("/")
        match (method, parts):
            case ("GET", ["state"]):
                self._json(settings_api.state())
            case ("GET", ["system"]):
                self._json(settings_api.system())
            case ("PUT", ["config"]):
                self._json(settings_api.save(self._body()))
            case ("POST", ["models"]):
                self._json({"models": settings_api.list_models(self._body())})
            case ("POST", ["llm-check"]):
                self._json(settings_api.check_llm(self._body()))
            case ("POST", ["uploads"]):
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > MAX_UPLOAD:
                    raise ApiError(413, "file is empty or larger than 2 GB")
                name = (query.get("name") or ["book.pdf"])[0]
                self._json(runner.add_upload(name, self.rfile, n).public())
            case ("GET", ["uploads", uid]):
                up = runner.uploads.get(uid)
                if up is None:
                    raise ApiError(404, "upload not found")
                self._json(up.public())
            case ("DELETE", ["uploads", uid]):
                runner.remove_upload(uid)
                self._json({"ok": True})
            case ("GET", ["jobs"]):
                self._json(runner.list())
            case ("POST", ["jobs"]):
                body = self._body()
                self._json(runner.submit(body.get("upload_id", ""), body.get("options") or {}).public())
            case ("POST", ["jobs", jid, "cancel"]):
                runner.cancel(jid)
                self._json({"ok": True})
            case ("DELETE", ["jobs", jid]):
                runner.remove(jid)
                self._json({"ok": True})
            case ("GET", ["jobs", jid, "epub"]):
                self._download(jid)
            case _:
                raise ApiError(404, "not found")

    def _download(self, jid: str) -> None:
        job = self.app.runner.jobs.get(jid)
        path = Path(job.result["path"]) if job and job.result else None
        if path is None or not path.exists():
            raise ApiError(404, "the EPUB is no longer there (moved or deleted)")
        ascii_name = re.sub(r"[^\w.\- ]", "_", path.name)
        disp = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(path.name)}"
        self._send(200, path.read_bytes(), "application/epub+zip", {"Content-Disposition": disp})


def make_server(host: str, port: int, dist: Path, settings: Settings) -> tuple[ThreadingHTTPServer, App]:
    app = App(dist, settings)
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server, app
