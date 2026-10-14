"""`pdf2epub ui` backend: settings API, job options, access control, uploads (no models loaded)."""

import json
import threading
import tomllib
import urllib.error
import urllib.request

import pymupdf
import pytest

from pdf2epub import config_store
from pdf2epub.config import Settings
from pdf2epub.webui import settings_api
from pdf2epub.webui.jobs import Cancelled, Job, JobReporter, Upload, job_settings, output_path, safe_name


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config_store, "config_path", lambda: path)
    monkeypatch.setenv("PDF2EPUB_NO_KEYRING", "1")
    return path


def test_save_writes_typed_values_and_removes_empty_ones(cfg):
    settings_api.save({"set": {"langs": ["en", "ar"], "llm.concurrency": "3", "llm.model": "m1", "use_gpu": False}})
    data = tomllib.loads(cfg.read_text())
    assert data["langs"] == ["en", "ar"]
    assert data["llm"] == {"concurrency": 3, "model": "m1"}
    assert data["use_gpu"] is False

    settings_api.save({"set": {"llm.model": "", "langs": []}, "unset": ["use_gpu"]})
    data = tomllib.loads(cfg.read_text())
    assert "langs" not in data and "use_gpu" not in data
    assert data["llm"] == {"concurrency": 3}


def test_save_rejects_bad_values_and_keys(cfg):
    with pytest.raises(ValueError, match="llm.concurrency"):
        settings_api.save({"set": {"llm.concurrency": "many"}})
    with pytest.raises(KeyError):
        settings_api.save({"set": {"work_dir": "/tmp"}})  # not editable from the browser
    assert not cfg.exists()


def test_api_key_without_keyring_goes_to_private_file(cfg):
    settings_api.save({"set": {"llm.base_url": "https://api.example/v1"}, "api_key": "sk-123"})
    assert tomllib.loads(cfg.read_text())["llm"]["api_key"] == "sk-123"
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"
    settings_api.save({"clear_api_key": True})
    assert "api_key" not in tomllib.loads(cfg.read_text())["llm"]


def test_env_overrides_are_reported(monkeypatch):
    monkeypatch.delenv("LLM_TIMEOUT_S", raising=False)
    monkeypatch.setenv("LLM_MODEL", "from-env")
    monkeypatch.setenv("PDF2EPUB_OUT_DIR", "/x")
    found = settings_api.env_overrides()
    assert "llm.model" in found and "out_dir" in found and "llm.timeout_s" not in found


def test_job_options_override_settings(tmp_path):
    s = job_settings({"langs": ["ar"], "llm": False, "review_policy": "all", "use_gpu": False, "keep_work": True})
    assert s.langs == ["ar"] and s.llm.enabled is False and s.llm.review_policy == "all"
    assert s.use_gpu is False and s.keep_work is True
    s = Settings(out_dir=tmp_path)
    up = Upload("u", tmp_path / "a.pdf", "a.pdf", "a", 1)
    assert output_path(s, {"output_name": "My/Book"}, up) == tmp_path / "MyBook.epub"
    assert output_path(s, {"output_name": ""}, up) is None
    assert safe_name("../../etc/passwd") == "passwd.pdf"


def test_reporter_records_steps_and_cancels():
    job = Job("j", Upload("u", None, "a.pdf", "a", 2), {})
    rep = JobReporter(job)
    rep.start("text", 2)
    rep.advance("text", 1, 2, "half")
    assert job.steps[0]["done"] == 1 and job.steps[0]["note"] == "half"
    rep.done("text", "ok")
    assert job.steps[0]["status"] == "done" and job.steps[0]["done"] == 2
    job.cancel = True
    with pytest.raises(Cancelled):
        rep.advance("text", 2, 2)
    assert job.public()["status"] == "cancelling"


@pytest.fixture
def server(tmp_path, monkeypatch):
    from pdf2epub.webui.jobs import JobRunner
    from pdf2epub.webui.server import make_server

    monkeypatch.setattr(JobRunner, "warm_up", lambda self: None)  # don't load OCR models in tests
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "ui.html").write_text("<!doctype html><title>ui</title>")
    srv, app = make_server("127.0.0.1", 0, dist, Settings(work_dir=tmp_path / "work"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", app
    srv.shutdown()


def _req(url, method="GET", data=None, cookie=None, ctype="application/json"):
    req = urllib.request.Request(url, data=data, method=method)
    if cookie:
        req.add_header("Cookie", cookie)
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def test_api_needs_the_access_key(server):
    base, app = server
    assert _req(f"{base}/api/jobs")[0] == 401
    assert _req(f"{base}/api/jobs", cookie="pdf2epub_ui=wrong")[0] == 401
    assert _req(f"{base}/?t=wrong")[0] == 403
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(f"{base}/?t={app.token}", timeout=10)
        raise AssertionError("expected a redirect")
    except urllib.error.HTTPError as e:
        assert e.code == 302
        cookie = e.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    status, body, _ = _req(f"{base}/api/jobs", cookie=cookie.split(";")[0])
    assert status == 200 and json.loads(body) == []
    # the app shell itself is public (no secrets in it); unknown paths fall back to it
    assert _req(f"{base}/settings")[0] == 200
    assert _req(f"{base}/../../etc/passwd")[1].startswith(b"<!doctype html>")


def test_upload_validates_pdf_and_lists_jobs(server, tmp_path):
    base, app = server
    cookie = f"pdf2epub_ui={app.token}"
    status, body, _ = _req(f"{base}/api/uploads?name=x.pdf", "POST", b"hello", cookie, "application/pdf")
    assert status == 400 and "not a PDF" in json.loads(body)["error"]

    pdf = tmp_path / "Book One.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    doc.save(pdf)
    status, body, _ = _req(
        f"{base}/api/uploads?name=Book%20One.pdf", "POST", pdf.read_bytes(), cookie, "application/pdf"
    )
    up = json.loads(body)
    assert status == 200 and up["pages"] == 2 and up["name"] == "Book One.pdf"

    status, body, _ = _req(f"{base}/api/jobs", "POST", json.dumps({"upload_id": "nope"}).encode(), cookie)
    assert status == 400

    status, body, _ = _req(f"{base}/api/uploads/{up['id']}", cookie=cookie)  # restored after a page refresh
    assert status == 200 and json.loads(body) == up

    _req(f"{base}/api/uploads/{up['id']}", "DELETE", cookie=cookie)
    assert up["id"] not in app.runner.uploads
    assert _req(f"{base}/api/uploads/{up['id']}", cookie=cookie)[0] == 404
    assert _req(f"{base}/api/jobs/none/epub", cookie=cookie)[0] == 404


def test_installed_layout_serves_packaged_ui_without_node(tmp_path, monkeypatch):
    from pdf2epub.webui import server

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "ui.html").write_text("<!doctype html>")
    monkeypatch.setattr(server, "PACKAGED_UI", dist)
    monkeypatch.setattr("pdf2epub.paths.locations", lambda: type("L", (), {"checkout": False})())
    monkeypatch.setattr("shutil.which", lambda name: None)  # no node/npm on PATH
    assert server.ensure_ui(Settings(cache_dir=tmp_path / "cache")) == dist


def test_waiting_jobs_say_what_they_wait_for(tmp_path, monkeypatch):
    from pdf2epub.webui.jobs import JobRunner

    monkeypatch.setattr(JobRunner, "warm_up", lambda self: None)
    runner = JobRunner(tmp_path / "uploads")
    up = Upload("u", tmp_path / "a.pdf", "a.pdf", "a", 1)
    runner.uploads["u"] = up
    job = Job("j", up, {})
    runner.jobs["j"] = job
    runner.preparing = "download"
    assert runner.list()[0]["waiting"] == "download"
    job.status = "done"
    assert runner.list()[0]["waiting"] is None


@pytest.mark.skipif(
    not __import__("shutil").which("npm") or not __import__("shutil").which("uv"), reason="needs npm + uv"
)
def test_wheel_ships_the_built_web_app(tmp_path):
    import subprocess
    import zipfile
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path)], cwd=root, check=True, capture_output=True)
    names = zipfile.ZipFile(next(tmp_path.glob("*.whl"))).namelist()
    assert "pdf2epub/webui/dist/ui.html" in names
    assert any(n.startswith("pdf2epub/webui/dist/assets/") and n.endswith(".js") for n in names)
