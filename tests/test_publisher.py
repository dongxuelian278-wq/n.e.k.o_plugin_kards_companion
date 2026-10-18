"""HostPushGate and LiveStatePublisher behavior (C-owned)."""

import json

from kards_companion.delivery.contracts import (
    STATUS_ACCEPTED,
    STATUS_LEGACY_UNCONFIRMED,
    STATUS_REJECTED,
    STATUS_UNKNOWN,
    GateResult,
    HostPushConfig,
    HostPushGate,
    normalize_receipt,
)
from kards_companion.delivery.publisher import (
    LIVE_STATE_TOOL_NAME,
    LiveStatePublisher,
    PublishResult,
    PublishStatus,
    build_commentary_prompt,
    build_live_state_text,
    semantic_fingerprint,
)
from kards_companion.state.model import SCENE_MATCH, BoardUnit, MatchState


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Recorder:
    def __init__(self, result=True) -> None:
        self.calls: list[dict] = []
        self.result = result

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if callable(self.result):
            return self.result(kwargs)
        return self.result


def make_state(**kw) -> MatchState:
    base: dict = {"scene": SCENE_MATCH, "in_match": True, "my_hq": 20,
                  "enemy_hq": 20, "game_number": 1}
    base.update(kw)
    return MatchState(**base)


# ---------------------------------------------------------------- receipts

def test_receipt_normalization_is_honest() -> None:
    assert normalize_receipt({"submitted": True})[0] == STATUS_ACCEPTED
    assert normalize_receipt({"ok": True})[0] == STATUS_ACCEPTED
    assert normalize_receipt({"submitted": False})[0] == STATUS_REJECTED
    assert normalize_receipt({"ok": False})[0] == STATUS_REJECTED
    # Explicit false wins over a conflicting true field.
    assert normalize_receipt({"submitted": False, "ok": True})[0] == STATUS_REJECTED
    assert normalize_receipt({"submitted": True, "ok": False})[0] == STATUS_REJECTED
    # Old SDK without any receipt is unconfirmed, never host-accepted.
    assert normalize_receipt(None)[0] == STATUS_LEGACY_UNCONFIRMED
    # Empty/unknown shapes are unknown: do not auto-retry.
    assert normalize_receipt({})[0] == STATUS_UNKNOWN
    assert normalize_receipt(object())[0] == STATUS_UNKNOWN
    # Non-boolean strings are not explicit confirmations.
    assert normalize_receipt({"submitted": "false"})[0] == STATUS_UNKNOWN

    class ObjectReceipt:
        def __init__(self, submitted=None):
            self.submitted = submitted

    class IsOkOnly:
        def __init__(self, ok):
            self._ok = ok

        def is_ok(self):
            return self._ok

    assert normalize_receipt(ObjectReceipt(True))[0] == STATUS_ACCEPTED
    assert normalize_receipt(ObjectReceipt(False))[0] == STATUS_REJECTED
    assert normalize_receipt(IsOkOnly(True))[0] == STATUS_ACCEPTED
    assert normalize_receipt(IsOkOnly(False))[0] == STATUS_REJECTED
    # Explicit submitted/ok false wins over a conflicting true value.
    assert normalize_receipt({"submitted": True, "ok": False})[0] == STATUS_REJECTED


def test_gate_receipt_results() -> None:
    clock = FakeClock()
    for receipt, expected in ([True, STATUS_ACCEPTED],
                              [{"submitted": False}, STATUS_REJECTED],
                              [None, STATUS_LEGACY_UNCONFIRMED],
                              [{}, STATUS_UNKNOWN]):
        gate = HostPushGate(Recorder(receipt), now_fn=clock)
        result = gate.push("read", parts=[], visibility=[])
        assert isinstance(result, GateResult)
        assert result.status == expected
        assert result.submitted is (expected == STATUS_ACCEPTED)
        # Unknown/legacy are never retryable as transport errors.
        assert result.retryable is False


# ---------------------------------------------------------------- gate hard gates

def test_dry_run_never_calls_host_and_is_simulated() -> None:
    clock = FakeClock()
    push = Recorder(True)
    gate = HostPushGate(push, config=HostPushConfig(dry_run=True), now_fn=clock)
    result = gate.push("respond", parts=[])
    assert result.allowed and result.simulated and not result.submitted
    assert result.status == "simulated"
    assert push.calls == []
    assert gate.is_simulating


def test_consent_false_blocks_read_respond_expire_and_test() -> None:
    clock = FakeClock()
    push = Recorder(True)
    gate = HostPushGate(push, config=HostPushConfig(consent=False), now_fn=clock)
    for intent in ("read", "respond", "context", "expire", "test"):
        result = gate.push(intent)
        assert not result.allowed and not result.submitted
        assert result.reason == "consent_required"
    assert push.calls == []


def test_pause_blocks_terminal_respond_but_allows_expire() -> None:
    clock = FakeClock()
    push = Recorder(True)
    gate = HostPushGate(push, now_fn=clock)
    gate.pause("user_pause")
    terminal = gate.push("respond", terminal=True, parts=[])
    assert not terminal.allowed and terminal.reason == "paused"
    assert gate.push("expire").accepted
    assert len(push.calls) == 1
    gate.resume()
    assert gate.push("respond", terminal=True, parts=[]).accepted


def test_stop_epoch_and_token_invalidate_old_ticks() -> None:
    clock = FakeClock()
    push = Recorder(True)
    gate = HostPushGate(push, now_fn=clock)
    old_token = gate.get_token()
    old_epoch = gate.epoch

    gate.begin_stop("monitoring_stopped")
    assert gate.phase == "stopping"
    # A new expire is allowed while stopping.
    assert gate.push("expire").accepted
    # Normal/terminal pushes are refused.
    assert gate.push("respond", terminal=True).reason == "stopping"
    # An old tick using the pre-stop token is rejected.
    stale = gate.push("respond", context_token=old_token)
    assert stale.reason == "stale_context"
    stale_epoch = gate.push("respond", expected_epoch=old_epoch)
    assert stale_epoch.reason == "stale_epoch"
    # A tick that captured the token after begin_stop may still expire.
    stopping_token = gate.get_token()
    assert gate.push("expire", context_token=stopping_token).accepted

    gate.finish_stop()
    assert gate.push("expire").reason == "stopped"
    # A late begin_stop must not reopen the expire channel.
    gate.begin_stop("late")
    assert gate.push("expire").reason == "stopped"

    gate.reopen()
    assert gate.epoch > old_epoch
    assert gate.get_token() != old_token
    assert gate.push("respond", terminal=True, parts=[]).accepted
    assert gate.push("respond", context_token=old_token).reason == "stale_context"

    # A pause from the previous lifecycle does not survive a restart.
    gate.pause("user_pause")
    gate.begin_stop("restart")
    gate.finish_stop()
    gate.reopen()
    assert not gate.paused
    assert gate.push("respond", parts=[]).accepted


def test_test_intent_requires_explicit_one_shot_authorization() -> None:
    clock = FakeClock()
    push = Recorder(True)
    gate = HostPushGate(push, config=HostPushConfig(
        authorized_real_test=False), now_fn=clock)
    result = gate.push("test", parts=[{"type": "text", "text": "ping"}])
    assert not result.allowed and result.simulated
    assert result.reason == "test_not_authorized"
    assert push.calls == []

    authorized = HostPushGate(push, config=HostPushConfig(
        authorized_real_test=True), now_fn=clock)
    first = authorized.push("test")
    assert first.accepted and len(push.calls) == 1
    second = authorized.push("test")
    assert not second.allowed and second.reason == "test_authorization_used"
    assert len(push.calls) == 1
    # The main entry revokes and re-grants per explicit test action; a fresh
    # grant is a new one-shot, not a permanently burned gate.
    authorized.update_config(HostPushConfig(authorized_real_test=False))
    authorized.update_config(HostPushConfig(authorized_real_test=True))
    third = authorized.push("test")
    assert third.accepted and len(push.calls) == 2


def test_gate_records_are_bounded_and_contain_no_payload() -> None:
    gate = HostPushGate(Recorder(True), config=HostPushConfig(max_records=2))
    gate.push("respond", parts=[{"type": "text", "text": "SECRET_PROMPT"}],
              metadata={"api_" + "key": "SECRET_" + "KEY"})
    gate.push("respond")
    gate.push("respond")
    snapshot = gate.snapshot()
    assert len(snapshot["records"]) == 2
    rendered = json.dumps(snapshot)
    assert "SECRET_PROMPT" not in rendered
    assert "SECRET_KEY" not in rendered
    assert "parts" not in rendered


# ---------------------------------------------------------------- publisher

def test_publish_uses_gate_and_coalesces() -> None:
    clock = FakeClock()
    push = Recorder(True)
    gate = HostPushGate(push, now_fn=clock)
    publisher = LiveStatePublisher(gate=gate, now_fn=clock, refresh_seconds=5,
                                   max_age_seconds=30)
    state = make_state()

    first = publisher.publish(state, text="T1")
    second = publisher.publish(make_state(), text="T1")
    assert first.status is PublishStatus.SUBMITTED and first.submitted
    assert second.status is PublishStatus.COALESCED and second.coalesced
    assert len(push.calls) == 1
    assert push.calls[0]["visibility"] == []
    assert push.calls[0]["ai_behavior"] == "read"
    assert push.calls[0]["coalesce_key"].startswith("kards:live-state:")
    assert push.calls[0]["metadata"]["session_id"] == ""

    assert publisher.publish(make_state(my_hq=13), text="T2").submitted
    assert len(push.calls) == 2
    assert publisher.expire(reason="test", now=clock.t + 1)
    assert len(push.calls) == 3
    assert "已失效" in push.calls[2]["parts"][0]["text"]
    assert push.calls[2]["metadata"]["context_expired"] is True
    assert publisher.cursor is None


def test_refresh_longer_than_max_age_does_not_refresh_before_ttl() -> None:
    clock = FakeClock()
    push = Recorder(True)
    publisher = LiveStatePublisher(push_message=push, now_fn=clock,
                                   refresh_seconds=30, max_age_seconds=10)
    state = make_state()
    assert publisher.publish(state, text="A").submitted
    clock.t += 9
    assert publisher.publish(make_state(), text="A").coalesced
    clock.t += 2  # TTL expired
    assert publisher.publish(make_state(), text="A").submitted
    assert len(push.calls) == 2


def test_dry_run_publisher_isolated_from_real_cursor() -> None:
    clock = FakeClock()
    push = Recorder(True)
    gate = HostPushGate(push, config=HostPushConfig(dry_run=True), now_fn=clock)
    publisher = LiveStatePublisher(gate=gate, now_fn=clock)
    result = publisher.publish(make_state(), text="DRY")
    assert result.status is PublishStatus.SUPPRESSED
    assert result.simulated and not result.submitted
    assert push.calls == []
    assert publisher.cursor is None
    assert publisher.simulation_cursor is not None

    publisher.update_config(dry_run=False)
    assert publisher.publish(make_state(my_hq=12), text="LIVE").submitted
    assert len(push.calls) == 1
    assert publisher.cursor is not None and publisher.cursor.complete


def test_consent_suppression_keeps_cursor_and_cleans_up_on_restore() -> None:
    clock = FakeClock()
    push = Recorder(True)
    gate = HostPushGate(push, now_fn=clock)
    publisher = LiveStatePublisher(gate=gate, now_fn=clock)
    assert publisher.publish(make_state(), text="A").submitted
    assert len(push.calls) == 1
    old_fingerprint = publisher.cursor.fingerprint

    publisher.update_config(consent=False)
    suppressed = publisher.publish(make_state(my_hq=10), text="B")
    assert suppressed.status is PublishStatus.SUPPRESSED
    assert not suppressed.delivered
    assert len(push.calls) == 1
    assert publisher.cursor is not None
    assert publisher.cursor.fingerprint == old_fingerprint  # no fake update
    assert publisher.pending_invalidation

    publisher.update_config(consent=True)
    clock.t += 1
    restored = publisher.publish(make_state(my_hq=10), text="B")
    assert restored.submitted
    # Tombstone for the old context, then the new state.
    assert len(push.calls) == 3
    assert push.calls[1]["metadata"]["context_expired"] is True
    assert push.calls[2]["metadata"]["context_expired"] is False
    assert not publisher.pending_invalidation


def test_expire_incomplete_retries_with_bounded_backoff_and_dedupes() -> None:
    clock = FakeClock()
    attempts = []

    def flaky(**kwargs):
        attempts.append(kwargs)
        return False

    publisher = LiveStatePublisher(push_message=flaky, now_fn=clock,
                                   expire_retry_base_seconds=1.0,
                                   expire_retry_max_seconds=4.0)
    assert not publisher.publish(make_state(), text="X")
    assert publisher.cursor is not None and not publisher.cursor.complete

    # First tombstone attempt fails; a duplicate call before the backoff is
    # suppressed (no second host attempt).
    assert not publisher.expire(reason="failed_publish")
    count = len(attempts)
    assert not publisher.expire(reason="failed_publish")
    assert len(attempts) == count

    clock.t += 1.0
    assert not publisher.expire(reason="failed_publish")
    assert len(attempts) == count + 1
    assert publisher.cursor is not None
    assert publisher.cursor.expire_attempts >= 1


def test_expire_unknown_receipt_is_terminal_and_never_retries() -> None:
    clock = FakeClock()
    push = Recorder({})
    publisher = LiveStatePublisher(push_message=push, now_fn=clock,
                                   expire_retry_base_seconds=2.0)
    # Unknown receipt: honest failure, but no immediate retry duplication.
    assert not publisher.publish(make_state(), text="X")
    assert publisher.cursor is not None

    push.result = {}  # tombstone also unknown
    assert not publisher.expire(reason="test")
    count = len(push.calls)
    assert not publisher.expire(reason="test")
    assert len(push.calls) == count
    assert publisher.cursor is not None
    assert publisher.cursor.confirmed is False

    # Advancing past any backoff must not re-send an unknown tombstone.
    clock.t += 100.0
    assert not publisher.expire(reason="test")
    assert len(push.calls) == count
    assert publisher.cursor is not None


def test_expire_legacy_none_receipt_is_terminal() -> None:
    clock = FakeClock()
    push = Recorder(None)
    publisher = LiveStatePublisher(push_message=push, now_fn=clock)
    assert not publisher.publish(make_state(), text="X")
    assert not publisher.expire(reason="test")
    count = len(push.calls)
    clock.t += 100.0
    assert not publisher.expire(reason="test")
    assert len(push.calls) == count
    assert publisher.cursor is not None
    assert publisher.cursor.confirmed is False


def test_unconfirmed_read_cursor_is_never_reported_as_coalesced() -> None:
    clock = FakeClock()
    push = Recorder({})
    publisher = LiveStatePublisher(push_message=push, now_fn=clock,
                                   refresh_seconds=30, max_age_seconds=60)
    first = publisher.publish(make_state(), text="X")
    assert first.status is PublishStatus.FAILED
    assert publisher.cursor is not None and publisher.cursor.confirmed is False

    second = publisher.publish(make_state(), text="X")
    assert second.status is PublishStatus.SUPPRESSED
    assert not second.delivered and not second.submitted
    assert second.reason == "unconfirmed_receipt"
    assert len(push.calls) == 1, "unconfirmed state is not re-pushed"


def test_dry_run_does_not_hide_consent_rejection() -> None:
    clock = FakeClock()
    push = Recorder(True)
    gate = HostPushGate(push, config=HostPushConfig(dry_run=True, consent=False),
                        now_fn=clock)
    publisher = LiveStatePublisher(gate=gate, now_fn=clock)

    result = publisher.publish(make_state(), text="X")
    assert result.status is PublishStatus.SUPPRESSED
    assert result.reason == "consent_required"
    assert push.calls == []

    entered = publisher.enter(make_state(), target="lanlan")
    assert entered.status is PublishStatus.SUPPRESSED
    assert entered.reason == "consent_required"
    assert push.calls == []


def test_publish_and_expire_use_expected_context_token() -> None:
    clock = FakeClock()
    push = Recorder(True)
    gate = HostPushGate(push, now_fn=clock)
    publisher = LiveStatePublisher(gate=gate, now_fn=clock)
    token = gate.get_token()
    gate.begin_stop("restart")
    gate.reopen()
    result = publisher.publish(make_state(), text="OLD", context_token=token)
    assert result.status is PublishStatus.FAILED
    assert result.reason in {"stale_context", "stale_epoch"}
    assert push.calls == []

    # A fresh token from the new epoch works.
    assert publisher.publish(make_state(), text="NEW").submitted


def test_context_enter_and_restore_use_gate_and_document_api() -> None:
    clock = FakeClock()
    push = Recorder(True)
    gate = HostPushGate(push, now_fn=clock)
    publisher = LiveStatePublisher(gate=gate, now_fn=clock, max_age_seconds=30)

    entered = publisher.enter(make_state(), target="lanlan", session_id="s1")
    assert entered.submitted
    call = push.calls[0]
    assert call["ai_behavior"] == "read" and call["visibility"] == []
    assert call["target_lanlan"] == "lanlan"
    assert call["metadata"]["context_action"] == "enter"
    assert call["metadata"]["context_api"]["enter"] == "LiveStatePublisher.enter"
    assert call["metadata"]["context_api"]["restore"] == "LiveStatePublisher.restore"
    assert LIVE_STATE_TOOL_NAME in call["parts"][0]["text"]

    restored = publisher.restore(make_state(), target="lanlan", session_id="s1")
    assert restored.submitted
    assert push.calls[1]["metadata"]["context_action"] == "restore"


def test_publish_failure_result_is_falsy_and_cursor_incomplete() -> None:
    publisher = LiveStatePublisher(push_message=lambda **kw: False)
    result = publisher.publish(make_state(), text="X")
    assert result.status is PublishStatus.FAILED
    assert not result.submitted and not result.delivered and bool(result) is False
    assert publisher.cursor is not None and not publisher.cursor.complete


def test_fingerprint_and_wire_text_contracts() -> None:
    a = make_state(turn_number=3)
    b = make_state(turn_number=3)
    assert semantic_fingerprint(a) == semantic_fingerprint(b)
    assert semantic_fingerprint(a) != semantic_fingerprint(make_state(turn_number=4))
    d = make_state(turn_number=3)
    d.units = [BoardUnit(instance_id=1, side="mine", attack=2, health=2)]
    assert semantic_fingerprint(d) != semantic_fingerprint(a)

    text = build_live_state_text(a, observed_at=123.0, session_id="s1")
    payload = json.loads(text.removeprefix("KARDS:"))
    assert payload["query_tool"] == LIVE_STATE_TOOL_NAME
    assert payload["session_id"] == "s1"
    assert "'kards_live_state'" not in text


def test_commentary_prompt_is_bounded_and_excludes_untrusted_payloads() -> None:
    state = make_state()
    for uid in range(30):
        state.units.append(BoardUnit(instance_id=uid,
                                     side="mine" if uid % 2 else "enemy",
                                     attack=1, health=1, cost=1))
    prompt = build_commentary_prompt("对手展开了虎式。", state, mode="companion")
    assert prompt.startswith("KARDS 陪玩解说边界")
    assert "教练模式" not in prompt
    assert "不读取、不描述、不猜测图片" in prompt
    assert len(prompt) <= 1600
    coach = build_commentary_prompt("x", state, mode="coach")
    assert "教练模式" in coach
    long_text = build_live_state_text(state)
    assert long_text.startswith("KARDS:")
    assert len(long_text.encode("utf-8")) <= 900 + 64
