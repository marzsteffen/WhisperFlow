"""System detection and model recommendation for the WhisperFlow installer.

Runs on the plain standard library before any virtual environment exists, on
both Windows and Linux. All thresholds are tuned for whisper.cpp GGML models,
which need roughly 1.5x their file size plus a fixed working-memory overhead.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

GIB = 1024**3
MIB = 1024**2

MODEL_SIZES = {
    "tiny": 77_691_713,
    "base": 147_951_465,
    "small": 487_601_967,
    "medium": 1_533_763_059,
    "large-v3-turbo": 1_624_555_275,
}

MODEL_LABELS = {
    "tiny": "Tiny (75 MB) · maximal schnell",
    "base": "Base (142 MB) · schnell",
    "small": "Small (465 MB) · empfohlene Mittelstufe",
    "medium": "Medium (1,5 GB) · genauer",
    "large-v3-turbo": "Large v3 Turbo (1,6 GB) · beste Qualität",
}

_VRAM_DEDICATED_TIERS = [(5 * GIB, "large-v3-turbo"), (3 * GIB, "medium"), (2 * GIB, "small")]
_RAM_TIERS = [(16 * GIB, "small"), (8 * GIB, "base"), (4 * GIB, "base"), (0, "tiny")]


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _ram_windows() -> int | None:
    try:
        import ctypes

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys)
    except Exception:  # noqa: BLE001 - detection must never break the installer
        return None
    return None


def _ram_linux() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _cpu_name() -> str:
    if platform.system() == "Linux":
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine()


def _registry_windows_gpus() -> list[dict[str, Any]]:
    try:
        import winreg

        key_path = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        gpus: list[dict[str, Any]] = []
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as root:
            for index in range(64):
                try:
                    subkey = winreg.OpenKey(root, f"{index:04d}")
                except OSError:
                    break
                with subkey:
                    try:
                        name = winreg.QueryValueEx(subkey, "DriverDesc")[0]
                    except OSError:
                        continue
                    vram = None
                    for value_name in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                        try:
                            raw = winreg.QueryValueEx(subkey, value_name)[0]
                        except OSError:
                            continue
                        if isinstance(raw, int) and raw > 0:
                            vram = raw
                            break
                        if isinstance(raw, list) and raw and isinstance(raw[0], int) and raw[0] > 0:
                            vram = raw[0]
                            break
                    gpus.append({"name": str(name), "vram_bytes": vram})
        return gpus
    except Exception:  # noqa: BLE001 - detection must never break the installer
        return []


def _nvidia_smi_gpus() -> list[dict[str, Any]]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return []
    try:
        result = subprocess.run(
            [executable, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 2:
            continue
        try:
            gpus.append({"name": parts[0], "vram_bytes": int(float(parts[1])) * MIB})
        except ValueError:
            gpus.append({"name": parts[0], "vram_bytes": None})
    return gpus


_PCI_VENDOR_NAMES = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}


def _linux_gpus() -> list[dict[str, Any]]:
    drm_root = Path("/sys/class/drm")
    gpus: list[dict[str, Any]] = []
    if not drm_root.is_dir():
        return []
    for card in sorted(drm_root.glob("card[0-9]*"), key=lambda path: path.name):
        device = card / "device"
        vendor_file = device / "vendor"
        if not vendor_file.is_file():
            continue
        vendor = vendor_file.read_text().strip()
        name = _PCI_VENDOR_NAMES.get(vendor, f"PCI-GPU {vendor}")
        vram = _read_int(device / "mem_info_vram_total")
        gpus.append({"name": name, "vram_bytes": vram})
    return gpus


def _is_dedicated(name: str, vram: int | None) -> bool:
    lowered = name.lower()
    if any(hint in lowered for hint in ("nvidia", "geforce", "rtx", "gtx", "quadro")):
        return True
    if "intel" in lowered and "arc" in lowered:
        return True
    if "radeon" in lowered or "amd" in lowered:
        if "rx" in lowered:
            return True
        return bool(vram and vram >= 2 * GIB)
    return bool(vram and vram >= 4 * GIB)


def detect() -> dict[str, Any]:
    """Collect hardware details; single fields degrade gracefully to None."""
    system = platform.system()
    ram = _ram_windows() if system == "Windows" else _ram_linux() if system == "Linux" else None
    if system == "Windows":
        gpus = _registry_windows_gpus()
    else:
        gpus = _linux_gpus()
    known_vram = [gpu for gpu in gpus if gpu.get("vram_bytes") is None]
    if known_vram:
        for gpu in known_vram:
            for candidate in _nvidia_smi_gpus():
                if candidate["name"] in gpu["name"] or gpu["name"] in candidate["name"]:
                    gpu["vram_bytes"] = candidate["vram_bytes"]
                    break
    for gpu in gpus:
        gpu["dedicated"] = _is_dedicated(gpu["name"], gpu.get("vram_bytes"))
    return {
        "os": f"{system} {platform.release()}",
        "cpu": _cpu_name(),
        "cores": os.cpu_count(),
        "ram_bytes": ram,
        "gpus": gpus,
    }


def recommend_model(hardware: dict[str, Any]) -> dict[str, Any]:
    """Choose the best Whisper model and inference backend for this system."""
    ram = hardware.get("ram_bytes") or 0
    ram_gb = ram / GIB
    cpu = hardware.get("cpu") or "Unbekannte CPU"
    cores = hardware.get("cores") or "?"
    gpus = [gpu for gpu in (hardware.get("gpus") or []) if gpu.get("name")]

    system_line = f"{cpu} · {cores} Kerne · {ram_gb:.0f} GB RAM" if ram else f"{cpu} · {cores} Kerne"

    dedicated = next((gpu for gpu in gpus if gpu.get("dedicated")), None)
    if dedicated:
        vram = dedicated.get("vram_bytes")
        model = None
        if vram:
            for threshold, candidate in _VRAM_DEDICATED_TIERS:
                if vram >= threshold:
                    model = candidate
                    break
            summary = f"{system_line} · {dedicated['name']} mit {vram / GIB:.0f} GB VRAM"
            if model:
                return {
                    "model": model,
                    "backend": "vulkan",
                    "summary": summary,
                    "reason": (
                        f"Dedizierte GPU mit {vram / GIB:.0f} GB VRAM: {MODEL_LABELS[model]} "
                        "läuft dort mit Vulkan-Beschleunigung flüssig."
                    ),
                }
            return {
                "model": "tiny",
                "backend": "cpu",
                "summary": summary,
                "reason": f"Zu wenig VRAM ({vram / MIB:.0f} MB) für GPU-Beschleunigung: {MODEL_LABELS['tiny']} auf der CPU.",
            }
        return {
            "model": "small",
            "backend": "vulkan",
            "summary": f"{system_line} · {dedicated['name']}",
            "reason": f"Dedizierte GPU mit unbekanntem VRAM: solide Mittelstufe {MODEL_LABELS['small']} mit Vulkan.",
        }

    if gpus:
        model = next((candidate for threshold, candidate in _RAM_TIERS if ram >= threshold), "tiny")
        if model != "tiny":
            return {
                "model": model,
                "backend": "vulkan",
                "summary": f"{system_line} · {gpus[0]['name']} (integrierte Grafik)",
                "reason": (
                    f"Integrierte Grafikeinheit mit {ram_gb:.0f} GB gemeinsamem Speicher: "
                    f"{MODEL_LABELS[model]} über Vulkan."
                ),
            }

    model = next((candidate for threshold, candidate in _RAM_TIERS if ram >= threshold), "tiny")
    gpu_note = f" · {gpus[0]['name']}" if gpus else ""
    return {
        "model": model,
        "backend": "cpu",
        "summary": f"{system_line}{gpu_note}",
        "reason": f"Kein GPU-Beschleuniger nutzbar: {MODEL_LABELS[model]} läuft auf der CPU.",
    }
