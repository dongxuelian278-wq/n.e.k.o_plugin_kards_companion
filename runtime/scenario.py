"""KARDS 场景解析（opening / in_match / pressure / critical / ended）。

分层原则
--------
* 可信图像不等于已确认场景；``unknown`` 永不凭连续帧当作离场。
* 短 ``unknown``/``menu``/``draft`` 只暂停业务（不重开对局、不重新 opening），
  明确 ``menu``/``draft`` 连续 ``scene_exit_confirm_frames`` 帧才确认离场。
* 我方已确认 HQ 危机优先于 opening；敌方低血不是危机。
* 压力窗口只由本帧新鲜的我方掉血/我方单位阵亡事件延长，窗口过后自动退出。
* 回到 match 是 resume（``needs_rebase=True``），不是 opening。

集成接缝
--------
``observe_scene(state, now=...)`` 是纯函数，不推进任何计数器，供集成在
调用 tracker 前做门控：只有 ``allow_diff=True`` 的帧才喂 tracker；``resume``
为真时先 ``tracker.rebase()`` 再 update。``update(state, events, now=...)``
是唯一推进 exit 计数/危机/压力/opening 的入口，每帧调用一次。
"""

from __future__ import annotations

import math
import time
from typing import Any, Iterable

from ..state.model import (
    E_HQ_DAMAGE,
    E_MATCH_START,
    E_ORDER_CARD,
    E_TURN_SWITCH,
    E_UNIT_ADVANCED,
    E_UNIT_DIED,
    E_UNIT_PLAYED,
    SCENE_DEFEAT,
    SCENE_DRAFT,
    SCENE_MATCH,
    SCENE_MENU,
    SCENE_UNKNOWN,
    SCENE_VICTORY,
    SIDE_MINE,
)
from .contracts import (
    CRITICAL,
    ENDED,
    IN_MATCH,
    OPENING,
    OUT_OF_BATTLE,
    PRESSURE,
)

_TERMINAL_SCENES = frozenset({SCENE_VICTORY, SCENE_DEFEAT})
_PAUSE_SCENES = frozenset({SCENE_MENU, SCENE_DRAFT, SCENE_UNKNOWN})
_OPENING_END_EVENTS = frozenset({
    E_TURN_SWITCH,
    E_UNIT_PLAYED,
    E_UNIT_DIED,
    E_UNIT_ADVANCED,
    E_ORDER_CARD,
    E_HQ_DAMAGE,
})


def _norm_scene(value: Any) -> str:
    scene = str(value or SCENE_UNKNOWN).strip().lower()
    if scene in (SCENE_MENU, SCENE_DRAFT, SCENE_MATCH, SCENE_VICTORY, SCENE_DEFEAT):
        return scene
    return SCENE_UNKNOWN


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _unwrap_event(item: Any) -> Any:
    """Accept GameEvent or CandidateEvent without importing runtime bits."""
    if hasattr(item, "kind") and hasattr(item, "details"):
        return item
    inner = getattr(item, "event", None)
    return inner if inner is not None else item


def _event_details(event: Any) -> dict[str, Any]:
    details = getattr(event, "details", None)
    return dict(details) if isinstance(details, dict) else {}


class KardsScenarioResolver:
    """Resolve the coarse scenario for delivery/read gating."""

    def __init__(
        self,
        *,
        now_fn=time.time,
        opening_grace_s: float = 6.0,
        pressure_window_s: float = 8.0,
        critical_hp: int = 5,
        hysteresis_s: float = 2.0,
        scene_exit_confirm_frames: int = 3,
        critical_confirm_frames: int = 2,
        recovery_hysteresis_seconds: float | None = None,
    ) -> None:
        self._now_fn = now_fn
        self.opening_grace_s = max(0.0, float(opening_grace_s))
        self.pressure_window_s = max(0.0, float(pressure_window_s))
        self.critical_hp = int(critical_hp)
        if recovery_hysteresis_seconds is not None:
            hysteresis_s = float(recovery_hysteresis_seconds)
        self.hysteresis_s = max(0.0, float(hysteresis_s))
        self.scene_exit_confirm_frames = max(1, int(scene_exit_confirm_frames))
        self.critical_confirm_frames = max(1, int(critical_confirm_frames))
        self.reset()

    # ------------------------------------------------------------- 状态

    def reset(self) -> None:
        self._scenario = OUT_OF_BATTLE
        self._reason = "reset"
        self._since: float | None = None
        self._updated_at: float | None = None
        self._game_number = 0

        self._in_battle = False
        self._confirmed_exit = False
        self._needs_rebase = False
        self._clear_rebase_after_report = False
        self._context_valid = False
        self._resume_pending = False
        self._exit_frames = 0
        self._exit_scene = ""

        self._opening = False
        self._opening_started = 0.0
        self._opening_turn = 0
        self._opening_reason = ""

        self._pressure_until = 0.0
        self._pressure_reason = ""

        self._business_now = 0.0
        self._last_now: float | None = None

        self._crisis_confirmed = False
        self._hp_low_frames = 0
        self._crisis_clear_started: float | None = None

    @property
    def scenario(self) -> str:
        return self._scenario

    # ------------------------------------------------------------- 纯门控

    def observe_scene(self, state: Any, *, now: float | None = None) -> dict[str, Any]:
        """Side-effect free scene gate for callers that update tracker first.

        Integrations should call this *before* ``DetectorRegistry.update``:
        skip the tracker when ``allow_diff`` is false, and call
        ``tracker.rebase()`` before the tracker update when ``resume`` is true.
        """
        del now  # pure: the gate reflects the last committed frame
        scene = _norm_scene(getattr(state, "scene", None))
        terminal = scene in _TERMINAL_SCENES
        allow_diff = terminal or scene == SCENE_MATCH
        # context 只表示“当前有可读的 live match”；menu/draft/unknown 与
        # 终局都不构成业务上下文。
        context_valid = scene == SCENE_MATCH
        resume = bool(
            scene == SCENE_MATCH
            and self._in_battle
            and not self._context_valid
            and not self._confirmed_exit
        )
        new_match = bool(
            scene == SCENE_MATCH
            and (not self._in_battle or self._confirmed_exit or self._scenario == ENDED)
        )
        return {
            "scene": scene,
            "scenario": self._scenario,
            "allow_diff": bool(allow_diff),
            "terminal": bool(terminal),
            "resume": resume,
            "new_match": new_match,
            "context_valid": bool(context_valid),
            "needs_rebase": bool(self._needs_rebase),
            "confirmed_exit": bool(self._confirmed_exit),
        }

    def pre_classify(self, state: Any, *, now: float | None = None) -> dict[str, Any]:
        """Alias for :meth:`observe_scene`."""
        return self.observe_scene(state, now=now)

    def consume_rebase(self) -> bool:
        """Acknowledge that the reported rebase was applied; returns the flag."""
        value = bool(self._needs_rebase)
        self._needs_rebase = False
        self._clear_rebase_after_report = False
        return value

    acknowledge_rebase = consume_rebase

    def snapshot(self) -> dict[str, Any]:
        return {
            "scenario": str(self._scenario),
            "reason": str(self._reason),
            "since": self._json_number(self._since),
            "updated_at": self._json_number(self._updated_at),
            "game_number": int(self._game_number),
            "context_valid": bool(self._context_valid),
            "needs_rebase": bool(self._needs_rebase),
            "confirmed_exit": bool(self._confirmed_exit),
        }

    # ------------------------------------------------------------- 主入口

    def update(
        self,
        state: Any,
        events: Iterable[Any] = (),
        *,
        now: float | None = None,
    ) -> str:
        at = self._resolve_now(state, now)
        scene = _norm_scene(getattr(state, "scene", None))
        event_list = [event for event in (_unwrap_event(item) for item in (events or ()))
                      if event is not None]

        if self._clear_rebase_after_report:
            self._needs_rebase = False
            self._clear_rebase_after_report = False

        self._advance_clock(at, scene == SCENE_MATCH)

        # 明确终局永远优先，且立刻确认。
        if scene in _TERMINAL_SCENES:
            self._enter_terminal(state, scene, at)
            return self._scenario

        # ENDED 后保持终局；只有新的 match 才会开启下一局。
        if self._scenario == ENDED:
            if scene == SCENE_MATCH:
                self._begin_battle(state, at, events=event_list, force_new=True)
                self._decide(state, at, event_list)
            else:
                self._context_valid = False
                self._updated_at = at
                self._reason = "ended"
            return self._scenario

        if not self._in_battle:
            if scene == SCENE_MATCH:
                self._begin_battle(state, at, events=event_list)
                self._decide(state, at, event_list)
            elif scene == SCENE_MENU:
                self._set_scenario(OUT_OF_BATTLE, at, "menu")
                self._context_valid = False
                self._confirmed_exit = False
                self._needs_rebase = False
            elif scene == SCENE_DRAFT:
                self._set_scenario(OUT_OF_BATTLE, at, "draft_unsupported")
                self._context_valid = False
                self._confirmed_exit = False
                self._needs_rebase = False
            else:
                self._set_scenario(OUT_OF_BATTLE, at, "unknown")
                self._context_valid = False
                self._confirmed_exit = False
                self._needs_rebase = False
            return self._scenario

        # ---- 已在局内 -------------------------------------------------
        if scene == SCENE_MATCH:
            game_number = _as_int(getattr(state, "game_number", 0)) or 0
            match_start = any(
                str(getattr(event, "kind", "")) == E_MATCH_START for event in event_list
            )
            new_game = match_start or (
                game_number > 0 and game_number > self._game_number
            )
            if new_game:
                self._begin_battle(
                    state, at, events=event_list,
                    game_number=max(game_number, self._game_number + 1),
                    force_new=True,
                )
                self._decide(state, at, event_list)
                return self._scenario

            self._context_valid = True
            self._exit_frames = 0
            self._exit_scene = ""
            if self._resume_pending:
                self._resume_pending = False
                # 本帧快照仍暴露 needs_rebase，供集成在下一帧前消费。
                self._needs_rebase = True
                self._clear_rebase_after_report = True
            elif self._needs_rebase:
                self._clear_rebase_after_report = True
            self._decide(state, at, event_list)
            return self._scenario

        if scene in (SCENE_MENU, SCENE_DRAFT):
            self._context_valid = False
            self._resume_pending = True
            self._needs_rebase = True
            self._clear_rebase_after_report = False
            self._advance_exit_streak(scene, at)
            return self._scenario

        # unknown 永不凭连续帧离场，只暂停并标记将来 rebase。
        self._context_valid = False
        self._resume_pending = True
        self._needs_rebase = True
        self._clear_rebase_after_report = False
        self._exit_frames = 0
        self._exit_scene = ""
        self._updated_at = at
        self._reason = "unknown_pause"
        return self._scenario

    # ------------------------------------------------------------- 场景内部

    def _begin_battle(
        self,
        state: Any,
        at: float,
        *,
        events: list[Any],
        force_new: bool = False,
        game_number: int | None = None,
    ) -> None:
        del events, force_new  # opening starts now; events are evaluated by _decide
        reported = _as_int(getattr(state, "game_number", 0)) or 0
        if game_number is None:
            game_number = reported if reported > 0 else self._game_number + 1
        self._game_number = max(int(game_number), 0)

        self._in_battle = True
        self._confirmed_exit = False
        self._needs_rebase = False
        self._clear_rebase_after_report = False
        self._context_valid = True
        self._resume_pending = False
        self._exit_frames = 0
        self._exit_scene = ""

        self._opening = True
        self._opening_started = self._business_now
        self._opening_turn = _as_int(getattr(state, "turn_number", 0)) or 0
        self._opening_reason = "opening"

        self._pressure_until = 0.0
        self._pressure_reason = ""

        self._crisis_confirmed = False
        self._hp_low_frames = 0
        self._crisis_clear_started = None

        self._set_scenario(OPENING, at, "opening")

    def _decide(self, state: Any, at: float, events: list[Any]) -> None:
        self._update_crisis(state)
        self._update_pressure(events, at)
        self._update_opening(state, events)

        if self._crisis_confirmed:
            self._set_scenario(CRITICAL, at, "own_hq_critical")
        elif self._opening:
            self._set_scenario(OPENING, at, self._opening_reason or "opening")
        elif self._pressure_active():
            self._set_scenario(PRESSURE, at, self._pressure_reason or "pressure")
        else:
            self._set_scenario(IN_MATCH, at, "in_match")

    def _update_crisis(self, state: Any) -> None:
        hp = _as_int(getattr(state, "my_hq", None))
        if hp is None:
            # 缺读：不推进确认、不解除已确认危机。
            self._hp_low_frames = 0
            self._crisis_clear_started = None
            return

        if hp <= self.critical_hp:
            self._hp_low_frames += 1
            self._crisis_clear_started = None
            if self._hp_low_frames >= self.critical_confirm_frames:
                self._crisis_confirmed = True
            return

        self._hp_low_frames = 0
        if not self._crisis_confirmed:
            self._crisis_clear_started = None
            return
        if self._crisis_clear_started is None:
            self._crisis_clear_started = self._business_now
        elif self._business_now - self._crisis_clear_started >= self.hysteresis_s:
            self._crisis_confirmed = False
            self._crisis_clear_started = None

    def _update_pressure(self, events: list[Any], at: float) -> None:
        for event in events:
            kind = str(getattr(event, "kind", "") or "")
            if kind not in (E_HQ_DAMAGE, E_UNIT_DIED):
                continue
            details = _event_details(event)
            if str(details.get("side") or "").strip().lower() != SIDE_MINE:
                continue
            if not self._event_fresh(event, at):
                continue
            self._pressure_until = max(
                self._pressure_until, self._business_now + self.pressure_window_s
            )
            self._pressure_reason = (
                "own_damage" if kind == E_HQ_DAMAGE else "unit_loss"
            )

    def _update_opening(self, state: Any, events: list[Any]) -> None:
        if not self._opening:
            return
        if self._business_now - self._opening_started >= self.opening_grace_s:
            self._opening = False
            self._opening_reason = "opening_grace"
            return

        observed_turn = _as_int(getattr(state, "turn_number", None))
        if observed_turn is not None and observed_turn > self._opening_turn:
            self._opening = False
            self._opening_reason = "turn_advanced"
            return

        for event in events:
            if str(getattr(event, "kind", "") or "") in _OPENING_END_EVENTS:
                self._opening = False
                self._opening_reason = "match_event"
                return

    def _advance_exit_streak(self, scene: str, at: float) -> None:
        # menu 与 draft 都属于“明确离场候选”，连续出现即累计；unknown 已在
        # update 中清零，match 帧也会清零。
        self._exit_frames += 1
        self._exit_scene = scene
        if self._exit_frames >= self.scene_exit_confirm_frames:
            self._in_battle = False
            self._confirmed_exit = True
            self._needs_rebase = True
            self._clear_rebase_after_report = False
            self._context_valid = False
            self._resume_pending = False
            self._opening = False
            self._pressure_until = 0.0
            self._crisis_confirmed = False
            self._exit_frames = 0
            self._set_scenario(ENDED, at, f"{scene}_confirmed_exit")
            return
        self._updated_at = at
        self._reason = f"{scene}_pause"

    def _enter_terminal(self, state: Any, scene: str, at: float) -> None:
        reported = _as_int(getattr(state, "game_number", 0)) or 0
        if reported > self._game_number:
            self._game_number = reported
        self._in_battle = False
        self._confirmed_exit = True
        self._needs_rebase = False
        self._clear_rebase_after_report = False
        self._context_valid = False
        self._resume_pending = False
        self._opening = False
        self._pressure_until = 0.0
        self._crisis_confirmed = False
        self._hp_low_frames = 0
        self._crisis_clear_started = None
        self._set_scenario(ENDED, at, "victory" if scene == SCENE_VICTORY else "defeat")

    def _pressure_active(self) -> bool:
        return self._business_now < self._pressure_until

    # ------------------------------------------------------------- 工具

    def _advance_clock(self, at: float, current_match: bool) -> None:
        delta = 0.0
        if self._last_now is not None:
            delta = at - self._last_now
            if not math.isfinite(delta) or delta < 0:
                delta = 0.0
        self._last_now = at
        # 只在上一帧和当前帧都是可信 match 时推进业务时钟，
        # 这样短 unknown/menu/draft 不会推进 opening/压力/hysteresis。
        if self._in_battle and self._context_valid and current_match:
            self._business_now += delta

    def _event_fresh(self, event: Any, at: float) -> bool:
        ts = _as_float(getattr(event, "ts", None))
        if ts is None:
            return True
        age = at - ts
        if age > self.pressure_window_s:
            return False
        # 时钟域不一致（负得离谱）时不凭 ts 丢弃真实事件。
        return True

    def _set_scenario(self, scenario: str, at: float, reason: str) -> None:
        scenario = str(scenario)
        if scenario != self._scenario:
            self._since = at
        self._scenario = scenario
        self._reason = str(reason)
        self._updated_at = at

    def _resolve_now(self, state: Any, now: float | None) -> float:
        explicit = _as_float(now)
        if explicit is not None:
            return explicit
        observed = _as_float(getattr(state, "detected_at", None))
        if observed is not None:
            return observed
        try:
            fallback = float(self._now_fn())
        except (TypeError, ValueError):
            fallback = 0.0
        return fallback if math.isfinite(fallback) else 0.0

    @staticmethod
    def _json_number(value: float | None) -> float | None:
        if value is None:
            return None
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        return out if math.isfinite(out) else None


__all__ = ["KardsScenarioResolver"]
