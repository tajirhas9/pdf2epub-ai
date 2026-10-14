"""Launching the workbench URL: free port, readiness wait, opening a browser (WSL-aware)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import urllib.request
import webbrowser
from functools import cache
from pathlib import Path


@cache
def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def bind_host() -> str:
    # WSL: Windows reaches the server through localhost forwarding / the VM address; listening on all
    # interfaces avoids the IPv6-only "localhost" bind Node may pick, which Windows can't reach.
    return "0.0.0.0" if is_wsl() else "127.0.0.1"


def free_port(preferred: int, host: str = "127.0.0.1", tries: int = 50) -> int:
    for port in range(preferred, preferred + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"no free port in {preferred}–{preferred + tries - 1}")


def wait_until_up(url: str, timeout: float = 60.0, alive=lambda: True) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and alive():
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except Exception:
            time.sleep(0.3)
    return False


def wsl_ip() -> str | None:
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3).stdout.split()
        return out[0] if out else None
    except Exception:
        return None


def open_url(url: str) -> bool:
    """Open in the user's browser. On WSL that is the *Windows* default browser."""
    try:
        if is_wsl():
            if shutil.which("wslview"):
                return subprocess.run(["wslview", url], capture_output=True).returncode == 0
            cmd = shutil.which("cmd.exe") or "/mnt/c/Windows/System32/cmd.exe"
            if Path(cmd).exists():
                # cwd on a Windows drive avoids cmd's "UNC paths are not supported" warning
                subprocess.run([cmd, "/c", "start", "", url], cwd="/mnt/c", capture_output=True, timeout=10)
                return True
            return False
        return webbrowser.open(url)
    except Exception:
        return False


def windows_reachable(urls: list[str]) -> str | None:
    """From WSL: the first URL the *Windows* side can load (localhost forwarding is not always on).
    None when it cannot be checked (no PowerShell)."""
    ps = shutil.which("powershell.exe") or "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    if not Path(ps).exists():
        return None
    script = (
        "foreach ($u in @(" + ",".join(f"'{u}'" for u in urls) + ")) {"
        " try { $null = Invoke-WebRequest -UseBasicParsing $u -TimeoutSec 3; Write-Output $u; break } catch {} }"
    )
    try:
        out = (
            subprocess.run(
                [ps, "-NoProfile", "-NonInteractive", "-Command", script],
                cwd="/mnt/c",
                capture_output=True,
                text=True,
                timeout=20,
            )
            .stdout.strip()
            .splitlines()
        )
        return out[0].strip() if out else None
    except Exception:
        return None
