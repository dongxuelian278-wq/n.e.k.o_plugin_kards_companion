"""HUD ROI calibration from a small set of reliable object anchors.

The default ROI table lives in :mod:`perception.hud_reader` and is expressed in
1920x1080-relative coordinates.  This module can refine that table when a
detector supplies reliable *object* boxes (``frontline``, ``end_turn_button``
or ``hq``).  It never runs OCR itself, never hashes pixels to declare success,
and never inspects the whole frame for anchors each tick.

Safety rules
------------
* No anchors -> proportional default ROI.  State is ``proportional`` and the
  layout is explicitly not confirmed; existing HUD regions remain usable.
* A reliable anchor set may apply translation and at most 5% uniform scale.
* Non-uniform scale, inconsistent translations and out-of-range scale produce
  an ``unavailable`` layout with every ROI paused instead of a guessed crop.
* The layout records the frame size.  Reusing it on a different frame size is
  refused; a size change immediately clears the previous transform.
"""

from __future__ import annotations

import math
import numbers
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from PIL import Image

from .hud_reader import DEFAULT_REGIONS

Rect = tuple[float, float, float, float]
REGION_STATES: frozenset[str] = frozenset(
    {"proportional", "scaled", "calibrated", "unavailable"}
)

BASE_WIDTH = 1920
BASE_HEIGHT = 1080
MIN_ANCHOR_CONFIDENCE = 0.5
MAX_UNIFORM_SCALE_DELTA = 0.05
SCALE_CONSISTENCY_TOLERANCE = 0.025
TRANSLATION_TOLERANCE = 0.025
MIN_PAIR_DISTANCE_PX = 48.0

# Reference centers in the 1920x1080 default coordinate system.  The hq list
# is ordered enemy first, my HQ second (top to bottom), matching the HUD.
DEFAULT_ANCHOR_CENTERS: dict[str, tuple[tuple[float, float], ...]] = {
    "frontline": ((0.500, 0.465),),
    "end_turn_button": ((0.925, 0.620),),
    "hq": ((0.633, 0.240), (0.633, 0.730)),
}
ANCHOR_NAMES: frozenset[str] = frozenset(DEFAULT_ANCHOR_CENTERS)

_ANCHOR_ALIASES = {
    "frontline": "frontline",
    "front_line": "frontline",
    "front-line": "frontline",
    "end_turn_button": "end_turn_button",
    "endturnbutton": "end_turn_button",
    "end_turn": "end_turn_button",
    "endturn": "end_turn_button",
    "hq": "hq",
    "hq_card": "hq",
    "hqcard": "hq",
}


@dataclass(frozen=True)
class AnchorEvidence:
    """One anchor accepted into a layout (bbox already normalized)."""

    name: str
    bbox: Rect
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "bbox": self.bbox,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class RegionLayout:
    """Immutable ROI table tied to one concrete input frame size."""

    width: int
    height: int
    state: str
    regions: dict[str, list[Rect]]
    confirmed: bool
    reason: str = ""
    paused: frozenset[str] = frozenset()
    anchors: tuple[AnchorEvidence, ...] = ()
    scale: float = 1.0
    translation: tuple[float, float] = (0.0, 0.0)
    calibrated_at: float | None = None
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if self.state not in REGION_STATES:
            raise ValueError(f"unknown region layout state: {self.state!r}")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("layout width/height must be positive")
        if not (0.0 <= self.scale <= 2.0):
            raise ValueError("layout scale is out of range")

    @property
    def calibration_proven(self) -> bool:
        return bool(self.confirmed and self.state == "calibrated")

    @property
    def available(self) -> bool:
        return self.state != "unavailable" and bool(self.regions)

    def region_rects(self, name: str) -> list[Rect]:
        return list(self.regions.get(name, ()))

    def crop(self, name: str, image: Image.Image, index: int = 0) -> Image.Image | None:
        """Crop one ROI from an image of exactly this layout's size.

        A mismatched image size means the calibration belongs to an old frame
        geometry and is refused rather than rescaled blindly.  Unavailable or
        paused ROI return ``None``.
        """
        if image.size != (self.width, self.height):
            return None
        if self.state == "unavailable" or name in self.paused:
            return None
        rects = self.regions.get(name)
        if not rects:
            return None
        if index < 0 or index >= len(rects):
            return None
        x, y, w, h = rects[index]
        left = max(0, min(self.width, int(round(x * self.width))))
        top = max(0, min(self.height, int(round(y * self.height))))
        right = max(0, min(self.width, int(round((x + w) * self.width))))
        bottom = max(0, min(self.height, int(round((y + h) * self.height))))
        if right <= left or bottom <= top:
            return None
        return image.crop((left, top, right, bottom))

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "state": self.state,
            "confirmed": self.confirmed,
            "calibration_proven": self.calibration_proven,
            "reason": self.reason,
            "paused": sorted(self.paused),
            "regions": {
                key: [list(rect) for rect in rects]
                for key, rects in self.regions.items()
            },
            "anchors": [anchor.to_dict() for anchor in self.anchors],
            "scale": self.scale,
            "translation": list(self.translation),
            "confidence": self.confidence,
            "calibrated_at": self.calibrated_at,
        }


@dataclass
class _ReliableAnchor:
    name: str
    bbox: Rect
    confidence: float
    center: tuple[float, float]


class RegionCalibrator:
    """Compute safe HUD ROI from default proportions and object anchors."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        min_confidence: float = MIN_ANCHOR_CONFIDENCE,
        base_size: tuple[int, int] = (BASE_WIDTH, BASE_HEIGHT),
        clock: Any = time.monotonic,
    ) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        threshold = float(min_confidence)
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            raise ValueError("min_confidence must be finite and within 0..1")
        width, height = int(base_size[0]), int(base_size[1])
        if width <= 0 or height <= 0:
            raise ValueError("base_size must be positive")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._enabled = enabled
        self._min_confidence = threshold
        self._base_size = (width, height)
        self._clock = clock
        self._layout: RegionLayout | None = None
        self._last_size: tuple[int, int] | None = None
        self._size_invalidations = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def layout(self) -> RegionLayout | None:
        return self._layout

    @property
    def state(self) -> str:
        return "unavailable" if self._layout is None else self._layout.state

    @property
    def regions(self) -> dict[str, list[Rect]]:
        """ROI keyed exactly like :data:`hud_reader.DEFAULT_REGIONS`.

        Before the first calibration the proportional default table is
        returned for compatibility, but :attr:`state` is still ``unavailable``
        and no calibration is claimed.
        """
        source = self._layout.regions if self._layout is not None else DEFAULT_REGIONS
        return {key: list(rects) for key, rects in source.items()}

    def calibrate(
        self,
        image: Image.Image,
        *,
        anchors: Any = None,
        now: float | None = None,
    ) -> RegionLayout:
        size = self._image_size(image)
        width, height = size
        if self._last_size is not None and size != self._last_size:
            # Geometry changed: no old translation/scale may leak into the new
            # frame, even if anchors are absent on this call.
            self._layout = None
            self._size_invalidations += 1
        self._last_size = size
        timestamp = self._time(now)

        if not self._enabled:
            self._layout = self._proportional(
                size, "auto_calibration_disabled", timestamp, confirmed=False
            )
            return self._layout

        if anchors is None:
            self._layout = self._proportional(
                size, "calibration_not_proven", timestamp, confirmed=False
            )
            return self._layout

        parsed, malformed = self._parse_anchors(anchors, width, height)
        if malformed:
            self._layout = self._unavailable(size, malformed, timestamp)
            return self._layout
        if not parsed:
            self._layout = self._proportional(
                size, "no_reliable_anchors", timestamp, confirmed=False
            )
            return self._layout

        layout = self._layout_from_anchors(parsed, size, timestamp)
        self._layout = layout
        return layout

    def reset(self) -> None:
        self._layout = None
        self._last_size = None

    def snapshot(self) -> dict[str, Any]:
        if self._layout is None:
            return {
                "state": "unavailable",
                "confirmed": False,
                "calibration_proven": False,
                "reason": "not_calibrated",
                "width": 0,
                "height": 0,
                "regions": {},
                "paused": sorted(DEFAULT_REGIONS),
                "anchors": [],
                "scale": 1.0,
                "translation": [0.0, 0.0],
                "confidence": 0.0,
                "calibrated_at": None,
                "size_invalidations": self._size_invalidations,
            }
        payload = self._layout.to_dict()
        payload["size_invalidations"] = self._size_invalidations
        return payload

    # ------------------------------------------------------------- internals

    @staticmethod
    def _image_size(image: Image.Image) -> tuple[int, int]:
        try:
            width, height = image.size
            width, height = int(width), int(height)
        except Exception as exc:
            raise ValueError("image must expose a positive size") from exc
        if width <= 0 or height <= 0:
            raise ValueError("image size must be positive")
        return width, height

    def _time(self, now: float | None) -> float:
        value = float(self._clock() if now is None else now)
        if not math.isfinite(value):
            raise ValueError("now must be finite")
        return value

    def _parse_anchors(
        self, anchors: Any, width: int, height: int
    ) -> tuple[list[_ReliableAnchor], str]:
        raw_items: Iterable[Any]
        if isinstance(anchors, Mapping):
            raw_items = [{"name": name, "bbox": bbox} for name, bbox in anchors.items()]
        elif isinstance(anchors, Iterable) and not isinstance(anchors, (str, bytes)):
            raw_items = anchors
        else:
            return [], "invalid_anchor_container"

        grouped: dict[str, list[_ReliableAnchor]] = {}
        for item in raw_items:
            name: Any
            bbox: Any
            confidence: Any = 1.0
            if isinstance(item, Mapping):
                name = item.get("name")
                bbox = item.get("bbox")
                confidence = item.get("confidence", 1.0)
            else:
                name = getattr(item, "name", None)
                bbox = getattr(item, "bbox", None)
                confidence = getattr(item, "confidence", 1.0)
                if bbox is None:
                    x1 = getattr(item, "x1", None)
                    y1 = getattr(item, "y1", None)
                    x2 = getattr(item, "x2", None)
                    y2 = getattr(item, "y2", None)
                    if all(
                        isinstance(value, numbers.Real)
                        and not isinstance(value, bool)
                        for value in (x1, y1, x2, y2)
                    ):
                        bbox = (x1, y1, float(x2) - float(x1), float(y2) - float(y1))
            if not isinstance(name, str):
                continue
            normalized_name = _ANCHOR_ALIASES.get(
                "_".join(name.strip().lower().split()).replace("-", "_")
            )
            if normalized_name is None:
                continue
            conf = _confidence(confidence)
            if conf is None:
                continue
            if conf < self._min_confidence:
                # Low-confidence detections are ignored; they must not move a
                # working layout or turn into a "calibration" success.
                continue
            rect = _bbox(bbox, width, height)
            if rect is None:
                return [], "invalid_anchor_geometry"
            center = (rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0)
            if not (0.0 <= center[0] <= 1.0 and 0.0 <= center[1] <= 1.0):
                return [], "anchor_center_outside_frame"
            grouped.setdefault(normalized_name, []).append(
                _ReliableAnchor(normalized_name, rect, conf, center)
            )

        selected: list[_ReliableAnchor] = []
        for name, items in grouped.items():
            items.sort(key=lambda anchor: -anchor.confidence)
            if name == "hq" and len(items) > 1:
                # The detector can legitimately provide both HQ boxes.  Pair
                # enemy/my by vertical order instead of treating them as a
                # conflict.  More than two are reduced to the two most
                # confident detections.
                top_two = sorted(items[:2], key=lambda anchor: anchor.center[1])
                selected.extend(top_two)
            else:
                selected.append(items[0])
        return selected, ""

    def _layout_from_anchors(
        self,
        anchors: list[_ReliableAnchor],
        size: tuple[int, int],
        timestamp: float,
    ) -> RegionLayout:
        width, height = size
        points = self._paired_reference_points(anchors, width, height)
        if not points:
            return self._unavailable(size, "anchor_pairing_failed", timestamp)

        scale_result = _estimate_scale(points)
        if isinstance(scale_result, str):
            return self._unavailable(size, scale_result, timestamp)
        scale, pair_scales = scale_result

        if len(points) >= 2 and abs(scale - 1.0) > MAX_UNIFORM_SCALE_DELTA:
            return self._unavailable(size, "uniform_scale_out_of_range", timestamp)
        if pair_scales and (
            max(pair_scales) - min(pair_scales) > SCALE_CONSISTENCY_TOLERANCE
        ):
            return self._unavailable(size, "anchor_scale_conflict", timestamp)

        translations: list[tuple[float, float]] = []
        center_x, center_y = 0.5, 0.5
        for ref, observed in points:
            translated_x = (
                center_x + (ref[0] - center_x) * scale
            )
            translated_y = (
                center_y + (ref[1] - center_y) * scale
            )
            translations.append(
                (observed[0] - translated_x, observed[1] - translated_y)
            )
        tx_values = [value[0] for value in translations]
        ty_values = [value[1] for value in translations]
        if (
            max(tx_values) - min(tx_values) > TRANSLATION_TOLERANCE
            or max(ty_values) - min(ty_values) > TRANSLATION_TOLERANCE
        ):
            return self._unavailable(size, "anchor_translation_conflict", timestamp)

        tx = _median(tx_values)
        ty = _median(ty_values)
        regions = _transform_regions(DEFAULT_REGIONS, scale, tx, ty)
        confidence = min(anchor.confidence for anchor in anchors)
        if len(points) >= 2:
            state = "calibrated"
            confirmed = True
            reason = ""
        else:
            # One anchor can only justify a translation.  Keep the result
            # usable but do not call it proven calibration.
            state = "scaled"
            confirmed = False
            reason = "single_anchor_translation_only"
        return RegionLayout(
            width=width,
            height=height,
            state=state,
            regions=regions,
            confirmed=confirmed,
            reason=reason,
            paused=frozenset(),
            anchors=tuple(
                AnchorEvidence(anchor.name, anchor.bbox, anchor.confidence)
                for anchor in anchors
            ),
            scale=scale,
            translation=(tx, ty),
            calibrated_at=timestamp,
            confidence=confidence,
        )

    def _paired_reference_points(
        self,
        anchors: list[_ReliableAnchor],
        width: int,
        height: int,
    ) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        points: list[tuple[tuple[float, float], tuple[float, float]]] = []
        hq = [anchor for anchor in anchors if anchor.name == "hq"]
        if hq:
            expected = list(DEFAULT_ANCHOR_CENTERS["hq"])
            observed = sorted(hq, key=lambda anchor: anchor.center[1])
            if len(observed) == 1:
                # A lone HQ box is matched to the nearest expected band; its
                # center still participates in translation/conflict checks.
                expected = [
                    min(expected, key=lambda ref: abs(ref[1] - observed[0].center[1]))
                ]
            for ref, anchor in zip(expected, observed):
                points.append((ref, anchor.center))
        for anchor in anchors:
            if anchor.name == "hq":
                continue
            refs = DEFAULT_ANCHOR_CENTERS.get(anchor.name)
            if refs:
                points.append((refs[0], anchor.center))
        return points

    def _proportional(
        self,
        size: tuple[int, int],
        reason: str,
        timestamp: float,
        *,
        confirmed: bool,
    ) -> RegionLayout:
        return RegionLayout(
            width=size[0],
            height=size[1],
            state="proportional",
            regions={key: list(rects) for key, rects in DEFAULT_REGIONS.items()},
            confirmed=confirmed,
            reason=reason,
            paused=frozenset(),
            calibrated_at=timestamp,
        )

    def _unavailable(
        self, size: tuple[int, int], reason: str, timestamp: float
    ) -> RegionLayout:
        return RegionLayout(
            width=size[0],
            height=size[1],
            state="unavailable",
            regions={},
            confirmed=False,
            reason=reason,
            paused=frozenset(DEFAULT_REGIONS),
            calibrated_at=timestamp,
        )


def _confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0.0 or result > 1.0:
        return None
    return result


def _bbox(value: Any, width: int, height: int) -> Rect | None:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        return None
    if len(value) != 4:
        return None
    if any(isinstance(item, bool) or not isinstance(item, numbers.Real) for item in value):
        return None
    x, y, w, h = (float(item) for item in value)
    if not all(math.isfinite(item) for item in (x, y, w, h)):
        return None
    if w <= 0.0 or h <= 0.0:
        return None
    # Accept normalized boxes from callers too.  A 1920x1080 pixel box cannot
    # be mistaken for one because its width/height exceed 1.
    if x >= 0.0 and y >= 0.0 and x + w <= 1.0 + 1e-9 and y + h <= 1.0 + 1e-9:
        return (x, y, w, h)
    x1 = max(0.0, min(float(width), x))
    y1 = max(0.0, min(float(height), y))
    x2 = max(0.0, min(float(width), x + w))
    y2 = max(0.0, min(float(height), y + h))
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1 / width, y1 / height, (x2 - x1) / width, (y2 - y1) / height)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _estimate_scale(
    points: list[tuple[tuple[float, float], tuple[float, float]]],
) -> tuple[float, list[float]] | str:
    if len(points) <= 1:
        return 1.0, []

    measures: list[float] = []
    for index, (ref_a, observed_a) in enumerate(points):
        for ref_b, observed_b in points[index + 1:]:
            # Both reference and observations are normalized; distance in an
            # isotropic coordinate system is enough.  Using width for x and
            # height for y also catches non-uniform desktop scaling.
            dx_ref = (ref_b[0] - ref_a[0]) * 1920.0
            dy_ref = (ref_b[1] - ref_a[1]) * 1080.0
            dx_obs = (observed_b[0] - observed_a[0]) * 1920.0
            dy_obs = (observed_b[1] - observed_a[1]) * 1080.0
            ref_distance = math.hypot(dx_ref, dy_ref)
            if ref_distance < MIN_PAIR_DISTANCE_PX:
                continue
            obs_distance = math.hypot(dx_obs, dy_obs)
            ratio = obs_distance / ref_distance
            if not math.isfinite(ratio) or ratio <= 0.0:
                return "invalid_anchor_scale"
            measures.append(ratio)

            if abs(dx_ref) >= 16.0 and abs(dy_ref) >= 16.0:
                sx = dx_obs / dx_ref
                sy = dy_obs / dy_ref
                if not (math.isfinite(sx) and math.isfinite(sy)):
                    return "invalid_anchor_scale"
                if abs(sx - sy) > SCALE_CONSISTENCY_TOLERANCE:
                    return "non_uniform_anchor_scale"

    if not measures:
        return "anchors_too_close"
    return _median(measures), measures


def _transform_regions(
    regions: Mapping[str, list[Rect]],
    scale: float,
    tx: float,
    ty: float,
) -> dict[str, list[Rect]]:
    transformed: dict[str, list[Rect]] = {}
    for name, rects in regions.items():
        transformed[name] = [
            _transform_rect(rect, scale, tx, ty) for rect in rects
        ]
    return transformed


def _transform_rect(rect: Rect, scale: float, tx: float, ty: float) -> Rect:
    x, y, w, h = rect
    center_x, center_y = 0.5, 0.5
    new_w = min(1.0, max(1e-6, w * scale))
    new_h = min(1.0, max(1e-6, h * scale))
    new_x = center_x + (x - center_x) * scale + tx
    new_y = center_y + (y - center_y) * scale + ty
    # Clamp by shifting, not by shrinking further, so a ROI adjacent to an
    # edge keeps its calibrated size whenever that size fits in the frame.
    new_x = min(max(0.0, new_x), 1.0 - new_w)
    new_y = min(max(0.0, new_y), 1.0 - new_h)
    return (new_x, new_y, new_w, new_h)


__all__ = [
    "ANCHOR_NAMES",
    "AnchorEvidence",
    "BASE_HEIGHT",
    "BASE_WIDTH",
    "DEFAULT_ANCHOR_CENTERS",
    "MIN_ANCHOR_CONFIDENCE",
    "REGION_STATES",
    "RegionCalibrator",
    "RegionLayout",
]
