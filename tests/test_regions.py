"""Safety contracts for HUD ROI calibration and HudReader region updates."""

from __future__ import annotations

import pytest
from PIL import Image

from kards_companion.perception.hud_reader import DEFAULT_REGIONS, HudReader
from kards_companion.perception.regions import (
    DEFAULT_ANCHOR_CENTERS,
    RegionCalibrator,
    RegionLayout,
)


def reference(name: str, index: int = 0) -> tuple[float, float]:
    return DEFAULT_ANCHOR_CENTERS[name][index]


def anchor(
    name: str,
    *,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    tx: float = 0.0,
    ty: float = 0.0,
    width: float = 80.0,
    height: float = 80.0,
    confidence: float = 0.9,
    index: int = 0,
    image_size: tuple[int, int] = (1920, 1080),
) -> dict[str, object]:
    ref_x, ref_y = reference(name, index)
    center_x = 0.5 + (ref_x - 0.5) * scale_x + tx
    center_y = 0.5 + (ref_y - 0.5) * scale_y + ty
    image_w, image_h = image_size
    return {
        "name": name,
        "bbox": (
            center_x * image_w - width / 2,
            center_y * image_h - height / 2,
            width,
            height,
        ),
        "confidence": confidence,
    }


def good_anchors(**kwargs: float) -> list[dict[str, object]]:
    return [
        anchor("end_turn_button", **kwargs),
        anchor("frontline", **kwargs),
    ]


@pytest.fixture
def frame_1080p() -> Image.Image:
    return Image.new("RGB", (1920, 1080), (4, 5, 6))


# --------------------------------------------------------------- layout basic

def test_calibrator_starts_unavailable_and_exposes_default_keys() -> None:
    calibrator = RegionCalibrator(clock=lambda: 1.0)
    snapshot = calibrator.snapshot()
    assert snapshot["state"] == "unavailable"
    assert snapshot["reason"] == "not_calibrated"
    assert snapshot["confirmed"] is False
    assert calibrator.layout is None
    assert set(calibrator.regions) == set(DEFAULT_REGIONS)


def test_no_anchors_keeps_proportional_defaults_not_proven(frame_1080p) -> None:
    calibrator = RegionCalibrator(clock=lambda: 1.0)
    layout = calibrator.calibrate(frame_1080p)
    assert layout.state == "proportional"
    assert layout.confirmed is False
    assert layout.calibration_proven is False
    assert layout.reason == "calibration_not_proven"
    assert layout.regions == {
        key: list(rects) for key, rects in DEFAULT_REGIONS.items()
    }
    assert layout.paused == frozenset()
    crop = layout.crop("my_hq", frame_1080p)
    assert crop is not None
    expected = frame_1080p.crop((1104, 670, 1325, 907))
    assert crop.size == expected.size
    assert calibrator.snapshot()["state"] == "proportional"


def test_low_confidence_anchors_are_ignored(frame_1080p) -> None:
    calibrator = RegionCalibrator(clock=lambda: 1.0)
    anchors = [
        anchor("end_turn_button", confidence=0.2),
        anchor("frontline", confidence=0.49),
    ]
    layout = calibrator.calibrate(frame_1080p, anchors=anchors)
    assert layout.state == "proportional"
    assert layout.reason == "no_reliable_anchors"
    assert layout.confirmed is False
    assert layout.anchors == ()


def test_consistent_anchors_translate_and_confirm(frame_1080p) -> None:
    calibrator = RegionCalibrator(clock=lambda: 5.0)
    layout = calibrator.calibrate(
        frame_1080p,
        anchors=good_anchors(tx=20 / 1920, ty=10 / 1080),
    )
    assert layout.state == "calibrated"
    assert layout.confirmed is True
    assert layout.calibration_proven is True
    assert layout.scale == pytest.approx(1.0, abs=0.002)
    assert layout.translation[0] == pytest.approx(20 / 1920, abs=0.002)
    assert layout.translation[1] == pytest.approx(10 / 1080, abs=0.002)
    assert layout.reason == ""
    assert len(layout.anchors) == 2
    assert all(anchor.confidence == 0.9 for anchor in layout.anchors)
    assert layout.crop("my_hq", frame_1080p) is not None
    snapshot = calibrator.snapshot()
    assert snapshot["state"] == "calibrated"
    assert snapshot["calibrated_at"] == 5.0


def test_limited_uniform_scale_is_accepted(frame_1080p) -> None:
    calibrator = RegionCalibrator(clock=lambda: 1.0)
    layout = calibrator.calibrate(
        frame_1080p, anchors=good_anchors(scale_x=1.03, scale_y=1.03)
    )
    assert layout.state == "calibrated"
    assert layout.scale == pytest.approx(1.03, abs=0.003)
    assert layout.confirmed is True


def test_scale_greater_than_five_percent_pauses_all_rois(frame_1080p) -> None:
    calibrator = RegionCalibrator(clock=lambda: 1.0)
    layout = calibrator.calibrate(
        frame_1080p, anchors=good_anchors(scale_x=1.10, scale_y=1.10)
    )
    assert layout.state == "unavailable"
    assert layout.reason == "uniform_scale_out_of_range"
    assert layout.regions == {}
    assert layout.paused == frozenset(DEFAULT_REGIONS)
    assert layout.crop("my_hq", frame_1080p) is None


def test_non_uniform_scale_is_rejected(frame_1080p) -> None:
    calibrator = RegionCalibrator(clock=lambda: 1.0)
    anchors = [
        anchor("end_turn_button", scale_x=1.03, scale_y=0.92),
        anchor("frontline", scale_x=1.03, scale_y=0.92),
    ]
    layout = calibrator.calibrate(frame_1080p, anchors=anchors)
    assert layout.state == "unavailable"
    assert layout.reason == "non_uniform_anchor_scale"
    assert layout.paused == frozenset(DEFAULT_REGIONS)
    assert layout.crop("enemy_hq", frame_1080p) is None


def test_conflicting_translations_are_not_blindly_accepted(frame_1080p) -> None:
    calibrator = RegionCalibrator(clock=lambda: 1.0)
    anchors = [
        anchor("end_turn_button", tx=0.04),
        anchor("frontline", tx=-0.04),
    ]
    layout = calibrator.calibrate(frame_1080p, anchors=anchors)
    assert layout.state == "unavailable"
    assert layout.reason in {
        "anchor_translation_conflict",
        "non_uniform_anchor_scale",
        "anchor_scale_conflict",
    }
    assert layout.regions == {}
    assert calibrator.snapshot()["state"] == "unavailable"


def test_single_reliable_anchor_is_scaled_not_confirmed(frame_1080p) -> None:
    calibrator = RegionCalibrator(clock=lambda: 1.0)
    layout = calibrator.calibrate(
        frame_1080p,
        anchors=[anchor("end_turn_button", tx=0.01, ty=0.01)],
    )
    assert layout.state == "scaled"
    assert layout.confirmed is False
    assert layout.calibration_proven is False
    assert layout.reason == "single_anchor_translation_only"
    assert layout.translation[0] == pytest.approx(0.01, abs=0.002)
    assert layout.crop("my_credits", frame_1080p) is not None


def test_hq_anchor_pair_participates_in_calibration(frame_1080p) -> None:
    calibrator = RegionCalibrator(clock=lambda: 1.0)
    anchors = [
        anchor("hq", index=0, tx=0.01),
        anchor("hq", index=1, tx=0.01),
        anchor("frontline", tx=0.01),
    ]
    layout = calibrator.calibrate(frame_1080p, anchors=anchors)
    assert layout.state == "calibrated"
    assert layout.confirmed is True
    assert len(layout.anchors) == 3
    assert layout.translation[0] == pytest.approx(0.01, abs=0.002)


def test_normalized_anchor_boxes_are_supported(frame_1080p) -> None:
    calibrator = RegionCalibrator(clock=lambda: 1.0)
    layout = calibrator.calibrate(
        frame_1080p,
        anchors=[
            {"name": "end_turn_button", "bbox": (0.90, 0.58, 0.04, 0.04),
             "confidence": 0.95},
            {"name": "frontline", "bbox": (0.46, 0.44, 0.08, 0.02),
             "confidence": 0.95},
        ],
    )
    # These synthetic boxes are near the reference; what matters here is that
    # normalized input is parsed rather than silently discarded.
    assert layout.state == "calibrated"
    assert len(layout.anchors) == 2


def test_resolution_change_invalidates_transform_immediately() -> None:
    calibrator = RegionCalibrator(clock=lambda: 1.0)
    original = calibrator.calibrate(
        Image.new("RGB", (1920, 1080)),
        anchors=good_anchors(tx=0.01),
    )
    assert original.state == "calibrated"

    smaller = Image.new("RGB", (1280, 720))
    proportional = calibrator.calibrate(smaller)
    assert proportional.state == "proportional"
    assert proportional.width == 1280
    assert proportional.confirmed is False
    assert proportional.calibration_proven is False
    # The old layout refuses to crop a frame with different geometry.
    assert original.crop("my_hq", smaller) is None
    assert calibrator.snapshot()["size_invalidations"] == 1


def test_new_resolution_can_be_calibrated_independently() -> None:
    calibrator = RegionCalibrator(clock=lambda: 1.0)
    image = Image.new("RGB", (1280, 720))
    layout = calibrator.calibrate(
        image,
        anchors=good_anchors(image_size=(1280, 720)),
    )
    assert layout.state == "calibrated"
    assert layout.width == 1280 and layout.height == 720
    assert layout.confirmed is True


def test_roi_is_clamped_inside_frame(frame_1080p) -> None:
    calibrator = RegionCalibrator(clock=lambda: 1.0)
    layout = calibrator.calibrate(
        frame_1080p,
        anchors=[{
            "name": "frontline",
            "bbox": (0.95 * 1920, 0.95 * 1080, 80, 80),
            "confidence": 0.99,
        }],
    )
    assert layout.state == "scaled"
    for rects in layout.regions.values():
        for x, y, w, h in rects:
            assert 0.0 <= x <= 1.0
            assert 0.0 <= y <= 1.0
            assert x + w <= 1.0 + 1e-9
            assert y + h <= 1.0 + 1e-9
            assert w > 0 and h > 0


def test_disabled_auto_calibration_keeps_proportions(frame_1080p) -> None:
    calibrator = RegionCalibrator(enabled=False, clock=lambda: 1.0)
    layout = calibrator.calibrate(frame_1080p, anchors=good_anchors())
    assert layout.state == "proportional"
    assert layout.reason == "auto_calibration_disabled"
    assert layout.confirmed is False


def test_malformed_reliable_anchor_pauses_instead_of_guessing(frame_1080p) -> None:
    calibrator = RegionCalibrator(clock=lambda: 1.0)
    layout = calibrator.calibrate(
        frame_1080p,
        anchors=[{"name": "frontline", "bbox": (1, 2), "confidence": 0.9}],
    )
    assert layout.state == "unavailable"
    assert layout.reason == "invalid_anchor_geometry"
    assert layout.crop("my_credits", frame_1080p) is None


def test_reset_returns_to_unavailable(frame_1080p) -> None:
    calibrator = RegionCalibrator(clock=lambda: 1.0)
    assert calibrator.calibrate(
        frame_1080p, anchors=good_anchors()
    ).state == "calibrated"
    calibrator.reset()
    assert calibrator.layout is None
    assert calibrator.state == "unavailable"
    assert calibrator.snapshot()["reason"] == "not_calibrated"
    assert set(calibrator.regions) == set(DEFAULT_REGIONS)


# ------------------------------------------------------------------ HudReader

def test_hud_reader_accepts_region_layout_and_plain_mapping(frame_1080p) -> None:
    calibrator = RegionCalibrator(clock=lambda: 1.0)
    layout = calibrator.calibrate(frame_1080p, anchors=good_anchors())
    reader = HudReader(None, layout=layout)
    assert reader.regions == {
        key: list(rects) for key, rects in layout.regions.items()
    }

    plain = {"my_hq": [(0.1, 0.2, 0.3, 0.4)]}
    reader.set_regions(plain)
    assert reader.regions == plain

    # Old keyword-only construction remains valid.
    legacy = HudReader(None, regions=DEFAULT_REGIONS)
    assert set(legacy.regions) == set(DEFAULT_REGIONS)


def test_hud_reader_pauses_when_layout_is_unavailable(frame_1080p) -> None:
    calibrator = RegionCalibrator(clock=lambda: 1.0)
    unavailable = calibrator.calibrate(
        frame_1080p, anchors=good_anchors(scale_x=1.2, scale_y=1.2)
    )
    assert isinstance(unavailable, RegionLayout)
    assert unavailable.state == "unavailable"
    reader = HudReader(None, layout=unavailable)
    assert reader.regions == {}
