"""Detector registry and candidate-event contracts."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from ..state.model import GameEvent
from .contracts import event_max_age_seconds, event_spec
from .snapshot import RuntimeSnapshot


@dataclass(frozen=True)
class CandidateEvent:
    """A detector event queued for arbitration.

    ``ttl_s`` and ``critical`` are derived from the shared event spec instead
    of caller guesses: only a confirmed own-side HQ crisis is ``preempt``.
    """

    event: GameEvent
    source: str
    enqueued_at: float
    ttl_s: float | None = None
    critical: bool = False

    def __post_init__(self) -> None:
        if self.ttl_s is None:
            ttl = event_max_age_seconds(self.event)
        else:
            ttl = event_max_age_seconds(self.event, float(self.ttl_s))
        object.__setattr__(self, "ttl_s", float(ttl))
        object.__setattr__(self, "critical", bool(event_spec(self.event).preempt))

    @classmethod
    def from_event(
        cls,
        event: GameEvent,
        *,
        source: str,
        enqueued_at: float,
        configured_ttl: float | None = None,
    ) -> "CandidateEvent":
        return cls(
            event=event,
            source=source,
            enqueued_at=float(enqueued_at),
            ttl_s=configured_ttl,
        )

    def expired(self, now: float) -> bool:
        return self.ttl_s > 0 and now - self.enqueued_at > self.ttl_s


@dataclass(frozen=True)
class DetectorDiagnostic:
    name: str
    duration_ms: float
    outcome: str
    error: str = ""


class Detector(Protocol):
    name: str

    def reset(self) -> None:
        ...

    def update(
        self,
        previous: RuntimeSnapshot | None,
        current: RuntimeSnapshot,
    ) -> list[CandidateEvent]:
        ...


class DetectorRegistry:
    """Stable-order detector fan-out with exception isolation."""

    def __init__(self, detectors: list[Detector] | None = None) -> None:
        self._detectors: list[Detector] = list(detectors or [])

    @property
    def detectors(self) -> tuple[Detector, ...]:
        return tuple(self._detectors)

    def add(self, detector: Detector) -> None:
        self._detectors.append(detector)

    def reset(self) -> None:
        for detector in self._detectors:
            try:
                detector.reset()
            except Exception:
                # One detector's reset failure must not block the rest.
                continue

    def _fanout(self, method: str, *args: Any, **kwargs: Any) -> list[Any]:
        results: list[Any] = []
        for detector in self._detectors:
            action = getattr(detector, method, None)
            if not callable(action):
                continue
            try:
                results.append(action(*args, **kwargs))
            except Exception:
                continue
        return results

    def reset_session(self) -> None:
        """Reset session memory without replacing detector instances."""
        for detector in self._detectors:
            action = getattr(detector, "reset_session", None)
            if callable(action):
                try:
                    action()
                except Exception:
                    continue
            else:
                try:
                    detector.reset()
                except Exception:
                    continue

    def reset_match(self) -> None:
        """Clear current-match memory on every detector that supports it."""
        self._fanout("reset_match")

    def rebase(self) -> None:
        self._fanout("rebase")

    def mark_delivered(self, event: GameEvent) -> None:
        """Forward the delivery acknowledgement to tracker-backed detectors."""
        self._fanout("mark_delivered", event)

    def rearm(self, *, now: float | None = None) -> list[CandidateEvent]:
        """Collect still-valid uncommitted condition events from detectors."""
        at = time.time() if now is None else float(now)
        out: list[CandidateEvent] = []
        for detector in self._detectors:
            action = getattr(detector, "rearm", None)
            try:
                if callable(action):
                    produced = action(now=at)
                    if produced:
                        out.extend(produced)
                    continue
                raw = getattr(detector, "rearm_uncommitted", None)
                if callable(raw):
                    events = raw(now=at)
                    if events:
                        out.extend(
                            CandidateEvent.from_event(
                                event,
                                source=str(getattr(detector, "name", "detector")),
                                enqueued_at=at,
                            )
                            for event in events
                        )
            except Exception:
                continue
        return out

    def update(
        self,
        previous: RuntimeSnapshot | None,
        current: RuntimeSnapshot,
    ) -> tuple[list[CandidateEvent], list[DetectorDiagnostic]]:
        if not current.trusted:
            return [], [
                DetectorDiagnostic(
                    name="registry",
                    duration_ms=0.0,
                    outcome="skipped",
                    error="capture_not_trusted",
                )
            ]
        events: list[CandidateEvent] = []
        diagnostics: list[DetectorDiagnostic] = []
        for detector in self._detectors:
            started = time.perf_counter()
            try:
                produced = detector.update(previous, current)
                events.extend(produced)
                diagnostics.append(
                    DetectorDiagnostic(
                        name=detector.name,
                        duration_ms=(time.perf_counter() - started) * 1000.0,
                        outcome="ok",
                    )
                )
            except Exception as exc:
                diagnostics.append(
                    DetectorDiagnostic(
                        name=detector.name,
                        duration_ms=(time.perf_counter() - started) * 1000.0,
                        outcome="error",
                        error=f"{type(exc).__name__}: {exc}"[:160],
                    )
                )
        return events, diagnostics
