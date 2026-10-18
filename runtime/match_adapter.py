"""KARDS MatchTracker -> CandidateEvent adapter.

The tracker owns cross-frame memory (unit identity, confirmation gates, miss
grace, crisis episodes), so it consumes only the current :class:`MatchState`;
the registry's ``previous`` snapshot is intentionally unused.  The snapshot's
``MatchState`` is passed through by reference so its clock fields
(``detected_at``), turn metadata and draft flags are never reconstructed or
dropped.

This adapter never replaces the tracker instance: ``reset()`` forwards
``reset_session()`` in place, which keeps the injected clock and any external
references valid.

Trajectory integration lives one layer below: feed
``perception.detector.UnitTracker.update_observations`` into ``MatchState.units``
(always keep its returned units, including the last-known units it returns when
``observation_valid`` is false).  This adapter exposes the resulting
``unit_diagnostics()`` and ``recent_unit_losses()``; absence is reported as
``unit_lost`` diagnostics, never as a synthetic ``unit_died`` event.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

from ..state.model import GameEvent
from ..state.tracker import MatchTracker
from .detectors import CandidateEvent
from .snapshot import RuntimeSnapshot


class MatchTrackerAdapter:
    """Expose :class:`MatchTracker` events as runtime candidate events."""

    name = "match_tracker"

    def __init__(
        self,
        tracker: MatchTracker | None = None,
        *,
        chatter_interval_s: float = 0.0,
        source: str = "match_tracker",
        ttl_s: float | None = None,
        critical_kinds: Iterable[str] = (),
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.chatter_interval_s = float(chatter_interval_s)
        self.source = source
        # Configured upper bound; the actual per-event TTL is min(spec, cap).
        self.ttl_s = None if ttl_s is None else float(ttl_s)
        # Deprecated compatibility knob: CandidateEvent derives critical from
        # the shared spec (confirmed own-side preempt), never from this set.
        self._critical_kinds = frozenset(critical_kinds)
        self._now = now_fn or getattr(tracker, "_now", time.time)
        self._tracker = tracker if tracker is not None else MatchTracker(now_fn=self._now)

    @property
    def tracker(self) -> MatchTracker:
        return self._tracker

    @property
    def game_number(self) -> int:
        return self._tracker.game_number

    @property
    def in_match(self) -> bool:
        return self._tracker.in_match

    @property
    def current(self) -> Any:
        return self._tracker.current

    @property
    def session_id(self) -> str:
        return self._tracker.session_id

    def reset(self) -> None:
        """Reset session memory in place; the tracker object is preserved."""
        self._tracker.reset_session()

    def reset_session(self) -> None:
        self._tracker.reset_session()

    def reset_match(self) -> None:
        self._tracker.reset_match()

    def rebase(self) -> None:
        self._tracker.rebase()

    def set_session_id(self, session_id: str) -> None:
        self._tracker.set_session_id(session_id)

    def mark_delivered(self, event: GameEvent) -> None:
        self._tracker.mark_delivered(event)

    def rearm_uncommitted(self, *, now: float | None = None) -> list[GameEvent]:
        return self._tracker.rearm_uncommitted(now=now)

    def current_ledger(self) -> dict[str, Any] | None:
        return self._tracker.current_ledger()

    def unit_diagnostics(self) -> dict[str, Any]:
        """Forward bounded unit entry/move/loss state to main integration."""
        return self._tracker.unit_diagnostics()

    def recent_unit_losses(self, limit: int = 20) -> list[dict[str, Any]]:
        """Observation-loss diagnostics (kind ``unit_lost``), never death."""
        return self._tracker.recent_unit_losses(limit)

    def snapshot(self) -> dict[str, Any]:
        return self._tracker.snapshot()

    def maybe_chatter(self, chatter_interval_s: float | None = None) -> GameEvent | None:
        interval = self.chatter_interval_s if chatter_interval_s is None else chatter_interval_s
        return self._tracker.maybe_chatter(interval)

    def rearm(self, *, now: float | None = None) -> list[CandidateEvent]:
        at = self._now() if now is None else float(now)
        return [
            self._candidate(event, enqueued_at=at)
            for event in self._tracker.rearm_uncommitted(now=at)
        ]

    def _candidate(self, event: GameEvent, *, enqueued_at: float) -> CandidateEvent:
        return CandidateEvent.from_event(
            event,
            source=self.source,
            enqueued_at=enqueued_at,
            configured_ttl=self.ttl_s,
        )

    def update(
        self,
        previous: RuntimeSnapshot | None,
        current: RuntimeSnapshot,
    ) -> list[CandidateEvent]:
        del previous  # tracker keeps its own cross-frame memory
        if not current.trusted:
            return []

        # Pass the snapshot state through unchanged: MatchTracker mutates
        # game_number/unit lineage in place, and MatchState's time and turn
        # fields must survive into the next snapshot.
        events: list[GameEvent] = list(self._tracker.update(current.state))
        chatter = self._tracker.maybe_chatter(self.chatter_interval_s)
        if chatter is not None:
            events.append(chatter)
        return [self._candidate(event, enqueued_at=current.observed_at) for event in events]
