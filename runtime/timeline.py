"""Bounded, redacted runtime decision timeline."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

#: Keys whose values must never reach the timeline, at any nesting level.
SENSITIVE_DETAIL_KEYS = frozenset({"image", "pixels", "raw_ocr", "screenshot", "api_key"})
#: Maximum nesting depth kept inside ``detail`` (root mapping is depth 0).
MAX_DETAIL_DEPTH = 8
#: Maximum length of a string kept inside ``detail``.
MAX_DETAIL_STRING = 512
#: Maximum number of items kept per mapping/list/tuple/set in ``detail``.
MAX_DETAIL_ITEMS = 64
#: Maximum length of a detail mapping key.
MAX_DETAIL_KEY = 128
#: Placeholder substituted for values that are unsafe, too deep or unsupported.
TRUNCATED = "<truncated>"


def _sensitive_key(key: Any) -> bool:
    return str(key).strip().lower() in SENSITIVE_DETAIL_KEYS


def _sanitize(value: Any, depth: int) -> Any:
    """Return a bounded, JSON-friendly copy of ``value``.

    Sensitive keys are dropped recursively, containers are truncated, long
    strings are cut and unsupported objects (bytes, images, arbitrary
    instances) are replaced by their type name so no payload content leaks.
    """
    if depth > MAX_DETAIL_DEPTH:
        return TRUNCATED
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return value[:MAX_DETAIL_STRING]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_DETAIL_ITEMS:
                break
            if _sensitive_key(key):
                continue
            safe_key = str(key)[:MAX_DETAIL_KEY]
            result[safe_key] = _sanitize(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth + 1) for item in value[:MAX_DETAIL_ITEMS]]
    if isinstance(value, (set, frozenset)):
        return [_sanitize(item, depth + 1) for item in list(value)[:MAX_DETAIL_ITEMS]]
    return f"<{type(value).__name__}>"[:MAX_DETAIL_STRING]


@dataclass
class TimelineEntry:
    ts: float
    stage: str
    outcome: str
    seq: int | None = None
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "stage": self.stage,
            "outcome": self.outcome,
            "seq": self.seq,
            "reason": self.reason,
            "detail": self.detail,
        }


class RuntimeTimeline:
    """Keep only bounded metadata; never store image pixels or raw OCR."""

    def __init__(self, max_entries: int = 300) -> None:
        self._entries: deque[TimelineEntry] = deque(maxlen=max(1, int(max_entries)))
        self._dropped = 0

    def record(
        self,
        stage: str,
        outcome: str,
        *,
        ts: float,
        seq: int | None = None,
        reason: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        if len(self._entries) == self._entries.maxlen:
            self._dropped += 1
        safe_detail = _sanitize(detail if isinstance(detail, dict) else {}, 0)
        self._entries.append(
            TimelineEntry(
                ts=float(ts),
                stage=str(stage),
                outcome=str(outcome),
                seq=seq,
                reason=str(reason)[:160],
                detail=safe_detail,
            )
        )

    def resize(self, max_entries: int) -> None:
        new_max = max(1, int(max_entries))
        overflow = len(self._entries) - new_max
        if overflow > 0:
            self._dropped += overflow
        items = list(self._entries)[-new_max:]
        self._entries = deque(items, maxlen=new_max)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        count = min(100, int(limit))
        if count <= 0:
            return []
        return [entry.to_dict() for entry in list(self._entries)[-count:]]

    def as_dict(self, limit: int = 20) -> dict[str, Any]:
        return {"recent": self.recent(limit), "dropped": self._dropped}
