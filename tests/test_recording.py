import io
import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image

from kards_companion.runtime import recording as recording_module
from kards_companion.runtime.recording import (
    MANIFEST_NAME,
    ROOT_MARKER_NAME,
    RecordingConfig,
    TemporaryFrameRecorder,
)

QUEUE_WAIT = 4.0


@dataclass
class FakeFrame:
    image: Image.Image
    frame_id: int
    captured_at: float
    width: int
    height: int
    backend: str = "unit_test"


def make_frame(
    image: Image.Image | None = None,
    *,
    frame_id: int = 1,
    captured_at: float = 1.0,
) -> FakeFrame:
    picture = image if image is not None else make_image()
    return FakeFrame(
        image=picture,
        frame_id=frame_id,
        captured_at=captured_at,
        width=picture.width,
        height=picture.height,
    )


def make_image(width: int = 80, height: int = 48) -> Image.Image:
    picture = Image.new("RGB", (width, height))
    pixels = picture.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = ((x * 7) % 256, (y * 13) % 256, ((x + y) * 5) % 256)
    return picture


def noise_image(size: int = 64) -> Image.Image:
    return Image.frombytes("RGB", (size, size), os.urandom(size * size * 3))


def jpeg_bytes(image: Image.Image, quality: int = 85) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def wait_until(predicate, timeout: float = QUEUE_WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def wait_idle(recorder: TemporaryFrameRecorder, timeout: float = QUEUE_WAIT) -> None:
    assert wait_until(lambda: recorder.snapshot()["pending"] == 0, timeout), (
        f"recorder did not drain: {recorder.snapshot()}"
    )


def session_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path for path in root.iterdir()
        if path.is_dir() and len(path.name) == 32
    )


def frame_files(root: Path) -> list[Path]:
    return sorted(root.glob("*/frame-*.jpg"))


def only_session(root: Path) -> Path:
    dirs = session_dirs(root)
    assert len(dirs) == 1, dirs
    return dirs[0]


def read_manifest(session_dir: Path) -> dict:
    return json.loads((session_dir / MANIFEST_NAME).read_text(encoding="utf-8"))


def make_dir_link(link: Path, target: Path) -> bool:
    """Create a symlink or (on Windows) a junction; return success."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        pass
    if os.name == "nt":
        try:
            import _winapi

            _winapi.CreateJunction(os.fspath(target), os.fspath(link))
            return True
        except Exception:
            return False
    return False


def enabled_config(**overrides) -> RecordingConfig:
    values = {
        "enabled": True,
        "min_free_bytes": 0,
        "queue_size": 8,
    }
    values.update(overrides)
    return RecordingConfig(**values)


def test_config_defaults_are_frozen_and_safe() -> None:
    config = RecordingConfig()
    assert config.enabled is False
    assert config.max_bytes == 2048 * 1024**2
    assert config.jpeg_quality == 85
    assert config.min_free_bytes == 256 * 1024**2
    assert config.queue_size == 4

    clamped = RecordingConfig(
        enabled=1, max_bytes=-5, jpeg_quality=999, min_free_bytes=-2, queue_size=0
    )
    assert clamped.enabled is True
    assert clamped.max_bytes == 0
    assert clamped.jpeg_quality == 100
    assert clamped.min_free_bytes == 0
    assert clamped.queue_size == 1


def test_disabled_recorder_creates_no_root_and_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "recording"
    recorder = TemporaryFrameRecorder(root)

    recorder.start_match("session-disabled", 1)
    assert recorder.submit(make_frame()) is False
    recorder.set_active(True, "test")
    assert recorder.submit(make_frame(frame_id=2, captured_at=2.0)) is False
    recorder.end_match()
    recorder.stop()

    assert not root.exists()
    snapshot = recorder.snapshot()
    assert snapshot["enabled"] is False
    assert snapshot["captured"] == 0
    assert snapshot["written"] == 0
    assert recorder.cleanup_orphans()["status"] == "missing"


@pytest.mark.parametrize("quality", [85, 40])
def test_records_original_size_jpeg_at_configured_quality(
    tmp_path: Path, quality: int
) -> None:
    root = tmp_path / "recording"
    image = make_image(97, 61)
    expected = jpeg_bytes(image, quality)
    recorder = TemporaryFrameRecorder(
        root, enabled_config(jpeg_quality=quality), logger=None
    )
    recorder.start_match("session-quality", 3)
    try:
        assert recorder.submit(make_frame(image, captured_at=10.0)) is True
        wait_idle(recorder)

        snapshot = recorder.snapshot()
        assert snapshot["written"] == 1
        assert snapshot["bytes"] > 0
        assert snapshot["active"] is True

        session = only_session(root)
        frames = list(session.glob("frame-*.jpg"))
        assert len(frames) == 1
        assert frames[0].read_bytes() == expected
        with Image.open(frames[0]) as saved:
            assert saved.format == "JPEG"
            assert saved.size == image.size

        manifest = read_manifest(session)
        assert manifest["plugin_owned"] is True
        assert manifest["magic"] == recording_module.MAGIC
        assert manifest["version"] == recording_module.VERSION
        assert manifest["session_id"] == "session-quality"
        assert manifest["session_token"] == session.name
        assert manifest["config"]["jpeg_quality"] == quality
    finally:
        recorder.end_match()
        recorder.stop()


def test_duplicate_timestamp_is_written_once(tmp_path: Path) -> None:
    root = tmp_path / "recording"
    recorder = TemporaryFrameRecorder(root, enabled_config())
    recorder.start_match("session-duplicate", 2)
    try:
        first = make_frame(frame_id=7, captured_at=123.0)
        assert recorder.submit(first) is True
        assert recorder.submit(first) is False
        # Same capture instant with a different frame id is still the same frame.
        assert recorder.submit(
            make_frame(frame_id=8, captured_at=123.0)
        ) is False
        wait_idle(recorder)

        snapshot = recorder.snapshot()
        assert snapshot["written"] == 1
        assert snapshot["duplicate_dropped"] >= 2
        assert len(frame_files(root)) == 1
    finally:
        recorder.end_match()
        recorder.stop()


def test_capacity_rolls_earliest_frames_under_budget(tmp_path: Path) -> None:
    root = tmp_path / "recording"
    image = noise_image(64)
    one_frame = len(jpeg_bytes(image))
    budget = 2 * one_frame + 1536
    recorder = TemporaryFrameRecorder(
        root,
        enabled_config(max_bytes=budget, queue_size=16),
    )
    recorder.start_match("session-rolling", 4)
    try:
        for index in range(1, 8):
            assert recorder.submit(
                make_frame(image, frame_id=index, captured_at=float(index))
            ) is True
        wait_idle(recorder)

        snapshot = recorder.snapshot()
        assert snapshot["bytes"] <= budget
        assert snapshot["written"] >= 3
        assert snapshot["files"] < snapshot["written"]

        session = only_session(root)
        names = sorted(path.name for path in session.glob("frame-*.jpg"))
        assert "frame-00000001.jpg" not in names
        assert names[-1] == "frame-00000007.jpg"
        total = sum(path.stat().st_size for path in session.iterdir() if path.is_file())
        assert total == snapshot["bytes"]
        assert total <= budget
    finally:
        recorder.end_match()
        recorder.stop()


def test_oversized_frame_is_dropped_without_resize_or_requality(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recording"
    image = noise_image(64)
    encoded = len(jpeg_bytes(image))
    recorder = TemporaryFrameRecorder(
        root, enabled_config(max_bytes=encoded - 10)
    )
    recorder.start_match("session-oversize", 5)
    try:
        assert recorder.submit(make_frame(image)) is True
        wait_idle(recorder)

        snapshot = recorder.snapshot()
        assert snapshot["written"] == 0
        assert snapshot["oversize_dropped"] == 1
        assert snapshot["error"] == "frame_too_large"
        assert not root.exists()
    finally:
        recorder.end_match()
        recorder.stop()


@pytest.mark.parametrize("failure", ["zero", "raise"])
def test_low_or_missing_disk_margin_blocks_creation(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    root = tmp_path / "recording"
    if failure == "zero":
        monkeypatch.setattr(recording_module, "_free_disk_bytes", lambda _path: 0)
        expected_error = "disk_margin"
    else:
        def broken(_path):
            raise OSError(28, "no space")

        monkeypatch.setattr(recording_module, "_free_disk_bytes", broken)
        expected_error = "disk_unavailable"

    recorder = TemporaryFrameRecorder(root, enabled_config())
    recorder.start_match("session-disk", 6)
    try:
        assert recorder.submit(make_frame()) is True
        wait_idle(recorder)

        snapshot = recorder.snapshot()
        assert snapshot["written"] == 0
        assert snapshot["disk_dropped"] == 1
        assert snapshot["error"] == expected_error
        assert not root.exists()
    finally:
        recorder.end_match()
        recorder.stop()


def test_manual_pause_cancels_inflight_and_queued_frames(tmp_path: Path) -> None:
    root = tmp_path / "recording"
    recorder = TemporaryFrameRecorder(root, enabled_config(queue_size=2))
    started = threading.Event()
    release = threading.Event()
    original_encode = recorder._encode_jpeg

    def blocked_encode(image, quality):
        started.set()
        assert release.wait(4.0)
        return original_encode(image, quality)

    recorder._encode_jpeg = blocked_encode  # type: ignore[method-assign]
    recorder.start_match("session-pause", 7)
    try:
        assert recorder.submit(make_frame(captured_at=1.0)) is True
        assert started.wait(2.0)
        recorder.set_active(False, "minimized")

        snapshot = recorder.snapshot()
        assert snapshot["active"] is False
        assert snapshot["state"] == "paused"
        assert snapshot["reason"] == "minimized"

        release.set()
        wait_idle(recorder)
        assert recorder.snapshot()["written"] == 0
        assert not root.exists()
    finally:
        release.set()
        recorder.end_match()
        recorder.stop()


def test_bounded_queue_drops_new_frames_without_blocking(tmp_path: Path) -> None:
    root = tmp_path / "recording"
    recorder = TemporaryFrameRecorder(root, enabled_config(queue_size=2))
    started = threading.Event()
    release = threading.Event()
    original_encode = recorder._encode_jpeg

    def blocked_encode(image, quality):
        started.set()
        assert release.wait(4.0)
        return original_encode(image, quality)

    recorder._encode_jpeg = blocked_encode  # type: ignore[method-assign]
    recorder.start_match("session-queue", 8)
    try:
        assert recorder.submit(make_frame(captured_at=1.0)) is True
        assert started.wait(2.0)

        began = time.monotonic()
        accepted = [
            recorder.submit(make_frame(captured_at=float(index)))
            for index in range(2, 7)
        ]
        elapsed = time.monotonic() - began

        assert elapsed < 1.0
        assert accepted == [True, True, False, False, False]
        snapshot = recorder.snapshot()
        assert snapshot["queued"] <= 2
        assert snapshot["pending"] <= 3
        assert snapshot["queue_dropped"] == 3

        release.set()
        wait_idle(recorder)
        snapshot = recorder.snapshot()
        assert snapshot["written"] == 3
        assert len(frame_files(root)) == 3
    finally:
        release.set()
        recorder.end_match()
        recorder.stop()


def test_end_match_prevents_late_worker_from_writing_next_session(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recording"
    recorder = TemporaryFrameRecorder(root, enabled_config())
    started = threading.Event()
    release = threading.Event()
    original_encode = recorder._encode_jpeg

    def blocked_encode(image, quality):
        started.set()
        assert release.wait(4.0)
        return original_encode(image, quality)

    recorder._encode_jpeg = blocked_encode  # type: ignore[method-assign]
    recorder.start_match("session-first", 1)
    try:
        assert recorder.submit(make_frame(captured_at=1.0)) is True
        assert started.wait(2.0)

        recorder.end_match("match_end")
        recorder.start_match("session-second", 2)
        release.set()
        wait_idle(recorder)

        assert not session_dirs(root) or all(
            read_manifest(path)["session_id"] == "session-second"
            for path in session_dirs(root)
        )
        assert len(frame_files(root)) == 0

        image = make_image(40, 30)
        assert recorder.submit(
            make_frame(image, frame_id=99, captured_at=2.0)
        ) is True
        wait_idle(recorder)

        session = only_session(root)
        manifest = read_manifest(session)
        assert manifest["session_id"] == "session-second"
        names = sorted(path.name for path in session.glob("frame-*.jpg"))
        assert names == ["frame-00000001.jpg"]
        # The stale late frame was counted, never written.
        assert recorder.snapshot()["late_dropped"] >= 1
    finally:
        release.set()
        recorder.end_match()
        recorder.stop()


def test_new_match_cleans_previous_session(tmp_path: Path) -> None:
    root = tmp_path / "recording"
    recorder = TemporaryFrameRecorder(root, enabled_config())
    recorder.start_match("session-old", 1)
    try:
        assert recorder.submit(make_frame(captured_at=1.0)) is True
        wait_idle(recorder)
        old_session = only_session(root)

        recorder.start_match("session-new", 2)
        assert not old_session.exists()
        assert recorder.snapshot()["captured"] == 0

        image = make_image(40, 30)
        assert recorder.submit(
            make_frame(image, frame_id=2, captured_at=2.0)
        ) is True
        wait_idle(recorder)

        session = only_session(root)
        assert read_manifest(session)["session_id"] == "session-new"
        assert len(frame_files(root)) == 1
    finally:
        recorder.end_match()
        recorder.stop()


def test_stop_joins_worker_and_removes_current_session(tmp_path: Path) -> None:
    root = tmp_path / "recording"
    recorder = TemporaryFrameRecorder(root, enabled_config())
    recorder.start_match("session-stop", 1)
    try:
        assert recorder.submit(make_frame(captured_at=1.0)) is True
        wait_idle(recorder)
        assert only_session(root).exists()

        worker = recorder._worker
        recorder.stop("test_stop")

        assert worker is not None and not worker.is_alive()
        assert session_dirs(root) == []
        assert (root / ROOT_MARKER_NAME).is_file()
        snapshot = recorder.snapshot()
        assert snapshot["state"] == "stopped"
        assert snapshot["active"] is False
        assert snapshot["clean"] is True
    finally:
        recorder.stop()


def test_stop_timeout_is_reported_dirty_and_never_late_writes(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "recording"
    recorder = TemporaryFrameRecorder(root, enabled_config())
    started = threading.Event()
    release = threading.Event()
    original_encode = recorder._encode_jpeg

    def blocked_encode(image, quality):
        started.set()
        assert release.wait(4.0)
        return original_encode(image, quality)

    recorder._encode_jpeg = blocked_encode  # type: ignore[method-assign]
    monkeypatch.setattr(recording_module, "WORKER_JOIN_SECONDS", 0.05)
    recorder.start_match("session-slow-stop", 1)
    worker = None
    try:
        assert recorder.submit(make_frame(captured_at=1.0)) is True
        assert started.wait(2.0)
        worker = recorder._worker
        recorder.stop("shutdown")

        snapshot = recorder.snapshot()
        assert snapshot["clean"] is False
        assert snapshot["error"] == "worker_stop_timeout"
        assert snapshot["active"] is False
        assert not root.exists()

        release.set()
        if worker is not None:
            worker.join(2.0)
        assert not root.exists()

        # A later no-op stop must not erase the dirty outcome.
        recorder.stop("again")
        snapshot = recorder.snapshot()
        assert snapshot["clean"] is False
        assert snapshot["error"] == "worker_stop_timeout"
    finally:
        release.set()
        if worker is not None:
            worker.join(2.0)
        recorder.stop()


def test_cleanup_orphans_only_removes_owned_uuid_sessions(tmp_path: Path) -> None:
    root = tmp_path / "recording"
    recorder = TemporaryFrameRecorder(root, enabled_config())
    recorder.start_match("session-source", 1)
    try:
        assert recorder.submit(make_frame(captured_at=1.0)) is True
        wait_idle(recorder)
        source = only_session(root)

        orphan_name = uuid.uuid4().hex
        orphan = root / orphan_name
        shutil.copytree(source, orphan)
        manifest = read_manifest(orphan)
        manifest["session_token"] = orphan_name
        manifest["session_id"] = "session-orphan"
        (orphan / MANIFEST_NAME).write_text(
            json.dumps(manifest), encoding="utf-8"
        )

        recorder.end_match("match_end")
        assert not source.exists()

        unknown_dir = root / "user-video"
        unknown_dir.mkdir()
        (unknown_dir / "keep.mp4").write_bytes(b"keep")
        unknown_uuid = root / uuid.uuid4().hex
        unknown_uuid.mkdir()
        (unknown_uuid / "keep.txt").write_text("keep", encoding="utf-8")

        result = recorder.cleanup_orphans()
        assert result["status"] == "ok"
        assert result["removed"] == 1
        assert result["skipped"] >= 2
        assert not orphan.exists()
        assert (unknown_dir / "keep.mp4").read_bytes() == b"keep"
        assert (unknown_uuid / "keep.txt").read_text(encoding="utf-8") == "keep"
        assert (root / ROOT_MARKER_NAME).is_file()
    finally:
        recorder.end_match()
        recorder.stop()


def test_cleanup_orphans_accepts_session_manifest_without_root_marker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recording"
    root.mkdir()
    token = uuid.uuid4().hex
    session = root / token
    session.mkdir()
    manifest = {
        "plugin_owned": True,
        "magic": recording_module.MAGIC,
        "version": recording_module.VERSION,
        "owner": recording_module.OWNER,
        "session_id": "orphan-without-root-marker",
        "session_token": token,
        "match_id": "1",
        "created_at": 1.0,
        "config": {"enabled": True},
    }
    (session / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    (session / "frame-00000001.jpg").write_bytes(b"jpeg")

    recorder = TemporaryFrameRecorder(root)  # disabled, no root marker
    result = recorder.cleanup_orphans()
    assert result["status"] == "ok"
    assert result["removed"] == 1
    assert not session.exists()
    assert not (root / ROOT_MARKER_NAME).exists()


def test_cleanup_refuses_symlinked_or_junction_session(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recording"
    recorder = TemporaryFrameRecorder(root, enabled_config())
    recorder.start_match("session-marker", 1)
    try:
        assert recorder.submit(make_frame(captured_at=1.0)) is True
        wait_idle(recorder)
        recorder.end_match("match_end")
    finally:
        recorder.stop()

    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "important.txt").write_text("do not delete", encoding="utf-8")
    link = root / uuid.uuid4().hex
    if not make_dir_link(link, victim):
        pytest.skip("symlink/junction creation is unavailable")

    result = recorder.cleanup_orphans()
    assert result["status"] == "ok"
    assert result["skipped"] >= 1
    assert (victim / "important.txt").read_text(encoding="utf-8") == "do not delete"
    assert link.exists()


def test_symlinked_root_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "recording"
    if not make_dir_link(root, target):
        pytest.skip("symlink/junction creation is unavailable")

    recorder = TemporaryFrameRecorder(root, enabled_config())
    recorder.start_match("session-symlink-root", 1)
    try:
        assert recorder.submit(make_frame(captured_at=1.0)) is True
        wait_idle(recorder)
        snapshot = recorder.snapshot()
        assert snapshot["written"] == 0
        assert snapshot["error"] == "root_unsafe"
        assert list(target.iterdir()) == []
    finally:
        recorder.end_match()
        recorder.stop()


def test_session_id_is_never_used_as_a_path(tmp_path: Path) -> None:
    root = tmp_path / "recording"
    recorder = TemporaryFrameRecorder(root, enabled_config())
    recorder.start_match("../../escape-root", "../escape-match")
    try:
        assert recorder.submit(make_frame(captured_at=1.0)) is True
        wait_idle(recorder)

        session = only_session(root)
        assert len(session.name) == 32
        assert read_manifest(session)["session_id"] == "../../escape-root"
        assert not (tmp_path / "escape-root").exists()
        assert not (root / ".." / "escape-match").exists()
        snapshot = recorder.snapshot()
        assert "\\" not in snapshot["session"] and "/" not in snapshot["session"]
    finally:
        recorder.end_match()
        recorder.stop()


def test_enable_disable_updates_are_applied_without_stale_writes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recording"
    recorder = TemporaryFrameRecorder(root)
    recorder.start_match("session-settings", 1)
    try:
        assert recorder.submit(make_frame(captured_at=1.0)) is False

        recorder.update_config(
            {"enabled": True, "min_free_bytes": 0, "queue_size": 2}
        )
        recorder.set_active(True, "frame_trusted")
        assert recorder.submit(
            make_frame(frame_id=2, captured_at=2.0)
        ) is True
        wait_idle(recorder)
        assert len(frame_files(root)) == 1

        recorder.update_config(
            {"enabled": False, "min_free_bytes": 0, "queue_size": 2}
        )
        assert recorder.snapshot()["active"] is False
        assert recorder.submit(
            make_frame(frame_id=3, captured_at=3.0)
        ) is False
        assert len(frame_files(root)) == 1
    finally:
        recorder.end_match()
        recorder.stop()


def test_frame_pixels_are_copied_before_worker_encodes(tmp_path: Path) -> None:
    root = tmp_path / "recording"
    image = Image.new("RGB", (48, 32), (10, 210, 30))
    reference = jpeg_bytes(image, 85)
    recorder = TemporaryFrameRecorder(root, enabled_config())
    recorder.start_match("session-copy", 1)
    try:
        assert recorder.submit(make_frame(image, captured_at=1.0)) is True
        # Mutating the capture-owned image must not change the queued copy.
        image.paste((255, 0, 0), (0, 0, image.width, image.height))
        wait_idle(recorder)

        frame = frame_files(root)[0]
        assert frame.read_bytes() == reference
    finally:
        recorder.end_match()
        recorder.stop()


def test_shrunk_budget_does_not_delete_frames_when_new_frame_cannot_fit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recording"
    image = noise_image(48)
    size = len(jpeg_bytes(image))
    recorder = TemporaryFrameRecorder(
        root, enabled_config(max_bytes=10 * size)
    )
    recorder.start_match("session-shrink", 1)
    try:
        assert recorder.submit(
            make_frame(image, frame_id=1, captured_at=1.0)
        ) is True
        assert recorder.submit(
            make_frame(image, frame_id=2, captured_at=2.0)
        ) is True
        wait_idle(recorder)
        before = sorted(path.name for path in frame_files(root))
        assert len(before) == 2

        recorder.update_config(enabled_config(max_bytes=size + 1))
        assert recorder.submit(
            make_frame(image, frame_id=3, captured_at=3.0)
        ) is True
        wait_idle(recorder)

        snapshot = recorder.snapshot()
        assert snapshot["budget_dropped"] == 1
        assert snapshot["error"] == "budget_exceeded"
        assert sorted(path.name for path in frame_files(root)) == before
    finally:
        recorder.end_match()
        recorder.stop()


def test_budget_is_user_customizable_and_not_capped_at_2gb(tmp_path: Path) -> None:
    root = tmp_path / "recording"
    custom_budget = 64 * 1024**3  # 64 GiB, a valid user choice
    recorder = TemporaryFrameRecorder(
        root,
        enabled_config(max_bytes=custom_budget),
    )
    recorder.start_match("session-large-budget", 1)
    try:
        assert recorder.snapshot()["max_bytes"] == custom_budget
        assert recorder.submit(make_frame(captured_at=1.0)) is True
        wait_idle(recorder)
        assert recorder.snapshot()["written"] == 1
        manifest = read_manifest(only_session(root))
        assert manifest["config"]["max_bytes"] == custom_budget
    finally:
        recorder.end_match()
        recorder.stop()


def test_start_match_retries_pending_cleanup_and_blocks_until_clean(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recording"
    recorder = TemporaryFrameRecorder(root, enabled_config())
    recorder.start_match("session-first", 1)
    try:
        assert recorder.submit(make_frame(captured_at=1.0)) is True
        wait_idle(recorder)
        old_session = only_session(root)
        old_token = old_session.name
        unknown = old_session / "user.keep"
        unknown.write_bytes(b"keep")

        recorder.end_match("match_end")
        snapshot = recorder.snapshot()
        assert snapshot["clean"] is False
        assert snapshot["error"] == "unknown_entries"
        assert unknown.read_bytes() == b"keep"

        # A new match must not start over an un-cleaned owned session.
        recorder.start_match("session-blocked", 2)
        snapshot = recorder.snapshot()
        assert snapshot["active"] is False
        assert snapshot["clean"] is False
        assert snapshot["reason"] == "start_blocked_pending_cleanup"
        assert snapshot["error"] in {"unknown_entries", "pending_cleanup"}
        assert snapshot["session"] == ""
        assert session_dirs(root) == [old_session]
        assert unknown.read_bytes() == b"keep"
        assert recorder.submit(
            make_frame(frame_id=2, captured_at=2.0)
        ) is False

        # Once the non-owned blocker is gone, the retry can finish and a new
        # match starts with a fresh token; the old session is not overwritten.
        unknown.unlink()
        recorder.start_match("session-second", 3)
        assert not old_session.exists()
        snapshot = recorder.snapshot()
        assert snapshot["active"] is True
        assert snapshot["clean"] is True
        assert snapshot["error"] == ""
        assert snapshot["session"] != old_token[:8]

        assert recorder.submit(
            make_frame(frame_id=3, captured_at=3.0)
        ) is True
        wait_idle(recorder)
        new_session = only_session(root)
        assert new_session.name != old_token
        assert read_manifest(new_session)["session_id"] == "session-second"
    finally:
        recorder.end_match()
        recorder.stop()


def test_snapshot_contains_no_paths_or_images(tmp_path: Path) -> None:
    root = tmp_path / "recording"
    recorder = TemporaryFrameRecorder(root, enabled_config())
    recorder.start_match("session-snapshot", 1)
    try:
        assert recorder.submit(make_frame(captured_at=1.0)) is True
        wait_idle(recorder)
        snapshot = recorder.snapshot()
        serialized = json.dumps(snapshot, sort_keys=True)
        assert str(root) not in serialized
        assert "image" not in snapshot
        assert "path" not in snapshot
        assert len(snapshot["session"]) <= 8
        assert snapshot["bytes"] <= snapshot["max_bytes"]
        assert set(snapshot).issubset(
            {
                "enabled", "active", "state", "reason", "error", "session",
                "queued", "inflight", "pending", "bytes", "max_bytes", "captured",
                "written", "files", "dropped", "queue_dropped",
                "duplicate_dropped", "late_dropped", "error_dropped",
                "oversize_dropped", "budget_dropped", "disk_dropped",
                "cancelled", "clean",
            }
        )
    finally:
        recorder.end_match()
        recorder.stop()
