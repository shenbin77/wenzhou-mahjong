"""The adviser keeps every capture local and never sends game input."""

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from assist_tea import analyze_snapshot, capture_and_analyze  # noqa: E402
from assist_window import append_feedback  # noqa: E402


FRAME = BOT_DIR.parent / "采集" / "2026-10-16" / "740195" / "r08" / "frame_02.png"


def test_real_two_player_frame_produces_visible_advice_without_changing_source(tmp_path):
    before = hashlib.sha256(FRAME.read_bytes()).hexdigest()
    marked = tmp_path / "advice.png"
    result = analyze_snapshot(
        FRAME, caishen_label="s7", confirmed_two_player=True,
        output_path=marked, log_dir=tmp_path / "logs",
    )
    assert result["status"] == "advise"
    assert result["dry_run"] is True
    assert len(result["hand"]) == 17
    assert result["suggested_boxes"] == [[1491, 768, 1581, 892]]
    image = cv2.imdecode(np.fromfile(str(marked), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None and tuple(image[768, 1491]) == (0, 0, 255)
    assert hashlib.sha256(FRAME.read_bytes()).hexdigest() == before


def test_unconfirmed_mode_and_incomplete_frame_cannot_offer_advice(tmp_path):
    with pytest.raises(ValueError, match="2 人局"):
        analyze_snapshot(
            FRAME, caishen_label="s7", confirmed_two_player=False,
            log_dir=tmp_path / "logs",
        )
    blank = tmp_path / "blank.png"
    ok, encoded = cv2.imencode(".png", np.zeros((900, 1600, 3), np.uint8))
    assert ok
    blank.write_bytes(encoded.tobytes())
    result = analyze_snapshot(
        blank, caishen_label="s7", confirmed_two_player=True,
        output_path=tmp_path / "blank.marked.png", log_dir=tmp_path / "logs",
    )
    assert result["status"] == "abstain"
    assert (tmp_path / "blank.marked.png").is_file()


def test_live_capture_checks_foreground_and_writes_append_only_record(tmp_path, monkeypatch):
    import assist_tea

    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"fake")
    commands = []
    image_bytes = FRAME.read_bytes()

    def fake_run(command, **kwargs):
        commands.append(command)
        if "dumpsys" in command:
            return SimpleNamespace(stdout=b"topResumedActivity=com.xm.zjgamecenter/org.cocos2dx.lua.AppActivity")
        if "screencap" in command:
            return SimpleNamespace(stdout=image_bytes)
        raise AssertionError(command)

    monkeypatch.setattr(assist_tea.subprocess, "run", fake_run)
    result = capture_and_analyze(
        adb_path=str(adb), serial="emulator-5556", session_dir=tmp_path / "session",
        caishen_label="s7", confirmed_two_player=True,
    )
    assert result["status"] == "advise"
    assert result["image_sha256"] == hashlib.sha256(image_bytes).hexdigest()
    assert Path(result["image"]).read_bytes() == image_bytes
    assert json.loads(Path(result["record"]).read_text(encoding="utf-8")) == result
    assert all("tap" not in command and "swipe" not in command for command in commands)


def test_live_capture_rejects_other_foreground_before_screenshot(tmp_path, monkeypatch):
    import assist_tea

    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"fake")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout=b"topResumedActivity=com.example.other/.Main")

    monkeypatch.setattr(assist_tea.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="游戏不在模拟器前台"):
        capture_and_analyze(
            adb_path=str(adb), serial="emulator-5556", session_dir=tmp_path / "session",
        )
    assert len(commands) == 1
    assert not (tmp_path / "session").exists()


def test_feedback_is_separate_append_only_event(tmp_path):
    record = tmp_path / "one.json"
    record.write_text('{"status":"advise"}', encoding="utf-8")
    original = record.read_bytes()
    path = append_feedback(record, "wrong_advice", "t5")
    event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert event["record"] == "one.json"
    assert event["verdict"] == "wrong_advice"
    assert event["actual_tile"] == "t5"
    assert record.read_bytes() == original
