"""What this computer has: OS, CPU, RAM and GPUs (any vendor). Used to pick a Paddle build and defaults.

Every probe is best effort: a missing tool or an unexpected output means "not found", never an error.
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from functools import cache
from pathlib import Path


@dataclass(frozen=True)
class Gpu:
    vendor: str  # nvidia | amd | intel | apple | other
    name: str
    driver: str = ""
    cuda_max: tuple[int, int] | None = None  # newest CUDA the NVIDIA driver supports
    compute_cap: tuple[int, int] | None = None  # NVIDIA sm version, e.g. (8, 9)
    mem_mb: int = 0


@dataclass(frozen=True)
class Hardware:
    os: str  # linux | windows | darwin
    arch: str  # x86_64 | arm64 | aarch64
    gpus: list[Gpu] = field(default_factory=list)
    cpu_count: int = 1
    ram_mb: int = 0
    wsl: bool = False

    @property
    def nvidia(self) -> list[Gpu]:
        return [g for g in self.gpus if g.vendor == "nvidia"]

    def public(self) -> dict:
        return asdict(self)


def _run(cmd: list[str], timeout: float = 8) -> str:
    if not shutil.which(cmd[0]):
        return ""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout if res.returncode == 0 else ""


def _version(text: str) -> tuple[int, int] | None:
    m = re.search(r"(\d+)\.(\d+)", text)
    return (int(m[1]), int(m[2])) if m else None


def parse_nvidia(query: str, header: str) -> list[Gpu]:
    """`nvidia-smi --query-gpu=name,driver_version,compute_cap,memory.total --format=csv,noheader,nounits`
    plus plain `nvidia-smi` (its header carries "CUDA Version: X.Y")."""
    m = re.search(r"CUDA Version:\s*(\d+\.\d+)", header)
    cuda = _version(m[1]) if m else None
    gpus = []
    for line in query.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4 or not parts[0]:
            continue
        mem = parts[3].split()[0] if parts[3] else ""
        gpus.append(
            Gpu(
                "nvidia",
                parts[0],
                driver=parts[1],
                cuda_max=cuda,
                compute_cap=_version(parts[2]),
                mem_mb=int(mem) if mem.isdigit() else 0,
            )
        )
    return gpus


def _nvidia() -> list[Gpu]:
    query = _run(
        ["nvidia-smi", "--query-gpu=name,driver_version,compute_cap,memory.total", "--format=csv,noheader,nounits"]
    )
    return parse_nvidia(query, _run(["nvidia-smi"])) if query else []


def parse_rocm(text: str) -> list[Gpu]:
    """`rocm-smi --showproductname` lines like "GPU[0] : Card series: AMD Radeon RX 7900 XTX"."""
    names = re.findall(r"GPU\[\d+\]\s*:\s*Card (?:series|SKU|model)\s*:\s*(.+)", text)
    seen: dict[str, None] = {}
    for n in names:
        seen.setdefault(n.strip(), None)
    return [Gpu("amd", n) for n in seen] if seen else []


def _linux_pci_gpus() -> list[Gpu]:
    """AMD / Intel display devices from sysfs (no vendor tools needed)."""
    vendors = {"0x1002": "amd", "0x8086": "intel"}
    out = []
    for dev in sorted(Path("/sys/class/drm").glob("card[0-9]*/device")):
        try:
            vendor = vendors.get((dev / "vendor").read_text().strip())
        except OSError:
            continue
        if vendor:
            out.append(Gpu(vendor, f"{vendor.upper() if vendor == 'amd' else 'Intel'} GPU"))
    return out


def _windows_gpus() -> list[Gpu]:
    text = _run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"])
    out = []
    for name in (n.strip() for n in text.splitlines()):
        low = name.lower()
        if not name or "nvidia" in low:  # NVIDIA comes from nvidia-smi, with driver details
            continue
        vendor = "amd" if ("amd" in low or "radeon" in low) else "intel" if "intel" in low else "other"
        out.append(Gpu(vendor, name))
    return out


def _apple_gpu(arch: str) -> list[Gpu]:
    if arch != "arm64":
        return []
    chip = _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip() or "Apple silicon"
    return [Gpu("apple", f"{chip} GPU")]


def _ram_mb() -> int:
    try:
        if sys.platform == "win32":

            class MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
                    (n, ctypes.c_ulonglong)
                    for n in ("total", "avail", "pagefile", "availpage", "virtual", "availvirtual", "ext")
                ]

            st = MemStatus()
            st.dwLength = ctypes.sizeof(MemStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))  # type: ignore[attr-defined]
            return int(st.total // 2**20)
        if sys.platform == "darwin":
            return int(_run(["sysctl", "-n", "hw.memsize"]).strip() or 0) // 2**20
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 2**20
    except (OSError, ValueError, AttributeError):
        return 0


def _arch() -> str:
    m = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "arm64", "aarch64": "aarch64"}.get(m, m)


@cache
def probe() -> Hardware:
    os_name = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    arch = _arch()
    gpus = _nvidia()
    if os_name == "linux":
        others = parse_rocm(_run(["rocm-smi", "--showproductname"])) or [
            g for g in _linux_pci_gpus() if g.vendor == "amd"
        ]
        intel = [g for g in _linux_pci_gpus() if g.vendor == "intel"]
        gpus += others + intel
    elif os_name == "windows":
        gpus += _windows_gpus()
    else:
        gpus += _apple_gpu(arch)
    wsl = os_name == "linux" and "microsoft" in platform.release().lower()
    return Hardware(os_name, arch, gpus, os.cpu_count() or 1, _ram_mb(), wsl)


def auto_workers(hw: Hardware | None = None, mb_per_worker: int = 1536) -> int:
    """Parallel page workers: one per CPU core, but never more than the RAM can hold."""
    hw = hw or probe()
    if not hw.ram_mb:
        return hw.cpu_count
    return max(1, min(hw.cpu_count, hw.ram_mb // mb_per_worker))
