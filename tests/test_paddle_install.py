"""Hardware probe parsing and PaddlePaddle build selection / install fallback (no real installs)."""

import tomllib
from pathlib import Path

import pytest

from pdf2epub import hardware, paddle_install
from pdf2epub.hardware import Gpu, Hardware
from pdf2epub.paddle_install import BUILDS, Choice, InstallError, choose

NVIDIA_QUERY = "NVIDIA GeForce RTX 4090, 575.57.08, 8.9, 24564\n"
NVIDIA_HEADER = "| NVIDIA-SMI 575.57.08   Driver Version: 575.57.08   CUDA Version: 12.9     |"


def nvidia(cuda, cc, name="NVIDIA GeForce RTX 4090", mem=24564):
    return Gpu("nvidia", name, "575", cuda, cc, mem)


def hw(os_="linux", arch="x86_64", gpus=(), ram_mb=32768, cpus=16):
    return Hardware(os_, arch, list(gpus), cpus, ram_mb)


def test_parse_nvidia_smi():
    [g] = hardware.parse_nvidia(NVIDIA_QUERY, NVIDIA_HEADER)
    assert g == Gpu("nvidia", "NVIDIA GeForce RTX 4090", "575.57.08", (12, 9), (8, 9), 24564)


def test_parse_nvidia_smi_old_driver_without_compute_cap():
    [g] = hardware.parse_nvidia("Tesla K80, 470.1, [N/A], 11441\n", "CUDA Version: 11.4")
    assert g.cuda_max == (11, 4) and g.compute_cap is None and g.mem_mb == 11441


def test_parse_rocm_dedupes_cards():
    text = "GPU[0] : Card series: AMD Radeon RX 7900 XTX\nGPU[1] : Card series: AMD Radeon RX 7900 XTX\n"
    assert hardware.parse_rocm(text) == [Gpu("amd", "AMD Radeon RX 7900 XTX")]


def test_probe_never_raises_without_tools(monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda _: None)
    hardware.probe.cache_clear()
    try:
        assert hardware.probe().cpu_count >= 1
    finally:
        hardware.probe.cache_clear()


def test_auto_workers_capped_by_ram():
    assert hardware.auto_workers(hw(cpus=16, ram_mb=4096)) == 2
    assert hardware.auto_workers(hw(cpus=4, ram_mb=65536)) == 4
    assert hardware.auto_workers(hw(cpus=8, ram_mb=0)) == 8  # unknown RAM: one per core


@pytest.mark.parametrize(
    ("machine", "want_keys", "reason_part"),
    [
        (hw("darwin", "arm64", [Gpu("apple", "Apple M3 GPU")]), ["cpu"], "no Metal"),
        (hw("darwin", "x86_64"), ["cpu-mac-intel"], "No NVIDIA GPU"),
        (hw("linux", "aarch64"), ["cpu-aarch64"], "No NVIDIA GPU"),
        (hw("linux", gpus=[nvidia((12, 9), (8, 9))]), ["cu129", "cu126", "cu118", "cpu"], "CUDA 12.9 build"),
        (hw("windows", gpus=[nvidia((12, 9), (12, 0), "RTX 5090")]), ["cu129", "cpu"], "CUDA 12.9 build"),
        (hw("windows", gpus=[nvidia((11, 8), (6, 1), "GTX 1060")]), ["cu118", "cpu"], "CUDA 11.8 build"),
        (hw("linux", gpus=[nvidia((12, 6), (6, 1), "GTX 1080")]), ["cu126", "cu118", "cpu"], "CUDA 12.6 build"),
        (hw("linux", gpus=[nvidia((11, 4), None, "Tesla K80")]), ["cpu"], "driver too old"),
        (hw("linux", "aarch64", [nvidia((12, 9), (8, 7), "Orin")]), ["cpu-aarch64"], "no GPU build for Linux"),
        (hw("linux", gpus=[Gpu("amd", "AMD Radeon RX 7900 XTX")]), ["cpu"], "no pip build for AMD"),
        (hw("windows", gpus=[Gpu("intel", "Intel Arc A770")]), ["cpu"], "no pip build for Intel"),
    ],
)
def test_choose_best_build(machine, want_keys, reason_part):
    choice = choose(machine)
    assert [b.key for b in choice.builds] == want_keys
    assert reason_part in choice.reason


def test_choose_overrides_and_unsupported_platform():
    rtx = hw(gpus=[nvidia((12, 9), (8, 9))])
    assert [b.key for b in choose(rtx, "cpu").builds] == ["cpu"]
    assert choose(hw("darwin", "arm64"), "gpu").builds == []  # no silent CPU when a GPU build was asked for
    none = choose(hw("windows", "arm64"))
    assert none.build is None and "no build" in none.reason


def test_extras_pins_match_build_table():
    py = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    extras = py["project"]["optional-dependencies"]
    by_key = {b.key: b for b in BUILDS}
    assert extras["cpu"] == [by_key["cpu"].spec]
    assert extras["gpu"] == [by_key["cu129"].spec]
    gpu_index = next(i["url"] for i in py["tool"]["uv"]["index"] if i["name"] == "paddle-cu129")
    assert gpu_index == by_key["cu129"].index


def test_install_commands():
    gpu = next(b for b in BUILDS if b.key == "cu129")
    uv = paddle_install.install_cmd(gpu, ["/bin/uv", "pip"])
    assert uv[:3] == ["/bin/uv", "pip", "install"] and "paddlepaddle-gpu==3.4.0" in uv
    assert uv[uv.index("--index") + 1] == gpu.index
    pip = paddle_install.install_cmd(gpu, [paddle_install.sys.executable, "-m", "pip"])
    assert pip[-2:] == ["--extra-index-url", gpu.index]


def test_install_falls_back_when_check_fails(monkeypatch):
    cu129, cpu = (next(b for b in BUILDS if b.key == k) for k in ("cu129", "cpu"))
    ran = []
    monkeypatch.setattr(paddle_install, "pip_command", lambda: ["/bin/uv", "pip"])
    monkeypatch.setattr(paddle_install, "_dist", lambda pkg: False)
    monkeypatch.setattr(paddle_install, "_stream", lambda cmd, say: ran.append(cmd[2:4]) or 0)
    monkeypatch.setattr(paddle_install, "verify", lambda b: (not b.gpu, "can't see the GPU" if b.gpu else "ok"))
    lines = []
    assert paddle_install.install(Choice([cu129, cpu], ""), lines.append) is cpu
    assert ["install", "--python"] in ran and ["uninstall", "--python"] in ran  # the broken GPU build is removed
    assert any("can't see the GPU" in line for line in lines)


def test_install_reports_when_nothing_works(monkeypatch):
    monkeypatch.setattr(paddle_install, "pip_command", lambda: ["/bin/uv", "pip"])
    monkeypatch.setattr(paddle_install, "_dist", lambda pkg: False)
    monkeypatch.setattr(paddle_install, "_stream", lambda cmd, say: 1)
    with pytest.raises(InstallError, match="install failed"):
        paddle_install.install(Choice([BUILDS[3]], ""), lambda _: None)
    with pytest.raises(InstallError, match="no build"):
        paddle_install.install(Choice([], "no build for Windows arm64"), lambda _: None)
