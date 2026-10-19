"""Offline guards for game-rendered controls; these tests never call ADB."""

import importlib.util
from pathlib import Path

import cv2
import numpy as np
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "harvest_tea.py"
SAMPLES = SCRIPT.parent.parent / "采集"
SPEC = importlib.util.spec_from_file_location("harvest_tea_under_test", SCRIPT)
harvest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harvest)


def test_tap_rejects_coordinates_outside_the_native_frame(monkeypatch):
    calls = []
    monkeypatch.setattr(harvest, "DRY_RUN", False)
    monkeypatch.setattr(harvest, "adb", lambda *args, **kw: calls.append(args))

    with pytest.raises(ValueError, match="超出截图范围"):
        harvest.tap(800, 1200, (900, 1600))
    assert calls == []


def test_swipe_uses_current_frame_height(monkeypatch):
    calls = []
    monkeypatch.setattr(harvest, "DRY_RUN", False)
    monkeypatch.setattr(harvest, "require_game_foreground", lambda: None)
    monkeypatch.setattr(harvest, "adb", lambda *args, **kw: record(args, calls))
    monkeypatch.setattr(harvest.time, "sleep", lambda _: None)

    harvest.swipe_up((900, 1600))
    assert calls == [("shell", "input", "swipe", "800", "720", "800", "315", "500")]


def test_input_refuses_other_foreground_app(monkeypatch):
    calls = []
    monkeypatch.setattr(harvest, "DRY_RUN", False)

    def fake_adb(*args, **kwargs):
        calls.append(args)
        return 0, b"topResumedActivity=ActivityRecord{1 com.other.app/Main}", b""

    monkeypatch.setattr(harvest, "adb", fake_adb)
    for action in (
        lambda: harvest.tap(400, 200, (900, 1600)),
        harvest.press_back,
        lambda: harvest.swipe_up((900, 1600)),
    ):
        with pytest.raises(RuntimeError, match="前景应用不是游戏"):
            action()
    assert all(args[:2] == ("shell", "dumpsys") for args in calls)


def test_tap_checks_foreground_before_input(monkeypatch):
    calls = []
    monkeypatch.setattr(harvest, "DRY_RUN", False)

    def fake_adb(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("shell", "dumpsys"):
            return 0, b"topResumedActivity=ActivityRecord{1 com.xm.zjgamecenter/Main}", b""
        return 0, b"", b""

    monkeypatch.setattr(harvest, "adb", fake_adb)
    harvest.tap(400, 200, (900, 1600))
    assert calls[0][:2] == ("shell", "dumpsys")
    assert calls[1] == ("shell", "input", "tap", "400", "200")


def record(args, calls):
    calls.append(args)
    return 0, b"", b""


def test_target_must_be_stable_in_a_second_frame(monkeypatch):
    frame = np.zeros((900, 1600), dtype=np.uint8)
    monkeypatch.setattr(harvest, "grab_gray", lambda: frame.copy())
    monkeypatch.setattr(harvest, "load_template", lambda _: (np.zeros((80, 100), dtype=np.uint8), .9, "test"))
    monkeypatch.setattr(harvest, "match_template", lambda *_: (True, .95, 400, 200))

    assert harvest.confirm_match("button", frame, 401, 199)[:2] == (400, 200)
    assert harvest.confirm_match("button", frame, 450, 200) is None


def test_failed_post_click_verification_stops_without_second_tap(monkeypatch):
    frame = np.zeros((900, 1600), dtype=np.uint8)
    calls = []
    monkeypatch.setattr(harvest, "grab_gray", lambda: frame)
    monkeypatch.setattr(harvest, "handle_popup", lambda _: False)
    monkeypatch.setattr(harvest, "match_template", lambda _, name:
                        (True, .95, 400, 200) if name == "button" else (False, .1, 0, 0))
    monkeypatch.setattr(harvest, "confirm_match", lambda *_: (400, 200, frame))
    monkeypatch.setattr(harvest, "tap", lambda *args: calls.append(args))
    monkeypatch.setattr(harvest.time, "sleep", lambda _: None)

    ok, _ = harvest.step_find_and_tap("button", "button", verify_tpl="next", retries=2)
    assert ok is False
    assert calls == [(400, 200, frame.shape)]


def test_dry_run_never_enters_date_clicker(monkeypatch):
    monkeypatch.setattr(harvest, "DRY_RUN", True)
    monkeypatch.setattr(harvest, "grab_gray", lambda: pytest.fail("ADB screenshot requested"))
    assert harvest.select_date("2026-10-16") is False


def test_templates_load_from_unicode_project_path(monkeypatch):
    monkeypatch.setattr(harvest, "_tpl_cache", {})
    monkeypatch.setattr(harvest, "TPL_DIR", str(SCRIPT.parent / "templates"))
    template = harvest.load_template("btn_date_dropdown")
    assert template is not None
    assert template[0].shape == (87, 102)


def test_template_match_uses_native_coordinates_at_smaller_geometry(monkeypatch):
    monkeypatch.setattr(harvest, "_tpl_cache", {})
    monkeypatch.setattr(harvest, "TPL_DIR", str(SCRIPT.parent / "templates"))
    template = harvest.load_template("btn_date_dropdown")[0]
    scaled = cv2.resize(template, (61, 52), interpolation=cv2.INTER_AREA)
    frame = np.zeros((540, 960), dtype=np.uint8)
    frame[100:152, 200:261] = scaled
    found, score, x, y = harvest.match_template(frame, "btn_date_dropdown")
    assert found and score > .99
    assert (x, y) == (230, 126)


def read_gray(path):
    return cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_GRAYSCALE)


def empty_results_frame():
    # Keep the real bottom replay-code control and clear the sole result row.
    frame = read_gray(SAMPLES / "misc_box_screens" / "tea_match_records.png")
    cv2.rectangle(frame, (20, 190), (1580, 375), 225, -1)
    return frame


def test_results_use_detail_buttons_and_never_the_bottom_code_entry(monkeypatch):
    monkeypatch.setattr(harvest, "_tpl_cache", {})
    monkeypatch.setattr(harvest, "TPL_DIR", str(SCRIPT.parent / "templates"))
    list_frame = read_gray(SAMPLES / "misc_box_screens" / "list_1015_real.png")
    empty_frame = empty_results_frame()
    assert len(harvest.match_all(list_frame, "btn_xiangqing")) == 3
    assert harvest.match_all(empty_frame, "btn_xiangqing") == []
    assert "btn_chakan_huifang" not in harvest.TEMPLATES


def test_detail_and_replay_page_controls_are_distinct(monkeypatch):
    monkeypatch.setattr(harvest, "_tpl_cache", {})
    monkeypatch.setattr(harvest, "TPL_DIR", str(SCRIPT.parent / "templates"))
    detail = read_gray(SAMPLES / "misc_box_screens" / "tea_detail.png")
    active = read_gray(SAMPLES / "misc_box_screens" / "tea_replay_tab.png")
    replay = read_gray(SAMPLES / "2026-10-16" / "740195" / "r16" / "frame_01.png")
    assert harvest.match_template(detail, "btn_replay_tab")[0]
    assert not harvest.match_template(active, "btn_replay_tab")[0]
    assert len(harvest.match_all(active, "btn_huifang")) == 5
    assert not harvest.match_template(detail, "btn_huifang")[0]
    assert harvest.match_template(replay, "btn_replay_close")[0]
    assert not harvest.match_template(active, "btn_replay_close")[0]


def test_screenshot_saves_unicode_path_without_overwriting(monkeypatch, tmp_path):
    frame = np.full((12, 18, 3), 127, dtype=np.uint8)
    ok, png = cv2.imencode(".png", frame)
    assert ok
    monkeypatch.setattr(harvest, "adb", lambda *args, **kw: (0, png.tobytes(), b""))
    target = tmp_path / "中文回放" / "截图.png"
    target.parent.mkdir()
    assert harvest.save_screenshot(str(target)) is True
    assert cv2.imdecode(np.fromfile(str(target), np.uint8), cv2.IMREAD_COLOR).shape == frame.shape
    with pytest.raises(FileExistsError):
        harvest.save_screenshot(str(target))


def test_replay_capture_refuses_archived_non_replay_frame(monkeypatch, tmp_path):
    root = SAMPLES / "2026-10-16" / "740195"
    bad = (root / "r07" / "frame_01.png").read_bytes()
    good = (root / "r16" / "frame_01.png").read_bytes()
    target = tmp_path / "round.png"
    monkeypatch.setattr(harvest, "adb", lambda *a, **k: (0, bad, b""))
    assert harvest.save_screenshot(str(target), "btn_replay_close") is False
    assert not target.exists()
    monkeypatch.setattr(harvest, "adb", lambda *a, **k: (0, good, b""))
    assert harvest.save_screenshot(str(target), "btn_replay_close") is True
    assert target.exists()


def test_invitation_uses_cancel_before_generic_close(monkeypatch):
    before = np.zeros((900, 1600), dtype=np.uint8)
    after = np.ones_like(before)
    calls = []
    monkeypatch.setattr(harvest, "DRY_RUN", False)
    monkeypatch.setattr(harvest, "grab_gray", lambda: after)
    monkeypatch.setattr(harvest, "confirm_match", lambda *_: (630, 590, before))
    monkeypatch.setattr(harvest, "tap", lambda *a: calls.append(a))
    monkeypatch.setattr(harvest.time, "sleep", lambda _: None)

    def match(frame, name):
        if frame is before and name == "invite_phrase":
            return True, .98, 850, 380
        if frame is before and name == "invite_cancel":
            return True, .99, 630, 590
        if frame is before and name == "btn_close_x":
            return True, .99, 1100, 250
        return False, 0, 0, 0

    monkeypatch.setattr(harvest, "match_template", match)
    assert harvest.handle_popup(before) is True
    assert calls == [(630, 590, before.shape)]


def test_discount_promo_requires_title_and_close_pair(monkeypatch):
    source = SAMPLES / "2026-10-19" / "promo_discount" / "raw_01.png"
    before = cv2.imdecode(np.fromfile(str(source), np.uint8), cv2.IMREAD_GRAYSCALE)
    lobby = SAMPLES / "templates" / "ui" / "overlays" / "lobby_main.png"
    after = cv2.imdecode(np.fromfile(str(lobby), np.uint8), cv2.IMREAD_GRAYSCALE)
    assert before is not None and after is not None
    score, cx, cy = harvest.discount_promo_target(before)
    assert score >= .94
    assert (cx, cy) == (1356, 134)
    assert harvest.discount_promo_target(after) is None

    calls = []
    monkeypatch.setattr(harvest, "DRY_RUN", False)
    monkeypatch.setattr(harvest, "confirm_match", lambda *_: (cx, cy, before))
    monkeypatch.setattr(harvest, "tap", lambda *args: calls.append(args))
    monkeypatch.setattr(harvest, "grab_gray", lambda: after)
    monkeypatch.setattr(harvest.time, "sleep", lambda _: None)
    assert harvest.handle_popup(before) is True
    assert calls == [(cx, cy, before.shape)]


def test_discount_promo_never_uses_unpaired_close(monkeypatch):
    frame = np.zeros((900, 1600), dtype=np.uint8)
    calls = []
    monkeypatch.setattr(harvest, "tap", lambda *args: calls.append(args))
    monkeypatch.setattr(harvest, "match_template", lambda _, name:
        (True, .99, 703, 193) if name == "promo_discount_title"
        else (False, .2, 0, 0))
    with pytest.raises(RuntimeError, match="位置未可靠确认"):
        harvest.handle_popup(frame)
    assert calls == []


def test_friend_drawer_requires_title_and_arrow_pair(monkeypatch):
    source = SAMPLES / "2026-10-19" / "friend_list" / "raw_01.png"
    before = cv2.imdecode(np.fromfile(str(source), np.uint8), cv2.IMREAD_GRAYSCALE)
    lobby = SAMPLES / "templates" / "ui" / "overlays" / "lobby_main.png"
    after = cv2.imdecode(np.fromfile(str(lobby), np.uint8), cv2.IMREAD_GRAYSCALE)
    assert before is not None and after is not None
    score, cx, cy = harvest.friend_list_target(before)
    assert score >= .94
    assert (cx, cy) == (498, 447)
    assert harvest.friend_list_target(after) is None

    calls = []
    monkeypatch.setattr(harvest, "DRY_RUN", False)
    monkeypatch.setattr(harvest, "confirm_match", lambda *_: (cx, cy, before))
    monkeypatch.setattr(harvest, "tap", lambda *args: calls.append(args))
    monkeypatch.setattr(harvest, "grab_gray", lambda: after)
    monkeypatch.setattr(harvest.time, "sleep", lambda _: None)
    assert harvest.handle_popup(before) is True
    assert calls == [(cx, cy, before.shape)]


def test_dry_run_does_not_claim_navigation_success(monkeypatch):
    frame = np.zeros((900, 1600), dtype=np.uint8)
    monkeypatch.setattr(harvest, "DRY_RUN", True)
    monkeypatch.setattr(harvest, "grab_gray", lambda: frame)
    monkeypatch.setattr(harvest, "handle_popup", lambda _: False)
    monkeypatch.setattr(harvest, "match_template", lambda *_: (True, .99, 400, 200))
    monkeypatch.setattr(harvest, "confirm_match", lambda *_: (400, 200, frame))
    monkeypatch.setattr(harvest.time, "sleep", lambda _: None)
    ok, _ = harvest.step_find_and_tap("button", "button", verify_tpl="next")
    assert ok is False


def test_checked_click_stops_when_expected_page_did_not_appear(monkeypatch):
    frame = np.zeros((900, 1600), dtype=np.uint8)
    taps = []
    monkeypatch.setattr(harvest, "DRY_RUN", False)
    monkeypatch.setattr(harvest, "grab_gray", lambda: frame)
    monkeypatch.setattr(harvest, "match_template", lambda _, name: (False, .1, 0, 0))
    monkeypatch.setattr(harvest, "confirm_match", lambda *_a, **_kw: (400, 200, frame))
    monkeypatch.setattr(harvest, "tap", lambda *a: taps.append(a))
    monkeypatch.setattr(harvest.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="点击后未出现"):
        harvest.checked_click("btn_huifang", frame, 400, 200,
                              "btn_replay_close", multiple=True)
    assert taps == [(400, 200, frame.shape)]


def test_replay_waits_for_controls_without_another_tap(monkeypatch):
    frame = np.zeros((900, 1600), dtype=np.uint8)
    taps = []
    probes = []
    monkeypatch.setattr(harvest, "DRY_RUN", False)
    monkeypatch.setattr(harvest, "grab_gray", lambda: probes.append(1) or frame)
    monkeypatch.setattr(harvest, "confirm_match", lambda *_a, **_kw: (400, 200, frame))
    monkeypatch.setattr(harvest, "tap", lambda *a: taps.append(a))
    monkeypatch.setattr(harvest.time, "sleep", lambda _: None)
    monkeypatch.setattr(harvest, "match_template", lambda *_:
                        (len(probes) >= 3, .95, 0, 0))
    assert harvest.checked_click("btn_huifang", frame, 400, 200,
                                 "btn_replay_close", verify_timeout=5) is frame
    assert len(probes) == 3
    assert taps == [(400, 200, frame.shape)]


def test_empty_results_never_click_bottom_code_entry(monkeypatch, tmp_path):
    frame = empty_results_frame()
    monkeypatch.setattr(harvest, "DRY_RUN", False)
    monkeypatch.setattr(harvest, "grab_gray", lambda: frame)
    monkeypatch.setattr(harvest, "handle_popup", lambda _: False)
    monkeypatch.setattr(harvest, "tap", lambda *_: pytest.fail("clicked an empty result page"))
    assert harvest.harvest_rows(str(tmp_path), max_pages=1) == (0, 0)


def test_recording_refuses_another_foreground_app(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(harvest, "DRY_RUN", False)
    monkeypatch.setattr(harvest.shutil, "which", lambda _: "ffprobe")
    monkeypatch.setattr(harvest, "adb", lambda *args, **kw:
                        (calls.append(args) or (0,
                         b"topResumedActivity=ActivityRecord{1 com.other.app/Main}", b"")))
    monkeypatch.setattr(harvest.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("recording started"))
    frame = np.zeros((900, 1600), dtype=np.uint8)
    with pytest.raises(RuntimeError, match="前景应用不是游戏"):
        harvest.capture_round(str(tmp_path), 1, 1, frame, 400, 200)
    assert calls == [("shell", "dumpsys", "activity", "activities")]


def test_failed_replay_click_kills_recorder_and_cleans_remote(monkeypatch, tmp_path):
    class Recorder:
        stopped = False

        def poll(self):
            return 1 if self.stopped else None

        def kill(self):
            self.stopped = True

        def wait(self, timeout):
            return 1

    recorder = Recorder()
    calls = []
    monkeypatch.setattr(harvest, "DRY_RUN", False)
    monkeypatch.setattr(harvest.shutil, "which", lambda _: "ffprobe")
    monkeypatch.setattr(harvest, "require_game_foreground", lambda: None)
    monkeypatch.setattr(harvest.subprocess, "Popen", lambda *a, **k: recorder)
    monkeypatch.setattr(harvest, "checked_click", lambda *a, **k:
                        (_ for _ in ()).throw(RuntimeError("未出现回放控件")))
    monkeypatch.setattr(harvest, "adb", lambda *args, **kw:
                        (calls.append(args) or (0, b"", b"")))
    monkeypatch.setattr(harvest.time, "sleep", lambda _: None)
    frame = np.zeros((900, 1600), dtype=np.uint8)
    with pytest.raises(RuntimeError, match="未出现回放控件"):
        harvest.capture_round(str(tmp_path), 1, 1, frame, 400, 200)
    assert recorder.stopped
    assert len(calls) == 1 and calls[0][:4] == ("shell", "rm", "-f", calls[0][3])
    assert calls[0][3].startswith("/sdcard/tea_replay_")
    assert not list(tmp_path.iterdir())
