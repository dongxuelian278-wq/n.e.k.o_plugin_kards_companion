"""Contracts for perception cadence settings and the due scheduler."""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError

import pytest

from kards_companion.perception.scheduling import (
    CONFIG_KEYS,
    PRESETS,
    PerceptionScheduler,
    PerceptionSettings,
    SettingsValidationError,
)


# ------------------------------------------------------------------ settings

def test_default_settings_are_balanced_and_frozen() -> None:
    settings = PerceptionSettings()
    assert settings.preset == "balanced"
    assert settings.perception_preset == "balanced"
    assert (settings.capture_fps, settings.change_fps, settings.hud_fps,
            settings.unit_fps) == (20.0, 10.0, 4.0, 8.0)
    assert settings.recording_enabled is False
    assert settings.recording_max_mb == 2048
    assert settings.recording_jpeg_quality == 85
    assert settings.roi_auto_calibrate is True
    assert settings.evidence_enabled is True
    with pytest.raises(FrozenInstanceError):
        settings.capture_fps = 10  # type: ignore[misc]


def test_named_preset_applies_its_rates_without_repeating_them() -> None:
    low = PerceptionSettings(preset="low_cost")
    assert (low.capture_fps, low.change_fps, low.hud_fps, low.unit_fps) == (
        15.0, 8.0, 3.0, 5.0
    )
    quality = PerceptionSettings(preset="quality")
    assert (quality.capture_fps, quality.change_fps, quality.hud_fps,
            quality.unit_fps) == (30.0, 12.0, 5.0, 10.0)
    assert PRESETS["balanced"] == (20.0, 10.0, 4.0, 8.0)


def test_direct_advanced_rates_switch_to_custom() -> None:
    settings = PerceptionSettings(capture_fps=25)
    assert settings.preset == "custom"
    assert settings.capture_fps == 25
    assert settings.change_fps == 10.0


def test_validate_changes_preset_and_advanced_edit() -> None:
    result = PerceptionSettings.validate_changes(
        {"perception_preset": "quality"}
    )
    assert result == {
        "perception_preset": "quality",
        "capture_fps": 30.0,
        "change_fps": 12.0,
        "hud_fps": 5.0,
        "unit_fps": 10.0,
    }

    advanced = PerceptionSettings.validate_changes({"hud_fps": 6})
    assert advanced["perception_preset"] == "custom"
    assert advanced["hud_fps"] == 6.0
    assert advanced["capture_fps"] == 20.0
    assert advanced["change_fps"] == 10.0
    assert advanced["unit_fps"] == 8.0

    current = PerceptionSettings(preset="low_cost")
    edited = PerceptionSettings.validate_changes(
        {"perception_preset": "low_cost", "unit_fps": 6}, current=current
    )
    assert edited["perception_preset"] == "custom"
    assert edited["unit_fps"] == 6.0
    assert edited["capture_fps"] == 15.0


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"unknown": 1}, "unknown"),
        ({"capture_fps": True}, "finite number"),
        ({"capture_fps": math.nan}, "finite"),
        ({"capture_fps": math.inf}, "finite"),
        ({"capture_fps": 0.5}, "between 1 and 30"),
        ({"capture_fps": 31}, "between 1 and 30"),
        ({"change_fps": 0.25}, "between 0.5 and 15"),
        ({"hud_fps": 6.5}, "between 0.5 and 6"),
        ({"unit_fps": 13}, "between 0.5 and 12"),
        ({"recording_max_mb": 2048.0}, "integer"),
        ({"recording_max_mb": True}, "integer"),
        ({"recording_max_mb": 32}, "between 64 and 65536"),
        ({"recording_max_mb": 65537}, "between 64 and 65536"),
        ({"recording_enabled": 1}, "boolean"),
        ({"roi_auto_calibrate": "yes"}, "boolean"),
        ({"perception_preset": "low"}, "perception_preset"),
        ({"recording_jpeg_quality": 70}, "unknown"),
        ({"evidence_enabled": False}, "unknown"),
    ],
)
def test_validate_changes_rejects_instead_of_clamping(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(SettingsValidationError, match=message):
        PerceptionSettings.validate_changes(changes)


def test_instance_validate_changes_uses_instance_as_current() -> None:
    current = PerceptionSettings(preset="low_cost")
    result = current.validate_changes({"unit_fps": 6})
    assert result["perception_preset"] == "custom"
    assert result["capture_fps"] == 15.0
    assert result["unit_fps"] == 6.0


def test_fps_changes_must_not_exceed_capture() -> None:
    with pytest.raises(SettingsValidationError, match="cannot exceed capture_fps"):
        PerceptionSettings.validate_changes({"capture_fps": 9})
    with pytest.raises(SettingsValidationError, match="cannot exceed capture_fps"):
        PerceptionSettings.validate_changes(
            {"capture_fps": 7, "change_fps": 8}
        )
    accepted = PerceptionSettings.validate_changes(
        {"capture_fps": 6, "change_fps": 6, "hud_fps": 6, "unit_fps": 6}
    )
    assert accepted["capture_fps"] == 6
    assert accepted["perception_preset"] == "custom"


def test_recording_limits_accept_boundaries() -> None:
    result = PerceptionSettings.validate_changes(
        {"recording_enabled": True, "recording_max_mb": 64}
    )
    assert result == {"recording_enabled": True, "recording_max_mb": 64}
    top = PerceptionSettings.validate_changes({"recording_max_mb": 65536})
    assert top["recording_max_mb"] == 65536


def test_validate_changes_can_use_mapping_current() -> None:
    current = PerceptionSettings(preset="quality").to_dict()
    result = PerceptionSettings.validate_changes(
        {"roi_auto_calibrate": False}, current=current
    )
    assert result == {"roi_auto_calibrate": False}


# ------------------------------------------------------------------ config

def test_from_config_new_settings_win_over_legacy_perf_mode() -> None:
    settings = PerceptionSettings.from_config(
        {
            "perf_mode": "power_saver",
            "perception_preset": "quality",
            "recording_max_mb": 4096,
        }
    )
    assert settings.preset == "quality"
    assert settings.capture_fps == 30
    assert settings.recording_max_mb == 4096


def test_named_preset_with_explicit_new_capture_rate_becomes_custom() -> None:
    settings = PerceptionSettings.from_config(
        {"perception_preset": "quality", "capture_fps": 25}
    )
    assert settings.preset == "custom"
    assert settings.capture_fps == 25


def test_from_config_honours_legacy_perf_mode_only_without_new_keys() -> None:
    low = PerceptionSettings.from_config(
        {"perf_mode": "power_saver", "capture_fps": 4}
    )
    assert low.preset == "low_cost"
    assert low.capture_fps == 15

    balanced = PerceptionSettings.from_config({"perf_mode": "realtime"})
    assert balanced.preset == "balanced"
    assert balanced.capture_fps == 20


def test_from_config_advanced_rate_is_custom_and_nested_config_works() -> None:
    settings = PerceptionSettings.from_config(
        {
            "kards": {
                "perception_preset": "balanced",
                "change_fps": 6,
            }
        }
    )
    assert settings.preset == "custom"
    assert settings.change_fps == 6


def test_from_config_to_dict_round_trip() -> None:
    original = PerceptionSettings(
        preset="custom",
        capture_fps=24,
        change_fps=6,
        hud_fps=3,
        unit_fps=6,
        recording_enabled=True,
        recording_max_mb=512,
        roi_auto_calibrate=False,
    )
    payload = original.to_dict()
    assert payload["perception_preset"] == "custom"
    assert payload["recording_max_mb"] == 512
    assert payload["recording_jpeg_quality"] == 85
    assert payload["evidence_enabled"] is True
    assert set(payload) >= CONFIG_KEYS
    assert PerceptionSettings.from_config(payload) == original


def test_named_preset_round_trip_stays_named() -> None:
    original = PerceptionSettings(preset="quality")
    assert PerceptionSettings.from_config(original.to_dict()) == original


def test_from_config_rejects_bad_types() -> None:
    with pytest.raises(SettingsValidationError):
        PerceptionSettings.from_config({"capture_fps": "20"})
    with pytest.raises(SettingsValidationError):
        PerceptionSettings.from_config({"recording_max_mb": 128.5})


# ----------------------------------------------------------------- scheduler

class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def set(self, value: float) -> None:
        self.value = value


def make_scheduler(settings: PerceptionSettings | None = None) -> tuple[
    PerceptionScheduler, FakeClock
]:
    clock = FakeClock()
    return PerceptionScheduler(settings, clock=clock), clock


def test_scheduler_tasks_have_independent_deadlines() -> None:
    scheduler, clock = make_scheduler()
    assert scheduler.due("change", frame_id=1) is True
    assert scheduler.due("hud", frame_id=1) is True
    assert scheduler.due("unit", frame_id=1) is True

    clock.set(0.10)
    assert scheduler.due("change", frame_id=2) is True
    assert scheduler.due("hud", frame_id=2) is False
    assert scheduler.due("unit", frame_id=2) is False

    clock.set(0.125)
    assert scheduler.due("unit", frame_id=3) is True
    assert scheduler.due("hud", frame_id=3) is False

    clock.set(0.25)
    assert scheduler.due("hud", frame_id=4) is True
    snapshot = scheduler.snapshot()
    assert snapshot["change"]["actual"] == 2
    assert snapshot["hud"]["actual"] == 2
    assert snapshot["unit"]["actual"] == 2
    assert snapshot["totals"] == {"actual": 6, "missed": 0}


def test_repeated_frame_id_does_not_advance_counts_or_deadline() -> None:
    scheduler, clock = make_scheduler()
    assert scheduler.due("change", frame_id=7) is True
    deadline = scheduler.next_deadline("change")
    clock.set(10.0)
    assert scheduler.due("change", frame_id=7) is False
    state = scheduler.snapshot()["change"]
    assert state["actual"] == 1
    assert state["missed"] == 0
    assert scheduler.next_deadline("change") == deadline

    # A genuinely new frame is served now, but there is no replay backlog.
    assert scheduler.due("change", frame_id=8) is True
    state = scheduler.snapshot()["change"]
    assert state["actual"] == 2
    assert state["missed"] == 99
    assert scheduler.next_deadline("change") == pytest.approx(10.1)


def test_scheduler_accepts_one_frame_per_task_but_not_twice() -> None:
    scheduler, _clock = make_scheduler()
    assert scheduler.due("change", frame_id="f1") is True
    assert scheduler.due("unit", frame_id="f1") is True
    assert scheduler.due("change", frame_id="f1") is False
    assert scheduler.due("unit", frame_id="f1") is False


def test_scheduler_does_not_schedule_capture_task() -> None:
    scheduler, _clock = make_scheduler()
    with pytest.raises(ValueError, match="unknown scheduler task"):
        scheduler.due("capture", frame_id=1)


def test_update_rebuilds_deadline_from_last_run_without_reset() -> None:
    scheduler, clock = make_scheduler()
    assert scheduler.due("hud", frame_id=1) is True
    assert scheduler.next_deadline("hud") == pytest.approx(0.25)

    scheduler.update(PerceptionSettings(preset="quality"))
    # HUD is 5 fps now; the old invocation remains counted.
    assert scheduler.snapshot()["hud"]["actual"] == 1
    assert scheduler.next_deadline("hud") == pytest.approx(0.2)
    clock.set(0.2)
    assert scheduler.due("hud", frame_id=2) is True


def test_mark_duration_and_snapshot_are_bounded_and_serializable() -> None:
    scheduler, _clock = make_scheduler()
    scheduler.mark_duration("hud", 0.0125)
    scheduler.mark_duration("hud", 0.02)
    for index in range(PerceptionScheduler.MAX_DURATIONS + 10):
        scheduler.mark_duration("hud", index / 1000.0)
    state = scheduler.snapshot()["hud"]
    assert len(state["durations"]) == PerceptionScheduler.MAX_DURATIONS
    assert state["durations"][-1] == pytest.approx(0.073)
    assert state["last_duration_ms"] == pytest.approx(73.0)
    assert state["average_duration_ms"] is not None
    with pytest.raises(SettingsValidationError):
        scheduler.mark_duration("hud", -1)
    with pytest.raises(ValueError):
        scheduler.mark_duration("capture", 1.0)


def test_scheduler_uses_only_injected_clock() -> None:
    scheduler, clock = make_scheduler()
    assert scheduler.due("hud", frame_id=1) is True
    clock.set(3.0)
    assert scheduler.due("hud", frame_id=2) is True
    assert scheduler.snapshot()["hud"]["actual"] == 2
