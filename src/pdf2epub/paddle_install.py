"""Pick and install the best PaddlePaddle build for this computer.

Package metadata can't see the hardware, so the choice happens here, at runtime: probe the GPUs
(`hardware.probe`), take the first build in `BUILDS` that fits, install it into the running Python
(`uv pip`, else `pip`), check it really works in a fresh process, and fall back down the list if not.

Paddle publishes pip builds for NVIDIA CUDA (Linux x86_64, Windows) and for CPU only. AMD, Intel and
Apple GPUs have no pip build (ROCm/XPU/NPU builds ship as Docker images), so they get CPU with a reason.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from pdf2epub.hardware import Gpu, Hardware, probe

log = logging.getLogger(__name__)

PADDLE_INDEX = "https://www.paddlepaddle.org.cn/packages/stable/"
PACKAGES = ("paddlepaddle-gpu", "paddlepaddle")
X86 = (("linux", "x86_64"), ("windows", "x86_64"))
OS_NAMES = {"darwin": "macOS", "windows": "Windows", "linux": "Linux"}


@dataclass(frozen=True)
class Build:
    key: str
    package: str
    version: str
    label: str
    platforms: tuple[tuple[str, str], ...]
    index: str | None = None  # extra package index (None = PyPI)
    cuda: tuple[int, int] | None = None  # CUDA the build needs from the driver (GPU builds only)
    cc: tuple[tuple[int, int], tuple[int, int]] | None = None  # supported compute capabilities

    @property
    def gpu(self) -> bool:
        return self.cuda is not None

    @property
    def spec(self) -> str:
        return f"{self.package}=={self.version}"

    def public(self) -> dict:
        return {"key": self.key, "package": self.package, "version": self.version, "label": self.label, "gpu": self.gpu}


# Best first. Versions: newest build Paddle publishes for Python 3.11 on that index.
# The [cpu]/[gpu] extras in pyproject.toml pin the "cpu" and "cu129" rows (tests keep them in sync).
BUILDS: tuple[Build, ...] = (
    Build(
        "cu129",
        "paddlepaddle-gpu",
        "3.4.0",
        "NVIDIA CUDA 12.9",
        X86,
        PADDLE_INDEX + "cu129/",
        (12, 9),
        ((7, 5), (12, 9)),
    ),  # Turing … Blackwell
    Build(
        "cu126",
        "paddlepaddle-gpu",
        "3.3.1",
        "NVIDIA CUDA 12.6",
        X86,
        PADDLE_INDEX + "cu126/",
        (12, 6),
        ((6, 0), (9, 0)),
    ),  # Pascal … Hopper
    Build(
        "cu118",
        "paddlepaddle-gpu",
        "3.3.1",
        "NVIDIA CUDA 11.8",
        X86,
        PADDLE_INDEX + "cu118/",
        (11, 8),
        ((3, 5), (9, 0)),
    ),  # older drivers
    Build("cpu", "paddlepaddle", "3.3.1", "CPU", (*X86, ("darwin", "arm64"))),
    Build("cpu-aarch64", "paddlepaddle", "3.3.1", "CPU", (("linux", "aarch64"),), PADDLE_INDEX + "cpu/"),
    Build("cpu-mac-intel", "paddlepaddle", "3.0.0", "CPU", (("darwin", "x86_64"),)),  # last Intel-Mac wheel
)


@dataclass(frozen=True)
class Choice:
    builds: list[Build]  # best first; the rest are fallbacks if the first one fails its check
    reason: str

    @property
    def build(self) -> Build | None:
        return self.builds[0] if self.builds else None

    def public(self) -> dict:
        return {"build": self.build.public() if self.build else None, "reason": self.reason}


def _ver(v: tuple[int, int] | None) -> str:
    return ".".join(map(str, v)) if v else "?"


def _fits(b: Build, gpu: Gpu) -> bool:
    if gpu.cuda_max is None or b.cuda is None or gpu.cuda_max < b.cuda:
        return False
    if gpu.compute_cap and b.cc:
        return b.cc[0] <= gpu.compute_cap <= b.cc[1]
    return True


def choose(hw: Hardware | None = None, want: str = "auto") -> Choice:
    """Best build for `hw` (want: auto | gpu | cpu), with a one-line reason for people."""
    hw = hw or probe()
    here = [b for b in BUILDS if (hw.os, hw.arch) in b.platforms]
    cpu = [b for b in here if not b.gpu]
    platform_name = f"{OS_NAMES[hw.os]} {hw.arch}"
    if not here:
        return Choice([], f"PaddlePaddle has no build for {platform_name}")
    if want == "cpu":
        return Choice(cpu, "CPU build (chosen by you)")

    if hw.nvidia:
        gpu = hw.nvidia[0]
        fit = [b for b in here if b.gpu and _fits(b, gpu)]
        if fit:
            why = f"{gpu.name} (driver supports CUDA {_ver(gpu.cuda_max)}) → {fit[0].label} build"
            return Choice(fit + cpu, why)
        if not any(b.gpu for b in here):
            why = f"{gpu.name}: PaddlePaddle has no GPU build for {platform_name}; using CPU"
        elif gpu.cuda_max is None or gpu.cuda_max < (11, 8):
            cuda = _ver(gpu.cuda_max)
            why = f"{gpu.name}: driver too old (CUDA {cuda}, need 11.8+) — update it for GPU speed; using CPU"
        else:
            why = f"{gpu.name} (compute {_ver(gpu.compute_cap)}) is not supported by PaddlePaddle; using CPU"
    elif hw.gpus:
        gpu = hw.gpus[0]
        what = {
            "apple": "has no Metal (Apple GPU) support",
            "amd": "has no pip build for AMD GPUs",
            "intel": "has no pip build for Intel GPUs",
        }.get(gpu.vendor, "can't use this GPU")
        why = f"{gpu.name}: PaddlePaddle {what}; using CPU"
    else:
        why = "No NVIDIA GPU found; using CPU"
    if want == "gpu":
        return Choice([], why.replace("; using CPU", ""))
    if hw.os == "darwin":
        why += " for page layout (text recognition uses Apple Vision)"
    return Choice(cpu, why)


def installed() -> tuple[str, str] | None:
    """(package, version) of the Paddle build in this Python, or None."""
    for pkg in PACKAGES:
        try:
            return pkg, importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            continue
    return None


def importable() -> bool:
    importlib.invalidate_caches()
    return importlib.util.find_spec("paddle") is not None


def pip_command() -> list[str] | None:
    """`uv pip` (works in uv venvs and uv tool envs, which have no pip), else this Python's pip."""
    if uv := shutil.which("uv"):
        return [uv, "pip"]
    if importlib.util.find_spec("pip"):
        return [sys.executable, "-m", "pip"]
    return None


def _is_uv(pip: list[str]) -> bool:
    return pip[0] != sys.executable


def install_cmd(b: Build, pip: list[str] | None = None) -> list[str] | None:
    pip = pip or pip_command()
    if pip is None:
        return None
    if _is_uv(pip):
        cmd = [*pip, "install", "--python", sys.executable, b.spec]
        return cmd + (["--index", b.index, "--index-strategy", "unsafe-best-match"] if b.index else [])
    return [*pip, "install", b.spec] + (["--extra-index-url", b.index] if b.index else [])


def uninstall_cmd(packages: list[str], pip: list[str] | None = None) -> list[str] | None:
    pip = pip or pip_command()
    if pip is None:
        return None
    if _is_uv(pip):
        return [*pip, "uninstall", "--python", sys.executable, *packages]
    return [*pip, "uninstall", "-y", *packages]


def manual_command(b: Build | None) -> str:
    """What to type when this process can't install it (no uv, no pip)."""
    if b is None:
        return ""
    idx = f" --index {b.index} --index-strategy unsafe-best-match" if b.index else ""
    return f'uv pip install --python "{sys.executable}" "{b.spec}"{idx}'


# Runs in a fresh process: the current one may hold an older paddle import.
_VERIFY = """
import json, pdf2epub, paddle
x = paddle.to_tensor([1.0, 2.0])
info = {"version": paddle.__version__, "cuda": bool(paddle.device.is_compiled_with_cuda()), "gpus": 0}
if info["cuda"]:
    info["gpus"] = paddle.device.cuda.device_count()
    if info["gpus"]:
        x = paddle.to_tensor([1.0, 2.0], place=paddle.CUDAPlace(0))
info["ok"] = float((x * 2).sum()) == 6.0
print(json.dumps(info))
"""


def verify(b: Build, timeout: float = 600) -> tuple[bool, str]:
    try:
        res = subprocess.run([sys.executable, "-c", _VERIFY], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "the check timed out"
    lines = res.stdout.strip().splitlines()
    try:
        info = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError):
        tail = (res.stderr.strip().splitlines() or ["import failed"])[-1]
        return False, tail[:300]
    if not info.get("ok"):
        return False, "a test computation gave a wrong result"
    if b.gpu and not info.get("gpus"):
        return False, "installed, but it can't see the GPU (driver/CUDA mismatch)"
    return True, f"PaddlePaddle {info['version']} on {'GPU' if b.gpu and info['gpus'] else 'CPU'}"


class InstallError(RuntimeError):
    pass


def _stream(cmd: list[str], say: Callable[[str], None]) -> int:
    say("$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.strip():
            say(line.rstrip())
    return proc.wait()


def install(choice: Choice, say: Callable[[str], None] = log.info) -> Build:
    """Install the first build of `choice` that passes `verify`; returns it. Raises InstallError."""
    if not choice.builds:
        raise InstallError(choice.reason)
    if pip_command() is None:
        raise InstallError(f"Neither uv nor pip is available. Install it yourself:  {manual_command(choice.build)}")
    errors = []
    for b in choice.builds:
        say(f"Installing PaddlePaddle {b.version} ({b.label} build)…")
        stale = [p for p in PACKAGES if p != b.package and _dist(p)]
        if stale:  # both builds ship the same `paddle` module
            _stream(uninstall_cmd(stale) or [], say)
        if _stream(install_cmd(b) or [], say) != 0:
            errors.append(f"{b.label}: install failed")
            say(f"✗ {b.label} build did not install")
            continue
        ok, detail = verify(b)
        if ok:
            importlib.invalidate_caches()
            say(f"✓ {detail}")
            return b
        errors.append(f"{b.label}: {detail}")
        say(f"✗ {b.label} build does not work here ({detail}); trying the next one")
        _stream(uninstall_cmd([b.package]) or [], say)
    raise InstallError("Could not install a working PaddlePaddle — " + "; ".join(errors))


def _dist(pkg: str) -> bool:
    try:
        importlib.metadata.version(pkg)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


def auto_install_enabled() -> bool:
    return os.environ.get("PDF2EPUB_NO_AUTO_INSTALL", "").lower() not in ("1", "true", "yes")


class Installer:
    """One background install at a time (the web app's "Check this computer" step)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state: dict = {
            "running": False,
            "done": False,
            "error": "",
            "build": None,
            "reason": "",
            "restart": False,
            "log": [],
        }
        self._log: deque[str] = deque(maxlen=40)

    @property
    def running(self) -> bool:
        return self.state["running"]

    def start(self, want: str = "auto") -> dict:
        with self._lock:
            if self.running:
                return self.status()
            choice = choose(want=want)
            self._log.clear()
            restart = "paddle" in sys.modules  # an old build is loaded here: the new one needs a restart
            self.state = {
                "running": True,
                "done": False,
                "error": "",
                "build": None,
                "reason": choice.reason,
                "restart": restart,
                "log": [],
            }
            self._thread = threading.Thread(target=self._run, args=(choice,), daemon=True, name="paddle-install")
            self._thread.start()
            return self.status()

    def _say(self, line: str) -> None:
        self._log.append(line)
        log.info("%s", line)

    def _run(self, choice: Choice) -> None:
        try:
            b = install(choice, self._say)
            self.state.update(build=b.public())
        except Exception as e:  # noqa: BLE001 - shown in the UI
            self.state.update(error=str(e))
        finally:
            self.state.update(running=False, done=True)

    def status(self) -> dict:
        return {**self.state, "log": list(self._log)}


INSTALLER = Installer()
