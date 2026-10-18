"""Opt-in, temporary per-match frame recording.

This module is deliberately independent from :mod:`perception.capture` so the
perception layer can submit a frame without creating an import cycle.  A frame
only has to expose the duck-typed attributes ``image``, ``frame_id``,
``captured_at``, ``width``, ``height`` and ``backend``.

Safety model
------------
* Recording is disabled by default and does nothing until a caller enables it,
  starts a match and marks the pipeline active.
* Frames are written only below the configured plugin-owned ``root`` as
  original-resolution JPEG files.  The root and every session directory are
  checked for reparse points/junctions immediately before use.
* Session directory names are random UUIDs; caller supplied session ids are
  never used as paths.
* Capacity is enforced by deleting the oldest frames written by *this* session
  only.  Metadata bytes are part of the same byte budget.
* A monotonic generation plus an activity epoch invalidate queued or in-flight
  work at ``end_match``/``stop``; a stale encoder can never rename a file into
  a later session.
* ``cleanup_orphans`` only removes directories that carry the plugin-owned
  marker/manifest and a strict UUID name.  Unknown entries are left in place
  and logged.

No OCR, no source video and no raw screenshots are produced here.
"""

from __future__ import annotations

import copy as _copy
import io
import json
import logging
import os
import queue
import re
import shutil
import stat
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Callable, Mapping

#: Marker written into the root and into every session manifest.
MAGIC = "kards_companion.temporary_frame_recorder"
#: On-disk schema version.  Older/newer versions are never deleted implicitly.
VERSION = 1
#: Plugin ownership marker value.
OWNER = "kards_companion"
#: File in ``root`` that proves the directory was claimed by this plugin.
ROOT_MARKER_NAME = ".kards-frame-recorder.json"
#: File inside every session directory.
MANIFEST_NAME = "manifest.json"
#: How long ``stop`` waits for the encoder thread before reporting a dirty stop.
WORKER_JOIN_SECONDS = 2.0
#: How long cleanup waits for an in-progress rename before giving up.
IO_LOCK_TIMEOUT_SECONDS = 2.0

# Compact uuid4().hex is what we create; the dashed canonical form is
# accepted for older/hand-repaired owned directories.
_SESSION_RE = re.compile(
    r"(?:[0-9a-f]{32}|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z"
)
_FRAME_RE = re.compile(r"frame-[0-9]{8}\.jpg\Z")
_TEMP_RE = re.compile(r"\.tmp-[0-9a-f]{32}\.(?:jpg|json)\Z")
_MAX_JSON_BYTES = 64 * 1024
_SENTINEL = object()
_LOG = logging.getLogger(__name__)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    if result != result or result in (float("inf"), float("-inf")):
        return float(default)
    return result


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value if value is not None else "").replace("\x00", "").strip()
    return text[: max(0, int(limit))]


def _config_dict(config: "RecordingConfig") -> dict[str, Any]:
    return {
        "enabled": bool(config.enabled),
        "max_bytes": int(config.max_bytes),
        "jpeg_quality": int(config.jpeg_quality),
        "min_free_bytes": int(config.min_free_bytes),
        "queue_size": int(config.queue_size),
    }


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _lexists(path: Path) -> bool:
    try:
        return os.path.lexists(os.fspath(path))
    except OSError:
        return False


def _stat_no_follow(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(os.fspath(path))
    except OSError:
        return None


def _is_reparse(st: os.stat_result | None) -> bool:
    if st is None:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attributes = getattr(st, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _is_real_dir(path: Path) -> bool:
    st = _stat_no_follow(path)
    return bool(st is not None and stat.S_ISDIR(st.st_mode) and not _is_reparse(st))


def _is_regular_file(path: Path) -> bool:
    st = _stat_no_follow(path)
    return bool(st is not None and stat.S_ISREG(st.st_mode) and not _is_reparse(st))


def _safe_error(exc: BaseException | None) -> str:
    """Return an error code suitable for snapshots (never a filesystem path)."""
    if exc is None:
        return "error"
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int):
        return f"{type(exc).__name__}:{errno}"
    return type(exc).__name__


def _free_disk_bytes(path: Path) -> int:
    """Free bytes for the volume containing *path*.

    Missing leaf directories walk up to an existing parent.  Tests can replace
    this module-level function to simulate a missing/low-disk probe.
    """
    candidate = Path(path)
    while True:
        try:
            return int(shutil.disk_usage(os.fspath(candidate)).free)
        except FileNotFoundError:
            parent = candidate.parent
            if parent == candidate:
                raise
            candidate = parent


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write *data* to a sibling temp file and atomically replace *path*."""
    temp = path.with_name(f".tmp-{uuid.uuid4().hex}{path.suffix}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(os.fspath(temp), flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
        os.replace(os.fspath(temp), os.fspath(path))
    finally:
        if _lexists(temp):
            try:
                os.unlink(os.fspath(temp))
            except OSError:
                pass


def _copy_frame_image(image: Any) -> Any:
    """Copy pixel data so the capture loop may reuse its buffer immediately."""
    copier = getattr(image, "copy", None)
    if callable(copier):
        copied = copier()
        if copied is not None and copied is not image:
            loader = getattr(copied, "load", None)
            if callable(loader):
                try:
                    loader()
                except Exception:
                    pass
            return copied
    return _copy.deepcopy(image)


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not _is_regular_file(path):
        return None
    st = _stat_no_follow(path)
    if st is None or st.st_size > _MAX_JSON_BYTES:
        return None
    try:
        with open(os.fspath(path), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


@dataclass(frozen=True)
class RecordingConfig:
    """Temporary frame recording configuration.

    ``max_bytes`` is a user-customizable budget covering JPEG files *and*
    session metadata.  The 2 GiB default is only a default, not an upper bound;
    callers may choose a larger value.  ``min_free_bytes`` is a hard disk
    headroom check performed before every file creation.
    """

    enabled: bool = False
    max_bytes: int = 2048 * 1024**2
    jpeg_quality: int = 85
    min_free_bytes: int = 256 * 1024**2
    queue_size: int = 4

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "max_bytes", max(0, _as_int(self.max_bytes, 0)))
        object.__setattr__(
            self, "jpeg_quality", min(100, max(1, _as_int(self.jpeg_quality, 85)))
        )
        object.__setattr__(
            self, "min_free_bytes", max(0, _as_int(self.min_free_bytes, 0))
        )
        object.__setattr__(
            self, "queue_size", min(64, max(1, _as_int(self.queue_size, 4)))
        )


def _coerce_config(config: RecordingConfig | Mapping[str, Any] | None) -> RecordingConfig:
    if config is None:
        return RecordingConfig()
    if isinstance(config, RecordingConfig):
        return config
    if isinstance(config, Mapping):
        allowed = {item.name for item in fields(RecordingConfig)}
        return RecordingConfig(**{key: value for key, value in config.items()
                                  if key in allowed})
    raise TypeError("config must be a RecordingConfig, mapping, or None")


@dataclass(frozen=True)
class _FrameJob:
    generation: int
    activity_epoch: int
    session_token: str
    session_dir: Path
    image: Any
    frame_id: int
    captured_at: float
    width: int
    height: int
    backend: str
    seq: int


@dataclass(frozen=True)
class _FrameFile:
    name: str
    size: int


class TemporaryFrameRecorder:
    """Bounded, opt-in recorder for temporary in-match JPEG frames."""

    def __init__(
        self,
        root: Path,
        config: RecordingConfig | Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.time,
        logger: Any = None,
    ) -> None:
        raw_root = os.fspath(root)
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise ValueError("recording root must not be empty")
        self._root = Path(raw_root).expanduser()
        self._config = _coerce_config(config)
        self._clock = clock if callable(clock) else time.time
        self._log = logger if logger is not None else _LOG

        # Serializes start/end/stop/orphan cleanup.  Workers never take this
        # lock, so cleanup cannot deadlock against an in-flight encode.
        self._lifecycle_lock = threading.RLock()
        # State lock: session identity, counters, queue reference.
        self._state_lock = threading.RLock()
        # I/O lock: root/session creation, writes, rolling deletes, cleanup.
        self._io_lock = threading.RLock()

        self._queue: queue.Queue[Any] | None = None
        self._worker: threading.Thread | None = None

        self._generation = 0
        self._activity_epoch = 0
        self._session_token = ""
        self._session_id = ""
        self._match_id = ""
        self._session_started_at = 0.0
        self._session_dir: Path | None = None
        self._session_ready = False
        self._manifest_counted = False
        self._manifest_cache: bytes | None = None
        self._root_ready = False

        self._active = False
        self._stopped = False
        self._reason = "idle"
        self._error = ""
        self._clean = True
        self._last_disk_error = ""
        # session_dir(str) -> token for directories whose cleanup failed; a
        # later stop retries them and never claims clean while they remain.
        self._pending_cleanups: dict[str, str] = {}

        self._seq = 0
        self._captured = 0
        self._written = 0
        self._file_count = 0
        self._dropped = 0
        self._dropped_queue = 0
        self._dropped_duplicate = 0
        self._dropped_late = 0
        self._dropped_error = 0
        self._dropped_oversize = 0
        self._dropped_budget = 0
        self._dropped_disk = 0
        self._dropped_cancelled = 0
        self._inflight = 0
        self._bytes = 0
        self._files: deque[_FrameFile] = deque()
        self._seen: set[tuple[str, Any]] = set()

    # ------------------------------------------------------------- public API

    def update_config(
        self, config: RecordingConfig | Mapping[str, Any] | None
    ) -> None:
        """Replace the active configuration.

        Disabling recording invalidates queued and in-flight work immediately.
        Existing temporary files stay until ``end_match``/``stop`` so a live
        match is not rewritten on every settings change.
        """
        new_config = _coerce_config(config)
        with self._state_lock:
            was_enabled = self._config.enabled
            self._config = new_config
            if self._queue is not None:
                # Queue.maxsize is a plain bound used by full(); applying it
                # live avoids a worker restart just for a settings change.
                self._queue.maxsize = int(new_config.queue_size)
            if not new_config.enabled:
                self._activity_epoch += 1
                self._active = False
                if was_enabled:
                    self._reason = "recording_disabled"
                self._drain_queue_locked()
            elif not was_enabled:
                # Explicitly turning the switch on for an existing match may
                # start recording without waiting for another lifecycle call.
                # A paused set_active(False) while already enabled is unaffected.
                self._activity_epoch += 1
                if self._session_dir is not None and not self._stopped:
                    self._active = True
                    self._reason = "recording_enabled"

    def start_match(self, session_id: str, match_id: int | str) -> None:
        """Start a new match, cleaning any previous temporary session first."""
        with self._lifecycle_lock:
            self._start_match_locked(session_id, match_id)

    def _start_match_locked(self, session_id: str, match_id: int | str) -> None:
        text = _bounded_text(session_id, 200)
        if not text:
            with self._state_lock:
                self._error = "invalid_session_id"
                self._reason = "start_rejected"
            self._warn("recording.start_match rejected: empty session id")
            return

        self._end_current("new_match")
        retry_clean, retry_error = self._retry_pending_cleanups()
        if not retry_clean:
            # Never start a new session over temporary files from an older one.
            # The pending entry is preserved for a later retry/stop/startup
            # cleanup; no new token/counters are installed here.
            with self._state_lock:
                self._active = False
                self._reason = "start_blocked_pending_cleanup"
                self._clean = False
                self._error = retry_error or "pending_cleanup"
            self._warn(
                "recording.start_match blocked by pending cleanup ({})",
                retry_error or "pending_cleanup",
            )
            return
        try:
            started_at = _as_float(self._clock(), 0.0)
        except Exception:
            started_at = 0.0

        with self._state_lock:
            self._generation += 1
            self._activity_epoch += 1
            self._session_token = uuid.uuid4().hex
            self._session_id = text
            self._match_id = _bounded_text(match_id, 120)
            self._session_started_at = started_at
            self._session_dir = self._root / self._session_token
            self._session_ready = False
            self._manifest_counted = False
            self._manifest_cache = None
            self._active = bool(self._config.enabled)
            self._stopped = False
            self._reason = "match_start"
            self._clean = True
            self._error = ""
            self._last_disk_error = ""

            self._seq = 0
            self._captured = 0
            self._written = 0
            self._file_count = 0
            self._dropped = 0
            self._dropped_queue = 0
            self._dropped_duplicate = 0
            self._dropped_late = 0
            self._dropped_error = 0
            self._dropped_oversize = 0
            self._dropped_budget = 0
            self._dropped_disk = 0
            self._dropped_cancelled = 0
            self._inflight = 0
            self._bytes = 0
            self._files.clear()
            self._seen.clear()
            self._drain_queue_locked()

    def set_active(self, active: bool, reason: str = "") -> None:
        """Pause/resume writing without ending the match."""
        active = bool(active)
        text = _bounded_text(reason, 80)
        with self._state_lock:
            if active and not self._config.enabled:
                active = False
            if active == self._active:
                if text:
                    self._reason = text
                return
            self._active = active
            self._activity_epoch += 1
            self._reason = text or ("active" if active else "inactive")
            if not active:
                # Frames captured before a manual pause/unknown screen must not
                # finish writing after the pause starts.
                self._drain_queue_locked()

    def submit(self, frame: Any) -> bool:
        """Queue one frame without ever waiting for the encoder.

        Returns ``True`` only when the frame was accepted into the bounded
        queue.  Disabled, paused, duplicate, invalid and queue-full frames
        return ``False``; queue-full frames are counted as dropped.
        """
        try:
            with self._state_lock:
                if (
                    not self._config.enabled
                    or not self._active
                    or self._stopped
                    or self._session_dir is None
                ):
                    return False

                image = getattr(frame, "image", None)
                if image is None:
                    self._dropped += 1
                    self._dropped_error += 1
                    self._error = "invalid_frame"
                    return False

                frame_id = _as_int(getattr(frame, "frame_id", 0), 0)
                captured_at = _as_float(
                    getattr(frame, "captured_at", 0.0), 0.0
                )
                if captured_at > 0.0:
                    identity: tuple[str, Any] | None = ("t", captured_at)
                elif frame_id > 0:
                    identity = ("f", frame_id)
                else:
                    identity = None
                if identity is not None and identity in self._seen:
                    self._dropped += 1
                    self._dropped_duplicate += 1
                    return False

                try:
                    copied = _copy_frame_image(image)
                except Exception:
                    self._dropped += 1
                    self._dropped_error += 1
                    self._error = "image_copy_failed"
                    return False

                if identity is not None:
                    self._seen.add(identity)
                self._seq += 1
                self._captured += 1
                job = _FrameJob(
                    generation=self._generation,
                    activity_epoch=self._activity_epoch,
                    session_token=self._session_token,
                    session_dir=self._session_dir,
                    image=copied,
                    frame_id=frame_id,
                    captured_at=captured_at,
                    width=_as_int(getattr(frame, "width", 0), 0),
                    height=_as_int(getattr(frame, "height", 0), 0),
                    backend=_bounded_text(getattr(frame, "backend", ""), 40),
                    seq=self._seq,
                )
                work_queue = self._ensure_worker_locked()

            try:
                work_queue.put_nowait(job)
                return True
            except queue.Full:
                with self._state_lock:
                    self._dropped += 1
                    self._dropped_queue += 1
                    self._error = "queue_full"
                return False
        except Exception:
            with self._state_lock:
                self._dropped += 1
                self._dropped_error += 1
                self._error = "submit_error"
            return False

    def end_match(self, reason: str = "match_end") -> None:
        """Remove all temporary files owned by the current match."""
        with self._lifecycle_lock:
            self._end_current(_bounded_text(reason, 80) or "match_end")

    def stop(self, reason: str = "stopped") -> None:
        """Stop the worker, join it with a bound, and clean the live session."""
        with self._lifecycle_lock:
            self._stop_locked(reason)

    def _stop_locked(self, reason: str = "stopped") -> None:
        text = _bounded_text(reason, 80) or "stopped"
        with self._state_lock:
            already_stopped = self._stopped
            previous_clean = self._clean
            previous_error = self._error
            pending = list(self._pending_cleanups.items())
            self._stopped = True
            self._reason = text
            self._generation += 1
            self._activity_epoch += 1
            self._active = False
            session_dir = self._session_dir
            session_token = self._session_token
            self._session_dir = None
            self._drain_queue_locked()
            work_queue = self._queue
            worker = self._worker

        if work_queue is not None:
            try:
                work_queue.put_nowait(_SENTINEL)
            except queue.Full:
                try:
                    work_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    work_queue.put_nowait(_SENTINEL)
                except queue.Full:
                    pass

        if worker is not None and worker.is_alive():
            worker.join(WORKER_JOIN_SECONDS)
        timed_out = bool(worker is not None and worker.is_alive())

        clean = True
        cleanup_error = ""
        if session_dir is not None:
            clean, cleanup_error, _removed = self._cleanup_session(
                session_dir, session_token
            )
            with self._state_lock:
                if clean:
                    self._pending_cleanups.pop(str(session_dir), None)
                else:
                    self._pending_cleanups[str(session_dir)] = session_token

        for pending_path, pending_token in pending:
            if session_dir is not None and pending_path == str(session_dir):
                continue
            pending_clean, pending_error, _removed = self._cleanup_session(
                Path(pending_path), pending_token
            )
            with self._state_lock:
                if pending_clean:
                    self._pending_cleanups.pop(pending_path, None)
                else:
                    self._pending_cleanups[pending_path] = pending_token
            if not pending_clean:
                clean = False
                if not cleanup_error:
                    cleanup_error = pending_error or "cleanup_incomplete"

        with self._state_lock:
            self._queue = None
            self._worker = None
            self._session_ready = False
            self._manifest_cache = None
            self._manifest_counted = False
            self._bytes = 0
            self._file_count = 0
            self._files.clear()
            pending_left = bool(self._pending_cleanups)
            if timed_out:
                self._clean = False
                self._error = "worker_stop_timeout"
            elif not clean or pending_left:
                self._clean = False
                self._error = cleanup_error or "pending_cleanup"
            elif already_stopped and not previous_clean:
                # A previous stop could not prove a clean shutdown; a later
                # no-op stop must not silently turn that into clean=true.
                self._clean = False
                self._error = previous_error or "cleanup_incomplete"
            else:
                self._clean = True
                self._error = ""

    def snapshot(self) -> dict[str, Any]:
        """Return bounded diagnostics only: no paths and no image data."""
        with self._state_lock:
            queue_depth = 0
            if self._queue is not None:
                try:
                    queue_depth = max(0, int(self._queue.qsize()))
                except Exception:
                    queue_depth = 0
            inflight = max(0, int(self._inflight))
            pending = queue_depth + inflight
            if not self._config.enabled:
                state = "disabled"
            elif self._stopped:
                state = "stopped"
            elif self._active and self._session_dir is not None:
                state = "recording"
            elif self._session_dir is not None:
                state = "paused"
            else:
                state = "idle"
            return {
                "enabled": bool(self._config.enabled),
                "active": bool(self._active),
                "state": state,
                "reason": _bounded_text(self._reason, 80),
                "error": _bounded_text(self._error, 160),
                "session": (
                    self._session_token[:8]
                    if self._config.enabled
                    and self._session_dir is not None
                    and self._session_token
                    else ""
                ),
                "queued": queue_depth,
                "inflight": inflight,
                "pending": pending,
                "bytes": max(0, int(self._bytes)),
                "max_bytes": int(self._config.max_bytes),
                "captured": max(0, int(self._captured)),
                "written": max(0, int(self._written)),
                "files": max(0, int(self._file_count)),
                "dropped": max(0, int(self._dropped)),
                "queue_dropped": max(0, int(self._dropped_queue)),
                "duplicate_dropped": max(0, int(self._dropped_duplicate)),
                "late_dropped": max(0, int(self._dropped_late)),
                "error_dropped": max(0, int(self._dropped_error)),
                "oversize_dropped": max(0, int(self._dropped_oversize)),
                "budget_dropped": max(0, int(self._dropped_budget)),
                "disk_dropped": max(0, int(self._dropped_disk)),
                "cancelled": max(0, int(self._dropped_cancelled)),
                "clean": bool(self._clean),
            }

    def cleanup_orphans(self) -> dict[str, Any]:
        """Remove abandoned plugin-owned sessions from previous runs.

        Ownership is proven per session by its manifest.  Unknown files,
        unknown directories and directories without a valid manifest are left
        untouched.  This method never creates the root.
        """
        with self._lifecycle_lock:
            return self._cleanup_orphans_locked()

    def _cleanup_orphans_locked(self) -> dict[str, Any]:
        with self._state_lock:
            current_session = self._session_dir
        result: dict[str, Any] = {
            "status": "ok",
            "removed": 0,
            "skipped": 0,
            "bytes": 0,
            "error": "",
        }
        if not self._io_lock.acquire(timeout=IO_LOCK_TIMEOUT_SECONDS):
            result.update(status="busy", error="io_busy")
            return result
        try:
            root = self._root
            if not _lexists(root):
                result["status"] = "missing"
                return result
            if not _is_real_dir(root):
                result.update(status="refused", error="root_not_directory")
                return result
            if _lexists(root / ROOT_MARKER_NAME) and self._read_root_marker() is None:
                # A corrupt/tampered root marker means the directory is not
                # safe to act on.  A missing marker is tolerated: ownership is
                # proven per session by its manifest.
                result.update(status="refused", error="root_marker_invalid")
                return result

            removed = 0
            skipped = 0
            removed_bytes = 0
            try:
                with os.scandir(os.fspath(root)) as scan:
                    entries = list(scan)
            except OSError as exc:
                result.update(status="error", error=_safe_error(exc))
                return result

            for entry in entries:
                name = entry.name
                if name == ROOT_MARKER_NAME:
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    is_dir = False
                entry_path = Path(entry.path)
                if (
                    not _SESSION_RE.fullmatch(name)
                    or not is_dir
                    or _is_reparse(_stat_no_follow(entry_path))
                ):
                    skipped += 1
                    self._warn("recording.cleanup_orphans: leaving unknown entry {}", name)
                    continue
                if current_session is not None and entry_path == current_session:
                    skipped += 1
                    continue
                if self._read_manifest(entry_path) is None:
                    skipped += 1
                    self._warn(
                        "recording.cleanup_orphans: leaving unowned session {}", name
                    )
                    continue
                clean, _error, size = self._remove_owned_session_locked(
                    entry_path, name
                )
                removed_bytes += size
                if clean:
                    removed += 1
                else:
                    skipped += 1
                    self._warn(
                        "recording.cleanup_orphans: session {} kept ({})", name, _error
                    )

            with self._state_lock:
                for pending_path in list(self._pending_cleanups):
                    if not _lexists(Path(pending_path)):
                        self._pending_cleanups.pop(pending_path, None)
            result.update(
                status="ok",
                removed=removed,
                skipped=skipped,
                bytes=removed_bytes,
            )
            return result
        except Exception as exc:
            result.update(status="error", error=_safe_error(exc))
            return result
        finally:
            self._io_lock.release()

    # ---------------------------------------------------------------- lifecycle

    def _end_current(self, reason: str) -> tuple[bool, str]:
        with self._state_lock:
            session_dir = self._session_dir
            session_token = self._session_token
            if session_dir is None:
                self._active = False
                self._reason = reason
                self._drain_queue_locked()
                return self._clean, self._error if not self._clean else ""
            self._generation += 1
            self._activity_epoch += 1
            self._active = False
            self._reason = reason
            self._session_dir = None
            self._session_ready = False
            self._manifest_cache = None
            self._drain_queue_locked()

        clean, error, _removed = self._cleanup_session(session_dir, session_token)
        with self._state_lock:
            self._bytes = 0
            self._manifest_counted = False
            self._file_count = 0
            self._files.clear()
            if clean:
                self._pending_cleanups.pop(str(session_dir), None)
            else:
                self._pending_cleanups[str(session_dir)] = session_token
            if not clean:
                self._clean = False
                self._error = error or "cleanup_incomplete"
            elif self._pending_cleanups:
                self._clean = False
                self._error = "pending_cleanup"
            else:
                self._clean = True
            return self._clean, self._error if not self._clean else ""

    def _cleanup_session(
        self, session_dir: Path | None, session_token: str
    ) -> tuple[bool, str, int]:
        if session_dir is None:
            return True, "", 0
        if not self._io_lock.acquire(timeout=IO_LOCK_TIMEOUT_SECONDS):
            return False, "io_busy", 0
        try:
            return self._remove_owned_session_locked(session_dir, session_token)
        except Exception as exc:
            return False, _safe_error(exc), 0
        finally:
            self._io_lock.release()

    def _retry_pending_cleanups(self) -> tuple[bool, str]:
        """Retry every session whose previous cleanup failed.

        Called under ``_lifecycle_lock``.  Returns false while any directory
        remains; the caller must not start a new match in that case.
        """
        with self._state_lock:
            pending = list(self._pending_cleanups.items())
        clean = True
        error = ""
        for path_text, token in pending:
            cleaned, cleanup_error, _removed = self._cleanup_session(
                Path(path_text), token
            )
            with self._state_lock:
                if cleaned:
                    self._pending_cleanups.pop(path_text, None)
                else:
                    self._pending_cleanups[path_text] = token
            if not cleaned:
                clean = False
                if not error:
                    error = cleanup_error or "cleanup_incomplete"
        with self._state_lock:
            if self._pending_cleanups:
                clean = False
                if not error:
                    error = "pending_cleanup"
        return clean, error

    # ------------------------------------------------------------------- worker

    def _ensure_worker_locked(self) -> queue.Queue[Any]:
        if self._queue is None:
            self._queue = queue.Queue(maxsize=self._config.queue_size)
        if self._worker is None or not self._worker.is_alive():
            work_queue = self._queue
            self._worker = threading.Thread(
                target=self._worker_loop,
                args=(work_queue,),
                name="kards-temporary-frame-recorder",
                daemon=True,
            )
            self._worker.start()
        return self._queue

    def _worker_loop(self, work_queue: queue.Queue[Any]) -> None:
        while True:
            item = work_queue.get()
            try:
                if item is _SENTINEL:
                    break
                if not isinstance(item, _FrameJob):
                    continue
                with self._state_lock:
                    self._inflight += 1
                try:
                    self._process_job(item)
                except Exception as exc:
                    self._record_drop("error", _safe_error(exc))
                finally:
                    with self._state_lock:
                        self._inflight = max(0, self._inflight - 1)
            finally:
                try:
                    work_queue.task_done()
                except (ValueError, AttributeError):
                    pass

    def _process_job(self, job: _FrameJob) -> None:
        with self._state_lock:
            current = self._job_current_locked(job)
            quality = int(self._config.jpeg_quality)
        if not current:
            self._count_late()
            return
        try:
            encoded = self._encode_jpeg(job.image, quality)
        except Exception as exc:
            self._record_drop("error", f"encode_failed:{type(exc).__name__}")
            return
        if not isinstance(encoded, (bytes, bytearray)) or not encoded:
            self._record_drop("error", "encode_empty")
            return
        payload = bytes(encoded)

        if not self._io_lock.acquire(timeout=WORKER_JOIN_SECONDS):
            self._record_drop("error", "io_busy")
            return
        try:
            if not self._job_current_locked(job):
                self._count_late()
                return
            self._store_job_locked(job, payload)
        finally:
            self._io_lock.release()

    def _encode_jpeg(self, image: Any, quality: int) -> bytes:
        mode = str(getattr(image, "mode", "") or "").upper()
        converted = None
        if mode and mode not in {"RGB", "L"}:
            converter = getattr(image, "convert", None)
            if callable(converter):
                converted = converter("RGB")
                image = converted
        try:
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=int(quality))
            return buffer.getvalue()
        finally:
            closer = getattr(converted, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass

    # --------------------------------------------------------------- file store

    def _store_job_locked(self, job: _FrameJob, jpeg: bytes) -> None:
        if not self._job_current_locked(job):
            self._count_late()
            return

        with self._state_lock:
            max_bytes = int(self._config.max_bytes)
            manifest = self._manifest_cache
        if manifest is None:
            manifest = self._build_manifest(job)
            with self._state_lock:
                self._manifest_cache = manifest

        # A single frame larger than the whole budget is dropped unchanged: no
        # automatic quality reduction and no resize.
        if len(jpeg) > max_bytes:
            self._record_drop("oversize", "frame_too_large")
            return
        # The session metadata is mandatory and is part of the same budget.
        if not self._manifest_counted and len(jpeg) + len(manifest) > max_bytes:
            if not self._files:
                self._record_drop("budget", "budget_exceeded")
                return

        marker_bytes = 0
        if not _lexists(self._root / ROOT_MARKER_NAME):
            marker_bytes = len(self._root_marker_payload())
        if not self._disk_allows_locked(
            self._root, len(jpeg) + len(manifest) + marker_bytes
        ):
            self._record_drop("disk", self._last_disk_error or "disk_margin")
            return

        ok, error = self._ensure_root_owned_locked()
        if not ok:
            self._record_drop("error", error)
            return
        ok, error = self._ensure_session_locked(job, manifest)
        if not ok:
            self._record_drop("error", error)
            return

        if not self._job_current_locked(job):
            self._count_late()
            return
        # Check disk headroom *before* rolling old frames: deleting old files
        # and then failing the write would lose data without recording new data.
        if not self._disk_allows_locked(job.session_dir, len(jpeg)):
            self._record_drop("disk", self._last_disk_error or "disk_margin")
            return
        with self._state_lock:
            max_bytes = int(self._config.max_bytes)
        if not self._make_room_locked(job.session_dir, len(jpeg), max_bytes):
            self._record_drop("budget", "budget_exceeded")
            return
        if not self._job_current_locked(job):
            self._count_late()
            return
        self._write_frame_locked(job, jpeg)

    def _ensure_root_owned_locked(self) -> tuple[bool, str]:
        root = self._root
        marker_path = root / ROOT_MARKER_NAME
        if self._root_ready:
            # Cheap lstat revalidation; parsing the marker JSON on every frame
            # would add avoidable write-path overhead.
            if _is_real_dir(root) and _is_regular_file(marker_path):
                return True, ""
            self._root_ready = False

        if _lexists(root):
            if not _is_real_dir(root):
                return False, "root_unsafe"
        else:
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return False, _safe_error(exc)
            if not _is_real_dir(root):
                return False, "root_unsafe"

        if _lexists(marker_path):
            if self._read_root_marker() is None:
                return False, "root_marker_invalid"
            self._root_ready = True
            return True, ""

        payload = self._root_marker_payload()
        if not self._disk_allows_locked(root, len(payload)):
            return False, self._last_disk_error or "disk_margin"
        try:
            _atomic_write_bytes(marker_path, payload)
        except OSError as exc:
            return False, _safe_error(exc)
        if self._read_root_marker() is None:
            return False, "root_marker_invalid"
        self._root_ready = True
        return True, ""

    def _ensure_session_locked(
        self, job: _FrameJob, manifest: bytes
    ) -> tuple[bool, str]:
        root = self._root
        session_dir = job.session_dir
        token = job.session_token
        if (
            not _SESSION_RE.fullmatch(token)
            or session_dir.name != token
            or session_dir.parent != root
        ):
            return False, "unsafe_session_path"
        if not _is_real_dir(root):
            return False, "root_unsafe"

        if self._session_ready:
            if not _is_real_dir(session_dir):
                return False, "unsafe_session_dir"
            if not _is_regular_file(session_dir / MANIFEST_NAME):
                self._session_ready = False
                return False, "session_manifest_missing"
            return True, ""

        if _lexists(session_dir):
            if not _is_real_dir(session_dir):
                return False, "unsafe_session_dir"
            manifest_path = session_dir / MANIFEST_NAME
            if _lexists(manifest_path):
                if self._read_manifest(session_dir) is None:
                    return False, "session_manifest_invalid"
                if not self._manifest_counted:
                    try:
                        manifest_size = int(manifest_path.stat().st_size)
                    except OSError:
                        manifest_size = len(manifest)
                    with self._state_lock:
                        self._bytes += manifest_size
                        self._manifest_counted = True
                self._session_ready = True
                return True, ""
            try:
                has_entries = any(True for _ in os.scandir(os.fspath(session_dir)))
            except OSError as exc:
                return False, _safe_error(exc)
            if has_entries:
                return False, "session_dir_conflict"
        else:
            if not self._disk_allows_locked(root, len(manifest)):
                return False, self._last_disk_error or "disk_margin"
            try:
                session_dir.mkdir(parents=False)
            except OSError as exc:
                return False, _safe_error(exc)
            if not _is_real_dir(session_dir):
                return False, "unsafe_session_dir"

        manifest_path = session_dir / MANIFEST_NAME
        try:
            _atomic_write_bytes(manifest_path, manifest)
        except OSError as exc:
            try:
                if not any(True for _ in os.scandir(os.fspath(session_dir))):
                    os.rmdir(os.fspath(session_dir))
            except OSError:
                pass
            return False, _safe_error(exc)
        with self._state_lock:
            self._bytes += len(manifest)
            self._manifest_counted = True
        self._session_ready = True
        return True, ""

    def _make_room_locked(
        self, session_dir: Path, incoming: int, max_bytes: int
    ) -> bool:
        with self._state_lock:
            total = int(self._bytes)
        if total + incoming > max_bytes:
            # Only roll if the removable frames can actually make the new
            # frame fit.  Otherwise deleting old frames would lose data and
            # still fail to record the incoming one.
            removable = sum(max(0, int(item.size)) for item in self._files)
            if total - removable + incoming > max_bytes:
                return False
        while total + incoming > max_bytes and self._files:
            oldest = self._files.popleft()
            frame_path = session_dir / oldest.name
            if not _FRAME_RE.fullmatch(oldest.name):
                with self._state_lock:
                    self._dropped += 1
                    self._dropped_error += 1
                    self._error = "unsafe_frame_path"
                return False
            if _lexists(frame_path):
                if not _is_regular_file(frame_path):
                    with self._state_lock:
                        self._dropped += 1
                        self._dropped_error += 1
                        self._error = "unsafe_frame_path"
                    return False
                try:
                    os.unlink(os.fspath(frame_path))
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    self._files.appendleft(oldest)
                    with self._state_lock:
                        self._dropped += 1
                        self._dropped_budget += 1
                        self._error = _safe_error(exc)
                    return False
            total = max(0, total - oldest.size)
            with self._state_lock:
                self._bytes = max(0, int(self._bytes) - oldest.size)
                self._file_count = max(0, int(self._file_count) - 1)
        return total + incoming <= max_bytes

    def _write_frame_locked(self, job: _FrameJob, jpeg: bytes) -> None:
        session_dir = job.session_dir
        name = f"frame-{job.seq:08d}.jpg"
        final = session_dir / name
        if _lexists(final):
            self._record_drop("error", "frame_path_exists")
            return
        if not self._job_current_locked(job):
            self._count_late()
            return
        try:
            _atomic_write_bytes(final, jpeg)
        except OSError as exc:
            self._record_drop("error", _safe_error(exc))
            return

        with self._state_lock:
            self._bytes += len(jpeg)
            self._file_count += 1
            self._written += 1
        self._files.append(_FrameFile(name=name, size=len(jpeg)))

    # ----------------------------------------------------------------- cleanup

    def _remove_owned_session_locked(
        self, session_dir: Path, session_token: str
    ) -> tuple[bool, str, int]:
        root = self._root
        if (
            not _SESSION_RE.fullmatch(session_token)
            or session_dir.name != session_token
            or session_dir.parent != root
        ):
            return False, "unsafe_session_path", 0
        if not _lexists(session_dir):
            return True, "", 0
        if not _is_real_dir(session_dir):
            return False, "unsafe_session_dir", 0
        if not _is_real_dir(root):
            return False, "root_unsafe", 0

        manifest_path = session_dir / MANIFEST_NAME
        manifest_ok = self._read_manifest(session_dir) is not None
        if _lexists(manifest_path) and not _is_regular_file(manifest_path):
            return False, "unsafe_manifest", 0

        removed_bytes = 0
        unknown: list[str] = []
        try:
            with os.scandir(os.fspath(session_dir)) as scan:
                entries = list(scan)
        except OSError as exc:
            return False, _safe_error(exc), 0

        for entry in entries:
            name = entry.name
            entry_path = Path(entry.path)
            known = name == MANIFEST_NAME or bool(
                _FRAME_RE.fullmatch(name) or _TEMP_RE.fullmatch(name)
            )
            if not known or not manifest_ok or not _is_regular_file(entry_path):
                unknown.append(name)
                continue
            try:
                size = int(entry_path.stat().st_size)
            except OSError:
                size = 0
            try:
                os.unlink(os.fspath(entry_path))
            except OSError as exc:
                unknown.append(name)
                self._warn("recording.cleanup: cannot remove {} ({})", name, _safe_error(exc))
                continue
            removed_bytes += size

        if unknown:
            self._warn(
                "recording.cleanup: leaving unknown entries in {}: {}",
                session_dir.name,
                ",".join(sorted(unknown)[:8]),
            )
            return False, "unknown_entries", removed_bytes
        try:
            os.rmdir(os.fspath(session_dir))
        except OSError as exc:
            return False, _safe_error(exc), removed_bytes
        return True, "", removed_bytes

    # ------------------------------------------------------------------ helpers

    def _job_current_locked(self, job: _FrameJob) -> bool:
        return bool(
            self._config.enabled
            and self._active
            and not self._stopped
            and self._generation == job.generation
            and self._activity_epoch == job.activity_epoch
            and self._session_token == job.session_token
            and self._session_dir == job.session_dir
        )

    def _drain_queue_locked(self) -> None:
        work_queue = self._queue
        if work_queue is None:
            return
        while True:
            try:
                item = work_queue.get_nowait()
            except queue.Empty:
                return
            try:
                work_queue.task_done()
            except (ValueError, AttributeError):
                pass
            if isinstance(item, _FrameJob):
                self._dropped += 1
                self._dropped_cancelled += 1

    def _record_drop(self, kind: str, error: str = "") -> None:
        with self._state_lock:
            self._dropped += 1
            if kind == "queue":
                self._dropped_queue += 1
            elif kind == "duplicate":
                self._dropped_duplicate += 1
            elif kind == "late":
                self._dropped_late += 1
            elif kind == "oversize":
                self._dropped_oversize += 1
            elif kind == "budget":
                self._dropped_budget += 1
            elif kind == "disk":
                self._dropped_disk += 1
            else:
                self._dropped_error += 1
            if error:
                self._error = _bounded_text(error, 160)

    def _count_late(self) -> None:
        self._record_drop("late")

    def _disk_allows_locked(self, probe: Path, incoming: int) -> bool:
        try:
            free = int(_free_disk_bytes(probe))
        except Exception:
            self._last_disk_error = "disk_unavailable"
            return False
        required = max(0, int(incoming))
        if free < required or free - required < int(self._config.min_free_bytes):
            self._last_disk_error = "disk_margin"
            return False
        self._last_disk_error = ""
        return True

    def _build_manifest(self, job: _FrameJob) -> bytes:
        with self._state_lock:
            payload = {
                "plugin_owned": True,
                "magic": MAGIC,
                "version": VERSION,
                "owner": OWNER,
                "session_id": self._session_id,
                "session": self._session_id,
                "session_token": job.session_token,
                "match_id": self._match_id,
                "created_at": float(self._session_started_at),
                "config": _config_dict(self._config),
            }
        return _json_bytes(payload)

    def _root_marker_payload(self) -> bytes:
        return _json_bytes(
            {
                "plugin_owned": True,
                "magic": MAGIC,
                "version": VERSION,
                "owner": OWNER,
            }
        )

    def _read_root_marker(self) -> dict[str, Any] | None:
        payload = _read_json_file(self._root / ROOT_MARKER_NAME)
        if payload is None:
            return None
        if (
            payload.get("magic") != MAGIC
            or payload.get("version") != VERSION
            or payload.get("owner") != OWNER
            or payload.get("plugin_owned") is not True
        ):
            return None
        return payload

    def _read_manifest(self, session_dir: Path) -> dict[str, Any] | None:
        payload = _read_json_file(session_dir / MANIFEST_NAME)
        if payload is None:
            return None
        if (
            payload.get("magic") != MAGIC
            or payload.get("version") != VERSION
            or payload.get("owner") != OWNER
            or payload.get("plugin_owned") is not True
            or payload.get("session_token") != session_dir.name
        ):
            return None
        return payload

    def _warn(self, message: str, *args: Any) -> None:
        warn = getattr(self._log, "warning", None)
        if not callable(warn):
            return
        try:
            # The host logger uses brace placeholders while stdlib logging
            # uses %-formatting; pre-format so both accept the message.
            text = message.format(*args) if args else message
        except Exception:
            text = message
        try:
            warn(text)
        except Exception:
            pass
