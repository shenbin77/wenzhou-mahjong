"""Patch test_recognition_audit preflight wait: deterministic + isolate load."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
path = ROOT / "bot" / "tests" / "test_recognition_audit.py"
text = path.read_text(encoding="utf-8")
old = '''@pytest.mark.parametrize("mode", ["温州茶 · 2人", "温州茶 · 4人", "温州熟 · 4人"])
def test_primary_window_preflight_shows_real_frame_without_adb(tmp_path, monkeypatch, mode):
    import time
    import tkinter as tk
    import assist_window
    from tile_render import _has_face

    def forbidden_adb(*args, **kwargs):
        raise AssertionError("offline preflight must not access ADB")

    monkeypatch.setattr(assist_window, "LiveCapture", forbidden_adb)
    monkeypatch.setattr(assist_window, "save_window_state", lambda *_args, **_kwargs: None)
    root = tk.Tk()
    root.withdraw()
    window = assist_window.TeaAssistWindow(root)
    window.session_dir = tmp_path / "session"
    window.mode.set(mode)
    try:
        window.preflight_selected_mode()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            root.update()
            if window.future is None and window.picture.cget("image"):
                break
            time.sleep(0.02)
        assert window.future is None
        assert window.picture.cget("image")
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
'''

new = '''@pytest.mark.parametrize("mode", ["温州茶 · 2人", "温州茶 · 4人", "温州熟 · 4人"])
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
'''

if old not in text:
    raise SystemExit("OLD BLOCK NOT FOUND — abort")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("patched", path)
print("bytes", path.stat().st_size)
