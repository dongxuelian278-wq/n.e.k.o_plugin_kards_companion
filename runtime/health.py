"""Pure capture-health contracts and transitions.

This module intentionally has no NEKO or image dependencies. A frame is only
trusted when the target window is identified and the frame timestamp is fresh.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

CaptureHealth: type[str] = str
CAPTURE_HEALTH_VALUES: Final[frozenset[str]] = frozenset(
    {
        "disabled",
        "process_not_found",
        "window_not_found",
        "minimized",
        "frame_unavailable",
        "stale_frame",
        "capturing",
        "degraded",
        "stopped",
    }
)


@dataclass(frozen=True)
class CaptureHealthSnapshot:
    """Public, serializable capture status."""

    status: str = "stopped"
    reason: str = ""
    reading: bool = False
    backend: str = ""
    frame_id: int = 0
    frame_age_ms: float | None = None
    width: int = 0
    height: int = 0
    hwnd: int | None = None
    pid: int | None = None
    process_name: str = ""
    window_title: str = ""
    foreground: bool = False
    visible: bool = False
    minimized: bool = False
    occluded: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "reading": self.reading,
            "backend": self.backend,
            "frame_id": self.frame_id,
            "frame_age_ms": self.frame_age_ms,
            "width": self.width,
            "height": self.height,
            "hwnd": self.hwnd,
            "pid": self.pid,
            "process_name": self.process_name,
            "window_title": self.window_title,
            "foreground": self.foreground,
            "visible": self.visible,
            "minimized": self.minimized,
            "occluded": self.occluded,
        }


def evaluate_capture_health(
    *,
    process_found: bool,
    window_found: bool,
    visible: bool,
    minimized: bool,
    frame_available: bool,
    frame_age_ms: float | None,
    max_age_ms: float,
    backend: str = "",
    degraded: bool = False,
    reason: str = "",
) -> str:
    """Return the only health state allowed to drive recognition."""
    if not process_found:
        return "process_not_found"
    if not window_found:
        return "window_not_found"
    if minimized:
        return "minimized"
    if not visible:
        return "frame_unavailable"
    if not frame_available:
        return "frame_unavailable"
    if frame_age_ms is None or frame_age_ms > max(0.0, float(max_age_ms)):
        return "stale_frame"
    if degraded or backend in {"imagegrab", "fallback"}:
        return "degraded"
    return "capturing"
