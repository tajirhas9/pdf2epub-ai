"""The installed-tool layout (no repo): user config/cache dirs, init wizard, config commands."""

import os
import subprocess
import sys
import tomllib
from pathlib import Path


def run(home: Path, *args: str, env_extra: dict | None = None, cwd: Path | None = None):
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("LLM_", "PDF2EPUB_", "PADDLE_", "HF_", "PLAYWRIGHT_"))
    }
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
        PDF2EPUB_INSTALLED_LAYOUT="1",
        PDF2EPUB_NO_KEYRING="1",  # CI has no keyring: exercise the file fallback
        **(env_extra or {}),
    )
    return subprocess.run(
        [sys.executable, "-m", "pdf2epub.cli", *args], env=env, cwd=cwd or home, capture_output=True, text=True
    )


def test_init_config_and_uninstall(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = run(home, "config", "path")
    cfg = Path(r.stdout.strip())
    assert cfg == home / ".config" / "pdf2epub" / "config.toml", r.stderr

    r = run(
        home,
        "init",
        "--provider",
        "openai",
        "--model",
        "gpt-test",
        "--api-key-env",
        "MY_KEY",
        "--yes",
        "--no-check",
        env_extra={"MY_KEY": "sk-secret-123"},
    )
    assert r.returncode == 0, r.stderr + r.stdout
    data = tomllib.loads(cfg.read_text())
    assert data["llm"]["base_url"] == "https://api.openai.com/v1"
    assert data["llm"]["model"] == "gpt-test"
    assert data["llm"]["api_key"] == "sk-secret-123"  # no keyring -> file fallback
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"

    shown = run(home, "config", "show").stdout
    assert "sk-secret-123" not in shown and "***" in shown
    eff = run(home, "config", "show", "--all").stdout
    assert 'api_key = "set"' in eff and "sk-secret" not in eff
    assert str(home / ".cache" / "pdf2epub" / "work") in eff  # work dir in the user cache
    assert f'out_dir = "{home}"' in eff  # output defaults to the current directory

    assert run(home, "config", "set", "langs", "en,ar").returncode == 0
    assert run(home, "config", "set", "llm.enabled", "false").returncode == 0
    assert run(home, "config", "set", "nope", "1").returncode != 0
    data = tomllib.loads(cfg.read_text())
    assert data["langs"] == ["en", "ar"] and data["llm"]["enabled"] is False

    # per-folder override and env precedence
    (home / "pdf2epub.toml").write_text('[llm]\nmodel = "folder-model"\n')
    eff = run(home, "config", "show", "--all").stdout
    assert 'model = "folder-model"' in eff
    eff = run(home, "config", "show", "--all", env_extra={"LLM_MODEL": "env-model"}).stdout
    assert 'model = "env-model"' in eff

    (home / ".cache" / "pdf2epub" / "work").mkdir(parents=True)
    r = run(home, "uninstall", "--yes")
    assert r.returncode == 0, r.stderr
    assert not cfg.exists() and not (home / ".cache" / "pdf2epub").exists()
