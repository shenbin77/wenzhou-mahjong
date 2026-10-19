"""Offline guards for game-rendered controls; these tests never call ADB."""

import importlib.util
from pathlib import Path

import cv2
import numpy as np
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "harvest_tea.py"
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
    monkeypatch.setattr(harvest, "adb", lambda *args, **kw: record(args, calls))
    monkeypatch.setattr(harvest.time, "sleep", lambda _: None)

    harvest.swipe_up((900, 1600))
    assert calls == [("shell", "input", "swipe", "800", "720", "800", "315", "500")]


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
    template = harvest.load_template("btn_date")
    assert template is not None
    assert template[0].shape == (84, 240)


def test_template_match_uses_native_coordinates_at_smaller_geometry(monkeypatch):
    monkeypatch.setattr(harvest, "_tpl_cache", {})
    monkeypatch.setattr(harvest, "TPL_DIR", str(SCRIPT.parent / "templates"))
    template = harvest.load_template("btn_date")[0]
    scaled = cv2.resize(template, (144, 50), interpolation=cv2.INTER_AREA)
    frame = np.zeros((540, 960), dtype=np.uint8)
    frame[100:150, 200:344] = scaled
    found, score, x, y = harvest.match_template(frame, "btn_date")
    assert found and score > .99
    assert (x, y) == (272, 125)


def test_replay_entry_click_anchor_is_inside_drawn_button(monkeypatch):
    monkeypatch.setattr(harvest, "_tpl_cache", {})
    monkeypatch.setattr(harvest, "TPL_DIR", str(SCRIPT.parent / "templates"))
    template = harvest.load_template("btn_chakan_huifang")[0]
    frame = np.zeros((900, 1600), dtype=np.uint8)
    frame[700:772, 1000:1368] = template
    found, _, x, y = harvest.match_template(frame, "btn_chakan_huifang")
    assert found
    assert (x, y) == (1294, 754)
    # Template midpoint x=1184 lands on the neighboring caption, not the button.
    assert x > 1000 + 225


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


def test_replay_click_stops_when_page_did_not_change(monkeypatch, tmp_path):
    frame = np.zeros((900, 1600), dtype=np.uint8)
    taps = []
    matches = iter([[(400, 200, .99)], [], []])
    monkeypatch.setattr(harvest, "DRY_RUN", False)
    monkeypatch.setattr(harvest, "grab_gray", lambda: frame)
    monkeypatch.setattr(harvest, "handle_popup", lambda _: False)
    monkeypatch.setattr(harvest, "match_all", lambda *_: next(matches))
    monkeypatch.setattr(harvest, "confirm_match", lambda *_a, **_kw: (400, 200, frame))
    monkeypatch.setattr(harvest, "tap", lambda *a: taps.append(a))
    monkeypatch.setattr(harvest, "save_screenshot", lambda _: pytest.fail("saved unverified replay"))
    monkeypatch.setattr(harvest.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="几乎未变化"):
        harvest.harvest_rows(str(tmp_path), max_pages=1)
    assert taps == [(400, 200, frame.shape)]
