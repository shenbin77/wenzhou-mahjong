"""The audit records predictions without treating them as ground truth."""

from __future__ import annotations

import sys
from pathlib import Path
import pytest


BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from recognition_audit import (append_frame_event, append_review,
                               build_frame_event, read_appended_events, read_events)


def _diagnostic(tile_id, *, accepted=True, reason="accepted"):
    return {"box": [0, 1, 80, 120], "region": "concealed", "label": "w1",
            "tile_id": tile_id, "score": 0.9, "margin": 0.1,
            "accepted": accepted, "reason": reason}


def test_every_prediction_is_unreviewed_until_a_human_checks(tmp_path):
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"raw frame")
    result = {"status": "advise", "hand": ["w1"] * 16,
              "vision_diagnostics": [_diagnostic(1)] * 16,
              "hand_ids": [1] * 16}
    event = build_frame_event(frame.read_bytes(), frame, result, mode="tea_2p")
    assert event["verification"] == "unreviewed_prediction"
    assert "more_than_four" in event["flags"]
    assert len(event["diagnostics"]) == 16
    append_frame_event(tmp_path, event)
    append_review(tmp_path, event, "wrong", "actual hand")
    assert read_events(tmp_path / "recognition_audit.jsonl")[0] == event
    review = read_events(tmp_path / "recognition_reviews.jsonl")[0]
    assert review["verdict"] == "wrong"
    assert review["corrected_hand"] == "actual hand"
    assert frame.read_bytes() == b"raw frame"


def test_flags_incomplete_and_missing_evidence_without_claiming_error(tmp_path):
    missing = build_frame_event(b"x", tmp_path / "x.png",
                                {"status": "abstain", "reason": "rule unclear"},
                                mode="shu_4p")
    assert missing["flags"] == ["abstain", "no_tile_evidence"]
    incomplete = build_frame_event(
        b"x", tmp_path / "x.png",
        {"status": "abstain", "vision_diagnostics": [
            _diagnostic(None, accepted=False, reason="ambiguous")]},
        mode="tea_2p")
    assert {"abstain", "unrecognized_tile", "unexpected_hand_count"} <= set(
        incomplete["flags"])
    assert incomplete["diagnostics"][0]["reason"] == "ambiguous"


def test_near_boundary_is_review_hint_and_live_tail_is_incremental(tmp_path):
    frame = tmp_path / "frame.png"
    item = _diagnostic(1)
    item["margin"] = 0.06
    event = build_frame_event(b"x", frame, {"status": "advise",
                                               "vision_diagnostics": [item] * 16},
                              mode="tea_2p")
    assert "near_acceptance_boundary" in event["flags"]
    assert event["diagnostics"][0]["accepted"] is True
    path = append_frame_event(tmp_path, event)
    first, offset = read_appended_events(path)
    assert first == [event]
    assert read_appended_events(path, offset) == ([], offset)
    with path.open("ab") as stream:
        stream.write(b'{"partial":true')
    assert read_appended_events(path, offset) == ([], offset)


def test_live_worker_records_each_analyzed_frame_and_errors(tmp_path, monkeypatch):
    import assist_window
    from live_state import FrameTiming

    class Capture:
        serial = "test"

        def grab(self):
            return b"captured png bytes"

    window = assist_window.TeaAssistWindow.__new__(assist_window.TeaAssistWindow)
    window._capture = Capture()
    monkeypatch.setattr(assist_window, "capture_result", lambda *args, **kwargs: (
        {"status": "abstain", "reason": "unclear"}, FrameTiming()))
    first = window._process_frame("tea_2p", "test", tmp_path, True)
    second = window._process_frame("tea_2p", "test", tmp_path, True)
    events = read_events(tmp_path / "recognition_audit.jsonl")
    assert len(events) == 2
    assert events[0]["frame"] != events[1]["frame"]
    assert first["result"]["audit_path"] == second["result"]["audit_path"]
    assert all(Path(event["frame"]).read_bytes() == b"captured png bytes"
               for event in events)

    def fail(*args, **kwargs):
        raise RuntimeError("recognition failed")

    monkeypatch.setattr(assist_window, "capture_result", fail)
    import pytest
    with pytest.raises(RuntimeError, match="recognition failed"):
        window._process_frame("tea_2p", "test", tmp_path, True)
    events = read_events(tmp_path / "recognition_audit.jsonl")
    assert len(events) == 3
    assert events[-1]["flags"] == ["analysis_error", "no_tile_evidence"]


def test_review_window_displays_recorded_frame_without_changing_source(tmp_path):
    import tkinter as tk
    from PIL import Image
    from recognition_review import RecognitionReview

    frame = tmp_path / "frame.png"
    Image.new("RGB", (1600, 900), "#444444").save(frame)
    before = frame.read_bytes()
    event = build_frame_event(before, frame, {
        "status": "advise", "hand": ["w1"],
        "vision_diagnostics": [_diagnostic(1)]}, mode="tea_2p")
    append_frame_event(tmp_path, event)
    root = tk.Tk()
    root.withdraw()
    try:
        review = RecognitionReview(root, tmp_path)
        review.window.withdraw()
        assert review.index == 0
        assert review.photo is not None
        assert "尚未人工复核" in review.summary.get()
        review.mark("correct")
        assert "人工确认正确" in review.summary.get()
        review.close()
    finally:
        root.destroy()
    assert frame.read_bytes() == before


def test_existing_mp4_is_sampled_and_followable_without_mutating_video(tmp_path):
    import hashlib
    import shutil
    import subprocess
    import pytest
    from recognition_video import audit_video

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe unavailable")
    video = tmp_path / "source.mp4"
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         "color=c=black:s=1600x900:r=1:d=2", "-c:v", "mpeg4", "-n", str(video)],
        check=True, capture_output=True)
    source_hash = hashlib.sha256(video.read_bytes()).hexdigest()
    outcome = audit_video(video, mode="shu_4p", session_dir=tmp_path / "session")
    events = read_events(outcome["audit"])
    assert outcome["sampled_frames"] == 2
    assert len(events) == 2
    assert [event["approx_second"] for event in events] == [0, 1]
    assert all(event["source"] == "mp4_1fps" for event in events)
    assert all(Path(event["frame"]).is_file() for event in events)
    assert hashlib.sha256(video.read_bytes()).hexdigest() == source_hash


@pytest.mark.parametrize("mode", ["温州茶 · 2人", "温州茶 · 4人", "温州熟 · 4人"])
def test_primary_window_preflight_shows_real_frame_without_adb(tmp_path, monkeypatch, mode):
    """Offline preflight UI path; isolate worker from suite CPU contention.

    Production still uses ThreadPoolExecutor; this test runs the submitted
    callable inline so a saturated machine cannot leave ``window.future``
    pending past a wall-clock poll (温州熟 flake under full-suite load).
    Production gates / timeouts are untouched.
    """
    import time
    import tkinter as tk
    from concurrent.futures import Future
    import assist_window
    from tile_render import _has_face

    def forbidden_adb(*args, **kwargs):
        raise AssertionError("offline preflight must not access ADB")

    def _inline_submit(fn, /, *args, **kwargs):
        # Same Future API as ThreadPoolExecutor.submit, without a worker thread.
        fut: Future = Future()
        if not fut.set_running_or_notify_cancel():
            return fut
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - mirror executor behaviour
            fut.set_exception(exc)
        return fut

    monkeypatch.setattr(assist_window, "LiveCapture", forbidden_adb)
    monkeypatch.setattr(assist_window, "save_window_state", lambda *_args, **_kwargs: None)
    root = tk.Tk()
    root.withdraw()
    window = assist_window.TeaAssistWindow(root)
    window.session_dir = tmp_path / "session"
    window.mode.set(mode)
    # Isolate load: real capture_result, but not on the shared 1-worker pool.
    monkeypatch.setattr(window.executor, "submit", _inline_submit)
    try:
        window.preflight_selected_mode()
        assert window.future is not None, f"preflight did not submit work for {mode!r}"
        # Worker already finished (inline); drain Tk until _poll paints.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            root.update()
            if window.future is None and window.picture.cget("image"):
                break
            time.sleep(0.02)
        assert window.future is None, (
            f"UI poll did not clear future for {mode!r} "
            f"(done={window.future.done() if window.future else None})"
        )
        assert window.picture.cget("image"), f"no preview image for {mode!r}"
        assert "离线预检" in window.preview_title.get()
        assert "旧截图" in window.status.get()
        assert "不能用于当前对局" in window.offline_notice.get()
        assert _has_face(window.refresh.state.suggestion_label)
        assert window.continuous is False
        if window.ticker is not None:
            root.after_cancel(window.ticker)
            window.ticker = None
        window._tick()
        assert "非实时" in window.fresh.get()
        assert "3 秒" not in window.fresh.get()
        assert window.refresh.state.actionable is True
        assert read_events(window.session_dir / "recognition_audit.jsonl")
    finally:
        window.close()


def test_video_frames_feed_primary_view_without_opening_second_window(tmp_path, monkeypatch):
    import time
    import tkinter as tk
    from PIL import Image
    import assist_window
    import recognition_video

    frame = tmp_path / "video_frame.png"
    Image.new("RGB", (1600, 900), "#333333").save(frame)
    monkeypatch.setattr(assist_window.filedialog, "askopenfilename",
                        lambda **_kwargs: str(tmp_path / "source.mp4"))

    def fake_video(_video, *, mode, session_dir, stop, progress):
        event = build_frame_event(frame.read_bytes(), frame,
                                  {"status": "abstain", "reason": "rule unreadable"},
                                  mode=mode, source="mp4_1fps")
        event["approx_second"] = 0
        append_frame_event(session_dir, event)
        progress(1)
        return {"sampled_frames": 1, "analysis_errors": 0, "stopped": False}

    monkeypatch.setattr(recognition_video, "audit_video", fake_video)
    monkeypatch.setattr(assist_window, "save_window_state", lambda *_args, **_kwargs: None)
    root = tk.Tk()
    root.withdraw()
    window = assist_window.TeaAssistWindow(root)
    window.session_dir = tmp_path / "session"
    try:
        window.import_video_review()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            root.update()
            if window.video_future is None:
                break
            time.sleep(0.02)
        assert window.video_future is None
        assert window.picture.cget("image")
        assert "录像复核" in window.preview_title.get()
        assert getattr(window, "recognition_review", None) is None
    finally:
        window.close()


def test_previous_compact_window_opens_as_primary_live_view(monkeypatch):
    import tkinter as tk
    import assist_window

    monkeypatch.setattr(assist_window, "load_window_state", lambda: {
        "x": 0, "y": 0, "w": 440, "h": 560, "topmost": True})
    monkeypatch.setattr(assist_window, "save_window_state", lambda *_args, **_kwargs: None)
    root = tk.Tk()
    root.withdraw()
    requested = []
    real_geometry = root.geometry
    def record_geometry(value):
        requested.append(value)
        return real_geometry(value)
    root.geometry = record_geometry
    window = assist_window.TeaAssistWindow(root)
    try:
        root.update()
        assert requested[0].startswith("1180x860")
        assert window.start_button.cget("text") == "开始跟随"
        assert window.auto_button.cget("text") == "开启全自动代打"
        assert window.embedded_follow_log._collapsed is False
        assert not hasattr(window, "tap_armed_check")
        assert window.picture.cget("text")
        window._ready = lambda: True
        window._schedule = lambda _delay: None
        window.start()
        assert window.start_button.cget("text") == "停止跟随"
        window.stop()
        assert window.start_button.cget("text") == "开始跟随"
    finally:
        window.close()
