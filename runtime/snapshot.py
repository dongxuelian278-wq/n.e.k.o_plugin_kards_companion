"""Serializable runtime snapshots used between perception and policy layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..state.model import MatchState
from .health import CaptureHealthSnapshot


@dataclass(frozen=True)
class FrameMeta:
    frame_id: int = 0
    captured_at: float = 0.0
    width: int = 0
    height: int = 0
    backend: str = ""
    valid: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "captured_at": self.captured_at,
            "width": self.width,
            "height": self.height,
            "backend": self.backend,
            "valid": self.valid,
            "reason": self.reason,
        }


@dataclass
class RuntimeSnapshot:
    """One trusted or explicitly rejected perception tick.

    ``image`` is deliberately runtime-only and is never exposed by
    ``to_public_dict`` or the diagnostic timeline.
    """

    seq: int
    observed_at: float
    health: CaptureHealthSnapshot
    frame: FrameMeta
    state: MatchState = field(default_factory=MatchState)
    image: Any = field(default=None, repr=False, compare=False)

    @property
    def trusted(self) -> bool:
        return self.health.reading and self.frame.valid and self.image is not None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "kards_runtime_snapshot_v1",
            "seq": self.seq,
            "observed_at": self.observed_at,
            "capture": self.health.to_dict(),
            "frame": self.frame.to_dict(),
            "state": self.state.to_public_dict(),
        }
