"""HUD 读数：HQ 血量与 Kredits 走 OCR。

区域为窗口归一化比例 (x, y, w, h)，默认值取自 1080p 实测帧，按候选顺序尝试。
HQ 卡位置在部分卡组会浮动，dry-run 校准后允许经 regions 覆盖。
parse_credits/parse_hp 与 OCR 解耦，可单测。
"""

import math
import numbers
import re
from collections.abc import Callable, Mapping
from typing import Any

from PIL import Image

DEFAULT_REGIONS: dict[str, list[tuple[float, float, float, float]]] = {
    "my_credits": [(0.015, 0.775, 0.062, 0.10)],
    "my_hq": [(0.575, 0.62, 0.115, 0.22), (0.66, 0.64, 0.10, 0.22)],
    "enemy_hq": [(0.575, 0.13, 0.115, 0.22), (0.66, 0.12, 0.10, 0.22)],
}

# Kredits 角标形如 “12K12”；艺术字体 OCR 会把 k 读成西里尔 к
_CREDIT_RE = re.compile(r"(\d{1,2})\s*[kк]\s*(\d{1,2})", re.I | re.UNICODE)
_HP_RE = re.compile(r"\d{1,2}")


def parse_credits(text: str) -> tuple[int, int] | None:
    """解析 '当前K上限' 角标。返回 (current, cap)，非法或不合 0<=current<=cap<=12 返回 None。"""
    match = _CREDIT_RE.search(text) or _CREDIT_RE.search(text.replace(" ", ""))
    if not match:
        return None
    current, cap = int(match.group(1)), int(match.group(2))
    if not 0 <= current <= cap <= 12:
        return None
    return current, cap


def _normalize_rects(
    raw: Any,
) -> list[tuple[float, float, float, float]]:
    """Validate normalized ROI, accepting one rect or a candidate list."""
    if raw is None:
        return []
    if (
        isinstance(raw, (tuple, list))
        and len(raw) == 4
        and all(isinstance(item, numbers.Real) and not isinstance(item, bool) for item in raw)
    ):
        candidates: Any = [raw]
    elif isinstance(raw, (tuple, list)):
        candidates = raw
    else:
        return []
    result: list[tuple[float, float, float, float]] = []
    for rect in candidates:
        if not isinstance(rect, (tuple, list)) or len(rect) != 4:
            continue
        if any(
            isinstance(item, bool)
            or not isinstance(item, numbers.Real)
            or not math.isfinite(float(item))
            for item in rect
        ):
            continue
        x, y, w, h = (float(item) for item in rect)
        if w <= 0.0 or h <= 0.0:
            continue
        # Clip to the frame; a malformed out-of-frame candidate is not allowed
        # to make PIL crop outside the image.
        x = min(max(0.0, x), 1.0)
        y = min(max(0.0, y), 1.0)
        w = min(w, 1.0 - x)
        h = min(h, 1.0 - y)
        if w <= 0.0 or h <= 0.0:
            continue
        result.append((x, y, w, h))
    return result


def parse_hp(text: str, previous: int | None = None) -> int | None:
    """从裁剪区文本挑 HQ 血量：多候选时优先与上一读数接近者（掉血是渐进的）。"""
    numbers = [int(n) for n in _HP_RE.findall(text) if 1 <= int(n) <= 99]
    if not numbers:
        return None
    if previous is not None:
        best = min(numbers, key=lambda n: abs(n - previous))
        if abs(best - previous) <= 6:
            return best
    return max(numbers)


class HudReader:
    def __init__(
        self,
        ocr: Any,
        *,
        regions: dict[str, list[tuple[float, float, float, float]]] | None = None,
        layout: Any = None,
    ) -> None:
        self._ocr = ocr
        self._regions: dict[str, list[tuple[float, float, float, float]]] = {}
        self._prev: dict[str, int] = {}
        self.telemetry = {"calls": 0, "hits": 0}
        # ``layout`` is duck-typed (RegionLayout) so this module stays
        # independent from perception.regions and its import direction.
        self.set_regions(layout if layout is not None else (regions or DEFAULT_REGIONS))

    @property
    def regions(self) -> dict[str, list[tuple[float, float, float, float]]]:
        return {name: list(rects) for name, rects in self._regions.items()}

    def set_regions(self, regions: Any) -> "HudReader":
        """Replace normalized ROI and clear continuity state.

        Accepts a plain mapping or a RegionLayout.  An unavailable layout has
        no usable regions (it is already expressed as an empty mapping), so
        HUD reads safely pause instead of using a guessed crop.
        """
        if regions is None:
            mapping: Mapping[str, Any] = DEFAULT_REGIONS
        elif getattr(regions, "state", None) == "unavailable":
            mapping = {}
        else:
            mapping = getattr(regions, "regions", regions)
        if not isinstance(mapping, Mapping):
            raise TypeError("regions must be a mapping or a RegionLayout")
        normalized: dict[str, list[tuple[float, float, float, float]]] = {}
        for name, raw_rects in mapping.items():
            if not isinstance(name, str):
                continue
            rects = _normalize_rects(raw_rects)
            if rects:
                normalized[name] = rects
        self._regions = normalized
        self._prev.clear()
        return self

    def _ocr_crop(self, image: Image.Image, rect: tuple[float, float, float, float]) -> str:
        import numpy as np
        if self._ocr is None:
            return ""
        w, h = image.size
        x, y, rw, rh = rect
        crop = image.crop((int(x * w), int(y * h), int((x + rw) * w), int((y + rh) * h)))
        crop = crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)
        try:
            result, _ = self._ocr(np.asarray(crop.convert("RGB")))
        except Exception:
            return ""
        lines = (result[0] if isinstance(result, tuple) else result) or []
        return " ".join(str(item[1]) for item in lines)

    def _read_number(self, image: Image.Image, name: str,
                     parser: Callable[[str, int | None], int | None]) -> int | None:
        previous = self._prev.get(name)
        for rect in self._regions.get(name, []):
            value = parser(self._ocr_crop(image, rect), previous)
            if value is not None:
                self._prev[name] = value
                return value
        return None

    def read(self, image: Image.Image) -> dict[str, Any]:
        """一次对局 HUD 读数；缺字段为 None，tracker 侧自动跳过比较。"""
        self.telemetry["calls"] += 1
        out: dict[str, Any] = {
            "my_hq": self._read_number(image, "my_hq", parse_hp),
            "enemy_hq": self._read_number(image, "enemy_hq", parse_hp),
        }
        credits = None
        for rect in self._regions.get("my_credits", []):
            credits = parse_credits(self._ocr_crop(image, rect))
            if credits is not None:
                break
        out["my_credits"], out["my_credit_cap"] = credits if credits else (None, None)
        self.telemetry["hits"] += sum(v is not None for k, v in out.items() if k.endswith("hq"))
        return out

    def reset(self) -> None:
        self._prev.clear()
