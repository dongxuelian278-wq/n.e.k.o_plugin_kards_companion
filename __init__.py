"""KARDS 猫娘陪玩插件主类。

被动读屏（dxcam/ImageGrab）→ 可信帧门控 → 场景前置门控 → 结构化局势与单位履历
→ 优先级事件解说。无可信帧（窗口缺失/最小化/画面过期）时识别与解说安全暂停。

所有宿主推送（read/respond/context/expire/test）都经过同一个 HostPushGate。
生命周期使用 epoch + gate context token：感知 tick 在入口绑定 token，stop/reopen
之后旧 tick 在 gate 的提交锁内被拒绝。主 ``_lifecycle_lock`` 从不跨越宿主 push，
stop 的 ``begin_stop`` 在锁外线程中执行，避免与在途 push 互等。

目标运行时：宿主冻结 Python 3.11。只读屏：不读内存、不抓包、不代操作。
"""

import asyncio
import copy
import inspect
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    ui,
)

try:
    # The host message hook is registered only when the SDK exposes it (see
    # neko_wows ``@message(id="chat_quiet_window", source="chat")``).  The
    # fallback keeps the package importable under test stubs without pretending
    # the hook is active.
    from plugin.sdk.plugin import message as _host_message
except ImportError:  # pragma: no cover - exercised by the stub SDK
    _host_message = None


def _message_hook(**kwargs):
    if _host_message is not None:
        return _host_message(**kwargs)

    def decorate(func):
        return func

    return decorate


try:
    from plugin.sdk.plugin import PluginSettings, SettingsField

    class KardsSettings(PluginSettings):
        model_config = {"toml_section": "kards"}
        mode: str = SettingsField("companion", hot=True, enum=["companion", "coach", "silent"],
                                  description="陪伴 / 教练 / 静默")
        dry_run: bool = SettingsField(False, hot=True, description="只记录事件到 events.jsonl，不推送")
        auto_start_monitoring: bool = SettingsField(
            True, hot=True, description="启动后自动开始监控（旧习惯默认开启）")
        llm_data_consent: bool = SettingsField(
            True, hot=True, description="允许把结构化局势交给宿主 LLM 生成解说")
        chatter_interval_seconds: float = SettingsField(60.0, hot=True, ge=0, le=600,
                                                        description="平稳期主动点评间隔（0=关）")
        cooldown_seconds: float = SettingsField(25.0, hot=True, ge=1, le=300, description="普通事件冷却")
        critical_cooldown_seconds: float = SettingsField(5.0, hot=True, ge=1, le=300, description="紧急事件冷却")
        min_priority: int = SettingsField(4, hot=True, ge=1, le=9, description="解说优先级门槛")
        do_not_disturb: bool = SettingsField(False, hot=True, description="免打扰")
        perf_mode: str = SettingsField("realtime", hot=True, enum=["realtime", "power_saver"],
                                       description="实时优先 / 省电")
        tick_ms: float = SettingsField(
            333.0, hot=True, ge=150, le=5000,
            description="主循环上限；实际采样由预设/四项 FPS 决定")
        perception_preset: str = SettingsField(
            "balanced", hot=True, enum=["low_cost", "balanced", "quality", "custom"],
            description="采样预设：低负载 / 平衡 / 质量 / 自定义")
        capture_fps: float = SettingsField(20.0, hot=True, ge=1, le=30, description="采集帧率")
        change_fps: float = SettingsField(10.0, hot=True, ge=0.5, le=15, description="画面变化检测帧率")
        hud_fps: float = SettingsField(4.0, hot=True, ge=0.5, le=6, description="HUD OCR 帧率")
        unit_fps: float = SettingsField(8.0, hot=True, ge=0.5, le=12, description="单位跟踪帧率")
        capture_fps_power_saver: float = SettingsField(
            4.0, hot=True, ge=0.5, le=30, description="旧版省电采集帧率（兼容保留）")
        roi_auto_calibrate: bool = SettingsField(
            True, hot=True, description="客户区 ROI 自动校准")
        recording_enabled: bool = SettingsField(
            False, hot=True, description="局内临时帧录制（默认关；不会发给 LLM）")
        recording_max_mb: int = SettingsField(
            2048, hot=True, ge=64, le=65536,
            description="临时帧滚动上限 MiB（64..65536，含 manifest）")
        catalog_enabled: bool = SettingsField(False, hot=True, description="卡牌库联网更新（离线加载始终可用）")
        catalog_url: str = SettingsField("", hot=True, description="卡牌数据源 URL（空=内置候选）")
        catalog_refresh_hours: float = SettingsField(24.0, hot=True, ge=1, le=720)
        vlm_enabled: bool = SettingsField(False, hot=True, description="VLM 兜底（密钥仅环境变量）")
        target_lanlan: str = SettingsField(
            "", hot=True, description="目标猫娘；空=跟随当前角色（绝不伪造角色名）")
        plugin_owned_output_enabled: bool = SettingsField(
            False, hot=True, description="允许插件自有输出；默认由宿主 LLM 生成短回复")
        user_chat_quiet_window_seconds: float = SettingsField(
            60.0, hot=True, ge=0, le=600, description="用户聊天后的普通播报安静窗")
        battle_output_quiet_window_seconds: float = SettingsField(
            30.0, hot=True, ge=0, le=600, description="普通战斗播报安静窗")
        battle_output_backpressure_seconds: float = SettingsField(
            20.0, hot=True, ge=0, le=600, description="普通播报背压窗")
        reply_max_chars: int = SettingsField(
            28, hot=True, ge=1, le=200, description="短 TTS 台词字数上限")
        battle_event_ttl_seconds: float = SettingsField(
            12.0, hot=True, ge=0, le=120, description="战斗事件最大年龄（秒）")
        imported_deck_code: str = SettingsField("", hot=True, description="已导入卡组代码")
except ImportError:  # 老 SDK 无声明式热设置，退化为 config.dump 快照
    KardsSettings = None  # type: ignore[assignment]
    SettingsField = None  # type: ignore[assignment]

from .adapters.target import clean_target_lanlan, resolve_target_lanlan
from .delivery.arbiter import ArbiterConfig, CommentaryArbiter
from .delivery.contracts import (
    STATUS_ACCEPTED,
    STATUS_LEGACY_UNCONFIRMED,
    STATUS_UNKNOWN,
    GateResult,
    HostPushConfig,
    HostPushGate,
)
from .delivery.publisher import (
    LiveStatePublisher,
    build_commentary_prompt,
    build_live_state_text,
)
from .policy.dispatcher import DeliveryDispatcher, DispatcherConfig
from .policy.safety import (
    OutputSafetyConfig,
    OutputSafetyGuard,
    SafetyCircuitBreaker,
    SafetyConfig,
)
from .runtime.contracts import normalize_event
from .runtime.detectors import DetectorRegistry
from .runtime.health import CaptureHealthSnapshot
from .runtime.match_adapter import MatchTrackerAdapter
from .runtime.scenario import KardsScenarioResolver
from .runtime.snapshot import FrameMeta, RuntimeSnapshot
from .runtime.timeline import RuntimeTimeline
from .knowledge.catalog import CardCatalog, CatalogError
from .knowledge.deck_code import DeckCodeError, ParsedDeck, parse_deck_code
from .perception.capture import (
    ScreenCapture,
    client_bbox,
    dhash,
    hamming,
    is_kards_process,
)
from .perception.detector import UnitTracker, YoloDetector
from .perception.hud_reader import HudReader
from .perception.regions import RegionCalibrator
from .perception.scheduling import (
    FPS_KEYS,
    PerceptionScheduler,
    PerceptionSettings,
    SettingsValidationError,
)
from .perception.screens import ScreenClassifier
from .runtime.recording import RecordingConfig, TemporaryFrameRecorder
from .state.model import (
    E_MATCH_END,
    SCENE_DEFEAT,
    SCENE_MATCH,
    SCENE_MENU,
    SCENE_UNKNOWN,
    SCENE_VICTORY,
    GameEvent,
    MatchState,
)
from .state.tracker import MatchTracker


PLUGIN_ID = "kards_companion"
STATUS_SCHEMA_VERSION = "kards_status_v1"
_LIVE_STATE_TOOL_NAME = "kards_get_live_state"
_MODEL_PATH = Path(__file__).resolve().parent / "models" / "kards_det.onnx"
_VALID_SCENES = frozenset({SCENE_MENU, SCENE_MATCH, SCENE_VICTORY, SCENE_DEFEAT})

# A's runtime.scheduling is the single source of truth for preset values and
# hard caps.  Order is capture / change / HUD / unit.
PERCEPTION_FPS_KEYS: tuple[str, ...] = FPS_KEYS
PERCEPTION_FPS_PRESETS: dict[str, dict[str, float]] = {
    name: dict(zip(PERCEPTION_FPS_KEYS, values))
    for name, values in PerceptionSettings.PRESETS.items()
}
PERCEPTION_FPS_CAPS: dict[str, float] = {
    key: float(limits[1])
    for key, limits in PerceptionSettings.LIMITS.items()
    if key in PERCEPTION_FPS_KEYS
}
PERCEPTION_FPS_MINS: dict[str, float] = {
    key: float(limits[0])
    for key, limits in PerceptionSettings.LIMITS.items()
    if key in PERCEPTION_FPS_KEYS
}
PERCEPTION_PRESET_VALUES = frozenset({*PERCEPTION_FPS_PRESETS, "custom"})
RECORDING_JPEG_QUALITY_DEFAULT = 85  # fixed user decision
RECORDING_MAX_MB_MIN = 64
RECORDING_MAX_MB_MAX = 65536
RECORDING_MAX_MB_DEFAULT = 2048
PERCEPTION_SETTINGS_ACTION_KEYS = frozenset(PerceptionSettings.CONFIG_KEYS)

# Config defaults are centralized so the manifest, runtime snapshot and tests
# cannot drift.  Explicit user values always win; these are only fallbacks.
_CONFIG_DEFAULTS: dict[str, Any] = {
    "mode": "companion",
    "dry_run": False,
    "auto_start_monitoring": True,
    "llm_data_consent": True,
    "chatter_interval_seconds": 60.0,
    "cooldown_seconds": 25.0,
    "critical_cooldown_seconds": 5.0,
    "min_priority": 4,
    "do_not_disturb": False,
    "perf_mode": "realtime",
    "tick_ms": 333.0,
    "perception_preset": "balanced",
    "capture_fps": 20.0,
    "change_fps": 10.0,
    "hud_fps": 4.0,
    "unit_fps": 8.0,
    "capture_fps_power_saver": 4.0,
    "roi_auto_calibrate": True,
    "recording_enabled": False,
    "recording_max_mb": RECORDING_MAX_MB_DEFAULT,
    "recording_jpeg_quality": RECORDING_JPEG_QUALITY_DEFAULT,
    "recording_scope": "match",
    "catalog_enabled": False,
    "catalog_url": "",
    "catalog_refresh_hours": 24.0,
    "vlm_enabled": False,
    "target_lanlan": "",
    "plugin_owned_output_enabled": False,
    "user_chat_quiet_window_seconds": 60.0,
    "battle_output_quiet_window_seconds": 30.0,
    "battle_output_backpressure_seconds": 20.0,
    "reply_max_chars": 28,
    "battle_event_ttl_seconds": 12.0,
    "imported_deck_code": "",
}


def _num(value: Any, default: float, *, minimum: float | None = None,
         maximum: float | None = None) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        out = float(default)
    if minimum is not None:
        out = max(minimum, out)
    if maximum is not None:
        out = min(maximum, out)
    return out


def _integer(value: Any, default: int, *, minimum: int | None = None,
             maximum: int | None = None) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        out = int(default)
    if minimum is not None:
        out = max(minimum, out)
    if maximum is not None:
        out = min(maximum, out)
    return out


def _boolean(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _tick_seconds(cfg: dict[str, Any]) -> float:
    return max(0.15, _num(cfg.get("tick_ms"), 333.0) / 1000.0)


def _legacy_perception_aliases(raw: dict[str, Any]) -> dict[str, Any]:
    """Map provisional/legacy names onto A's canonical scheduling keys."""
    mapped = dict(raw)
    aliases = {
        "detect_fps": "change_fps",
        "ocr_fps": "hud_fps",
        "tracker_fps": "unit_fps",
    }
    for old, new in aliases.items():
        if new not in mapped and old in mapped:
            mapped[new] = mapped[old]
    if str(mapped.get("perception_preset") or "").strip().lower() == "low":
        mapped["perception_preset"] = "low_cost"
    if "roi_auto_calibrate" not in mapped:
        if "auto_calibrate" in mapped:
            mapped["roi_auto_calibrate"] = mapped["auto_calibrate"]
        elif str(mapped.get("roi_mode") or "client").strip().lower() == "client":
            mapped["roi_auto_calibrate"] = True
    if "recording_max_mb" not in mapped and "recording_max_bytes" in mapped:
        try:
            mapped["recording_max_mb"] = int(
                float(mapped["recording_max_bytes"]) / (1024 * 1024))
        except (TypeError, ValueError, OverflowError):
            pass
    return mapped


def _perception_settings_from_config(cfg: dict[str, Any]) -> PerceptionSettings:
    """Parse the canonical settings, failing closed to safe defaults."""
    try:
        return PerceptionSettings.from_config(_legacy_perception_aliases(cfg))
    except SettingsValidationError:
        return PerceptionSettings()
    except Exception:
        return PerceptionSettings()


def _perception_read_model(settings: PerceptionSettings,
                           cfg: dict[str, Any]) -> dict[str, Any]:
    """Serializable read model shared by status, settings and the UI."""
    del cfg  # Settings are the single source of truth; no shadow toggles.
    return {
        "perception_preset": settings.preset,
        "preset": settings.preset,
        "fps": settings.rates,
        "capture_fps": settings.capture_fps,
        "change_fps": settings.change_fps,
        "hud_fps": settings.hud_fps,
        "unit_fps": settings.unit_fps,
        "roi_auto_calibrate": settings.roi_auto_calibrate,
        "evidence_enabled": settings.evidence_enabled,
        "recording_enabled": settings.recording_enabled,
        "recording_max_mb": settings.recording_max_mb,
        "recording_jpeg_quality": RECORDING_JPEG_QUALITY_DEFAULT,
        "recording_scope": "match",
    }


def _capture_fps(cfg: dict[str, Any]) -> float:
    return float(_perception_settings_from_config(cfg).capture_fps)


def _loop_seconds(cfg: dict[str, Any]) -> float:
    """Poll at least as fast as the capture rate, capped by legacy tick_ms."""
    capture_interval = 1.0 / max(0.5, _capture_fps(cfg))
    return max(0.02, min(_tick_seconds(cfg), capture_interval))


def _result_payload(result: Any) -> Any:
    """Unwrap Ok/Err across SDK objects and the test stub's dict shape."""
    if isinstance(result, dict):
        if result.get("ok") is False:
            return result.get("error", result)
        return result.get("data", result.get("value"))
    for name in ("data", "value"):
        value = getattr(result, name, None)
        if value is not None:
            return value
    return None


def _result_is_error(result: Any) -> bool:
    if isinstance(result, dict):
        return result.get("ok") is False
    for name in ("is_error", "is_err"):
        probe = getattr(result, name, None)
        if callable(probe):
            try:
                if probe():
                    return True
            except Exception:
                pass
    return False


def _store_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    inner = getattr(value, "value", None)
    if inner is not None:
        return inner
    if isinstance(value, dict) and "ok" in value:
        return value.get("data", default)
    return value


def _supports_kwarg(func: Any, name: str) -> bool:
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


@neko_plugin
class KardsCompanionPlugin(NekoPluginBase):
    """生命周期、感知循环调度与对外 entry/tool 面。"""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.logger = self.enable_file_logging(
            log_level=(os.environ.get("NEKO_LOG_LEVEL") or "INFO").strip().upper())
        self._cfg: dict[str, Any] = dict(_CONFIG_DEFAULTS)
        self._perception_settings = _perception_settings_from_config(self._cfg)
        self.scheduler = PerceptionScheduler(
            self._perception_settings, clock=time.monotonic)
        self._scheduler_lock = threading.Lock()
        self.region_calibrator = RegionCalibrator(
            enabled=self._perception_settings.roi_auto_calibrate,
            clock=time.monotonic)
        self._region_lock = threading.Lock()
        self._monitoring = False
        self._started_on_loop = False
        self._startup_armed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._capture: ScreenCapture | None = None
        self._loop_task: asyncio.Task | None = None
        self._catalog_task: asyncio.Task | None = None
        self._ocr: Any = None
        self._detector: YoloDetector | None = None
        self._unit_tracker = UnitTracker()
        self._unit_lock = threading.Lock()
        self._last_unit_diagnostics: dict[str, Any] = {}
        # Missing/zero capture ids get a unique positive id from this
        # lifecycle-guarded counter, used by both the scheduler and evidence.
        self._capture_frame_seq = 0
        self._deck: ParsedDeck | None = None
        self._hud_cache: dict[str, Any] = {}
        self._hud_hash: int | None = None
        self._applied_catalog_url: str | None = None
        self._events_log: list[dict[str, Any]] = []
        self._last_detector_diagnostics: list[dict[str, Any]] = []
        self._pipeline_started = False
        self._last_start_error = ""
        self._capture_pause_active = False
        self._pause_reason = ""
        self._user_paused = False
        self._last_scene = SCENE_UNKNOWN
        self._classification_valid = False
        self._confirmed_exit_handled = False
        self._pending_rearm: list[GameEvent] = []

        # Lifecycle mutex: guards session ownership (capture/tasks/epoch) and
        # the in-memory pipeline state.  It must NEVER be held across a host
        # push or a gate begin_stop call.
        self._lifecycle_lock = threading.RLock()
        self._epoch = 0
        self._ticks_in_flight = 0
        self._ticks_idle = threading.Event()
        self._ticks_idle.set()
        # Serializes async lifecycle transitions across event loops.  The lock
        # is acquired in an executor so holding it across awaits never blocks
        # the asyncio command loop.
        self._transition_guard = threading.Lock()
        self._session_id = ""
        self.catalog = CardCatalog(self.data_path("cards.json"))

        # Runtime identity: the installed plugin may be ``kards_companion_dev``
        # or any other real id.  Never label a dev install as the formal id.
        self._runtime_id, self._source_identity = self._resolve_runtime_identity()

        self.timeline = RuntimeTimeline()
        self.scenario = KardsScenarioResolver()
        self.tracker = MatchTracker()
        self.match_detector = MatchTrackerAdapter(
            self.tracker,
            chatter_interval_s=60.0,
            source="match_tracker",
            ttl_s=None,
        )
        self.arbiter = CommentaryArbiter(ArbiterConfig())
        self.gate = HostPushGate(
            self.push_message,
            config=HostPushConfig(
                dry_run=False, consent=True, simulated=False,
                authorized_real_test=False),
            now_fn=time.time,
        )
        self.publisher = LiveStatePublisher(
            gate=self.gate,
            logger=self.logger,
            source=self._runtime_id,
            now_fn=time.time,
            refresh_seconds=30.0,
            max_age_seconds=60.0,
        )
        self.breaker = SafetyCircuitBreaker(SafetyConfig(), now_fn=time.time)
        self.read_breaker = SafetyCircuitBreaker(SafetyConfig(), now_fn=time.time)
        self.guard = OutputSafetyGuard(OutputSafetyConfig(), now_fn=time.time)
        self.dispatcher = DeliveryDispatcher(
            self.arbiter,
            gate=self.gate,
            read_publisher=self.publisher,
            breaker=self.breaker,
            read_breaker=self.read_breaker,
            safety_guard=self.guard,
            config=self._dispatcher_config(),
            now_fn=time.time,
            timeline=self.timeline,
            logger=self.logger,
            target_resolver=lambda event: resolve_target_lanlan(self, event),
            on_delivered=self._on_delivered,
        )

        self.detectors = DetectorRegistry()
        self.detectors.add(self.match_detector)
        self._snapshot_seq = 0
        self._previous_snapshot: RuntimeSnapshot | None = None
        self._last_trusted_snapshot: RuntimeSnapshot | None = None
        self._last_capture_health = CaptureHealthSnapshot()

        # Persistence futures are drained on shutdown/stop instead of being
        # fire-and-forget across a closing command loop.
        self._persist_futures: set[Any] = set()
        self._pending_persist: list[dict[str, Any]] = []
        self._persisted_match_keys: set[str] = set()

        # Local-only recording never touches host consent or the LLM path.
        self._recorder_io_lock = threading.Lock()
        self.recorder = self._make_recorder()
        self._recording_match_live = False
        # Scene-scoped gate read by the capture producer thread.  It is only
        # true for a confirmed in-match frame with recording enabled, so a
        # paused/menu/unknown frame can never enter the recorder queue.
        self._recording_in_match = False
        self._recording_accepting = False

        # Cross-tick canonical state.  HUD/unit work is scheduled independently
        # of classification, so a tick that skips one task must carry the last
        # known value forward instead of feeding a false empty board to the
        # tracker.
        self._pipeline_state = MatchState()
        self._region_signature: Any = None

    # ------------------------------------------------------------- identity

    def _resolve_runtime_identity(self) -> tuple[str, str]:
        for value in (getattr(self, "plugin_id", ""),
                      getattr(getattr(self, "ctx", None), "plugin_id", "")):
            text = str(value or "").strip()
            if text:
                return text[:160], "sdk"
        return PLUGIN_ID, "manifest_fallback"

    @property
    def runtime_plugin_id(self) -> str:
        return self._runtime_id

    # ------------------------------------------------------------------ 配置

    def _dispatcher_config(self, *, authorized_real_test: bool = False) -> DispatcherConfig:
        target = clean_target_lanlan(self._cfg.get("target_lanlan"))
        kwargs: dict[str, Any] = {
            "source": self._runtime_id,
            "target_lanlan": target,
            "plugin_owned_output_enabled": _boolean(
                self._cfg.get("plugin_owned_output_enabled"), False),
            "reply_max_chars": _integer(self._cfg.get("reply_max_chars"), 28,
                                        minimum=1, maximum=200),
            "battle_event_ttl_seconds": _num(
                self._cfg.get("battle_event_ttl_seconds"), 12.0,
                minimum=0.0, maximum=120.0),
            "user_chat_quiet_window_seconds": _num(
                self._cfg.get("user_chat_quiet_window_seconds"), 60.0,
                minimum=0.0, maximum=600.0),
            "battle_output_quiet_window_seconds": _num(
                self._cfg.get("battle_output_quiet_window_seconds"), 30.0,
                minimum=0.0, maximum=600.0),
            "battle_output_backpressure_seconds": _num(
                self._cfg.get("battle_output_backpressure_seconds"), 20.0,
                minimum=0.0, maximum=600.0),
            "dry_run": _boolean(self._cfg.get("dry_run"), False),
            "llm_data_consent": _boolean(self._cfg.get("llm_data_consent"), True),
            "simulated": False,
            "authorized_real_test": bool(authorized_real_test),
            "known_targets": (target,) if target else (),
        }
        accepted = {
            field.name for field in getattr(DispatcherConfig, "__dataclass_fields__", {}).values()
        } if hasattr(DispatcherConfig, "__dataclass_fields__") else set(kwargs)
        return DispatcherConfig(**{key: value for key, value in kwargs.items()
                                   if key in accepted})

    def _apply_config(self, cfg: dict[str, Any]) -> None:
        raw = cfg.get("kards") if isinstance(cfg.get("kards"), dict) else cfg
        raw = dict(raw or {})
        normalized: dict[str, Any] = {}
        for key, default in _CONFIG_DEFAULTS.items():
            value = raw.get(key, default)
            if isinstance(default, bool):
                normalized[key] = _boolean(value, default)
            elif isinstance(default, int):
                normalized[key] = _integer(value, default)
            elif isinstance(default, float):
                normalized[key] = _num(value, default)
            else:
                normalized[key] = "" if value is None else str(value)
        normalized["mode"] = str(normalized.get("mode") or "companion").strip().lower()
        if normalized["mode"] not in ("companion", "coach", "silent"):
            normalized["mode"] = "companion"
        normalized["perf_mode"] = str(normalized.get("perf_mode") or "realtime").strip().lower()
        if normalized["perf_mode"] not in ("realtime", "power_saver"):
            normalized["perf_mode"] = "realtime"
        normalized["min_priority"] = _integer(normalized.get("min_priority"), 4,
                                              minimum=1, maximum=9)
        normalized["reply_max_chars"] = _integer(normalized.get("reply_max_chars"), 28,
                                                 minimum=1, maximum=200)
        normalized["target_lanlan"] = clean_target_lanlan(normalized.get("target_lanlan"))

        perception_cfg = _legacy_perception_aliases(raw)
        try:
            perception = PerceptionSettings.from_config(perception_cfg)
        except SettingsValidationError as exc:
            self.logger.warning("perception settings rejected, using defaults: {}", exc)
            perception = PerceptionSettings()
        normalized["perception_preset"] = perception.preset
        for key in PERCEPTION_FPS_KEYS:
            normalized[key] = float(getattr(perception, key))
        normalized["roi_auto_calibrate"] = perception.roi_auto_calibrate
        normalized["recording_enabled"] = perception.recording_enabled
        normalized["recording_max_mb"] = perception.recording_max_mb
        # Fixed user decisions: JPEG quality 85, in-match only.  These are not
        # user-editable and never enter the settings action.
        normalized["recording_jpeg_quality"] = RECORDING_JPEG_QUALITY_DEFAULT
        normalized["recording_scope"] = "match"

        previous = dict(self._cfg)
        self._cfg = normalized
        self._perception_settings = perception
        try:
            with self._scheduler_lock:
                self.scheduler.update(perception)
        except Exception as exc:
            self.logger.warning("perception scheduler update failed: {}: {}",
                                type(exc).__name__, exc)
        self._sync_region_calibrator(previous)

        self.arbiter.update(ArbiterConfig(
            mode=normalized["mode"],
            do_not_disturb=normalized["do_not_disturb"],
            llm_data_consent=normalized["llm_data_consent"],
            min_priority=normalized["min_priority"],
            cooldown_seconds=_num(normalized.get("cooldown_seconds"), 25.0,
                                  minimum=1.0, maximum=300.0),
            critical_cooldown_seconds=_num(
                normalized.get("critical_cooldown_seconds"), 5.0,
                minimum=1.0, maximum=300.0),
            max_event_age_seconds=_num(
                normalized.get("battle_event_ttl_seconds"), 12.0,
                minimum=0.0, maximum=120.0),
        ))
        self.match_detector.chatter_interval_s = _num(
            normalized.get("chatter_interval_seconds"), 60.0,
            minimum=0.0, maximum=600.0)

        endpoint = str(normalized.get("catalog_url") or "").strip()
        if endpoint != (self._applied_catalog_url or ""):
            try:
                self.catalog = CardCatalog(self.data_path("cards.json"), endpoint=endpoint)
                self._applied_catalog_url = endpoint
            except CatalogError as exc:
                self.logger.warning("catalog_url rejected, keep previous source: {}", exc)

        code = str(normalized.get("imported_deck_code") or "")
        if code:
            try:
                self._deck = parse_deck_code(code)
            except DeckCodeError as exc:
                self.logger.warning("stored deck invalid: {}", exc)

        # One config object drives dispatcher, gate consent/dry state and the
        # publisher's gate view.  Any change purges the pending slot inside C.
        try:
            self.dispatcher.update_config(self._dispatcher_config())
        except Exception as exc:
            self.logger.warning("dispatcher config update failed: {}: {}",
                                type(exc).__name__, exc)

        if self._capture is not None:
            try:
                self._capture.fps = _capture_fps(self._cfg)
            except Exception:
                pass
        self._refresh_recording_config(previous)

        target_changed = previous.get("target_lanlan") != normalized.get("target_lanlan")
        if target_changed and self._monitoring:
            self._expire_read_context("target_changed", token=self.gate.get_token())

    # ------------------------------------------------------- 感知 / 录制设置

    def _sync_region_calibrator(self, previous: dict[str, Any] | None = None) -> None:
        enabled = bool(self._perception_settings.roi_auto_calibrate)
        with self._region_lock:
            if self.region_calibrator.enabled == enabled:
                return
            self.region_calibrator = RegionCalibrator(
                enabled=enabled, clock=time.monotonic)
            regions = self.region_calibrator.regions
        hud = getattr(self, "hud", None)
        if hud is not None:
            try:
                hud.set_regions(regions)
            except Exception:
                pass
        with self._lifecycle_lock:
            self._hud_hash = None
            self._hud_cache = {}
            self._region_signature = None

    def _region_snapshot(self) -> dict[str, Any]:
        with self._region_lock:
            return self.region_calibrator.snapshot()

    def _perception_settings_payload(self) -> dict[str, Any]:
        settings = self._perception_settings
        with self._scheduler_lock:
            task_snapshot = self.scheduler.snapshot()
        tasks = {}
        for task, payload in task_snapshot.get("tasks", {}).items():
            tasks[task] = {
                "fps": payload.get("fps"),
                "actual": payload.get("actual"),
                "missed": payload.get("missed"),
                "last_duration_ms": payload.get("last_duration_ms"),
                "next_in_ms": payload.get("next_in_ms"),
            }
        return {
            **_perception_read_model(settings, self._cfg),
            "presets": {
                name: dict(values) for name, values in PERCEPTION_FPS_PRESETS.items()
            },
            "caps": dict(PERCEPTION_FPS_CAPS),
            "mins": dict(PERCEPTION_FPS_MINS),
            "scheduler": {
                "settings": settings.to_dict(),
                "tasks": tasks,
            },
            "regions": self._region_snapshot(),
        }

    def _recording_config(self) -> RecordingConfig:
        settings = self._perception_settings
        return RecordingConfig(
            enabled=bool(settings.recording_enabled),
            max_bytes=int(settings.recording_max_mb) * 1024 * 1024,
            jpeg_quality=RECORDING_JPEG_QUALITY_DEFAULT,
        )

    def _make_recorder(self) -> TemporaryFrameRecorder | None:
        try:
            recorder = TemporaryFrameRecorder(
                Path(self.data_path("recording")),
                config=self._recording_config(),
                clock=time.time,
                logger=self.logger,
            )
        except Exception as exc:
            self.logger.warning("recording recorder init failed: {}: {}",
                                type(exc).__name__, exc)
            return None
        return recorder

    def _recording_payload(self) -> dict[str, Any]:
        settings = self._perception_settings
        recorder = getattr(self, "recorder", None)
        raw: dict[str, Any] = {}
        if recorder is not None:
            try:
                raw = recorder.snapshot()
            except Exception as exc:
                self.logger.warning("recording snapshot failed: {}: {}",
                                    type(exc).__name__, exc)
                raw = {"state": "error", "error": type(exc).__name__}
        raw_state = str(raw.get("state") or "")
        state = {
            "recording": "active",
            "paused": "paused",
            "idle": "idle",
            "stopped": "closed",
            "disabled": "off",
        }.get(raw_state, "unknown")
        if not settings.recording_enabled:
            state = "off"
        elif recorder is None:
            state = "unavailable"
        max_bytes = _integer(
            raw.get("max_bytes"),
            int(settings.recording_max_mb) * 1024 * 1024, minimum=0)
        return {
            "enabled": bool(settings.recording_enabled),
            "available": recorder is not None,
            "active": bool(raw.get("active")) and state == "active",
            "state": state,
            "raw_state": raw_state,
            "reason": str(raw.get("reason") or ""),
            "scope": "match",
            "jpeg_quality": RECORDING_JPEG_QUALITY_DEFAULT,
            "full_resolution": True,
            "local_only": True,
            "max_bytes": int(max_bytes),
            "bytes": _integer(raw.get("bytes"), 0, minimum=0),
            "frame_count": _integer(
                raw.get("written", raw.get("files", 0)), 0, minimum=0),
            "captured": _integer(raw.get("captured"), 0, minimum=0),
            "files": _integer(raw.get("files"), 0, minimum=0),
            "dropped": _integer(raw.get("dropped"), 0, minimum=0),
            "pending": _integer(raw.get("pending"), 0, minimum=0),
            "clean": bool(raw.get("clean", not raw.get("error"))),
            "error": str(raw.get("error") or ""),
        }

    def _refresh_recording_config(self, previous: dict[str, Any] | None = None) -> None:
        recorder = getattr(self, "recorder", None)
        if recorder is not None:
            try:
                with self._recorder_io_lock:
                    recorder.update_config(self._recording_config())
            except Exception as exc:
                self.logger.warning("recording config update failed: {}: {}",
                                    type(exc).__name__, exc)
        enabled = bool(self._perception_settings.recording_enabled)
        with self._lifecycle_lock:
            if not enabled:
                self._recording_accepting = False
            else:
                self._recording_accepting = bool(
                    self._recording_match_live and self._recording_in_match
                    and self._monitoring and not self._user_paused)

    def _recording_set_active(self, active: bool, reason: str) -> None:
        recorder = getattr(self, "recorder", None)
        active = bool(active)
        with self._lifecycle_lock:
            self._recording_accepting = bool(
                active and self._perception_settings.recording_enabled
                and self._recording_match_live
                and self._recording_in_match and self._monitoring
                and not self._user_paused)
        if recorder is None:
            return
        try:
            with self._recorder_io_lock:
                recorder.set_active(active, reason)
        except Exception as exc:
            self.logger.warning("recording set_active failed: {}: {}",
                                type(exc).__name__, exc)

    def _start_recording_match(self, session_id: str) -> None:
        recorder = getattr(self, "recorder", None)
        if recorder is None or not self._perception_settings.recording_enabled:
            return
        with self._lifecycle_lock:
            if not self._monitoring or self._user_paused:
                return
            match_id = int(self.tracker.game_number or 0)
        try:
            with self._recorder_io_lock:
                recorder.start_match(session_id, match_id)
                recorder.set_active(True, "in_match")
        except Exception as exc:
            self.logger.warning("recording start_match failed: {}: {}",
                                type(exc).__name__, exc)
            return
        with self._lifecycle_lock:
            self._recording_match_live = True
            self._recording_in_match = True
            self._recording_accepting = bool(
                self._perception_settings.recording_enabled
                and not self._user_paused)
        self.timeline.record(
            "recording", "started", ts=time.time(),
            detail={"session_id": str(session_id or ""),
                    "match_id": int(match_id),
                    "jpeg_quality": RECORDING_JPEG_QUALITY_DEFAULT,
                    "scope": "match"})

    def _end_recording_match(self, reason: str) -> None:
        recorder = getattr(self, "recorder", None)
        with self._lifecycle_lock:
            self._recording_match_live = False
            self._recording_in_match = False
            self._recording_accepting = False
        if recorder is None:
            return
        before: dict[str, Any] = {}
        try:
            with self._recorder_io_lock:
                before = recorder.snapshot()
                recorder.end_match(reason)
        except Exception as exc:
            self.logger.warning("recording end_match failed: {}: {}",
                                type(exc).__name__, exc)
            return
        self.timeline.record(
            "recording", "closed", ts=time.time(), reason=str(reason),
            detail={"bytes": _integer(before.get("bytes"), 0, minimum=0),
                    "frame_count": _integer(
                        before.get("written", before.get("files", 0)), 0,
                        minimum=0),
                    "clean": bool(before.get("clean", True))})

    def _stop_recording(self, reason: str) -> None:
        recorder = getattr(self, "recorder", None)
        with self._lifecycle_lock:
            self._recording_match_live = False
            self._recording_in_match = False
            self._recording_accepting = False
        if recorder is None:
            return
        before: dict[str, Any] = {}
        try:
            with self._recorder_io_lock:
                before = recorder.snapshot()
                recorder.stop(reason)
            snapshot = recorder.snapshot()
        except Exception as exc:
            self.logger.warning("recording stop failed: {}: {}",
                                type(exc).__name__, exc)
            return
        self.timeline.record(
            "recording", "stopped", ts=time.time(), reason=str(reason),
            detail={"bytes": _integer(before.get("bytes"), 0, minimum=0),
                    "frame_count": _integer(
                        before.get("written", before.get("files", 0)), 0,
                        minimum=0),
                    "clean": bool(snapshot.get("clean", True))})
        if not bool(snapshot.get("clean", True)):
            self.timeline.record(
                "recording", "unclean_stop", ts=time.time(),
                reason=str(snapshot.get("error") or "pending_cleanup"),
                detail={"state": str(snapshot.get("state") or "")})

    @staticmethod
    def _recording_frame_allowed(result: Any) -> bool:
        """Only a validated KARDS client-area frame may reach the recorder.

        A desktop backend's client bbox is the sole crop source.  An unknown
        source, missing window identity, non-KARDS process, or a frame whose
        pixel size differs from that client bbox is refused: a full-desktop
        frame must never be written.
        """
        if result is None:
            return False
        if getattr(result, "trusted", False) is not True:
            return False
        image = getattr(result, "image", None)
        window = getattr(result, "window", None)
        source = str(getattr(result, "source", "")
                     or getattr(result, "backend", "") or "")
        if image is None or window is None:
            return False
        if source not in {"host_wgc", "dxcam", "imagegrab"}:
            return False
        process = str(getattr(window, "process_name", "") or "")
        if process and not is_kards_process(process):
            return False
        bbox = client_bbox(window)
        if not bbox or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            return False
        width = _integer(getattr(result, "width", 0), 0, minimum=0)
        height = _integer(getattr(result, "height", 0), 0, minimum=0)
        try:
            image_size = tuple(int(value) for value in image.size)
        except Exception:
            return False
        if image_size != (width, height):
            return False
        return (width, height) == (bbox[2] - bbox[0], bbox[3] - bbox[1])

    def _on_capture_frame(self, result: Any) -> None:
        """Capture producer callback: never block on encoder or disk I/O.

        ``submit`` copies the pixels and performs a bounded ``put_nowait``;
        JPEG encoding and rolling deletes happen on the recorder worker.
        """
        if not self._recording_accepting:
            return
        recorder = getattr(self, "recorder", None)
        if recorder is None or not self._recording_match_live:
            return
        if not self._recording_frame_allowed(result):
            return
        try:
            recorder.submit(result)
        except Exception as exc:
            self.logger.warning("recording submit failed: {}: {}",
                                type(exc).__name__, exc)

    async def _reload_config(self) -> None:
        try:
            cfg = await self.config.dump(timeout=5.0)
        except Exception as exc:
            self.logger.warning("config dump failed: {}: {}", type(exc).__name__, exc)
            return
        self._apply_config(cfg if isinstance(cfg, dict) else {})

    @lifecycle(id="startup")
    async def startup(self, **_):
        await self._reload_config()
        if self.recorder is not None:
            try:
                cleanup = await asyncio.to_thread(self.recorder.cleanup_orphans)
                if cleanup.get("status") not in ("ok", "missing"):
                    self.logger.warning("recording orphan cleanup: {}", cleanup)
            except Exception as exc:
                self.logger.warning("recording orphan cleanup failed: {}: {}",
                                    type(exc).__name__, exc)
        self._startup_armed = True
        # Never start long-lived tasks on the SDK's transient startup loop.
        self._maybe_schedule_auto_start()
        return Ok({"status": "ready", "monitoring": self._monitoring,
                   "runtime_plugin_id": self._runtime_id,
                   "source_identity": self._source_identity})

    @lifecycle(id="config_change")
    async def on_config_change(self, **_):
        await self._reload_config()
        self._maybe_schedule_auto_start()
        return Ok({"status": "reloaded", "mode": self._cfg.get("mode"),
                   "runtime_plugin_id": self._runtime_id})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        await self._stop_pipeline(reason="shutdown", clear_loop=True)
        await self._drain_persist_futures(timeout=3.0)
        return Ok({"status": "shutdown"})

    async def _on_command_loop_start(self) -> None:
        # @lifecycle 走一次性 asyncio.run，常驻任务必须挂在命令循环上。
        self._loop = asyncio.get_running_loop()
        self._started_on_loop = True
        if self._startup_armed and _boolean(self._cfg.get("auto_start_monitoring"), True):
            await self._start_pipeline()

    def _maybe_schedule_auto_start(self) -> None:
        if not (self._startup_armed and _boolean(self._cfg.get("auto_start_monitoring"), True)):
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            # The SDK command-loop hook will start it on the real loop.
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            if not self._monitoring:
                asyncio.create_task(self._start_pipeline())
            return
        try:
            asyncio.run_coroutine_threadsafe(self._start_pipeline(), loop)
        except Exception as exc:
            self.logger.warning("auto-start scheduling failed: {}: {}",
                                type(exc).__name__, exc)

    @_message_hook(id="chat_quiet_window", source="chat")
    async def on_chat_message(self, *, _ctx: Any = None, **kwargs):
        """Remember *when* the user spoke, never *what* they said.

        Only the session target in ``_ctx`` is inspected.  Message text, voice
        transcripts and other payload keys are ignored and never stored.  The
        quiet window applies only when the observed target matches the
        configured target (or no fixed target is configured).
        """
        del kwargs  # Never inspect text/voice content.
        observed = ""
        if isinstance(_ctx, dict):
            observed = str(_ctx.get("lanlan_name") or _ctx.get("target_lanlan") or "")
        else:
            observed = str(getattr(_ctx, "lanlan_name", "")
                           or getattr(_ctx, "target_lanlan", "") or "")
        observed = clean_target_lanlan(observed)
        configured = clean_target_lanlan(self._cfg.get("target_lanlan"))
        applies = not observed or not configured or observed == configured
        now = time.time()
        if applies:
            self.dispatcher.note_user_chat(now=now)
        self.timeline.record("chat", "quiet_window", ts=now,
                             reason="user_chat",
                             detail={"target_present": bool(observed),
                                     "applied": bool(applies)})
        return Ok({"status": "observed", "target_present": bool(observed),
                   "quiet_applied": bool(applies)})

    # ---------------------------------------------------------------- 生命周期

    async def _start_pipeline(self) -> bool:
        await self._acquire_transition()
        try:
            return await self._start_pipeline_serialized()
        finally:
            self._release_transition()

    async def _start_pipeline_serialized(self) -> bool:
        loop = self._loop or asyncio.get_running_loop()
        with self._lifecycle_lock:
            if self._monitoring:
                return True
            self._epoch += 1
            epoch = self._epoch
            self._monitoring = True
            self._pipeline_started = True
            self._last_start_error = ""
            self._loop = loop
            self._user_paused = False
            self._reset_runtime_state_locked(reason="pipeline_start")
            session_id = uuid.uuid4().hex
            self._session_id = session_id
            # reset_session clears the id; set it afterwards by contract.
            self.tracker.set_session_id(session_id)

        # A new session must not inherit the previous host read context.  If the
        # gate cannot reopen, roll back this exact start generation instead of
        # leaving UI-visible monitoring=True while every push is stopped.
        try:
            self.gate.reopen()
        except Exception as exc:
            self.logger.warning("gate reopen failed: {}: {}", type(exc).__name__, exc)
            with self._lifecycle_lock:
                if (self._epoch == epoch and self._monitoring
                        and self._session_id == session_id):
                    self._monitoring = False
                    self._pipeline_started = False
                    self._last_start_error = "gate_reopen"
                    self._reset_runtime_state_locked(reason="start_failed")
                    self._session_id = ""
                    self._last_scene = SCENE_UNKNOWN
                    self._classification_valid = False
                    self._confirmed_exit_handled = False
                    self._capture_pause_active = False
                    self._pause_reason = ""
                    self.timeline.record(
                        "pipeline", "start_failed", ts=time.time(),
                        reason="gate_reopen",
                        detail={"error": type(exc).__name__})
            return False
        self._expire_read_context("pipeline_start", token=self.gate.get_token())

        if self._ocr is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
                self._ocr = RapidOCR()
            except Exception as exc:
                self.logger.warning("RapidOCR unavailable ({}), degrade to capture-only", exc)
            self.classifier = ScreenClassifier(self._ocr)
            self.hud = HudReader(self._ocr)
        if self._detector is None and _MODEL_PATH.is_file():
            try:
                self._detector = YoloDetector(str(_MODEL_PATH))
            except Exception as exc:
                self.logger.warning("detector load failed: {}: {}", type(exc).__name__, exc)

        capture = ScreenCapture(
            fps=_capture_fps(self._cfg),
            on_frame=self._on_capture_frame,
            # The producer thread owns the single capture path; the perception
            # loop consumes latest_capture() and never opens a competing grab.
            ring_enabled=False,
            capture_thread_enabled=True,
        )
        with self._lifecycle_lock:
            if not self._monitoring or self._epoch != epoch:
                capture.stop()
                return False
            self._capture = capture
            capture.start()
            self._loop_task = asyncio.create_task(self._perception_loop(epoch))
            self._catalog_task = asyncio.create_task(self._catalog_loop(epoch))
            self._last_start_error = ""
        self.logger.info("kards companion pipeline started (mode={}, runtime={})",
                         self._cfg.get("mode"), self._runtime_id)
        return True

    async def _stop_pipeline(self, *, reason: str = "monitoring_stopped",
                             clear_loop: bool = False) -> None:
        await self._acquire_transition()
        try:
            await self._stop_pipeline_serialized(reason=reason, clear_loop=clear_loop)
        finally:
            self._release_transition()

    async def _stop_pipeline_serialized(self, *, reason: str,
                                        clear_loop: bool) -> None:
        with self._lifecycle_lock:
            self._monitoring = False
            self._epoch += 1
            stop_epoch = self._epoch
            loop_task, self._loop_task = self._loop_task, None
            catalog_task, self._catalog_task = self._catalog_task, None
            capture, self._capture = self._capture, None
            self._capture_pause_active = True
            self._pause_reason = reason
            self._user_paused = False
            self._last_start_error = ""
            self._pending_rearm = []

        # 1) Close gate admission outside the lifecycle lock.  begin_stop may
        #    wait for a host push already in flight; running it in a worker
        #    keeps the asyncio command loop responsive.
        try:
            await asyncio.to_thread(self.gate.begin_stop, reason)
        except Exception as exc:
            self.logger.warning("gate begin_stop failed: {}: {}", type(exc).__name__, exc)

        # 2) Stop producers, then wait for the worker thread that may still be
        #    inside capture/OCR.  Stale ticks see the new epoch and cannot push.
        for task in (loop_task, catalog_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        await self._wait_for_ticks_idle()
        # Recording is local-only; stop/clean its current session after
        # in-flight ticks are done so no late frame can touch the next session.
        try:
            await asyncio.to_thread(self._stop_recording, reason)
        except Exception as exc:
            self.logger.warning("recording stop failed: {}: {}",
                                type(exc).__name__, exc)

        # 3) Drop queued respond items without touching policy modules.
        self._purge_dispatcher_queue()

        # 4) Expire the read context with the cleanup token minted by
        #    begin_stop.  Consent/dry/stop policy is enforced by the gate.
        try:
            cleanup_token = self.gate.get_token()
        except Exception:
            cleanup_token = None
        self._expire_read_context(reason, token=cleanup_token)

        try:
            await asyncio.to_thread(self.gate.finish_stop)
        except Exception as exc:
            self.logger.warning("gate finish_stop failed: {}: {}", type(exc).__name__, exc)

        with self._lifecycle_lock:
            if self._epoch == stop_epoch and not self._monitoring:
                self._reset_runtime_state_locked(reason="monitoring_stopped")
                self._session_id = ""
                self._last_scene = SCENE_UNKNOWN
                self._classification_valid = False
                self._confirmed_exit_handled = False
                self._capture_pause_active = False
                self._pause_reason = ""
                self.timeline.record("pipeline", "stopped", ts=time.time(),
                                     reason=reason)
                if clear_loop:
                    self._loop = None
                    self._started_on_loop = False
                    self._startup_armed = False

        if capture is not None:
            try:
                await asyncio.to_thread(capture.stop)
            except Exception:
                pass

    async def _acquire_transition(self) -> None:
        await asyncio.get_running_loop().run_in_executor(
            None, self._transition_guard.acquire)

    def _release_transition(self) -> None:
        try:
            self._transition_guard.release()
        except RuntimeError:
            pass

    async def _pause_pipeline(self, reason: str = "user_pause") -> None:
        await self._acquire_transition()
        try:
            await self._pause_pipeline_serialized(reason)
        finally:
            self._release_transition()

    async def _pause_pipeline_serialized(self, reason: str) -> None:
        with self._lifecycle_lock:
            if not self._monitoring:
                return
            if self._user_paused:
                return
            self._user_paused = True
            self._pause_reason = reason
            self._recording_in_match = False
            self._recording_accepting = False
        try:
            self.guard.pause(reason, auto=False)
        except Exception:
            pass
        # Pause/expire admission first; a token minted before this call can no
        # longer push.  Ticks keep running but do no recognition/delivery.
        try:
            await asyncio.to_thread(self.gate.pause, reason)
        except Exception as exc:
            self.logger.warning("gate pause failed: {}: {}", type(exc).__name__, exc)
        await self._wait_for_ticks_idle()
        try:
            await asyncio.to_thread(self._recording_set_active, False, "user_pause")
        except Exception as exc:
            self.logger.warning("recording pause failed: {}: {}",
                                type(exc).__name__, exc)
        self._purge_dispatcher_queue()
        try:
            pause_token = self.gate.get_token()
        except Exception:
            pause_token = None
        self._expire_read_context(reason, token=pause_token)
        self.timeline.record("pipeline", "paused", ts=time.time(), reason=reason)

    async def _resume_pipeline(self) -> None:
        await self._acquire_transition()
        try:
            await self._resume_pipeline_serialized()
        finally:
            self._release_transition()

    async def _resume_pipeline_serialized(self) -> None:
        with self._lifecycle_lock:
            if not self._monitoring:
                return
            self._user_paused = False
            self._pending_rearm = list(self.tracker.rearm_uncommitted(now=time.time()))
            if self.tracker.in_match:
                self.tracker.rebase()
                with self._unit_lock:
                    self._unit_tracker.rebase()
            recording_live = bool(self._recording_match_live)
        # UI resume is explicit: clear both the manual pause and any automatic
        # failure pause.  There is no automatic half-open leak here.
        for kwargs in ({}, {"auto": True}):
            try:
                self.guard.resume(**kwargs)
            except Exception:
                pass
        try:
            await asyncio.to_thread(self.gate.resume)
        except Exception as exc:
            self.logger.warning("gate resume failed: {}: {}", type(exc).__name__, exc)
        if recording_live:
            # Re-open the existing session; the scene gate keeps accepting
            # false until the next confirmed in-match frame.
            try:
                await asyncio.to_thread(
                    self._recording_set_active, True, "resume")
            except Exception as exc:
                self.logger.warning("recording resume failed: {}: {}",
                                    type(exc).__name__, exc)
        self.timeline.record("pipeline", "resumed", ts=time.time(), reason="user_resume")

    async def _catalog_loop(self, epoch: int | None = None) -> None:
        while self._session_active(epoch):
            if _boolean(self._cfg.get("catalog_enabled"), False):
                updated = await asyncio.to_thread(
                    self.catalog.sync,
                    force=False,
                    max_age_hours=_num(self._cfg.get("catalog_refresh_hours"), 24.0,
                                       minimum=1.0, maximum=720.0))
                if updated:
                    self.logger.info("kards catalog refreshed: {} cards", self.catalog.size())
            await asyncio.sleep(600)

    async def _perception_loop(self, epoch: int | None = None) -> None:
        while self._session_active(epoch):
            await asyncio.sleep(_loop_seconds(self._cfg))
            if not self._session_active(epoch):
                return
            try:
                await asyncio.to_thread(self._run_tick, epoch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning("perception tick failed: {}: {}", type(exc).__name__, exc)

    def _session_active(self, epoch: int | None = None) -> bool:
        # User pause keeps the loop alive; _perceive_once is the gate that
        # skips recognition and delivery while paused.
        with self._lifecycle_lock:
            return (self._monitoring
                    and (epoch is None or self._epoch == epoch))

    def _tick_active_locked(self, epoch: int, capture: Any) -> bool:
        """Caller must hold ``_lifecycle_lock``."""
        return (self._monitoring and not self._user_paused
                and self._epoch == epoch and self._capture is capture)

    def _run_tick(self, epoch: int | None = None) -> None:
        """Thread-side tick wrapper so stop can wait for real completion."""
        # Capture the gate token before taking the lifecycle lock: get_token can
        # wait on the gate commit lock and must never be called under our lock.
        try:
            token = self.gate.get_token()
            token_epoch = int(self.gate.epoch)
        except Exception:
            token = None
            token_epoch = None
        with self._lifecycle_lock:
            if not self._session_active(epoch):
                return
            self._ticks_in_flight += 1
            self._ticks_idle.clear()
        try:
            self._perceive_once(epoch, context_token=token, token_epoch=token_epoch)
        finally:
            with self._lifecycle_lock:
                self._ticks_in_flight = max(0, self._ticks_in_flight - 1)
                if self._ticks_in_flight == 0:
                    self._ticks_idle.set()

    async def _wait_for_ticks_idle(self, timeout: float = 5.0) -> bool:
        if self._ticks_idle.is_set():
            return True
        idle = await asyncio.to_thread(self._ticks_idle.wait, timeout)
        if not idle:
            self.logger.warning(
                "perception tick still in flight after {:.1f}s during stop", timeout)
        return bool(idle)

    # -------------------------------------------------------------- 状态清理

    def _purge_dispatcher_queue(self) -> int:
        """Drop queued respond items through C's public purge API."""
        purge = getattr(self.dispatcher, "purge", None)
        if callable(purge):
            try:
                removed = purge("lifecycle_purge")
                return len(removed) if isinstance(removed, list) else 0
            except Exception as exc:
                self.logger.warning("dispatcher purge failed: {}: {}",
                                    type(exc).__name__, exc)
        return 0

    def _expire_read_context(self, reason: str, *, token: str | None = None) -> bool:
        """Retire the host read context outside the lifecycle lock."""
        expire = getattr(self.publisher, "expire", None)
        if not callable(expire):
            return False
        try:
            if token is not None and _supports_kwarg(expire, "context_token"):
                return bool(expire(reason=reason, context_token=token))
            return bool(expire(reason=reason))
        except Exception as exc:
            self.logger.warning("publisher expire failed: {}: {}", type(exc).__name__, exc)
            return False

    def _reset_runtime_state_locked(self, *, reason: str = "reset") -> None:
        """Clear every cross-tick read/delivery cache. Caller holds the lock."""
        self._capture_pause_active = True
        self._pause_reason = reason
        self._last_capture_health = CaptureHealthSnapshot(
            status="stopped", reason=reason, reading=False)
        try:
            self.arbiter.reset()
        except Exception:
            pass
        for component in (self.guard, self.breaker, self.read_breaker):
            reset = getattr(component, "reset", None)
            if callable(reset):
                try:
                    reset()
                except Exception:
                    pass
        try:
            self.tracker.reset_session()
        except Exception:
            pass
        try:
            self.scenario.reset()
        except Exception:
            pass
        with self._unit_lock:
            reset_units = getattr(self._unit_tracker, "reset_session", None)
            if callable(reset_units):
                try:
                    reset_units()
                except Exception:
                    pass
            self._last_unit_diagnostics = {}
        self._pipeline_state = MatchState()
        self._region_signature = None
        self._recording_in_match = False
        self._recording_accepting = False
        self._hud_cache = {}
        self._hud_hash = None
        hud = getattr(self, "hud", None)
        reset_hud = getattr(hud, "reset", None)
        if callable(reset_hud):
            try:
                reset_hud()
            except Exception:
                pass
        classifier = getattr(self, "classifier", None)
        invalidate = getattr(classifier, "invalidate", None)
        if callable(invalidate):
            try:
                invalidate()
            except Exception:
                pass
        try:
            with self._region_lock:
                self.region_calibrator.reset()
                regions = self.region_calibrator.regions
            if hud is not None:
                hud.set_regions(regions)
        except Exception:
            pass
        self._previous_snapshot = None
        self._last_trusted_snapshot = None
        self._last_detector_diagnostics = []
        self._pending_rearm = []
        self._last_scene = SCENE_UNKNOWN
        self._classification_valid = False
        self._confirmed_exit_handled = False

    # ---------------------------------------------------------------- 感知 tick

    def _ensure_session_locked(self) -> str:
        if not self._session_id:
            self._session_id = uuid.uuid4().hex
            self.tracker.set_session_id(self._session_id)
        return self._session_id

    def _next_capture_frame_id(self, candidate: Any = 0) -> int:
        """Return one positive frame id; caller holds ``_lifecycle_lock``.

        A real capture id is kept as-is (the counter only advances past it so
        a later missing id cannot collide).  Missing/zero/negative ids get a
        fresh monotonic value instead of repeatedly masquerading as frame 0.
        """
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            if value > self._capture_frame_seq:
                self._capture_frame_seq = value
            return value
        self._capture_frame_seq += 1
        return self._capture_frame_seq

    def _perceive_once(self, epoch: int | None = None, *,
                       context_token: str | None = None,
                       token_epoch: int | None = None) -> None:
        """One perception tick.

        Trusted frame: scenario pre-gate → tracker/detector → normalize → one
        scenario update under the lifecycle lock, then all delivery/read host
        calls outside the lock with the token captured at tick entry.

        Untrusted frame: no classifier/HUD/YOLO/tracker, one scenario pause
        update, one delivery purge + read-context expire at the transition.
        """
        if context_token is None:
            try:
                context_token = self.gate.get_token()
                token_epoch = int(self.gate.epoch)
            except Exception:
                context_token = None
                token_epoch = None

        with self._lifecycle_lock:
            current_epoch = self._epoch if epoch is None else int(epoch)
            capture = self._capture
            if (not self._monitoring or self._user_paused
                    or current_epoch != self._epoch or capture is None):
                return
            previous = self._last_trusted_snapshot
            session_id = self._ensure_session_locked()

        # The capture thread is the sole producer.  Consume its latest result;
        # calling capture_frame() while the worker is alive returns that same
        # snapshot and never starts a competing capture.  Fake captures in
        # tests have no latest_capture() and fall back to their one-shot API.
        result = None
        latest_capture = getattr(capture, "latest_capture", None)
        if callable(latest_capture):
            try:
                result = latest_capture()
            except Exception:
                result = None
        if result is None:
            result = capture.capture_frame()
        health = capture.health_snapshot()
        observed_at = time.time()
        if result is not None:
            try:
                captured_at = float(getattr(result, "captured_at", 0.0) or 0.0)
            except (TypeError, ValueError):
                captured_at = 0.0
            if captured_at > 0.0:
                observed_at = captured_at

        with self._lifecycle_lock:
            if not self._tick_active_locked(current_epoch, capture):
                return
            self._last_capture_health = health
            self._snapshot_seq += 1
            seq = self._snapshot_seq

        # RuntimeSnapshot.trusted is the authority: capture.reading + a valid
        # frame + an image.  CaptureResult.trusted is only evidence used by the
        # recorder's window/source check, never the sole recognition gate.
        result_image = getattr(result, "image", None) if result is not None else None
        if result is None or not health.reading or result_image is None:
            reason = health.reason or (
                "frame_unavailable" if result is None else "capture_not_trusted")
            frame_id = 0
            if result is not None:
                try:
                    frame_id = int(getattr(result, "frame_id", 0) or 0)
                except (TypeError, ValueError):
                    frame_id = 0
            frame = FrameMeta(
                frame_id=frame_id or int(health.frame_id or 0),
                captured_at=0.0,
                width=health.width,
                height=health.height,
                backend=health.backend,
                valid=False,
                reason=reason,
            )
            snapshot = RuntimeSnapshot(
                seq=seq, observed_at=observed_at, health=health, frame=frame,
                state=MatchState(), image=None)
            with self._lifecycle_lock:
                if not self._tick_active_locked(current_epoch, capture):
                    return
                self._previous_snapshot = snapshot
                first_pause = not self._capture_pause_active
                self._capture_pause_active = True
                self._pause_reason = reason
                # Unknown/minimized/stale frames pause recording but keep the
                # temporary match session (it is only deleted on match end).
                self._recording_in_match = False
                self._recording_accepting = False
                # One scenario pause update per tick; it never confirms exit
                # from unknown frames.
                try:
                    self.scenario.update(snapshot.state, now=observed_at)
                except Exception:
                    pass
                if first_pause:
                    self.timeline.record(
                        "capture", "paused", ts=observed_at, seq=seq,
                        reason=reason,
                        detail={"status": health.status,
                                "backend": health.backend,
                                "frame_age_ms": health.frame_age_ms,
                                "frame_id": int(frame_id or health.frame_id or 0),
                                "reading": False})
            # Host-facing cleanup happens outside the lifecycle lock.  Every
            # untrusted tick retries the publisher's bounded expire backoff
            # with the token captured at tick entry; only the transition tick
            # purges the respond slot.
            if first_pause:
                self._purge_dispatcher_queue()
            self._recording_set_active(False, reason)
            self._expire_read_context(reason, token=context_token)
            return

        image = result_image
        raw_frame = result.to_frame_meta(valid=True, reason=health.reason)
        try:
            # Source frame id only.  Health may already describe a newer
            # producer frame; borrowing it here would mislabel this image.
            candidate_frame_id = int(getattr(raw_frame, "frame_id", 0) or 0)
        except (TypeError, ValueError):
            candidate_frame_id = 0

        # Carry the last committed state forward.  HUD/unit/change run on
        # independent deadlines, so a tick that skips a task must not feed an
        # empty board or a None HP value into MatchTracker.
        with self._lifecycle_lock:
            if not self._tick_active_locked(current_epoch, capture):
                return
            # Resolve the id exactly once, under the same lock that owns the
            # tick sequence.  Every scheduler/evidence/unit marker below uses
            # this value.
            frame_id = self._next_capture_frame_id(candidate_frame_id)
            frame = FrameMeta(
                frame_id=frame_id,
                captured_at=observed_at,
                width=raw_frame.width,
                height=raw_frame.height,
                backend=raw_frame.backend,
                valid=True,
                reason=raw_frame.reason,
            )
            base = self._pipeline_state
            state = MatchState(
                scene=base.scene,
                game_number=int(base.game_number or 0),
                in_match=bool(base.in_match),
                my_turn=bool(base.my_turn),
                turn_number=int(base.turn_number or 0),
                my_hq=base.my_hq,
                enemy_hq=base.enemy_hq,
                my_credits=base.my_credits,
                my_credit_cap=base.my_credit_cap,
                hand_count=base.hand_count,
                enemy_hand_count=base.enemy_hand_count,
                units=[unit.copy() for unit in base.units],
                last_order_card=copy.deepcopy(base.last_order_card),
                detected_at=observed_at,
                draft_unsupported=bool(base.draft_unsupported),
            )
            # Runtime cadence marker (also backwards compatible if the shared
            # model does not declare it): a non-unit tick carries last-known
            # units but is not fresh unit evidence.
            state.unit_observation_valid = False
            state.unit_observation_frame = 0
            classifier = getattr(self, "classifier", None)
            hud = getattr(self, "hud", None)

        with self._scheduler_lock:
            change_due = self.scheduler.due("change", frame_id=frame_id)

        if change_due:
            started = time.monotonic()
            if classifier is not None:
                try:
                    scene, my_turn = classifier.classify(image)
                except Exception as exc:
                    self.logger.warning("scene classify failed: {}: {}",
                                        type(exc).__name__, exc)
                    scene, my_turn = SCENE_UNKNOWN, False
            else:
                scene, my_turn = SCENE_UNKNOWN, False
            state.scene = str(scene or SCENE_UNKNOWN)
            state.my_turn = bool(my_turn) if state.scene == SCENE_MATCH else False
            state.in_match = state.scene == SCENE_MATCH
            with self._scheduler_lock:
                self.scheduler.mark_duration(
                    "change", max(0.0, time.monotonic() - started))

        # Pure scene gate runs before the expensive HUD/unit tasks so a resume
        # can rebase both trackers before the first observation is consumed.
        with self._lifecycle_lock:
            if not self._tick_active_locked(current_epoch, capture):
                return
            pre = self.scenario.observe_scene(state, now=observed_at)
        if pre.get("new_match"):
            with self._unit_lock:
                self._unit_tracker.reset_match()
            # No observation may be inherited from the previous match when
            # this tick skips the unit task.
            state.units = []
        if pre.get("resume"):
            if self.tracker.in_match:
                self.tracker.rebase()
            with self._unit_lock:
                self._unit_tracker.rebase()

        if state.scene == SCENE_MATCH:
            with self._scheduler_lock:
                hud_due = (hud is not None
                           and self.scheduler.due("hud", frame_id=frame_id))
                unit_due = self.scheduler.due("unit", frame_id=frame_id)
            if hud_due:
                started = time.monotonic()
                hud_values = self._hud_read_gated(image, hud)
                # A missing OCR field never erases the last known value; the
                # tracker already treats None as "no comparison this frame".
                for key in ("my_hq", "enemy_hq", "my_credits", "my_credit_cap"):
                    value = hud_values.get(key)
                    if value is not None:
                        setattr(state, key, value)
                with self._scheduler_lock:
                    self.scheduler.mark_duration(
                        "hud", max(0.0, time.monotonic() - started))
            if unit_due:
                started = time.monotonic()
                unit_diagnostics = self._apply_detector(
                    state, image, frame_id=frame_id, run=True,
                    observed_at=observed_at,
                    observation=self._observation_evidence(capture, result, health))
                # A unit-due tick is the only tick allowed to advance unit
                # gates.  Model/inference failure yields False here, so the
                # carried last-known units stay silent instead of counting as
                # a missing observation.
                state.unit_observation_valid = bool(
                    (unit_diagnostics or {}).get("observation_valid", False))
                state.unit_observation_frame = int(frame_id)
                with self._scheduler_lock:
                    self.scheduler.mark_duration(
                        "unit", max(0.0, time.monotonic() - started))

        snapshot = RuntimeSnapshot(
            seq=seq, observed_at=observed_at, health=health, frame=frame,
            state=state, image=image)

        with self._lifecycle_lock:
            if not self._tick_active_locked(current_epoch, capture):
                return
            resumed = self._capture_pause_active

            candidates: list[Any] = []
            diagnostics: list[Any] = []
            detector_previous = None if (resumed or pre.get("resume")) else previous
            if pre.get("allow_diff"):
                candidates, diagnostics = self.detectors.update(detector_previous, snapshot)
            # No tracker/diff for menu/draft/unknown.  Terminal frames still
            # allow_diff so a confirmed victory/defeat closes the match.

            events = [normalize_event(item.event, session_id=session_id)
                      for item in candidates]
            # Exactly one scenario advance per tick.
            scenario_name = self.scenario.update(state, events, now=observed_at)
            scene_snapshot = self.scenario.snapshot()
            if pre.get("resume"):
                try:
                    self.scenario.consume_rebase()
                except Exception:
                    pass

            confirmed_exit = bool(scene_snapshot.get("confirmed_exit"))
            terminal_frame = any(event.kind == E_MATCH_END for event in events)
            if confirmed_exit and not self._confirmed_exit_handled:
                # Preserve the session and cumulative game number; only the
                # current match memory is dropped.  This also covers a terminal
                # frame where MatchTracker has already closed _in_match itself.
                if self.tracker.in_match:
                    try:
                        self.tracker.reset_match()
                    except Exception:
                        pass
                with self._unit_lock:
                    reset_match = getattr(self._unit_tracker, "reset_match", None)
                    if callable(reset_match):
                        reset_match()
                state.units = []
                self._confirmed_exit_handled = True
            elif pre.get("new_match") or (self.tracker.in_match and not confirmed_exit):
                self._confirmed_exit_handled = False

            if resumed or pre.get("resume"):
                # Conditions that were already true before a pause are
                # re-armed without refreshing their original ts.
                try:
                    rearmed = self.tracker.rearm_uncommitted(now=observed_at)
                except Exception:
                    rearmed = []
                for event in rearmed:
                    events.append(normalize_event(event, session_id=session_id))

            # Stable identity dedupe: the tracker event wins over a re-arm copy.
            deduped: list[GameEvent] = []
            seen: set[str] = set()
            for event in events:
                identity = str((event.details or {}).get("identity")
                               or event.fingerprint())
                if identity in seen:
                    continue
                seen.add(identity)
                deduped.append(event)
            events = deduped

            # Commit the full state only after the trackers have consumed it.
            # MatchTracker mutates lineage/game_number in place, so this is the
            # canonical baseline for the next scheduled task.
            self._pipeline_state = state
            recording_in_match = bool(
                state.scene == SCENE_MATCH
                and scene_snapshot.get("context_valid")
                and not terminal_frame and not confirmed_exit)
            self._recording_in_match = recording_in_match
            if not recording_in_match or not self._perception_settings.recording_enabled:
                self._recording_accepting = False
            elif self._recording_match_live:
                self._recording_accepting = not self._user_paused
            self._previous_snapshot = snapshot
            self._last_trusted_snapshot = snapshot
            self._last_detector_diagnostics = [
                {"name": item.name,
                 "duration_ms": round(float(item.duration_ms), 3),
                 "outcome": item.outcome,
                 "error": item.error}
                for item in diagnostics
            ]
            for item in diagnostics:
                if item.outcome == "error":
                    self.timeline.record(
                        "detector", "error", ts=observed_at, seq=seq,
                        reason=item.error,
                        detail={"name": item.name,
                                "frame_id": int(frame_id or 0)})

            scene_text = str(state.scene or SCENE_UNKNOWN)
            if scene_text in _VALID_SCENES:
                self._last_scene = scene_text
                self._classification_valid = True
            else:
                # Keep the last confirmed scene for status; unknown/draft do
                # not erase it.
                self._classification_valid = False
            if resumed:
                self._capture_pause_active = False
                self._pause_reason = ""
                self.timeline.record(
                    "capture", "resumed", ts=observed_at, seq=seq,
                    reason="frame_trusted",
                    detail={"status": health.status,
                            "backend": health.backend,
                            "frame_age_ms": health.frame_age_ms,
                            "frame_id": int(frame_id or 0),
                            "reading": True})

        # ---- All host-facing work is outside the lifecycle lock. -------------
        terminal = any(event.kind == E_MATCH_END for event in events)
        invalid_context = not bool(scene_snapshot.get("context_valid"))
        confirmed_exit = bool(scene_snapshot.get("confirmed_exit"))
        if invalid_context or confirmed_exit or terminal:
            # The old respond slot / read context cannot survive a broken or
            # ended scene.  Purge first, then expire with the tick token; a
            # terminal result is still admitted below after the old slot is
            # gone.  Repeated invalid frames retry publisher backoff, never a
            # freshly minted token.
            self._purge_dispatcher_queue()
            reason = ("match_ended" if terminal else
                      ("confirmed_exit" if confirmed_exit else "scene_invalid"))
            self._expire_read_context(reason, token=context_token)
        self._attach_frame_evidence(
            events, frame, session_id=session_id, seq=seq,
            observed_at=observed_at)
        # Local recording is independent of LLM consent and dry-run.  The
        # capture callback queues every frame while an in-match session is
        # live; here we only control the session/scene boundary after the
        # scenario resolver has confirmed a live match context.
        recording_in_match = bool(self._recording_in_match)
        if not (terminal or confirmed_exit):
            if recording_in_match:
                if pre.get("new_match") or not self._recording_match_live:
                    self._start_recording_match(session_id)
                else:
                    self._recording_set_active(True, "in_match")
            else:
                self._recording_set_active(
                    False, f"scene_{state.scene or SCENE_UNKNOWN}")
        self._record_events(events)
        self._dispatch_candidates(
            events, state, session_id=session_id,
            scenario=scenario_name, context_token=context_token,
            token_epoch=token_epoch, observed_at=observed_at)
        if terminal or confirmed_exit:
            # End after dispatch so a confirmed terminal event is still
            # arbitrated/delivered before the local session is cleaned.
            self._end_recording_match(
                "match_ended" if terminal else "confirmed_exit")
        validity = self._context_validity(observed_at)
        if (self.tracker.in_match and validity["valid"]
                and not self._user_paused):
            self._publish_live_state(
                state, session_id=session_id,
                context_token=context_token, token_epoch=token_epoch,
                observed_at=observed_at)

    def _attach_frame_evidence(self, events: list[GameEvent], frame: FrameMeta, *,
                               session_id: str, seq: int,
                               observed_at: float) -> None:
        """Attach bounded, structured evidence; never image pixels or OCR text."""
        if not bool(getattr(self._perception_settings, "evidence_enabled", True)):
            return
        evidence = {
            "frame_id": int(frame.frame_id or 0),
            "captured_at": float(frame.captured_at),
            "session_id": str(session_id or ""),
            "seq": int(seq),
            "observed_at": float(observed_at),
            "backend": str(frame.backend or ""),
            "width": int(frame.width or 0),
            "height": int(frame.height or 0),
        }
        for event in events:
            details = event.details if isinstance(event.details, dict) else {}
            details.setdefault("frame_id", evidence["frame_id"])
            details.setdefault("evidence", dict(evidence))
            event.details = details

    def _hud_read_gated(self, image, hud: HudReader) -> dict[str, Any]:
        # RapidOCR 单次 200ms+，每 tick 全帧跑会挤爆 333ms 预算：角落指纹不变即复用上值
        w, h = image.size
        fp = dhash(image.crop((0, 0, int(w * 0.75), h)))
        with self._lifecycle_lock:
            if not self._monitoring or self._user_paused:
                return {}
            epoch = self._epoch
            if self._hud_hash is not None and hamming(fp, self._hud_hash) <= 2:
                return self._hud_cache
        result = hud.read(image)
        with self._lifecycle_lock:
            # A stop may have reset the HUD while OCR was running; never commit
            # a stale read into the next session.
            if not self._monitoring or self._user_paused or self._epoch != epoch:
                return {}
            self._hud_hash = fp
            self._hud_cache = result
        return result

    @staticmethod
    def _region_anchors(detections: Any) -> list[dict[str, Any]]:
        """Convert only reliable object boxes into calibrator anchors.

        The detector exposes ``conf`` while RegionCalibrator expects
        ``confidence``; passing Detection objects directly would silently look
        like 1.0 confidence and let a weak box move the HUD.  Only detached
        boxes (never dHash/whole-image guesses) at >= 0.5 are eligible.
        """
        anchors: list[dict[str, Any]] = []
        for det in detections or ():
            name = str(getattr(det, "name", "") or "")
            if name not in {"frontline", "end_turn_button", "hq"}:
                continue
            try:
                confidence = float(getattr(det, "conf", 0.0) or 0.0)
                x1 = float(getattr(det, "x1", 0.0))
                y1 = float(getattr(det, "y1", 0.0))
                x2 = float(getattr(det, "x2", 0.0))
                y2 = float(getattr(det, "y2", 0.0))
            except (TypeError, ValueError):
                continue
            if confidence < 0.5 or x2 <= x1 or y2 <= y1:
                continue
            anchors.append({
                "name": name,
                "bbox": (x1, y1, x2 - x1, y2 - y1),
                "confidence": confidence,
            })
        return anchors

    @staticmethod
    def _observation_evidence(capture: Any, result: Any,
                              health: CaptureHealthSnapshot) -> dict[str, Any]:
        """Capture health passed to UnitTracker's absence gate.

        A trusted frame is still not automatically valid *absence* evidence:
        desktop backends need foreground/occlusion proof, while host WGC may
        legitimately read a background window.  UnitTracker owns that decision;
        main only supplies the raw evidence.
        """
        backend = str(
            getattr(result, "source", "")
            or getattr(result, "backend", "")
            or health.backend
            or "")
        foreground: bool | None = None
        if isinstance(getattr(health, "foreground", None), bool):
            foreground = bool(health.foreground)
        elif isinstance(getattr(result, "foreground", None), bool):
            foreground = bool(result.foreground)
        return {
            "occlusion_state": getattr(capture, "occlusion_state", None),
            "foreground": foreground,
            "backend": backend,
            "occluded": None if not health.occluded else True,
            "capture_reading": bool(health.reading),
        }

    def _apply_detector(self, state: MatchState, image, *, frame_id: int,
                        run: bool, observed_at: float | None = None,
                        observation: dict[str, Any] | None = None
                        ) -> dict[str, Any]:
        """Run the due unit task and commit its trajectory units to state.

        ``None`` (model unavailable/error) and ``[]`` (successful empty board)
        are distinct evidence: the former retains confirmed last-known units
        with ``observed=False`` and never advances a miss counter.
        """
        if not run:
            return {}
        detections: list[Any] | None = None
        if self._detector is None:
            status = "model_unavailable"
        else:
            try:
                raw = self._detector.detect(image)
            except Exception as exc:
                self.logger.warning("detector step failed: {}: {}",
                                    type(exc).__name__, exc)
                status = f"detector_error:{type(exc).__name__}"
            else:
                if raw is None:
                    status = "model_unavailable"
                else:
                    try:
                        detections = list(raw)
                    except Exception as exc:
                        detections = None
                        status = f"detector_error:{type(exc).__name__}"
                    else:
                        status = "ok"

        source_at = time.time() if observed_at is None else float(observed_at)
        gate = observation or {}
        with self._unit_lock:
            units, diagnostics = self._unit_tracker.update_observations(
                detections,
                image.size,
                frame_id=frame_id,
                observed_at=source_at,
                detector_available=detections is not None,
                observation_valid=detections is not None,
                status=status,
                occlusion_state=gate.get("occlusion_state"),
                foreground=gate.get("foreground"),
                backend=str(gate.get("backend") or ""),
                occluded=gate.get("occluded"),
                capture_reading=gate.get("capture_reading"),
            )
            diagnostics = dict(diagnostics)
            diagnostics["captured_at"] = round(source_at, 3)
            self._last_unit_diagnostics = copy.deepcopy(diagnostics)

        # Layout calibration never invents anchors from a dHash or the whole
        # frame.  No detector/anchors simply yields the proportional,
        # explicitly-unverified layout; it is not a failure.
        anchors = self._region_anchors(detections) if detections is not None else None
        try:
            with self._region_lock:
                layout = self.region_calibrator.calibrate(
                    image, anchors=anchors, now=time.monotonic())
                signature = (
                    layout.state,
                    layout.width,
                    layout.height,
                    tuple(sorted(
                        (name, tuple(tuple(rect) for rect in rects))
                        for name, rects in layout.regions.items())),
                )
                changed = signature != self._region_signature
                if changed:
                    self._region_signature = signature
            hud = getattr(self, "hud", None)
            if changed and hud is not None:
                setter = getattr(hud, "set_regions", None)
                if callable(setter):
                    setter(layout)
                # The ROI geometry changed; a previous crop's OCR cache is no
                # longer valid even when the screen dHash is unchanged.
                with self._lifecycle_lock:
                    self._hud_hash = None
                    self._hud_cache = {}
        except Exception as exc:
            self.logger.warning("region calibration failed: {}: {}",
                                type(exc).__name__, exc)

        for unit in units:
            if not getattr(unit, "first_seen_turn", 0):
                unit.first_seen_turn = int(state.turn_number or 0)
        state.units = units
        return diagnostics

    # -------------------------------------------------------------- 事件分发

    def _record_events(self, events: list[GameEvent]) -> None:
        dry_run = _boolean(self._cfg.get("dry_run"), False)
        for event in events:
            record = event.to_dict()
            self._events_log.append(record)
            del self._events_log[:-30]
            if dry_run:
                self._append_event_log(record)
            elif event.kind == E_MATCH_END:
                self._schedule_persist(event)

    def _dispatch_candidates(self, candidates: list[GameEvent],
                             state: MatchState, *, session_id: str,
                             scenario: str, context_token: str | None,
                             token_epoch: int | None,
                             observed_at: float) -> None:
        """Run the single dispatcher tick outside the lifecycle lock."""
        target = resolve_target_lanlan(self)
        tick = getattr(self.dispatcher, "tick", None)
        if not callable(tick):
            return
        # Tracker events are stamped with its own wall clock, which can be a
        # fraction of a millisecond after the capture's observed_at.  Use the
        # newest source timestamp so a fresh event is never labelled future.
        dispatch_now = observed_at
        for event in candidates:
            try:
                dispatch_now = max(dispatch_now, float(getattr(event, "ts", 0.0)))
            except (TypeError, ValueError):
                continue
        # Never upgrade a stale tick to the current token.  The token captured
        # at perception entry is the only one this tick may use.
        if token_epoch is not None:
            try:
                if int(self.gate.epoch) != int(token_epoch):
                    self.timeline.record(
                        "dispatcher", "stale_epoch", ts=observed_at,
                        reason="perception_epoch_changed",
                        detail={"token_epoch": int(token_epoch),
                                "gate_epoch": int(self.gate.epoch)})
                    return
            except Exception:
                pass
        try:
            outcomes = tick(
                candidates, state,
                scenario=scenario,
                session_id=session_id,
                match_id=self.tracker.game_number,
                target_lanlan=target,
                context_token=context_token,
                now=dispatch_now,
            )
        except TypeError:
            # Transitional compatibility while older C revisions are loading.
            tick(candidates, state, context_token=context_token, now=dispatch_now)
            return
        except Exception as exc:
            self.logger.warning("dispatcher tick failed: {}: {}", type(exc).__name__, exc)
            return

    def _publish_live_state(self, state: MatchState, *, session_id: str,
                            context_token: str | None,
                            token_epoch: int | None,
                            observed_at: float) -> None:
        text = build_live_state_text(state).replace(
            '"query_tool":"kards_live_state"',
            f'"query_tool":"{_LIVE_STATE_TOOL_NAME}"',
        )
        target = resolve_target_lanlan(self)
        publish = getattr(self.publisher, "publish", None)
        if not callable(publish):
            return
        if token_epoch is not None:
            try:
                if int(self.gate.epoch) != int(token_epoch):
                    return
            except Exception:
                pass
        try:
            if _supports_kwarg(publish, "context_token"):
                # Dispatcher read breaker + the same token bound at tick entry.
                self.dispatcher.push_read(
                    state, text=text, target=target, session_id=session_id,
                    context_token=context_token, now=observed_at)
            else:
                # Publisher currently derives the gate token internally; this
                # branch is only for a C revision that has not added the token
                # parameter yet.  The epoch check above keeps stale ticks out.
                publish(state, text=text, target=target, session_id=session_id,
                        now=observed_at)
        except Exception as exc:
            self.logger.warning("read publish failed: {}: {}", type(exc).__name__, exc)

    def _push_live_state(self, state: MatchState) -> None:
        """Compatibility helper for callers/tests that publish a live state."""
        try:
            token = self.gate.get_token()
            token_epoch = int(self.gate.epoch)
        except Exception:
            token = None
            token_epoch = None
        with self._lifecycle_lock:
            session_id = self._session_id or self._ensure_session_locked()
        self._publish_live_state(
            state, session_id=session_id, context_token=token,
            token_epoch=token_epoch, observed_at=time.time())

    def _on_delivered(self, event: GameEvent, state: MatchState,
                      metadata: Any = None) -> None:
        """Called exactly once after the gate reports an explicit acceptance."""
        meta = metadata if isinstance(metadata, dict) else {}
        session = str(meta.get("session_id")
                      or (event.details or {}).get("session_id") or "")
        with self._lifecycle_lock:
            if session and self._session_id and session != self._session_id:
                return
            try:
                self.tracker.mark_delivered(event)
            except Exception:
                pass
        if event.kind == E_MATCH_END and not _boolean(self._cfg.get("dry_run"), False):
            self._schedule_persist(event)

    def _append_event_log(self, record: dict[str, Any]) -> None:
        try:
            path = self.data_path("events.jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            self.logger.warning("event log write failed: {}", exc)

    # ---------------------------------------------------------------- 持久化

    def _schedule_persist(self, event: GameEvent) -> None:
        details = event.details or {}
        summary = {
            "game": details.get("game"),
            "result": details.get("result"),
            "played": (details.get("ledger") or {}).get("played", []),
            "died": (details.get("ledger") or {}).get("died", []),
            "ended": (details.get("ledger") or {}).get("ended", time.time()),
            "session_id": details.get("session_id", ""),
        }
        key = f"{summary.get('session_id')}|{summary.get('game')}|{details.get('identity', '')}"
        with self._lifecycle_lock:
            if key in self._persisted_match_keys:
                return
            self._persisted_match_keys.add(key)
            loop = self._loop
        if loop is not None and loop.is_running():
            coroutine = self._persist_summary(summary)
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            try:
                if running is loop:
                    future = asyncio.create_task(coroutine)
                else:
                    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            except Exception as exc:
                self.logger.warning("persist scheduling failed: {}: {}",
                                    type(exc).__name__, exc)
                coroutine.close()
                with self._lifecycle_lock:
                    self._pending_persist.append(summary)
                return
            with self._lifecycle_lock:
                self._persist_futures.add(future)
            future.add_done_callback(self._persist_done)
            return
        with self._lifecycle_lock:
            self._pending_persist.append(summary)

    def _persist_done(self, future: Any) -> None:
        with self._lifecycle_lock:
            self._persist_futures.discard(future)
        try:
            future.result()
        except Exception as exc:
            self.logger.warning("persist match failed: {}: {}",
                                type(exc).__name__, exc)

    async def _drain_persist_futures(self, timeout: float = 3.0) -> None:
        with self._lifecycle_lock:
            futures = list(self._persist_futures)
            pending = list(self._pending_persist)
            self._pending_persist.clear()
        for summary in pending:
            try:
                await self._persist_summary(summary)
            except Exception as exc:
                self.logger.warning("pending persist failed: {}: {}", type(exc).__name__, exc)
        for future in futures:
            try:
                if isinstance(future, asyncio.Future):
                    await asyncio.wait_for(asyncio.shield(future), timeout)
                else:
                    await asyncio.wait_for(asyncio.wrap_future(future), timeout)
            except Exception as exc:
                self.logger.warning("persist future did not finish: {}: {}",
                                    type(exc).__name__, exc)

    async def _persist_summary(self, summary: dict[str, Any]) -> None:
        store = getattr(self, "store", None)
        if store is None:
            return
        try:
            stored = await store.get("matches", [])
            matches = list(_store_value(stored, []) or [])
            await store.set("matches", (matches + [summary])[-50:])
            stored_records = await store.get("records", {})
            records = dict(_store_value(stored_records, {}) or {})
            tally = dict(records.get("overall", {"won": 0, "lost": 0}))
            key = "won" if summary.get("result") == "won" else "lost"
            tally[key] = int(tally.get(key, 0)) + 1
            records["overall"] = tally
            await store.set("records", records)
        except Exception as exc:
            self.logger.warning("persist match failed: {}: {}", type(exc).__name__, exc)

    async def _persist_match(self, event: GameEvent) -> None:  # compatibility
        details = event.details or {}
        await self._persist_summary({
            "game": details.get("game"),
            "result": details.get("result"),
            "played": (details.get("ledger") or {}).get("played", []),
            "died": (details.get("ledger") or {}).get("died", []),
            "ended": (details.get("ledger") or {}).get("ended", time.time()),
            "session_id": details.get("session_id", ""),
        })

    # ---------------------------------------------------------------- entries

    @ui.action(id="monitor_start", label="开始监控")
    @plugin_entry(id="monitor_start", name="开始监控",
                  description="启动 KARDS 被动读屏监控；无有效帧时识别与解说自动暂停。")
    async def monitor_start(self, **_):
        started = await self._start_pipeline()
        if not started:
            reason = self._last_start_error or "start_failed"
            return Err(SdkError(f"监控启动失败：{reason}"))
        return Ok({"monitoring": self._monitoring, "paused": self._user_paused})

    @ui.action(id="monitor_stop", label="停止监控")
    @plugin_entry(id="monitor_stop", name="停止监控",
                  description="停止监控并让实时读上下文过期。")
    async def monitor_stop(self, **_):
        await self._stop_pipeline(reason="user_stop")
        return Ok({"monitoring": False})

    @ui.action(id="monitor_pause", label="暂停解说")
    @plugin_entry(id="monitor_pause", name="暂停解说",
                  description="暂停识别与推送并让实时读上下文过期；采集/监控保持运行。")
    async def monitor_pause(self, **_):
        await self._pause_pipeline("user_pause")
        return Ok({"monitoring": self._monitoring, "paused": self._user_paused})

    @ui.action(id="monitor_resume", label="恢复解说")
    @plugin_entry(id="monitor_resume", name="恢复解说",
                  description="恢复识别与推送；首帧重新建立基线，不补报暂停期间事件。")
    async def monitor_resume(self, **_):
        await self._resume_pipeline()
        return Ok({"monitoring": self._monitoring, "paused": self._user_paused})

    @ui.action(id="monitor_status", label="刷新状态")
    @plugin_entry(id="monitor_status", name="查询状态",
                  description="返回采集健康/reading、场景上下文、检测器注册表、投递统计与诊断时间线；"
                              "无有效帧或分类不可信时 paused=true，识别与解说暂停。")
    async def monitor_status(self, **_):
        return Ok(self._status_payload())

    @ui.action(id="monitor_diagnostics", label="拉取诊断")
    @plugin_entry(id="monitor_diagnostics", name="诊断信息",
                  description="返回扩展诊断：最近时间线、检测器诊断、gate 回执、投递熔断与最后快照元数据。")
    async def monitor_diagnostics(self, **_):
        payload = self._status_payload()
        payload["timeline"] = self.timeline.as_dict(limit=50)
        snapshot = self._previous_snapshot
        payload["last_snapshot"] = (self._public_snapshot_dict(snapshot)
                                    if snapshot is not None else None)
        payload["gate_records"] = self.gate.recent_records(limit=30)
        payload["simulated_records"] = list(getattr(self.dispatcher, "simulated_records", []) or [])[-20:]
        return Ok(payload)

    @ui.action(id="target_set", label="设置目标")
    @plugin_entry(id="target_set", name="设置目标",
                  description="设置固定目标猫娘；留空=跟随当前角色。绝不伪造角色名。",
                  input_schema={"type": "object", "properties": {
                      "target": {"type": "string"}}})
    async def target_set(self, *, target: Any = "", target_lanlan: Any = None, **_):
        value = clean_target_lanlan(target if target_lanlan is None else target_lanlan)
        await self.config.update({"kards": {"target_lanlan": value}})
        merged = dict(self._cfg)
        merged["target_lanlan"] = value
        self._apply_config({"kards": merged})
        return Ok({"target_lanlan_present": bool(value)})

    @ui.action(id="monitor_test_say", label="连接测试")
    @plugin_entry(id="monitor_test_say", name="连接测试",
                  description="一次真实连接测试：必须显式授权、真实目标、非 dry/consent 关闭；"
                              "回执只表示 SDK 本地提交，不代表 LLM 生成或 TTS 播放。",
                  input_schema={"type": "object", "properties": {
                      "target": {"type": "string"},
                      "text": {"type": "string"},
                      "authorized_real_test": {"type": "boolean"}},
                      "required": ["authorized_real_test"]})
    async def monitor_test_say(self, *, target: Any = "", text: Any = "",
                               authorized_real_test: Any = False, **_):
        if authorized_real_test is not True:
            return Err(SdkError("真实连接测试需要显式 authorized_real_test=true"))
        configured = resolve_target_lanlan(self)
        explicit = clean_target_lanlan(target)
        chosen = explicit or configured
        if not chosen:
            return Err(SdkError("无法确认目标角色，拒绝伪造 target_lanlan"))
        gate = self.gate
        if not gate.is_running:
            return Err(SdkError("gate 未运行（监控已停止/暂停）"))
        if gate.paused:
            return Err(SdkError("gate 已暂停，拒绝真实连接测试"))
        if gate.config.dry_run or gate.config.simulated:
            return Err(SdkError("dry-run 下不会进行真实连接测试"))
        if not gate.config.consent:
            return Err(SdkError("llm_data_consent=false，禁止任何真实推送"))
        guard = getattr(self.dispatcher, "_guard", self.guard)
        verdict = None
        try:
            verdict = guard.allow(critical=False, terminal=False, now=time.time())
        except TypeError:
            try:
                verdict = guard.allow(now=time.time())
            except Exception:
                verdict = None
        if verdict is not None and not bool(getattr(verdict, "allowed", verdict)):
            return Err(SdkError(f"输出安全门拒绝：{getattr(verdict, 'reason', 'safety_paused')}"))

        line = " ".join(str(text or "连接测试：本条只验证本地推送通道。").split())[:80]
        prompt = build_commentary_prompt(
            f"连接测试（非对局，无历史）：{line}", MatchState(),
            mode=self.arbiter.config.mode,
            max_chars=max(1, int(self._cfg.get("reply_max_chars", 28))))
        metadata = {
            "kind": "connection_test",
            "context_type": "kards_connection_test",
            "delivery_intent": "connection_test",
            "battle_reply_contract": "short_tts_line",
            "max_reply_chars": int(self._cfg.get("reply_max_chars", 28)),
            "reply_style_contract": "one_sentence_only",
            "host_callback_contract": "neko.callback.v1",
            "plugin_recommended_reply": True,
            "plugin_owned_output_enabled": bool(
                self._cfg.get("plugin_owned_output_enabled")),
            "match_id": 0,
            "attempt": 1,
            "event_max_age_seconds": float(
                self._cfg.get("battle_event_ttl_seconds", 12.0)),
            "replace_pending_intent": True,
            "interrupt_pending_intent": False,
            "host_capabilities": {
                "replace_pending": "unknown",
                "interrupt_pending": "unknown",
            },
            "non_game": True,
            "history_available": False,
            "no_history": True,
            "local_submission_only": True,
            "sdk_submission_semantics": "immediate_local_submission_not_host_consumption",
            "llm_generated": "unknown",
            "tts_played": "unknown",
            "target_lanlan_present": True,
        }
        # Authorize exactly this call on the gate only.  ``authorized_real_test``
        # does not change the gate epoch, so a legitimate pending battle event
        # is not purged by a diagnostic connection test.
        base_gate_config = gate.config
        try:
            gate.update_config(HostPushConfig(
                dry_run=base_gate_config.dry_run,
                consent=base_gate_config.consent,
                simulated=base_gate_config.simulated,
                max_records=base_gate_config.max_records,
                authorized_real_test=True,
            ))
        except Exception as exc:
            return Err(SdkError(f"无法授权测试：{type(exc).__name__}"))
        try:
            token = self.gate.get_token()
            result: GateResult = self.gate.push(
                "test",
                critical=False,
                terminal=False,
                context_token=token,
                now=time.time(),
                source=self._runtime_id,
                visibility=[],
                ai_behavior="respond",
                parts=[{"type": "text", "text": prompt}],
                priority=5,
                coalesce_key="kards:connection_test",
                target_lanlan=chosen,
                metadata=metadata,
            )
        except Exception as exc:
            return Err(SdkError(f"连接测试失败：{type(exc).__name__}"))
        finally:
            try:
                gate.update_config(HostPushConfig(
                    dry_run=base_gate_config.dry_run,
                    consent=base_gate_config.consent,
                    simulated=base_gate_config.simulated,
                    max_records=base_gate_config.max_records,
                    authorized_real_test=False,
                ))
            except Exception:
                pass
        if result.accepted:
            try:
                guard.mark_output(critical=False, terminal=False, now=time.time())
            except Exception:
                pass
        elif result.rejected and result.retryable:
            try:
                guard.record_failure(now=time.time())
            except Exception:
                pass
        return Ok({
            "accepted": bool(result.accepted),
            "sdk_submission": result.status,
            "status": result.status,
            "reason": result.reason,
            "receipt": result.to_dict(),
            "non_game": True,
            "history": None,
            "llm_generated": "unknown",
            "tts_played": "unknown",
            "target_lanlan_present": True,
            "local_submission_only": True,
        })

    @ui.action(id="mode_set", label="切换模式")
    @plugin_entry(id="mode_set", name="切换模式",
                  description="陪伴 companion / 教练 coach / 静默 silent",
                  input_schema={"type": "object", "properties": {
                      "mode": {"type": "string", "enum": ["companion", "coach", "silent"]}},
                      "required": ["mode"]})
    async def mode_set(self, *, mode: Any = None, **_):
        value = str(mode or "").strip().lower()
        if value not in ("companion", "coach", "silent"):
            return Err(SdkError(f"无效模式 {mode!r}"))
        await self.config.update({"kards": {"mode": value}})
        await self._reload_config()
        return Ok({"mode": value})

    @ui.action(id="dry_run_set", label="切换试运行")
    @plugin_entry(id="dry_run_set", name="切换试运行",
                  description="true=完整模拟仲裁/安全链，但 gate 最终 no-op，只写 events.jsonl。",
                  input_schema={"type": "object", "properties": {
                      "enabled": {"type": "boolean"}}, "required": ["enabled"]})
    async def dry_run_set(self, *, enabled: Any = None, **_):
        flag = bool(enabled) if enabled is not None else not _boolean(
            self._cfg.get("dry_run"), False)
        await self.config.update({"kards": {"dry_run": flag}})
        await self._reload_config()
        return Ok({"dry_run": flag})

    @ui.action(id="perception_settings_set", label="保存感知设置")
    @plugin_entry(id="perception_settings_set", name="感知与录制设置",
                  description="严格白名单：采样预设/四项 FPS/客户区 ROI 自动校准/"
                              "本地录制开关与 64..65536MB 上限。未知字段与固定项直接拒绝。",
                  input_schema={
                      "type": "object",
                      "properties": {
                          "perception_preset": {
                              "type": "string",
                              "enum": ["low_cost", "balanced", "quality", "custom"]},
                          "capture_fps": {"type": "number", "minimum": 1, "maximum": 30},
                          "change_fps": {"type": "number", "minimum": 0.5, "maximum": 15},
                          "hud_fps": {"type": "number", "minimum": 0.5, "maximum": 6},
                          "unit_fps": {"type": "number", "minimum": 0.5, "maximum": 12},
                          "roi_auto_calibrate": {"type": "boolean"},
                          "recording_enabled": {"type": "boolean"},
                          "recording_max_mb": {"type": "integer",
                                               "minimum": RECORDING_MAX_MB_MIN,
                                               "maximum": RECORDING_MAX_MB_MAX},
                      },
                  })
    async def perception_settings_set(self, **kwargs):
        kwargs = {key: value for key, value in kwargs.items()
                  if not str(key).startswith("_")}
        unknown = sorted(set(kwargs) - PERCEPTION_SETTINGS_ACTION_KEYS)
        if unknown:
            return Err(SdkError(f"不支持的设置项：{', '.join(unknown)}"))
        if not kwargs:
            return Err(SdkError("没有可应用的设置项"))
        try:
            patch = PerceptionSettings.validate_changes(
                kwargs, current=self._perception_settings)
        except SettingsValidationError as exc:
            return Err(SdkError(str(exc)))
        if not patch:
            return Err(SdkError("没有可应用的设置项"))
        try:
            await self.config.update({"kards": patch})
        except Exception as exc:
            return Err(SdkError(f"保存感知设置失败：{type(exc).__name__}"))
        merged = dict(self._cfg)
        merged.update(patch)
        self._apply_config({"kards": merged})
        return Ok({
            "updated": sorted(patch),
            "perception_settings": self._perception_settings_payload(),
            "recording": self._recording_payload(),
        })

    @ui.action(id="deck_import", label="导入卡组")
    @plugin_entry(id="deck_import", name="导入卡组",
                  description="粘贴 KARDS 卡组代码（%%xx|…），离线解析并持久化。",
                  input_schema={"type": "object", "properties": {
                      "code": {"type": "string"}}, "required": ["code"]})
    async def deck_import(self, *, code: Any = "", **_):
        try:
            deck = parse_deck_code(str(code or ""))
        except DeckCodeError as exc:
            return Err(SdkError(str(exc)))
        self._deck = deck
        self._cfg["imported_deck_code"] = deck.normalized_code
        await self.config.update({"kards": {"imported_deck_code": deck.normalized_code}})
        return Ok({"main": deck.main_nation, "ally": deck.ally_nation,
                   "cards": len(deck.import_ids)})

    # ---------------------------------------------------------------- 状态面

    def _public_config(self) -> dict[str, Any]:
        """Allowlisted config for UI/diagnostics; never dump arbitrary keys."""
        allowed = (
            "mode", "dry_run", "auto_start_monitoring", "llm_data_consent",
            "chatter_interval_seconds", "cooldown_seconds",
            "critical_cooldown_seconds", "min_priority", "do_not_disturb",
            "perf_mode", "tick_ms", "perception_preset", "capture_fps",
            "change_fps", "hud_fps", "unit_fps", "capture_fps_power_saver",
            "roi_auto_calibrate", "recording_enabled", "recording_max_mb",
            "catalog_enabled", "catalog_refresh_hours", "vlm_enabled",
            "plugin_owned_output_enabled", "user_chat_quiet_window_seconds",
            "battle_output_quiet_window_seconds",
            "battle_output_backpressure_seconds", "reply_max_chars",
            "battle_event_ttl_seconds",
        )
        out = {key: self._cfg.get(key, _CONFIG_DEFAULTS.get(key))
               for key in allowed}
        out["target_lanlan_present"] = bool(
            clean_target_lanlan(self._cfg.get("target_lanlan")))
        return out

    def _context_validity(self, now: float | None = None) -> dict[str, Any]:
        """Unified trusted-context check for status, tools and read publishing."""
        current = time.time() if now is None else float(now)
        max_age = max(1.0, _tick_seconds(self._cfg) * 3.0)
        with self._lifecycle_lock:
            snapshot = self._last_trusted_snapshot
            monitoring = self._monitoring
            paused = self._user_paused
            session_id = self._session_id
            tracker_session = self.tracker.session_id
            in_match = self.tracker.in_match
        scene_snapshot = self.scenario.snapshot()
        age_ms: float | None = None
        reason = ""
        if not monitoring:
            reason = "monitoring_stopped"
        elif paused:
            reason = "user_paused"
        elif snapshot is None:
            reason = "no_trusted_snapshot"
        elif not snapshot.trusted:
            reason = "snapshot_not_trusted"
        else:
            age = max(0.0, current - float(snapshot.observed_at))
            age_ms = round(age * 1000.0, 1)
            if age > max_age:
                reason = "context_stale"
            elif snapshot.state.scene != SCENE_MATCH or not in_match:
                reason = "scene_not_match"
            elif not scene_snapshot.get("context_valid"):
                reason = "scenario_context_invalid"
            elif not session_id or tracker_session != session_id:
                reason = "session_changed"
        return {
            "valid": not reason,
            "reason": reason,
            "age_ms": age_ms,
            "max_age_ms": round(max_age * 1000.0, 1),
            "snapshot_seq": None if snapshot is None else snapshot.seq,
            "session_id": session_id,
        }

    @staticmethod
    def _public_health(health: CaptureHealthSnapshot) -> dict[str, Any]:
        payload = health.to_dict()
        # Capture backends can only report occlusion positively; the current
        # implementations always set False, which must not be rendered as
        # "not occluded".  Publish unknown instead of a false claim.
        payload["occluded"] = None
        payload["occlusion_detection"] = "unavailable"
        return payload

    @classmethod
    def _public_snapshot_dict(cls, snapshot: RuntimeSnapshot) -> dict[str, Any]:
        payload = snapshot.to_public_dict()
        capture = payload.get("capture")
        if isinstance(capture, dict):
            capture["occluded"] = None
            capture["occlusion_detection"] = "unavailable"
        return payload

    def _current_health(self) -> CaptureHealthSnapshot:
        capture = self._capture
        if capture is not None:
            probe = getattr(capture, "health_snapshot", None)
            if callable(probe):
                return probe()
            return self._last_capture_health
        if self._monitoring:
            return CaptureHealthSnapshot(
                status="frame_unavailable", reason="capture_unavailable", reading=False)
        reason = "monitoring_stopped" if self._pipeline_started else "capture_not_started"
        return CaptureHealthSnapshot(status="stopped", reason=reason, reading=False)

    def _capture_payload(self, health: CaptureHealthSnapshot) -> dict[str, Any]:
        payload = self._public_health(health)
        snapshot = self._previous_snapshot
        payload["frame"] = snapshot.frame.to_dict() if snapshot is not None else None
        if snapshot is not None:
            payload["seq"] = snapshot.seq
            payload["observed_at"] = snapshot.observed_at
            payload["trusted"] = snapshot.trusted
        else:
            payload["seq"] = 0
            payload["observed_at"] = None
            payload["trusted"] = False
        validity = self._context_validity()
        payload["context_valid"] = bool(validity["valid"])
        payload["context_reason"] = validity["reason"]
        payload["context_age_ms"] = validity["age_ms"]
        payload["snapshot_seq"] = validity["snapshot_seq"]
        payload["classification_valid"] = self._classification_valid
        payload["user_paused"] = self._user_paused
        return payload

    def _detector_payload(self) -> dict[str, Any]:
        snapshot = self._previous_snapshot
        validity = self._context_validity()
        paused = not bool(validity["valid"])
        with self._lifecycle_lock:
            tracker_snapshot = self.tracker.snapshot()
            tracker_view = {
                "in_match": self.tracker.in_match,
                "game_number": self.tracker.game_number,
                "session_id": self.tracker.session_id,
            }
        with self._unit_lock:
            unit_tracker = self._unit_tracker.snapshot()
            unit_last = dict(self._last_unit_diagnostics)
        return {
            "registry": [detector.name for detector in self.detectors.detectors],
            "last": list(self._last_detector_diagnostics),
            "paused": paused,
            "unit_tracker": unit_tracker,
            "unit_last": unit_last,
            "match_tracker": {
                **tracker_view,
                "delivered": tracker_snapshot.get("delivered", 0),
                "rebase_pending": tracker_snapshot.get("rebase_pending", False),
            },
        }

    def _delivery_payload(self) -> dict[str, Any]:
        target = resolve_target_lanlan(self)
        gate_snapshot = self.gate.snapshot()
        guard_snapshot: Any = {}
        try:
            guard_snapshot = self.guard.snapshot()
        except Exception:
            guard_snapshot = {}
        return {
            "stats": self.dispatcher.stats(),
            "pending": self.dispatcher.pending,
            "breaker": self.dispatcher.breaker_snapshot().to_dict(),
            "mode": self.arbiter.config.mode,
            "target_lanlan_present": bool(target),
            "coalesce_key": getattr(self.dispatcher.config, "coalesce_key", ""),
            "reply_max_chars": getattr(self.dispatcher.config, "reply_max_chars", 28),
            "quiet_window": {
                "user_chat_seconds": self._cfg.get("user_chat_quiet_window_seconds"),
                "battle_output_seconds": self._cfg.get("battle_output_quiet_window_seconds"),
                "backpressure_seconds": self._cfg.get("battle_output_backpressure_seconds"),
            },
            "gate": {
                "phase": gate_snapshot.get("phase"),
                "epoch": gate_snapshot.get("epoch"),
                "simulating": gate_snapshot.get("simulating"),
                "paused": gate_snapshot.get("paused"),
                "real_test_used": gate_snapshot.get("real_test_used"),
                "config": gate_snapshot.get("config"),
            },
            "output_guard": guard_snapshot,
            "simulated_records": list(
                getattr(self.dispatcher, "simulated_records", []) or [])[-10:],
        }

    def _pipeline_payload(self, health: CaptureHealthSnapshot) -> dict[str, Any]:
        loop_task = self._loop_task
        scene = self.scenario.snapshot()
        validity = self._context_validity()
        paused = (not bool(health.reading) or self._capture_pause_active
                  or self._user_paused or not bool(validity["valid"]))
        return {
            "monitoring": self._monitoring,
            "started": self._pipeline_started,
            "loop_running": bool(loop_task is not None and not loop_task.done()),
            "paused": paused,
            "pause_reason": (self._pause_reason if not self._user_paused
                             else "user_pause"),
            "start_error": self._last_start_error,
            "user_paused": self._user_paused,
            "tick_seconds": _loop_seconds(self._cfg),
            "capture_fps": _capture_fps(self._cfg),
            "change_fps": self._perception_settings.change_fps,
            "hud_fps": self._perception_settings.hud_fps,
            "unit_fps": self._perception_settings.unit_fps,
            "mode": self._cfg.get("mode"),
            "dry_run": _boolean(self._cfg.get("dry_run"), False),
            "scenario": scene.get("scenario"),
            "context_valid": bool(validity["valid"]),
            "context_reason": validity["reason"],
            "context_age_ms": validity["age_ms"],
            "confirmed_exit": scene.get("confirmed_exit"),
            "classification_valid": self._classification_valid,
            "session_id": self._session_id,
            "auto_start_monitoring": _boolean(self._cfg.get("auto_start_monitoring"), True),
        }

    def _status_payload(self) -> dict[str, Any]:
        health = self._current_health()
        capture = self._capture_payload(health)
        snapshot = self._previous_snapshot
        scene_snapshot = self.scenario.snapshot()
        capture_backend = (
            self._capture.backend if self._capture is not None else None)
        return {
            "schema_version": STATUS_SCHEMA_VERSION,
            "runtime_plugin_id": self._runtime_id,
            "source_identity": self._source_identity,
            "config": self._public_config(),
            "pipeline": self._pipeline_payload(health),
            "capture": capture,
            "detectors": self._detector_payload(),
            "delivery": self._delivery_payload(),
            "perception_settings": self._perception_settings_payload(),
            "recording": self._recording_payload(),
            "scenario": scene_snapshot,
            "timeline": self.timeline.as_dict(limit=20),
            "last_snapshot": (self._public_snapshot_dict(snapshot)
                              if snapshot is not None else None),
            # ---- 兼容旧运行状态字段 ----
            "monitoring": self._monitoring,
            "window_found": bool(self._capture and self._capture.window),
            "foreground": bool(self._capture and self._capture.foreground),
            "capture_backend": capture_backend,
            "capture_stats": self._capture.stats if self._capture else {},
            "scene": self._last_scene,
            "mode": self._cfg.get("mode"),
            "dry_run": _boolean(self._cfg.get("dry_run"), False),
            "in_match": self.tracker.in_match,
            "game_number": self.tracker.game_number,
            "context_valid": bool(scene_snapshot.get("context_valid")),
            "classification_valid": self._classification_valid,
            "detector_loaded": self._detector is not None,
            "ocr_available": self._ocr is not None,
            "ocr_telemetry": (getattr(getattr(self, "hud", None), "telemetry", {}) or {}),
            "catalog": {"ready": self.catalog.ready, "cards": self.catalog.size(),
                        "fetched_at": self.catalog.fetched_at, "error": self.catalog.last_error,
                        "builtin": self.catalog.builtin},
            "deck": None if self._deck is None else {
                "main": self._deck.main_nation, "ally": self._deck.ally_nation,
                "cards": len(self._deck.import_ids)},
            "recent_events": self._events_log[-10:],
        }

    def _export_allowed(self) -> tuple[bool, str]:
        """Hard data-export policy shared by gate-backed tools."""
        if self.gate.phase == "stopped":
            return False, "stopped"
        if self.gate.phase == "stopping":
            return False, "stopping"
        if self.gate.paused:
            return False, "paused"
        if not self.gate.config.consent:
            return False, "consent_required"
        if self.gate.config.dry_run or self.gate.config.simulated:
            return False, "dry_run"
        return True, ""

    # ------------------------------------------------------------- llm tools

    @ui.context(id="dashboard", title="KARDS 陪玩")
    async def dashboard_context(self, **_):
        """Hosted UI context.  ``capture.reading`` / ``pipeline.context_valid``
        are false whenever no trusted frame or classification is available;
        recognition and commentary are paused then, and the UI must not present
        old-frame state as current."""
        status = await self.monitor_status()
        payload = _result_payload(status)
        return {"status": payload if isinstance(payload, dict) else {}}

    @llm_tool(name="kards_get_live_state",
              description="查询 KARDS 当前对局结构化状态：HQ 血量、双方回合、单位列表（含身份置信度）、最近事件。"
                          "无有效帧/分类、consent 关闭或 dry-run 时返回 paused=true 且 state=null，"
                          "不得用旧画面推断局势。玩家问局势时用。",
              parameters={"type": "object", "properties": {}})
    async def kards_get_live_state(self, **_):
        health = self._current_health()
        validity = self._context_validity()
        allowed, blocked = self._export_allowed()
        usable = bool(allowed and validity["valid"])
        current = self.tracker.current or MatchState()
        state = current.to_public_dict() if usable else None
        reason = blocked or validity["reason"] or ("" if usable else "not_in_match")
        return {
            "monitoring": self._monitoring,
            "paused": not usable,
            "reason": reason,
            "capture": self._public_health(health),
            "context_valid": bool(validity["valid"]),
            "context_age_ms": validity["age_ms"],
            "snapshot_seq": validity["snapshot_seq"],
            "classification_valid": self._classification_valid,
            "session_id": self._session_id,
            "in_match": self.tracker.in_match,
            "state": state,
            # Blocked tools expose no event details, deck or game facts.
            "recent_events": self._events_log[-12:] if usable else [],
            "deck": (None if (not usable or self._deck is None) else {
                "main": self._deck.main_nation, "ally": self._deck.ally_nation}),
            "data_policy": {
                "consent": bool(self.gate.config.consent),
                "dry_run": bool(self.gate.config.dry_run),
                "export_allowed": bool(allowed),
            },
        }

    @llm_tool(name="kards_search_cards",
              description="按卡名/关键词查询 KARDS 卡牌资料（费用、攻防、国家、效果文本）。内置卡牌库离线可用。",
              parameters={"type": "object", "properties": {
                  "query": {"type": "string", "description": "卡名或关键词"}},
                  "required": ["query"]})
    async def kards_search_cards(self, *, query: Any = "", **_):
        if not self.catalog.ready:
            return {"output": {"error": "卡牌库加载失败，请检查插件内置数据文件"}, "is_error": True}
        return {"cards": [c.to_dict() for c in self.catalog.search(str(query or ""), limit=6)]}

    @llm_tool(name="kards_import_deck",
              description="导入玩家粘贴的 KARDS 卡组代码（形如 %%45|xxxxxx;yy;…）。",
              parameters={"type": "object", "properties": {
                  "code": {"type": "string"}}, "required": ["code"]})
    async def kards_import_deck(self, *, code: Any = "", **_):
        result = await self.deck_import(code=code)
        data = _result_payload(result)
        if _result_is_error(result):
            return {"output": data or {"error": "解析失败"}, "is_error": True}
        return {"output": data}

    @llm_tool(name="kards_get_match_history",
              description="查询近期 KARDS 对局记录与胜负统计（复盘用）。consent 关闭或 dry-run 时不返回对局数据。",
              parameters={"type": "object", "properties": {
                  "limit": {"type": "integer", "description": "返回条数，默认 10"}}})
    async def kards_get_match_history(self, *, limit: Any = 10, **_):
        allowed, blocked = self._export_allowed()
        if not allowed:
            return {"matches": [], "records": {}, "blocked": blocked}
        store = getattr(self, "store", None)
        if store is None:
            return {"matches": [], "records": {}}
        stored = await store.get("matches", [])
        matches = list(_store_value(stored, []) or [])
        stored_records = await store.get("records", {})
        records = _store_value(stored_records, {}) or {}
        try:
            count = max(1, min(50, int(limit)))
        except (TypeError, ValueError):
            count = 10
        return {"matches": matches[-count:], "records": records}
