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

from assist_tea import analyze_manual_hand, analyze_snapshot, capture_and_analyze, import_and_analyze  # noqa: E402
from assist_window import TeaAssistWindow, TILE_CHOICES, append_feedback, tea_round_fingerprint  # noqa: E402


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


def test_imported_frame_and_manual_correction_keep_separate_records(tmp_path):
    before = hashlib.sha256(FRAME.read_bytes()).hexdigest()
    imported = import_and_analyze(
        FRAME, session_dir=tmp_path / "session", caishen_label="s7",
        confirmed_two_player=True,
    )
    assert imported["status"] == "advise"
    assert imported["source"] == "imported_image"
    assert imported["image_sha256"] == before
    assert Path(imported["image"]).read_bytes() == FRAME.read_bytes()
    assert json.loads(Path(imported["record"]).read_text(encoding="utf-8")) == imported

    corrected = analyze_manual_hand(
        " ".join(imported["hand"]), caishen_label="s7",
        confirmed_two_player=True, session_dir=tmp_path / "session",
        image_path=imported["image"], corrected_from=imported["record"],
    )
    assert corrected["status"] == "advise"
    assert corrected["source"] == "manual_correction"
    assert corrected["suggested_boxes"] == []
    assert corrected["corrected_from"] == imported["record"]
    assert json.loads(Path(corrected["record"]).read_text(encoding="utf-8")) == corrected
    assert hashlib.sha256(FRAME.read_bytes()).hexdigest() == before


def test_manual_correction_rejects_unconfirmed_or_impossible_hand(tmp_path):
    labels = "w2 w4 w5 w7 s1 s1 s3 s6 s6 s7 s7 t1 t2 t8 t8 t8 t5"
    with pytest.raises(ValueError, match="2 人局"):
        analyze_manual_hand(labels, caishen_label="s7", confirmed_two_player=False)
    with pytest.raises(ValueError, match="16 或 17"):
        analyze_manual_hand("w1 w2", caishen_label="s7", confirmed_two_player=True)
    with pytest.raises(ValueError, match="不支持"):
        analyze_manual_hand(labels.replace("w2", "hongzhong"),
                            caishen_label="s7", confirmed_two_player=True)
    with pytest.raises(ValueError):
        analyze_manual_hand(" ".join(["w1"] * 5 + labels.split()[5:]),
                            caishen_label="s7", confirmed_two_player=True,
                            session_dir=tmp_path)
    assert not list(tmp_path.glob("manual_*.json"))


class _Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def test_continuous_tea_stops_before_showing_next_round_with_old_caishen():
    r01 = BOT_DIR.parent / "采集" / "2026-10-19" / "833667" / "r01"
    r02 = BOT_DIR.parent / "采集" / "2026-10-19" / "833667" / "r02"
    assert tea_round_fingerprint(r01 / "frame_15s.png") == tea_round_fingerprint(r01 / "frame_20s.png")
    assert tea_round_fingerprint(r01 / "frame_20s.png") != tea_round_fingerprint(r02 / "frame_04s_complete.png")

    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window.mode = _Value("温州茶 · 2人")
    window.continuous = True
    window.continuous_round = None
    window.timer = None
    window.status = _Value("")
    window.hand = _Value("旧手牌")
    window.current = {"status": "advise"}
    window.preview = object()
    shown = []
    window.picture = SimpleNamespace(configure=lambda **kwargs: shown.append(kwargs))

    assert window._accept_continuous_result({"status": "advise", "image": str(r01 / "frame_20s.png")})
    assert window._accept_continuous_result({"status": "advise", "image": str(r01 / "frame_15s.png")})
    assert not window._accept_continuous_result({"status": "advise", "image": str(r02 / "frame_04s_complete.png")})
    assert window.continuous is False and window.current is None
    assert "重新核对财神牌" in window.status.get()
    assert window.hand.get() == "手牌：—" and shown[-1] == {"image": ""}


def test_tea_preflight_requires_current_caishen_before_capture():
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window.mode = _Value("温州茶 · 2人")
    window.confirm_mode = _Value(True)
    window.caishen = _Value("")
    window.status = _Value("")
    assert not window._ready()
    assert "财神牌" in window.status.get()
    window.caishen.set(next(iter(TILE_CHOICES)))
    assert window._ready()
