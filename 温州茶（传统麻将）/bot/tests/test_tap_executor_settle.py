"""Regression tests for the settled-count gate in tap_executor.execute.

Field failures (2026-10-25, three live stops): the first tap really did
discard, but the post-tap frame was captured mid-animation while the hand
was still re-sorting, so count_tiles read before-2 and the old
single-frame gate aborted as "状态异常，禁止第二击". These tests replay
that frame choreography offline: counts start ambiguous, then settle.
No real device is touched — adb is always faked.
"""

import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from assist_tea import analyze_snapshot  # noqa: E402
from tap_executor import TapExecutor, TapUnverified  # noqa: E402

FRAME = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261023_201640"
         / "live" / "tea_2p" / "20261023_205409_557426_31fea1.png")


@pytest.fixture(scope="module")
def analysis(tmp_path_factory) -> dict:
    logs = tmp_path_factory.mktemp("tap_settle_logs")
    return analyze_snapshot(
        FRAME, caishen_label="", confirmed_two_player=True, log_dir=logs,
    )


def frame_png() -> bytes:
    return FRAME.read_bytes()


def _erase_region(png_bytes: bytes, x0, y0, x1, y1) -> bytes:
    image = cv2.imdecode(
        np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR).copy()
    image[y0:y1, x0:x1] = (20, 60, 30)  # dark table, outside the tile mask
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def erase_tile(png_bytes: bytes, box) -> bytes:
    x0, y0, x1, y1 = (int(value) for value in box)
    return _erase_region(png_bytes, x0, y0, x1, y1)


def erase_two_tiles(png_bytes: bytes, box) -> bytes:
    """Erase the target tile and its right neighbour.

    On the fixture frame the hand boxes are contiguous (pitch ~= width+3),
    so extending the erase region by one tile width + slack removes exactly
    two boxes — the mid-animation undercount seen live (before -> before-2).
    """
    x0, y0, x1, y1 = (int(value) for value in box)
    return _erase_region(png_bytes, x0, y0, x1 + (x1 - x0) + 8, y1)


RAISE_LIFT_PX = 25


def raise_tile(png_bytes: bytes, box) -> bytes:
    image = cv2.imdecode(
        np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR).copy()
    x0, y0, x1, y1 = (int(value) for value in box)
    image[y0 - RAISE_LIFT_PX:y1 - RAISE_LIFT_PX, x0:x1] = image[y0:y1, x0:x1]
    image[y1 - RAISE_LIFT_PX:y1, x0:x1] = (20, 60, 30)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def confirmed_after(png_bytes: bytes, box) -> bytes:
    """Erased target + discard animation bar above its column."""
    erased = erase_tile(png_bytes, box)
    image = cv2.imdecode(
        np.frombuffer(erased, dtype=np.uint8), cv2.IMREAD_COLOR).copy()
    x0, y0, x1, _y1 = (int(value) for value in box)
    image[max(0, y0 - 70):max(0, y0 - 35), x0:x1] = (0, 0, 220)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


class FakeAdb:
    """Record every command; answer dumpsys / screencap / tap offline.

    Screencap pops one frame per call and asserts when exhausted, so a
    test fails loudly if execute() screenshots more than choreographed.
    """

    def __init__(self, png_frames: list[bytes]):
        self.frames = list(png_frames)
        self.commands: list[list[str]] = []

    def __call__(self, command, capture_output=False, timeout=None, check=False,
                 creationflags=0, **kwargs):
        parts = [str(part) for part in command]
        if not parts or parts[0] != "fake-adb":
            return subprocess.CompletedProcess(command, 0, b"", b"")
        self.commands.append(parts)
        joined = " ".join(parts)
        if "dumpsys" in joined:
            stdout = (b"topResumedActivity=ActivityRecord{1 u0 "
                      b"com.xm.zjgamecenter/com.xm.main.MainActivity}\n")
            return subprocess.CompletedProcess(command, 0, stdout, b"")
        if "screencap" in joined:
            assert self.frames, "unexpected extra screencap"
            return subprocess.CompletedProcess(
                command, 0, self.frames.pop(0), b"")
        if "input" in joined and "tap" in joined:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        raise AssertionError(f"unexpected adb command: {joined}")

    def tap_commands(self) -> list[list[str]]:
        return [cmd for cmd in self.commands
                if "input" in cmd and "tap" in cmd]

    def screencaps(self) -> int:
        return len([c for c in self.commands if "screencap" in c])


class AlternatingAdb(FakeAdb):
    """FakeAdb whose screencap cycles the tail frames forever."""

    def __init__(self, first_frames: list[bytes], cycle_frames: list[bytes]):
        super().__init__(first_frames)
        self.cycle = list(cycle_frames)
        self.cycle_idx = 0

    def __call__(self, command, capture_output=False, timeout=None, check=False,
                 creationflags=0, **kwargs):
        parts = [str(part) for part in command]
        if parts and parts[0] == "fake-adb" and "screencap" in " ".join(parts) \
                and not self.frames:
            self.commands.append(parts)
            frame = self.cycle[self.cycle_idx % len(self.cycle)]
            self.cycle_idx += 1
            return subprocess.CompletedProcess(command, 0, frame, b"")
        return super().__call__(command, capture_output=capture_output,
                                timeout=timeout, check=check,
                                creationflags=creationflags, **kwargs)


def _executor(monkeypatch, fake, session_dir, **kwargs) -> TapExecutor:
    monkeypatch.setattr(subprocess, "run", fake)
    return TapExecutor(enabled=True, adb_path="fake-adb",
                       serial="emulator-test", select_delay=0,
                       verify_delay=0, session_dir=session_dir, **kwargs)


def _read_evidence(session_dir: Path) -> dict:
    probe = sorted((session_dir / "tap_probe").iterdir())[-1]
    return json.loads((probe / "evidence.json").read_text(encoding="utf-8"))


def test_animation_count_settles_to_minus_one_and_discards(
        analysis, monkeypatch, tmp_path):
    """Live replay: first post-tap read is -2 (animation), settles to -1."""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    erased_one = erase_tile(png, box)
    fake = FakeAdb([png, erase_two_tiles(png, box), erased_one, erased_one])
    executor = _executor(monkeypatch, fake, tmp_path,
                         settle_interval=0.01, settle_deadline=5.0)
    outcome = executor.execute(analysis)

    assert outcome["executed"] is True
    assert outcome["stage"] == "discarded_first_click"
    assert outcome["after_count"] == outcome["before_count"] - 1
    assert len(fake.tap_commands()) == 1, "第一击已出牌，绝不允许发第二击"
    assert fake.screencaps() == 4
    before = outcome["before_count"]
    gate = _read_evidence(tmp_path)["selected_gate"]
    assert gate["count_samples"] == [before - 2, before - 1, before - 1]
    assert gate["discarded"] is True


def test_settled_abnormal_count_still_aborts_without_second_tap(
        analysis, monkeypatch, tmp_path):
    """Settles at -2 (a real anomaly, not animation) -> fail-closed abort."""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    erased_two = erase_two_tiles(png, box)
    fake = FakeAdb([png, erased_two, erased_two])
    executor = _executor(monkeypatch, fake, tmp_path,
                         settle_interval=0.01, settle_deadline=5.0)
    with pytest.raises(TapUnverified, match="既不等于点击前"):
        executor.execute(analysis)
    assert len(fake.tap_commands()) == 1
    gate = _read_evidence(tmp_path)["selected_gate"]
    assert gate["count_samples"] == [gate["count"], gate["count"]]
    assert gate["discarded"] is False and gate["ready"] is False


def test_unsettled_count_aborts_without_second_tap(
        analysis, monkeypatch, tmp_path):
    """Count oscillates -2/-1 until the deadline -> fail-closed, no 2nd tap."""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    erased_one = erase_tile(png, box)
    erased_two = erase_two_tiles(png, box)
    fake = AlternatingAdb([png], [erased_two, erased_one])
    executor = _executor(monkeypatch, fake, tmp_path,
                         settle_interval=0.02, settle_deadline=0.6)
    with pytest.raises(TapUnverified, match="未落定"):
        executor.execute(analysis)
    assert len(fake.tap_commands()) == 1, "未落定绝不允许发第二击"


def test_settle_recovers_to_raised_then_second_tap_verifies(
        analysis, monkeypatch, tmp_path):
    """Ambiguous first read settles back to before (raised) -> 2nd tap path."""
    png = frame_png()
    box = [int(v) for v in analysis["suggested_boxes"][0]]
    raised = raise_tile(png, box)
    fake = FakeAdb([png, erase_two_tiles(png, box), raised, raised,
                    confirmed_after(png, box)])
    executor = _executor(monkeypatch, fake, tmp_path,
                         settle_interval=0.01, settle_deadline=5.0)
    outcome = executor.execute(analysis)

    assert outcome["executed"] is True and outcome["stage"] == "verified"
    assert outcome["before_count"] - outcome["after_count"] == 1
    assert len(fake.tap_commands()) == 2
    before = outcome["before_count"]
    gate = _read_evidence(tmp_path)["selected_gate"]
    assert gate["count_samples"] == [before - 2, before, before]
    assert gate["ready"] is True and gate["lifted"] is True