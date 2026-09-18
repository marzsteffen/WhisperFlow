"""Tests for installer hardware detection and model recommendation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hardware  # noqa: E402

GIB = 1024**3


def _hardware(**overrides):
    base = {
        "os": "Windows 11",
        "cpu": "Test CPU",
        "cores": 8,
        "ram_bytes": 16 * GIB,
        "gpus": [],
    }
    base.update(overrides)
    return base


def test_recommend_prefers_vram_tiers_on_dedicated_gpu() -> None:
    rec = hardware.recommend_model(_hardware(gpus=[{"name": "NVIDIA GeForce RTX 3070", "vram_bytes": 8 * GIB, "dedicated": True}]))
    assert rec["model"] == "large-v3-turbo"
    assert rec["backend"] == "vulkan"


def test_recommend_medium_for_midrange_vram() -> None:
    rec = hardware.recommend_model(_hardware(gpus=[{"name": "AMD Radeon RX 6600", "vram_bytes": 4 * GIB, "dedicated": True}]))
    assert rec["model"] == "medium"
    assert rec["backend"] == "vulkan"


def test_recommend_tiny_when_vram_insufficient() -> None:
    rec = hardware.recommend_model(_hardware(gpus=[{"name": "NVIDIA GeForce GT 710", "vram_bytes": 1 * GIB, "dedicated": True}]))
    assert rec["model"] == "tiny"
    assert rec["backend"] == "cpu"


def test_recommend_small_on_integrated_graphics_with_enough_ram() -> None:
    rec = hardware.recommend_model(_hardware(gpus=[{"name": "AMD Radeon 860M", "vram_bytes": None, "dedicated": False}]))
    assert rec["model"] == "small"
    assert rec["backend"] == "vulkan"


def test_recommend_cpu_fallback_without_gpu() -> None:
    rec = hardware.recommend_model(_hardware(gpus=[]))
    assert rec["model"] == "small"
    assert rec["backend"] == "cpu"


def test_recommend_tiny_for_low_ram_system() -> None:
    rec = hardware.recommend_model(_hardware(ram_bytes=3 * GIB, gpus=[]))
    assert rec["model"] == "tiny"
    assert rec["backend"] == "cpu"


def test_recommend_handles_missing_values() -> None:
    rec = hardware.recommend_model({"os": "", "cpu": "", "cores": None, "ram_bytes": None, "gpus": None})
    assert rec["model"] in hardware.MODEL_SIZES
    assert rec["backend"] in {"vulkan", "cpu"}


def test_recommend_known_laptop_configuration() -> None:
    rec = hardware.recommend_model(
        _hardware(
            cpu="AMD Ryzen AI 9 HX 370 w/ Radeon 890M",
            cores=12,
            ram_bytes=32 * GIB,
            gpus=[{"name": "AMD Radeon 890M", "vram_bytes": None, "dedicated": False}],
        )
    )
    assert rec["model"] == "small"
    assert rec["backend"] == "vulkan"
    assert "Radeon 890M" in rec["summary"]


@pytest.mark.parametrize("model", list(hardware.MODEL_SIZES))
def test_every_model_is_reachable_via_some_vram_tier(model: str) -> None:
    vram_tiers = [model_key for _, model_key in hardware._VRAM_DEDICATED_TIERS]
    ram_tiers = [model_key for _, model_key in hardware._RAM_TIERS]
    assert model in vram_tiers or model in ram_tiers or model == "tiny"
