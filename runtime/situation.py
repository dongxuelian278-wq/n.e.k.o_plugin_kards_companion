"""统一局势视图（``kards_situation_v1``）。

纯标准库、无宿主/模型依赖：把一次真实感知快照（MatchState + 两张模型的能力
边界 + 卡面区域观察）汇总成**同一份** JSON-safe 事实视图，供 read / respond /
LLM tool 共同消费。这里只做“已有事实的裁切与标注”，绝不推断：

* 总部血量、费用、手牌数、实时攻防、回合归属没有可靠 producer，一律写进
  ``unknown`` / ``limitations``，不写成 0 / False；
* UI 检测能力不支持 ``unit_board`` 时，``facts.units`` 的 list/count 为
  ``None``，绝不输出“空盘 + count=0”；
* 侧别未知的单位保留在 ``unknown`` 桶；未在本帧观测到的单位只进 ``retained``，
  不给“当前身份”；
* 卡牌是**牌面观察**，不是出牌 / 阵亡 / 棋盘身份；白名单为空或能力不支持时
  ``recognized_cards`` 必须为空并给出 reason，绝不释放原始 top1；
* 场景边界不可用（只靠 ``end_turn_button`` 进入 match，没有真实终局信号），
  ``end_turn_button`` 消失不等于对局结束；
* 所有 source 时间都来自采集/UI 实跑时刻，``built_at`` 只是发布时刻，不刷新
  ``observed_at``；``meta.truncated`` 明确说明哪些列表被截断。

公共 API
--------
``build_situation(state, **kwargs) -> dict``
    主接线每 tick 调用一次；结果写入 ``MatchState.situation``。

``situation_payload(state) -> dict``
    返回 state 上已冻结的完整视图（没有则 ``{}``）；供 delivery owner 直接读。

``situation_facts(state) -> dict``
    同上，但只返回 ``facts`` 段（没有则 ``{}``）。

``semantic_value(value)`` / ``semantic_fingerprint(state)`` 在 ``state.model``；
delivery 侧可按同一规范递归剔除 volatile 时间/帧字段。
"""

from __future__ import annotations

import copy
import math
from typing import Any, Iterable, Mapping, Sequence

SITUATION_SCHEMA = "kards_situation_v1"
SITUATION_VERSION = 1

# 卡面观察的硬 TTL：超过 2 秒的牌面绝不进入当前局势。
SITUATION_CARD_TTL_SECONDS = 2.0
# read/write 公用的新鲜上限；主循环用它判定 context 是否仍可信。
SITUATION_FRESH_MAX_AGE_SECONDS = 1.5
# Any source/UI/card timestamp more than this far in the future is clock
# corruption, not evidence.
SITUATION_FUTURE_TOLERANCE_SECONDS = 0.25
# 列表硬上限：truncated 会在 meta 中显式标注。
SITUATION_MAX_CARDS = 20
SITUATION_MAX_UNITS = 18
SITUATION_MAX_EVENTS = 10
SITUATION_MAX_LIMITATIONS = 12
SITUATION_MAX_UI_CLASSES = 16
SITUATION_MAX_TEXT = 160

# UI 类 -> 区域类别。
CARD_REGIONS = ("card_hand", "played_card")
CARD_APPEARANCE_BY_REGION = {
    "card_hand": "hand_full",
    "played_card": "zoom_full",
}
UNIT_CLASS = "unit_board"
MATCH_CLASSES = ("frontline", "end_turn_button", "end_turn_button_active")
TERMINAL_CLASSES = ("screen_victory", "screen_defeat")
SCENE_CLASSES = frozenset({*MATCH_CLASSES, *TERMINAL_CLASSES})
SIDE_BUCKETS = ("mine", "enemy", "unknown")

_EVENT_DETAIL_KEYS = (
    "side", "game", "band", "edge", "hp", "loss", "result",
    "lane", "cause", "previous_band",
)

# --------------------------------------------------------------------- bounds

def _text(value: Any, limit: int = SITUATION_MAX_TEXT) -> str:
    if value is None or isinstance(value, (bytes, bytearray, memoryview)):
        return ""
    if not isinstance(value, str):
        if isinstance(value, (bool, int, float)):
            value = str(value)
        else:
            # Never serialize arbitrary object repr (pixels/keys); only type.
            return f"<{type(value).__name__}>"[:max(0, int(limit))]
    text = " ".join(value.replace("\x00", " ").split())
    return text[:max(0, int(limit))]


def _number(value: Any, default: float | None = None,
            *, minimum: float | None = None,
            maximum: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    if minimum is not None:
        out = max(minimum, out)
    if maximum is not None:
        out = min(maximum, out)
    return out


def _integer(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _bounded_list(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value[:max(0, int(limit))]]


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Recursive copy that can never leak bytes/objects into a host payload."""
    if depth >= 8:
        return None
    if value is None or isinstance(value, (bool, int, str)):
        return _text(value) if isinstance(value, str) else value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:32]:
            name = _text(key, 48)
            if not name:
                continue
            out[name] = _json_safe(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1) for item in value[:32]]
    return f"<{type(value).__name__}>"


# ---------------------------------------------------------------- capabilities

def _ui_model_view(models: Mapping[str, Any] | None) -> dict[str, Any]:
    models = models if isinstance(models, Mapping) else {}
    ui = models.get("ui_det")
    ui = ui if isinstance(ui, Mapping) else {}
    supported = [
        _text(item, 48) for item in _bounded_list(
            ui.get("supported_classes"), SITUATION_MAX_UI_CLASSES)
        if _text(item, 48)
    ]
    unsupported = [
        _text(item, 48) for item in _bounded_list(
            ui.get("unsupported_classes"), SITUATION_MAX_UI_CLASSES)
        if _text(item, 48)
    ]
    state = _text(ui.get("state"), 48) or "unavailable"
    loaded = bool(ui.get("loaded")) if "loaded" in ui else state == "loaded"
    diagnostics = _text(ui.get("diagnostics"), 120)
    # models absent/old -> fail closed: no verified class, no board facts.
    if not supported and not unsupported:
        state = "capabilities_unknown"
        loaded = False
        diagnostics = diagnostics or "supported_classes_missing"
    return {
        "available": bool(loaded),
        "state": state,
        "supported": supported,
        "unsupported": unsupported,
        "diagnostics": diagnostics,
    }


def _card_capability(models: Mapping[str, Any] | None,
                     ui: Mapping[str, Any]) -> dict[str, Any]:
    models = models if isinstance(models, Mapping) else {}
    card = models.get("card_cls")
    card = card if isinstance(card, Mapping) else {}
    supported_classes = set(ui.get("supported", ()))
    regions = [name for name in CARD_REGIONS if name in supported_classes]
    model_state = _text(card.get("state"), 48) or "unavailable"
    model_loaded = (
        bool(card.get("loaded")) if "loaded" in card else model_state == "loaded")
    trusted = _integer(
        models.get("trusted_card_count",
                   card.get("trusted_card_count", 0)), 0)
    trusted = max(0, trusted)
    if trusted <= 0:
        reason = "whitelist_empty"
    elif not regions:
        reason = "capability_unavailable"
    elif not model_loaded:
        reason = "model_unavailable"
    else:
        reason = ""
    appearances = [CARD_APPEARANCE_BY_REGION[name] for name in regions]
    return {
        "supported": not reason,
        "regions": regions,
        "appearances": appearances,
        "trusted_card_count": trusted,
        "whitelist_empty": trusted <= 0,
        "reason": reason,
    }


def _scene_capability(ui: Mapping[str, Any]) -> dict[str, Any]:
    supported = set(ui.get("supported", ()))
    match_signals = [name for name in MATCH_CLASSES if name in supported]
    terminal_signals = [name for name in TERMINAL_CLASSES if name in supported]
    if "frontline" in match_signals:
        boundary = "frontline_or_end_turn_button"
    elif "end_turn_button" in match_signals:
        boundary = "end_turn_button_only"
    else:
        boundary = "unavailable"
    return {
        "supported_signals": match_signals + terminal_signals,
        "terminal_signals_supported": terminal_signals,
        "boundary": boundary,
        "match_boundary_unavailable": not bool(terminal_signals),
        "turn_attribution_supported": False,
    }


def _unit_capability(ui: Mapping[str, Any], *,
                     detector_available: bool | None,
                     observation_valid: bool | None,
                     status: str) -> dict[str, Any]:
    declared = UNIT_CLASS in set(ui.get("supported", ()))
    available = (declared if detector_available is None
                 else bool(declared and detector_available))
    if not declared:
        reason = "capability_unavailable"
    elif detector_available is False:
        reason = "detector_unavailable"
    elif observation_valid is False:
        reason = "observation_invalid"
    else:
        reason = "ok"
    return {
        "supported": bool(declared),
        "detector_available": detector_available,
        "observation_valid": observation_valid,
        "status": _text(status, 48) or ("ok" if reason == "ok" else reason),
        "reason": reason,
        "available": bool(available),
    }


def _capabilities(models: Mapping[str, Any] | None, *,
                  detector_available: bool | None,
                  observation_valid: bool | None,
                  status: str) -> dict[str, Any]:
    ui = _ui_model_view(models)
    cards = _card_capability(models, ui)
    scene = _scene_capability(ui)
    units = _unit_capability(
        ui, detector_available=detector_available,
        observation_valid=observation_valid, status=status)
    return {
        "ui": ui,
        "units": units,
        "cards": cards,
        "scene": scene,
    }


# -------------------------------------------------------------------- cards

def _card_observation(item: Mapping[str, Any], *, session_id: str,
                      now: float, ttl: float) -> tuple[dict[str, Any] | None, str]:
    card_id = _text(item.get("card_id"), 80)
    if not card_id or card_id.lower() == "unknown":
        return None, "invalid"
    item_session = _text(item.get("session_id"), 120)
    if not item_session or (session_id and item_session != session_id):
        return None, "cross_session"
    region = _text(item.get("region"), 32)
    if region not in CARD_REGIONS:
        return None, "unsupported"
    appearance = _text(item.get("appearance"), 32) or CARD_APPEARANCE_BY_REGION[region]
    observed_at = _number(item.get("observed_at"))
    if observed_at is None or observed_at <= 0.0:
        return None, "invalid"
    source_frame = _integer(
        item.get("source_frame", item.get("frame_id", 0)), 0)
    if source_frame <= 0:
        return None, "invalid"
    age = float(now) - observed_at
    if age < -SITUATION_FUTURE_TOLERANCE_SECONDS:
        # Same global future tolerance as source/UI timestamps.
        return None, "invalid"
    if age > ttl:
        return None, "stale"
    conf = _number(item.get("conf"), 0.0, minimum=0.0, maximum=1.0) or 0.0
    observation_id = _text(item.get("observation_id"), 96) or (
        f"card:{source_frame}:{card_id}")
    return {
        "observation_id": observation_id,
        "session_id": item_session,
        "source_frame": source_frame,
        "observed_at": round(observed_at, 3),
        "age_ms": round(max(0.0, age) * 1000.0, 1),
        "ttl_ms": round(ttl * 1000.0, 1),
        "region": region,
        "appearance": appearance,
        "card_id": card_id,
        "name": _text(item.get("name"), 80),
        "confidence": round(float(conf), 3),
        "fresh": True,
        # A card face is not a board identity and does not prove a play/death.
        # Unknown is None, never a fabricated False.
        "claim": "card_face_observation",
        "board_identity": False,
        "played": None,
        "died": None,
        "play_claim": "unsupported",
        "death_claim": "unsupported",
    }, ""


def _recognized_cards(observations: Iterable[Any] | None, *,
                      cards_capability: Mapping[str, Any],
                      session_id: str, now: float,
                      ttl: float, limit: int,
                      context_valid: bool,
                      ui_facts_ok: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    dropped = {"stale": 0, "cross_session": 0, "unsupported": 0, "invalid": 0}
    seen: set[str] = set()
    fresh: list[dict[str, Any]] = []
    supported = bool(cards_capability.get("supported"))
    regions = set(cards_capability.get("regions") or ())
    if not context_valid or not ui_facts_ok:
        # Fresh-looking card crops from an invalid context are history, never
        # current facts.
        return [], {
            "reason": "context_invalid",
            "ttl_seconds": round(ttl, 3),
            "dropped_stale": 0,
            "dropped_cross_session": 0,
            "dropped_unsupported": 0,
            "dropped_invalid": 0,
            "truncated": False,
        }
    for item in observations or ():
        if not isinstance(item, Mapping):
            dropped["invalid"] += 1
            continue
        if not supported:
            dropped["unsupported"] += 1
            continue
        region = _text(item.get("region"), 32)
        if region not in regions:
            dropped["unsupported"] += 1
            continue
        if _text(item.get("appearance"), 32) and \
                _text(item.get("appearance"), 32) not in set(
                    cards_capability.get("appearances") or ()):
            dropped["unsupported"] += 1
            continue
        entry, reason = _card_observation(
            item, session_id=session_id, now=now, ttl=ttl)
        if entry is None:
            if reason in dropped:
                dropped[reason] += 1
            else:
                dropped["invalid"] += 1
            continue
        if entry["observation_id"] in seen:
            continue
        seen.add(entry["observation_id"])
        fresh.append(entry)

    fresh.sort(key=lambda item: (item["observed_at"], item["source_frame"]),
               reverse=True)
    truncated = len(fresh) > limit
    fresh = fresh[:max(0, int(limit))]
    if cards_capability.get("reason"):
        reason = str(cards_capability["reason"])
    elif fresh:
        reason = ""
    elif any(dropped.values()):
        reason = "no_fresh_observations"
    else:
        reason = "no_card_observations"
    return fresh, {
        "reason": reason,
        "ttl_seconds": round(ttl, 3),
        "dropped_stale": dropped["stale"],
        "dropped_cross_session": dropped["cross_session"],
        "dropped_unsupported": dropped["unsupported"],
        "dropped_invalid": dropped["invalid"],
        "truncated": bool(truncated),
    }


# -------------------------------------------------------------------- units

def _unit_fact(unit: Any, *, current: bool) -> dict[str, Any]:
    side = _text(getattr(unit, "side", "unknown"), 16) or "unknown"
    if side not in SIDE_BUCKETS:
        side = "unknown"
    fact: dict[str, Any] = {
        "instance_id": _integer(getattr(unit, "instance_id", 0), 0),
        "side": side,
        "lane": _text(getattr(unit, "lane", ""), 16),
        "slot": getattr(unit, "slot", None),
        "observed": bool(getattr(unit, "observed", False)),
        "current": bool(current),
        "last_seen_frame": _integer(getattr(unit, "last_seen_frame", 0), 0) or None,
        "observed_at": round(_number(getattr(unit, "observed_at", None), 0.0) or 0.0, 3),
        "detection_conf": round(
            _number(getattr(unit, "detection_confidence", None), 0.0) or 0.0, 3),
        "track_confirmed": bool(getattr(unit, "track_confirmed", False)),
    }
    if fact["slot"] is not None:
        fact["slot"] = _integer(fact["slot"], 0)
    bbox = getattr(unit, "bbox", None)
    if bbox is not None:
        values: list[float] = []
        try:
            for value in bbox:
                number = _number(value)
                if number is not None:
                    values.append(round(number, 4))
        except TypeError:
            values = []
        if len(values) == 4:
            fact["bbox"] = values
    name_conf = _number(getattr(unit, "name_confidence", 0.0), 0.0) or 0.0
    card_id = _text(getattr(unit, "card_id", None), 80)
    card_name = _text(getattr(unit, "card_name", None), 80)
    if card_name and name_conf >= 0.6:
        if current:
            fact["name"] = card_name
            if card_id:
                fact["card"] = card_id
        else:
            # Historical identity only; never present it as the current unit.
            fact["last_known_name"] = card_name
            if card_id:
                fact["last_known_card"] = card_id
    elif current:
        describe = getattr(unit, "describe", None)
        if callable(describe):
            try:
                fact["desc"] = _text(describe(), 80)
            except Exception:
                pass
    attack = getattr(unit, "attack", None)
    health = getattr(unit, "health", None)
    if current and attack is not None:
        fact["attack"] = _integer(attack, 0)
    if current and health is not None:
        fact["health"] = _integer(health, 0)
    return fact


def _unit_facts(state: Any, *, units_capability: Mapping[str, Any],
                observation_valid: bool | None, scene: str,
                context_valid: bool, ui_facts_ok: bool,
                limit: int) -> dict[str, Any]:
    supported = bool(units_capability.get("supported"))
    unavailable = {
        "supported": supported,
        "status": "unavailable",
        "reason": units_capability.get("reason") or "capability_unavailable",
        "detector_available": units_capability.get("detector_available"),
        "observation_valid": observation_valid,
        "observed": None,
        "retained": None,
        "counts": None,
        "truncated": False,
    }
    if not supported:
        # Never encode an unsupported unit model as an empty board / count 0.
        return unavailable
    if not context_valid or not ui_facts_ok:
        return {
            **unavailable,
            "reason": "context_invalid",
        }
    if scene != "match":
        return {
            **unavailable,
            "reason": "scene_not_match",
        }

    raw_units = list(getattr(state, "units", ()) or ())
    current_evidence = bool(
        observation_valid is True
        and units_capability.get("available") is not False
        and units_capability.get("detector_available") is not False)
    if not current_evidence and not raw_units:
        # Detector unavailable / carried tick with no known units: the board is
        # unknown, not an empty board.  Never emit counts=0 here.
        return {
            **unavailable,
            "status": "unavailable",
            "reason": units_capability.get("reason") or "observation_invalid",
        }

    observed: dict[str, list[dict[str, Any]]] = {name: [] for name in SIDE_BUCKETS}
    retained: dict[str, list[dict[str, Any]]] = {name: [] for name in SIDE_BUCKETS}
    observed_count = 0
    retained_count = 0
    for unit in raw_units:
        current = bool(current_evidence and getattr(unit, "observed", False))
        fact = _unit_fact(unit, current=current)
        bucket = fact["side"]
        if current:
            observed_count += 1
            observed[bucket].append(fact)
        else:
            retained_count += 1
            retained[bucket].append(fact)

    # Explicit observed/retained counts per bucket.  No combined "mine" total
    # that would make retained history look like a current board count.
    counts: dict[str, int] = {"observed": observed_count,
                              "retained": retained_count}
    for name in SIDE_BUCKETS:
        counts[f"observed_{name}"] = len(observed[name])
        counts[f"retained_{name}"] = len(retained[name])
    # Hard-bound the union of observed + retained to ``limit``.  Observed units
    # win; counts remain the true pre-truncation counts.
    truncated = False
    remaining = max(0, int(limit))
    for group in (observed, retained):
        for name in SIDE_BUCKETS:
            items = group[name]
            if remaining <= 0 and items:
                truncated = True
                group[name] = []
                continue
            if len(items) > remaining:
                group[name] = items[:remaining]
                truncated = True
            remaining -= len(group[name])
    return {
        "supported": True,
        "status": "ok" if current_evidence else "carried",
        "reason": "",
        "detector_available": units_capability.get("detector_available"),
        "observation_valid": observation_valid,
        "observed": observed,
        "retained": retained,
        "counts": counts,
        "truncated": bool(truncated),
    }


# ------------------------------------------------------------------- events

def _safe_event(event: Any, *, session_id: str, now: float,
                frame_id: int, observed_at: float | None,
                limit: int = SITUATION_MAX_TEXT) -> dict[str, Any] | None:
    details: Mapping[str, Any] = {}
    if isinstance(event, Mapping):
        kind = _text(event.get("kind"), 48)
        summary = _text(event.get("summary"), limit)
        priority = _integer(event.get("priority", 0), 0)
        raw_details = event.get("details")
        details = raw_details if isinstance(raw_details, Mapping) else {}
        ts = _number(event.get("ts"), None)
    else:
        kind = _text(getattr(event, "kind", ""), 48)
        summary = _text(getattr(event, "summary", ""), limit)
        priority = _integer(getattr(event, "priority", 0), 0)
        raw_details = getattr(event, "details", None)
        details = raw_details if isinstance(raw_details, Mapping) else {}
        ts = _number(getattr(event, "ts", None), None)
    if not kind:
        return None
    event_session = _text(details.get("session_id"), 120)
    if event_session and session_id and event_session != session_id:
        return None
    safe_details: dict[str, Any] = {}
    for key in _EVENT_DETAIL_KEYS:
        if key not in details:
            continue
        value = details[key]
        if isinstance(value, bool):
            safe_details[key] = value
        elif isinstance(value, int):
            safe_details[key] = value
        elif isinstance(value, float):
            number = _number(value)
            if number is not None:
                safe_details[key] = round(number, 3)
        elif isinstance(value, str):
            safe_details[key] = _text(value, 64)
    source_frame = _integer(details.get("frame_id", 0), 0) or int(frame_id or 0)
    source_at = _number(details.get("observed_at"), None)
    if source_at is None:
        source_at = ts
    if source_at is None:
        source_at = observed_at
    source_age_ms = None
    if source_at is not None:
        source_age_ms = round(max(0.0, float(now) - float(source_at)) * 1000.0, 1)
    return {
        "kind": kind,
        "priority": priority,
        "summary": summary,
        "details": safe_details,
        "source_frame": source_frame,
        "observed_at": None if source_at is None else round(float(source_at), 3),
        "source_age_ms": source_age_ms,
    }


def _events(events: Iterable[Any] | None, *, session_id: str, now: float,
            frame_id: int, observed_at: float | None,
            allow_chatter: bool = False) -> tuple[list[dict[str, Any]], bool]:
    out: list[dict[str, Any]] = []
    for event in events or ():
        kind = (event.get("kind") if isinstance(event, Mapping)
                else getattr(event, "kind", ""))
        if not allow_chatter and str(kind or "") == "chatter":
            # "局势平稳" is only allowed when the situation actually verifies
            # a board/card fact; otherwise it would be a fabricated claim.
            continue
        fact = _safe_event(event, session_id=session_id, now=now,
                           frame_id=frame_id, observed_at=observed_at)
        if fact is not None:
            out.append(fact)
    truncated = len(out) > SITUATION_MAX_EVENTS
    return out[-SITUATION_MAX_EVENTS:], truncated


# --------------------------------------------------------------- limitations

def _limitations(capabilities: Mapping[str, Any], scene: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = [
        {"field": "hq", "status": "unsupported", "reason": "hud_not_implemented",
         "note": "不读取总部血量；不得猜成 0 或补全"},
        {"field": "credits", "status": "unsupported", "reason": "hud_not_implemented",
         "note": "不读取费用；不得猜成 0 或补全"},
        {"field": "hand_count", "status": "unsupported", "reason": "hud_not_implemented",
         "note": "不读取手牌数量"},
        {"field": "enemy_hand_count", "status": "unsupported",
         "reason": "hud_not_implemented", "note": "不读取敌方手牌数量"},
        {"field": "my_turn", "status": "unsupported",
         "reason": "turn_attribution_unavailable",
         "note": "active/inactive 回合类未验收；回合归属未知"},
        {"field": "turn_number", "status": "local_counter",
         "reason": "local_turn_counter_not_actual",
         "note": "turn 是本地观测计数器，不是实际对局回合号；不得当作真实回合"},
        {"field": "unit_attack_health", "status": "unsupported",
         "reason": "card_identity_not_bound_to_board",
         "note": "卡面观察不得当作棋盘单位攻防"},
        {"field": "live_combat", "status": "unsupported",
         "reason": "no_damage_or_trade_signal",
         "note": "不推断实时攻防/换血/阵亡"},
        {"field": "match_boundary", "status": "unsupported",
         "reason": "match_boundary_unavailable",
         "note": "end_turn_button 消失不等于对局结束；胜利/失败只认显式终局信号"},
        {"field": "game_number", "status": "local_segment",
         "reason": "observation_segment",
         "note": "仅为本地观测分段号，不保证真实对局号"},
    ]
    units = capabilities.get("units") or {}
    if not units.get("supported") or not units.get("available"):
        entries.append({
            "field": "units", "status": "limited",
            "reason": units.get("reason") or "capability_unavailable",
            "note": "本帧没有可信单位观测；不输出空盘或单位数量",
        })
    cards = capabilities.get("cards") or {}
    if cards.get("reason"):
        entries.append({
            "field": "cards", "status": "unsupported",
            "reason": cards.get("reason"),
            "note": "卡面身份不可用；不输出原始 top1/未验证卡名",
        })
    scene_cap = capabilities.get("scene") or {}
    if not scene_cap.get("match_boundary_unavailable", True):
        for entry in entries:
            if entry.get("field") == "match_boundary":
                entry.update({
                    "status": "limited",
                    "reason": "terminal_signals_only",
                    "note": "终局只认显式 victory/defeat；按钮消失仍不代表对局结束",
                })
    if scene_cap.get("boundary") != "frontline_or_end_turn_button":
        entries.append({
            "field": "scene", "status": "limited",
            "reason": scene_cap.get("boundary") or "unknown",
            "note": "场景只由有限按钮信号推断，不能证明完整对局阶段",
        })
    if scene in ("victory", "defeat") and not scene_cap.get("terminal_signals_supported"):
        entries.append({
            "field": "result", "status": "unsupported",
            "reason": "terminal_class_unsupported",
            "note": "没有可信终局信号；不得确认胜负",
        })
    return entries


def _unknown(capabilities: Mapping[str, Any], scene: str) -> dict[str, str]:
    unknown = {
        "hq": "unsupported",
        "credits": "unsupported",
        "hand_count": "unsupported",
        "enemy_hand_count": "unsupported",
        "my_turn": "unsupported",
        "turn_number": "local_counter_not_actual",
        "unit_attack_health": "unsupported",
        "live_combat": "unsupported",
        "match_boundary": (
            "terminal_signals_only"
            if not (capabilities.get("scene") or {}).get(
                "match_boundary_unavailable", True)
            else "unsupported"),
        "result": ("unsupported"
                   if scene not in ("victory", "defeat")
                   or not (capabilities.get("scene") or {}).get(
                       "terminal_signals_supported")
                   else "observed"),
        "game_number": "local_segment",
    }
    units = capabilities.get("units") or {}
    if not units.get("supported"):
        unknown["units"] = units.get("reason") or "capability_unavailable"
    cards = capabilities.get("cards") or {}
    if cards.get("reason"):
        unknown["cards"] = cards.get("reason")
    return unknown


# ------------------------------------------------------------------- builder

def build_situation(
    state: Any,
    *,
    models: Mapping[str, Any] | None = None,
    observations: Iterable[Any] | None = None,
    session_id: str = "",
    frame_id: int = 0,
    observed_at: float | None = None,
    now: float | None = None,
    context_valid: bool = False,
    reason: str = "",
    ui_observed_at: float | None = None,
    events: Iterable[Any] | None = None,
    unit_diagnostics: Mapping[str, Any] | None = None,
    observation_valid: bool | None = None,
    detector_available: bool | None = None,
    scenario: str | None = None,
    ui_signals: Sequence[str] | None = None,
    scene_confidence: float | None = None,
    card_ttl_seconds: float = SITUATION_CARD_TTL_SECONDS,
    max_cards: int = SITUATION_MAX_CARDS,
    max_units: int = SITUATION_MAX_UNITS,
) -> dict[str, Any]:
    """Aggregate one perception snapshot into ``kards_situation_v1``.

    ``observed_at`` is the capture/source timestamp; ``now`` is only used to
    measure age and is never written back as the source time.  ``ui_observed_at``
    is the timestamp of the last **new** UI-detector sample (heartbeat); if it is
    omitted the capture time is used, but a caller that knows the UI sample time
    must pass it so a static frame cannot look freshly observed.
    """
    at_now = _number(now, None)
    at_source = _number(observed_at, None)
    if at_now is None or at_now <= 0.0:
        at_now = at_source if at_source is not None and at_source > 0.0 else 0.0
    if at_source is None or at_source <= 0.0:
        at_source = at_now
    ui_at = _number(ui_observed_at, None)
    if ui_at is None or ui_at <= 0.0:
        ui_at = at_source
    diagnostics = unit_diagnostics if isinstance(unit_diagnostics, Mapping) else {}
    observation_valid = (
        observation_valid if isinstance(observation_valid, bool)
        else diagnostics.get("observation_valid"))
    detector_available = (
        detector_available if isinstance(detector_available, bool)
        else diagnostics.get("detector_available"))
    status = _text(diagnostics.get("status"), 64)
    source_age = float(at_now) - float(at_source)
    ui_age = float(at_now) - float(ui_at)
    # A timestamp slightly in the future (<250ms) is clock jitter; anything
    # larger is not evidence and must not be refreshed with publication time.
    source_time_ok = ( -SITUATION_FUTURE_TOLERANCE_SECONDS <= source_age
                       <= SITUATION_FRESH_MAX_AGE_SECONDS)
    ui_time_ok = ( -SITUATION_FUTURE_TOLERANCE_SECONDS <= ui_age
                   <= SITUATION_FRESH_MAX_AGE_SECONDS)

    capabilities = _capabilities(
        models, detector_available=detector_available,
        observation_valid=observation_valid, status=status)
    ui_facts_ok = bool(
        capabilities["ui"].get("available")
        and capabilities["scene"].get("supported_signals"))
    scene_observed = _text(getattr(state, "scene", "unknown"), 24) or "unknown"
    if scene_observed not in ("menu", "draft", "match", "victory", "defeat",
                              "unknown"):
        scene_observed = "unknown"
    scene_current = bool(
        context_valid and ui_facts_ok and ui_time_ok and source_time_ok)
    scene = scene_observed if scene_current else "unknown"
    fresh = scene_current
    valid_scene = bool(fresh and scene == "match")
    turn_supported = bool(
        (capabilities.get("scene") or {}).get("turn_attribution_supported"))

    facts: dict[str, Any] = {
        "scene": scene,
        "in_match": bool(valid_scene),
        "game_number": (
            _integer(getattr(state, "game_number", 0), 0) or None),
        # The tracker's turn is a local synthesized counter, not an actual
        # turn number from the game.  Never present it as a fact.
        "turn": None,
        "local_turn_counter": _integer(
            getattr(state, "turn_number", 0), 0),
        "units": _unit_facts(
            state, units_capability=capabilities["units"],
            observation_valid=observation_valid, scene=scene,
            context_valid=bool(fresh), ui_facts_ok=ui_facts_ok,
            limit=max_units),
        "ui_signals": [
            _text(item, 32) for item in _bounded_list(ui_signals, 12)
            if _text(item, 32)
        ] if scene_current else [],
    }
    if turn_supported:
        my_turn = getattr(state, "my_turn", None)
        facts["my_turn"] = my_turn if isinstance(my_turn, bool) else None
    # No order/played-card fact: a card face is never evidence that an order
    # was played.  ``state.last_order_card`` is intentionally not promoted.

    cards, card_meta = _recognized_cards(
        observations, cards_capability=capabilities["cards"],
        session_id=session_id, now=at_now,
        ttl=max(0.0, float(card_ttl_seconds)), limit=max_cards,
        context_valid=bool(fresh), ui_facts_ok=ui_facts_ok)
    unit_counts = facts["units"].get("counts") or {}
    has_verified_units = False
    if facts["units"].get("supported") and isinstance(unit_counts, dict):
        try:
            has_verified_units = (
                int(unit_counts.get("observed", 0))
                + int(unit_counts.get("retained", 0))) > 0
        except (TypeError, ValueError):
            has_verified_units = False
    event_input = events if (fresh and ui_facts_ok) else ()
    event_facts, events_truncated = _events(
        event_input, session_id=session_id, now=at_now,
        frame_id=frame_id, observed_at=at_source,
        allow_chatter=bool(has_verified_units or cards))
    facts["events"] = event_facts

    observed_units = [
        unit for bucket in (facts["units"].get("observed") or {}).values()
        for unit in bucket
    ]
    unit_conf = None
    if observed_units:
        unit_conf = round(sum(float(item.get("detection_conf") or 0.0)
                              for item in observed_units)
                          / len(observed_units), 3)
    card_conf = round(max((item["confidence"] for item in cards), default=0.0), 3) \
        if cards else None
    # Never invent a scene confidence: only a real UI box confidence supplied
    # by the caller is a fact, and only while the scene is current.
    scene_conf = _number(scene_confidence, None,
                         minimum=0.0, maximum=1.0)
    if scene_conf is not None and scene_current:
        scene_conf = round(float(scene_conf), 3)
    else:
        scene_conf = None

    all_limitations = _limitations(capabilities, scene)
    limitations = all_limitations[:SITUATION_MAX_LIMITATIONS]
    unknown = _unknown(capabilities, scene)
    limits = {
        "match_boundary_unavailable": bool(
            (capabilities.get("scene") or {}).get(
                "match_boundary_unavailable", True)),
        "game_number_source": "local_observation_segment",
        "turn_attribution_supported": turn_supported,
        "scene_boundary": (capabilities.get("scene") or {}).get("boundary"),
        "unit_board_supported": bool(capabilities["units"].get("supported")),
        "card_identity_supported": bool(capabilities["cards"].get("supported")),
        "card_ttl_seconds": round(max(0.0, float(card_ttl_seconds)), 3),
        "fresh_max_age_seconds": SITUATION_FRESH_MAX_AGE_SECONDS,
        # Full UI class lists live here (prompt minimal excludes them) so a
        # compact prompt can still keep capability boundaries.
        "ui_supported_classes": list(capabilities["ui"].get("supported") or ()),
        "ui_unsupported_classes": list(capabilities["ui"].get("unsupported") or ()),
        "truncated": {
            "cards": bool(card_meta.get("truncated")),
            "units": bool(facts["units"].get("truncated")),
            "events": bool(events_truncated),
            "limitations": len(all_limitations) > SITUATION_MAX_LIMITATIONS,
        },
    }

    payload = {
        "schema": SITUATION_SCHEMA,
        "version": SITUATION_VERSION,
        "valid": bool(fresh),
        "fresh": bool(fresh),
        "context_valid": bool(context_valid),
        "source": {
            "session_id": _text(session_id, 120),
            "frame_id": _integer(frame_id, 0),
            "captured_at": round(float(at_source), 3),
            "observed_at": round(float(at_source), 3),
            "ui_observed_at": round(float(ui_at), 3),
            "ui_age_ms": round(max(0.0, ui_age) * 1000.0, 1),
            "fresh": bool(fresh),
            "valid": bool(fresh),
            "context_valid": bool(context_valid),
            "reason": _text(reason, 96),
        },
        "capabilities": capabilities,
        "facts": facts,
        "unknown": unknown,
        "limitations": limitations,
        "recognized_cards": cards,
        "card_observations": card_meta,
        "limits": limits,
        "llm_notice": (
            ("match_boundary_unavailable：场景只由有限按钮信号推断；"
             "按钮消失不等于对局结束。")
            if limits["match_boundary_unavailable"] else
            ("终局只认显式 victory/defeat；按钮消失仍不代表对局结束。")
        ) + ("game_number 是本地观测段号，不是实际对局计数；"
             "没有 HQ/费用/实时攻防/回合归属事实。"),
        "meta": {
            "observed_at": round(float(at_source), 3),
            "ui_observed_at": round(float(ui_at), 3),
            "source_frame": _integer(frame_id, 0),
            "built_at": round(float(at_now), 3),
            "confidence": {
                "scene": scene_conf,
                "units": unit_conf,
                "cards": card_conf,
            },
            "truncated": limits["truncated"],
        },
    }
    return _json_safe(payload)


def situation_payload(state: Any) -> dict[str, Any]:
    """Return the frozen situation view stored on ``state`` (or ``{}``)."""
    value = getattr(state, "situation", None)
    if not isinstance(value, Mapping):
        return {}
    return copy.deepcopy(dict(value))


def situation_facts(state: Any) -> dict[str, Any]:
    """Return only ``situation["facts"]`` for a state (or ``{}``)."""
    facts = situation_payload(state).get("facts")
    return copy.deepcopy(facts) if isinstance(facts, Mapping) else {}


def situation_context_text(situation: Mapping[str, Any] | None) -> str:
    """Short context-entry explanation built from capability boundaries only."""
    payload = situation if isinstance(situation, Mapping) else {}
    capabilities = payload.get("capabilities") or {}
    units = capabilities.get("units") or {}
    cards = capabilities.get("cards") or {}
    scene = capabilities.get("scene") or {}
    parts = ["KARDS 实时局势上下文已建立"]
    if not units.get("supported"):
        parts.append("单位/棋盘能力不可用，不会给出空盘结论")
    if cards.get("reason"):
        parts.append(f"卡面身份不可用（{cards.get('reason')}）")
    if scene.get("boundary") == "end_turn_button_only":
        parts.append("场景仅由结束回合按钮推断，不代表真实对局边界")
    parts.append("总部血量/费用/实时攻防/回合归属不可用")
    parts.append("需要当前事实时调用 kards_get_live_state")
    return _text("；".join(parts), 300)


__all__ = [
    "CARD_APPEARANCE_BY_REGION",
    "CARD_REGIONS",
    "SITUATION_CARD_TTL_SECONDS",
    "SITUATION_FRESH_MAX_AGE_SECONDS",
    "SITUATION_MAX_CARDS",
    "SITUATION_MAX_UNITS",
    "SITUATION_SCHEMA",
    "build_situation",
    "situation_context_text",
    "situation_facts",
    "situation_payload",
]
