"""Frozen pure-logic event contracts shared by the KARDS backend agents.

Depends only on ``state.model``.  ``GameEvent`` is not modified; every helper
is deterministic and can be tested with constructed events.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Final, Mapping

from ..state.model import (
    E_CHATTER,
    E_HQ_CRITICAL,
    E_HQ_DAMAGE,
    E_MATCH_END,
    E_MATCH_START,
    E_ORDER_CARD,
    E_TURN_SWITCH,
    E_UNIT_ADVANCED,
    E_UNIT_DIED,
    E_UNIT_PLAYED,
    HQ_CRITICAL_HP,
    SIDE_ENEMY,
    SIDE_MINE,
    GameEvent,
)

# Scenarios -----------------------------------------------------------------
OUT_OF_BATTLE: Final[str] = "out_of_battle"
OPENING: Final[str] = "opening"
IN_MATCH: Final[str] = "in_match"
PRESSURE: Final[str] = "pressure"
CRITICAL: Final[str] = "critical"
ENDED: Final[str] = "ended"
SCENARIOS: Final[tuple[str, ...]] = (OUT_OF_BATTLE, OPENING, IN_MATCH, PRESSURE, CRITICAL, ENDED)

# Categories ----------------------------------------------------------------
CATEGORY_OPENING: Final[str] = "opening"
CATEGORY_STATE: Final[str] = "state"
CATEGORY_BATTLE: Final[str] = "battle"
CATEGORY_PRESSURE: Final[str] = "pressure"
CATEGORY_CRITICAL: Final[str] = "critical"
CATEGORY_CHATTER: Final[str] = "chatter"
CATEGORY_TERMINAL: Final[str] = "terminal"
CATEGORY_RECOVERY: Final[str] = "recovery"
CATEGORIES: Final[tuple[str, ...]] = (CATEGORY_OPENING, CATEGORY_STATE, CATEGORY_BATTLE,
    CATEGORY_PRESSURE, CATEGORY_CRITICAL, CATEGORY_CHATTER, CATEGORY_TERMINAL, CATEGORY_RECOVERY)

# Levels / War Thunder-style edges -----------------------------------------
LEVEL_INFO: Final[str] = "info"
LEVEL_NOTICE: Final[str] = "notice"
LEVEL_WARNING: Final[str] = "warning"
LEVEL_CRITICAL: Final[str] = "critical"
LEVEL_TERMINAL: Final[str] = "terminal"
LEVELS: Final[tuple[str, ...]] = (LEVEL_INFO, LEVEL_NOTICE, LEVEL_WARNING, LEVEL_CRITICAL, LEVEL_TERMINAL)

EDGE_ENTER: Final[str] = "enter"
EDGE_UPGRADE: Final[str] = "upgrade"
EDGE_RECOVERY: Final[str] = "recovery"
EDGE_STEADY: Final[str] = "steady"
EDGES: Final[tuple[str, ...]] = (EDGE_ENTER, EDGE_UPGRADE, EDGE_RECOVERY, EDGE_STEADY)

_ALLOWED: Final[dict[str, frozenset[str]]] = {
    OUT_OF_BATTLE: frozenset({CATEGORY_TERMINAL}),
    OPENING: frozenset({CATEGORY_OPENING, CATEGORY_STATE, CATEGORY_BATTLE,
                        CATEGORY_PRESSURE, CATEGORY_CRITICAL, CATEGORY_RECOVERY,
                        CATEGORY_TERMINAL}),
    IN_MATCH: frozenset(CATEGORIES),
    PRESSURE: frozenset({CATEGORY_STATE, CATEGORY_BATTLE, CATEGORY_PRESSURE,
                         CATEGORY_CRITICAL, CATEGORY_RECOVERY, CATEGORY_TERMINAL}),
    CRITICAL: frozenset({CATEGORY_PRESSURE, CATEGORY_CRITICAL, CATEGORY_RECOVERY,
                         CATEGORY_TERMINAL}),
    ENDED: frozenset({CATEGORY_TERMINAL}),
}

@dataclass(frozen=True)
class EventSpec:
    kind: str
    category: str
    priority: int
    cooldown_seconds: float
    max_age_seconds: float
    preempt: bool
    terminal: bool
    once_per_match: bool
    wants_recovery: bool

_CATALOG: Final[dict[str, EventSpec]] = {
    E_MATCH_START: EventSpec(E_MATCH_START, CATEGORY_OPENING, 5, 0.0, 4.0, False, False, True, False),
    E_TURN_SWITCH: EventSpec(E_TURN_SWITCH, CATEGORY_STATE, 4, 3.0, 6.0, False, False, False, False),
    E_HQ_DAMAGE: EventSpec(E_HQ_DAMAGE, CATEGORY_PRESSURE, 5, 8.0, 6.0, False, False, False, False),
    E_HQ_CRITICAL: EventSpec(E_HQ_CRITICAL, CATEGORY_CRITICAL, 8, 5.0, 6.0, True, False, False, True),
    E_MATCH_END: EventSpec(E_MATCH_END, CATEGORY_TERMINAL, 9, 0.0, 8.0, False, True, True, False),
    E_UNIT_PLAYED: EventSpec(E_UNIT_PLAYED, CATEGORY_BATTLE, 5, 3.0, 6.0, False, False, False, False),
    E_UNIT_DIED: EventSpec(E_UNIT_DIED, CATEGORY_BATTLE, 6, 4.0, 6.0, False, False, False, False),
    E_UNIT_ADVANCED: EventSpec(E_UNIT_ADVANCED, CATEGORY_BATTLE, 4, 3.0, 5.0, False, False, False, False),
    E_ORDER_CARD: EventSpec(E_ORDER_CARD, CATEGORY_BATTLE, 5, 6.0, 6.0, False, False, False, False),
    E_CHATTER: EventSpec(E_CHATTER, CATEGORY_CHATTER, 5, 0.0, 6.0, False, False, False, False),
}


def _details(event: Any) -> Mapping[str, Any]:
    value = getattr(event, "details", None)
    return value if isinstance(value, Mapping) else {}


def _side(event: Any) -> str:
    value = str(_details(event).get("side") or "").strip().lower()
    return value if value in (SIDE_MINE, SIDE_ENEMY) else ""


def _explicit_edge(event: Any) -> str:
    value = str(_details(event).get("edge") or "").strip().lower()
    return value if value in EDGES else ""


def _infer_category(kind: str) -> str:
    if kind == E_MATCH_END:
        return CATEGORY_TERMINAL
    if kind == E_CHATTER:
        return CATEGORY_CHATTER
    if kind == E_HQ_CRITICAL:
        return CATEGORY_CRITICAL
    if kind == E_HQ_DAMAGE:
        return CATEGORY_PRESSURE
    if kind in (E_UNIT_PLAYED, E_UNIT_DIED, E_UNIT_ADVANCED, E_ORDER_CARD):
        return CATEGORY_BATTLE
    if kind == E_MATCH_START:
        return CATEGORY_OPENING
    return CATEGORY_STATE


def event_spec(event: GameEvent) -> EventSpec:
    """Canonical spec; KARDS side/edge overrides are applied here."""
    kind = str(getattr(event, "kind", "") or "").strip()
    side = _side(event)
    base = _CATALOG.get(kind)
    if base is None:
        category = _infer_category(kind)
        try:
            priority = int(getattr(event, "priority", 5) or 5)
        except (TypeError, ValueError):
            priority = 5
        priority = min(9, max(1, priority))
        terminal = category == CATEGORY_TERMINAL
        return EventSpec(kind, category, priority, 0.0, 0.0,
                         False, terminal, terminal, False)
    if kind == E_HQ_DAMAGE:
        if side == SIDE_MINE:
            return replace(base, priority=7)
        if side == SIDE_ENEMY:
            return replace(base, category=CATEGORY_BATTLE, priority=5)
        return replace(base, priority=6)
    if kind == E_HQ_CRITICAL:
        if event_edge(event) == EDGE_RECOVERY:
            return replace(base, category=CATEGORY_RECOVERY, priority=4,
                           preempt=False, wants_recovery=False)
        if side == SIDE_MINE:
            return replace(base, category=CATEGORY_CRITICAL, priority=8,
                           preempt=True, wants_recovery=True)
        if side == SIDE_ENEMY:
            return replace(base, category=CATEGORY_BATTLE, priority=6,
                           preempt=False, wants_recovery=False)
        # Unconfirmed side can never become own-side critical.
        return replace(base, category=CATEGORY_PRESSURE, priority=6,
                       preempt=False, wants_recovery=False)
    return base


def event_level(event: GameEvent) -> str:
    """Delivery level.  Only a confirmed own-side HQ crisis is critical."""
    explicit = str(_details(event).get("level") or "").strip().lower()
    kind = str(getattr(event, "kind", "") or "").strip()
    side = _side(event)
    if kind == E_HQ_CRITICAL and event_edge(event) == EDGE_RECOVERY:
        return LEVEL_NOTICE
    if explicit in LEVELS:
        if (kind == E_HQ_CRITICAL and side != SIDE_MINE
                and explicit == LEVEL_CRITICAL):
            return LEVEL_WARNING
        return explicit
    if kind == E_HQ_CRITICAL:
        if side == SIDE_MINE:
            return LEVEL_CRITICAL
        # Enemy crisis is good news; unknown side is unconfirmed.
        return LEVEL_WARNING
    if kind == E_HQ_DAMAGE:
        return LEVEL_WARNING if side == SIDE_MINE else LEVEL_NOTICE
    if kind == E_MATCH_END:
        return LEVEL_TERMINAL
    if kind == E_UNIT_DIED:
        return LEVEL_WARNING if side == SIDE_MINE else LEVEL_NOTICE
    if kind == E_UNIT_ADVANCED:
        return LEVEL_WARNING if side == SIDE_ENEMY else LEVEL_NOTICE
    if kind == E_ORDER_CARD:
        return LEVEL_WARNING if side == SIDE_ENEMY else LEVEL_NOTICE
    if kind == E_UNIT_PLAYED:
        return LEVEL_NOTICE if side == SIDE_ENEMY else LEVEL_INFO
    if kind in (E_MATCH_START, E_TURN_SWITCH, E_CHATTER):
        return LEVEL_INFO
    spec = event_spec(event)
    if spec.category == CATEGORY_TERMINAL:
        return LEVEL_TERMINAL
    if spec.category == CATEGORY_CRITICAL and side == SIDE_MINE:
        return LEVEL_CRITICAL
    if spec.category in (CATEGORY_PRESSURE, CATEGORY_BATTLE):
        return LEVEL_WARNING
    return LEVEL_NOTICE


def event_edge(event: GameEvent) -> str:
    """War Thunder-style condition edge: enter/upgrade/recovery/steady."""
    explicit = _explicit_edge(event)
    if explicit:
        return explicit
    kind = str(getattr(event, "kind", "") or "").strip()
    side = _side(event)
    details = _details(event)
    if kind == E_MATCH_START:
        return EDGE_ENTER
    if kind == E_HQ_CRITICAL:
        if side not in (SIDE_MINE, SIDE_ENEMY):
            return EDGE_STEADY
        try:
            hp = int(details.get("hp"))
        except (TypeError, ValueError):
            hp = None
        if hp is not None and hp > HQ_CRITICAL_HP:
            return EDGE_RECOVERY
        try:
            band = int(details.get("band") or 0)
        except (TypeError, ValueError):
            band = 0
        if band >= 2:
            return EDGE_UPGRADE
        return EDGE_ENTER
    if kind == E_UNIT_PLAYED:
        return EDGE_ENTER
    if kind == E_UNIT_ADVANCED:
        return EDGE_UPGRADE
    if kind in (E_HQ_DAMAGE, E_UNIT_DIED):
        return EDGE_STEADY
    try:
        band = int(details.get("band") or 0)
    except (TypeError, ValueError):
        band = 0
    if band >= 2:
        return EDGE_UPGRADE
    if band == 1:
        return EDGE_ENTER
    return EDGE_STEADY


def _component(value: Any, default: str = "-") -> str:
    if value is None:
        return default
    text = " ".join(str(value).replace("\x00", " ").split())
    text = text.replace("|", "_")[:64].strip()
    return text or default


def event_identity(event: GameEvent) -> str:
    """``session|game|kind|side|entity|turn|band|edge|occurrence|episode``.

    ``entity`` prefers a stable id.  ``occurrence`` disambiguates two real
    same-turn events; ``episode`` identifies a continuous condition episode.
    Both are supplied by the tracker at the source.
    """
    d = _details(event)
    entity = "-"
    for key in ("entity", "entity_id", "unit_id", "instance_id"):
        if d.get(key) is not None and str(d[key]).strip():
            entity = _component(d[key])
            break
    occurrence = d.get("occurrence", d.get("seq"))
    episode = d.get("episode", d.get("condition_episode"))
    return "|".join((
        _component(d.get("session_id")),
        _component(d.get("game", d.get("game_number", 0)), "0"),
        _component(getattr(event, "kind", "")),
        _component(d.get("side")),
        entity,
        _component(d.get("turn", d.get("turn_number"))),
        _component(d.get("band", d.get("crisis_band"))),
        _component(event_edge(event)),
        _component(occurrence),
        _component(episode),
    ))


def event_max_age_seconds(event: GameEvent, configured: float = 12.0) -> float:
    """Strict freshness: ordinary and terminal both use min(spec, configured).

    ``configured <= 0`` falls back to the spec age.  There is no terminal
    extension and no re-reporting of a stale final result.
    """
    spec_age = float(event_spec(event).max_age_seconds)
    if not math.isfinite(spec_age) or spec_age < 0:
        spec_age = 0.0
    try:
        default = float(configured)
    except (TypeError, ValueError):
        default = 0.0
    if not math.isfinite(default) or default <= 0:
        return spec_age
    if spec_age <= 0:
        return default
    return min(spec_age, default)


def category_allowed(scenario: str, category: str) -> bool:
    """Scenario/category selection gate; never a privacy or stop gate."""
    category = str(category or "").strip().lower()
    allowed = _ALLOWED.get(str(scenario or "").strip().lower())
    if allowed is None:
        return category == CATEGORY_TERMINAL
    return category in allowed


def normalize_event(event: GameEvent, *, session_id: str = "") -> GameEvent:
    """Return a canonical copy (canonical priority, details, identity).

    Does not mutate the input.  Idempotent.
    """
    details = dict(_details(event))
    if str(session_id or "").strip():
        details["session_id"] = _component(session_id, "")
    else:
        details["session_id"] = _component(details.get("session_id"), "")

    spec = event_spec(event)
    details["category"] = spec.category
    details["level"] = event_level(event)
    details["edge"] = event_edge(event)
    details["max_age_seconds"] = event_max_age_seconds(event)
    details.pop("identity", None)
    staged = replace(event, priority=spec.priority, details=details)
    details["identity"] = event_identity(staged)
    return replace(staged, details=details)


__all__ = [
    "CATEGORIES", "CATEGORY_BATTLE", "CATEGORY_CHATTER", "CATEGORY_CRITICAL",
    "CATEGORY_OPENING", "CATEGORY_PRESSURE", "CATEGORY_RECOVERY", "CATEGORY_STATE",
    "CATEGORY_TERMINAL", "CRITICAL", "EDGES", "EDGE_ENTER", "EDGE_RECOVERY",
    "EDGE_STEADY", "EDGE_UPGRADE", "ENDED", "EventSpec", "IN_MATCH", "LEVELS",
    "LEVEL_CRITICAL", "LEVEL_INFO", "LEVEL_NOTICE", "LEVEL_TERMINAL",
    "LEVEL_WARNING", "OPENING", "OUT_OF_BATTLE", "PRESSURE", "SCENARIOS",
    "category_allowed", "event_edge", "event_identity", "event_level",
    "event_max_age_seconds", "event_spec", "normalize_event"]
