"""Tests for ``clipforge.perf`` (v0.8.0, Worker A)."""

from __future__ import annotations

import pytest

from clipforge import perf


def test_detect_hardware_never_crashes_and_has_shape():
    hw = perf.detect_hardware()
    assert set(hw) == {"cpu_cores", "ram_gb", "gpu", "platform"}
    assert isinstance(hw["cpu_cores"], int) and hw["cpu_cores"] >= 1
    assert hw["ram_gb"] is None or hw["ram_gb"] > 0
    assert hw["gpu"] is None or isinstance(hw["gpu"], str)
    assert isinstance(hw["platform"], str) and hw["platform"]


def test_detect_hardware_survives_missing_info(monkeypatch):
    monkeypatch.setattr(perf.os, "cpu_count", lambda: (_ for _ in ()).throw(OSError("no cpu")))
    monkeypatch.setattr(perf, "_gpu_name", lambda: (_ for _ in ()).throw(RuntimeError("no gpu")))
    hw = perf.detect_hardware()  # must not raise
    assert hw["cpu_cores"] == 1
    assert hw["gpu"] is None


def test_recommend_profile_thresholds():
    # low: no GPU, or <= 4 cores, or <= 8 GB RAM.
    assert perf.recommend_profile({"cpu_cores": 16, "ram_gb": 64.0, "gpu": None}) == "low"
    assert perf.recommend_profile({"cpu_cores": 4, "ram_gb": 32.0, "gpu": "RTX 4090"}) == "low"
    assert perf.recommend_profile({"cpu_cores": 8, "ram_gb": 8.0, "gpu": "RTX 4090"}) == "low"
    assert perf.recommend_profile({"cpu_cores": 2, "ram_gb": 4.0, "gpu": None}) == "low"
    # high: GPU + >= 8 cores + >= 16 GB.
    assert perf.recommend_profile({"cpu_cores": 8, "ram_gb": 16.0, "gpu": "RTX 4060"}) == "high"
    assert perf.recommend_profile({"cpu_cores": 16, "ram_gb": 64.0, "gpu": "RTX 4090"}) == "high"
    # balanced: in between.
    assert perf.recommend_profile({"cpu_cores": 6, "ram_gb": 12.0, "gpu": "GTX 1660"}) == "balanced"
    assert perf.recommend_profile({"cpu_cores": 8, "ram_gb": 12.0, "gpu": "RTX 4060"}) == "balanced"
    # Missing / broken input degrades to the safe side.
    assert perf.recommend_profile(None) == "low"
    assert perf.recommend_profile({}) == "low"
    assert perf.recommend_profile({"cpu_cores": "nope"}) == "low"


def test_profile_settings_fully_specified():
    required = {
        "whisper_model", "preview", "max_concurrent_jobs",
        "batch_max", "animations", "ffmpeg_preset", "max_parallel_clips",
    }
    for name in perf.PROFILE_NAMES:
        settings = perf.PROFILE_SETTINGS[name]
        assert required <= set(settings), name
    assert perf.PROFILE_SETTINGS["low"]["whisper_model"] == "tiny"
    assert perf.PROFILE_SETTINGS["low"]["max_concurrent_jobs"] == 1
    assert perf.PROFILE_SETTINGS["low"]["batch_max"] == 5
    assert perf.PROFILE_SETTINGS["high"]["whisper_model"] == "base"
    assert perf.PROFILE_SETTINGS["high"]["preview"] == "1080p"
    assert perf.PROFILE_SETTINGS["high"]["max_concurrent_jobs"] == 4
    assert perf.PROFILE_SETTINGS["high"]["batch_max"] == 20
    # Concurrency grows with the profile.
    assert (
        perf.PROFILE_SETTINGS["low"]["max_concurrent_jobs"]
        < perf.PROFILE_SETTINGS["balanced"]["max_concurrent_jobs"]
        < perf.PROFILE_SETTINGS["high"]["max_concurrent_jobs"]
    )


def test_override_persistence_round_trip(tmp_path):
    assert perf.load_override(tmp_path) == "auto"
    assert perf.save_override("high", tmp_path) == "high"
    assert perf.load_override(tmp_path) == "high"
    assert (tmp_path / perf.OVERRIDE_FILENAME).exists()
    active = perf.get_active_profile(data_dir=tmp_path)
    assert active["profile"] == "high"
    assert active["mode"] == "high"
    assert active["settings"] == perf.PROFILE_SETTINGS["high"]
    perf.save_override("auto", tmp_path)
    assert perf.load_override(tmp_path) == "auto"


def test_override_invalid_mode_rejected_and_bad_file_is_auto(tmp_path):
    with pytest.raises(ValueError):
        perf.save_override("ultra", tmp_path)
    (tmp_path / perf.OVERRIDE_FILENAME).write_text("{not json", encoding="utf-8")
    assert perf.load_override(tmp_path) == "auto"
    (tmp_path / perf.OVERRIDE_FILENAME).write_text('{"mode": "ultra"}', encoding="utf-8")
    assert perf.load_override(tmp_path) == "auto"


def test_get_active_profile_auto_resolves_to_recommended(tmp_path):
    hw = {"cpu_cores": 16, "ram_gb": 32.0, "gpu": "RTX 4090", "platform": "Linux"}
    active = perf.get_active_profile(data_dir=tmp_path, hw=hw)
    assert active["mode"] == "auto"
    assert active["profile"] == "high"
    assert active["hardware"] == hw
    # Returned settings are a copy — mutating them must not leak.
    active["settings"]["batch_max"] = 999
    assert perf.PROFILE_SETTINGS["high"]["batch_max"] == 20
