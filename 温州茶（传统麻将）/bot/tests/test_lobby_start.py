"""Guarded Start Game workflow: explicit authorisation, proof, one click."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import sys

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lobby_start
from lobby_start import execute_auto_start, plan_auto_start
from player_identity import PlayerIdentity
from session_watch import WatchState


BUTTON_BOX = [663, 525, 937, 625]


def _identity(status="match"):
    return PlayerIdentity(
        status, "玉姐", "玉姐" if status == "match" else "拾则",
        0.99, 0.4, "identity-detail")


def _ready_patches(monkeypatch):
    monkeypatch.setattr(
        lobby_start, "detect_waiting_room",
        lambda _image: {
            "matched": True, "status": "matched",
            "start_button_box": BUTTON_BOX,
        })
    monkeypatch.setattr(
        lobby_start, "detect_player_identity",
        lambda _image, _expected: _identity())
    monkeypatch.setattr(
        lobby_start, "opponent_seat_present",
        lambda _image: (True, {"gold_text_pixels": 300}))
    monkeypatch.setattr(lobby_start, "_button_score", lambda *_args: 0.999)


def test_plan_requires_identity_opponent_button_and_nonzero_cards(monkeypatch):
    _ready_patches(monkeypatch)
    image = np.zeros((900, 1600, 3), np.uint8)
    ready = plan_auto_start(
        image, expected_account="玉姐", room_cards=None)
    assert ready.status == "ready"
    assert ready.point == (800, 575)
    assert "房卡读数未知" in ready.detail

    zero = plan_auto_start(image, expected_account="玉姐", room_cards=0)
    assert zero.status == "blocked"
    assert "不会自动充值" in zero.detail

    monkeypatch.setattr(
        lobby_start, "detect_player_identity",
        lambda _image, _expected: _identity("mismatch"))
    mismatch = plan_auto_start(
        image, expected_account="玉姐", room_cards=9)
    assert mismatch.status == "blocked"


def test_plan_waits_until_opponent_is_present(monkeypatch):
    _ready_patches(monkeypatch)
    monkeypatch.setattr(
        lobby_start, "opponent_seat_present",
        lambda _image: (False, {"gold_text_pixels": 0}))
    result = plan_auto_start(
        np.zeros((900, 1600, 3), np.uint8),
        expected_account="玉姐", room_cards=9)
    assert result.status == "wait"
    assert "等待另一名玩家" in result.detail


def test_execute_clicks_once_after_fresh_proof_and_reaches_game(
        monkeypatch, tmp_path):
    _ready_patches(monkeypatch)
    image = np.zeros((900, 1600, 3), np.uint8)
    frames = iter([image, image])
    taps = []
    monkeypatch.setattr(
        lobby_start, "classify_watch",
        lambda _image: SimpleNamespace(state=WatchState.IN_GAME))
    monkeypatch.setattr(
        lobby_start, "game_started_evidence",
        lambda _image: (True, {"hand_count": 16, "start_button": False}))
    result = execute_auto_start(
        image,
        expected_account="玉姐", room_cards=9,
        capture_image=lambda: next(frames),
        tap_point=lambda x, y: taps.append((x, y)),
        sleep_fn=lambda _seconds: None,
        lock_path=tmp_path / "start.lock",
    )
    assert result.status == "started"
    assert result.clicked is True
    assert taps == [(800, 575)]


def test_execute_non_host_ready_animation_waits_for_real_deal(
        monkeypatch, tmp_path):
    _ready_patches(monkeypatch)
    image = np.zeros((900, 1600, 3), np.uint8)
    taps = []
    monkeypatch.setattr(
        lobby_start, "classify_watch",
        lambda _image: SimpleNamespace(state=WatchState.IN_GAME))
    monkeypatch.setattr(
        lobby_start, "game_started_evidence",
        lambda _image: (False, {
            "hand_count": 0, "start_button": False, "reason": "not_dealt"}))
    result = execute_auto_start(
        image,
        expected_account="玉姐", room_cards=9,
        capture_image=lambda: image,
        tap_point=lambda x, y: taps.append((x, y)),
        sleep_fn=lambda _seconds: None,
        lock_path=tmp_path / "start.lock",
    )
    assert result.status == "ready_wait"
    assert result.final_state == "waiting_room"
    assert result.clicked is True
    assert len(taps) == 1
    assert "等待房主" in result.detail


def test_game_started_uses_physical_boxes_when_strict_labels_abstain(
        monkeypatch):
    """A dealt dark/uncertain hand must leave the waiting-room loop.

    Tile identities are intentionally *not* trusted here.  The ordinary live
    pipeline re-runs strict recognition before it may advise or click.
    """
    image = np.zeros((900, 1600, 3), np.uint8)
    monkeypatch.setattr(
        lobby_start, "classify_watch",
        lambda _image: SimpleNamespace(state=WatchState.IN_GAME))
    monkeypatch.setattr(
        lobby_start, "waiting_room_visual_evidence",
        lambda _image: {"matched": False})

    class PhysicalHand:
        last_meld_boxes = []

        def count_tiles(self, _image):
            return 17, [(index, 0, index + 1, 1) for index in range(17)]

    monkeypatch.setattr(lobby_start, "TileVision", PhysicalHand)
    matched, evidence = lobby_start.game_started_evidence(image)
    assert matched is True
    assert evidence["hand_count"] == 17
    assert evidence["concealed_count"] == 17
    assert evidence["exposed_box_count"] == 0


def test_game_started_accepts_melded_physical_total(monkeypatch):
    image = np.zeros((900, 1600, 3), np.uint8)
    monkeypatch.setattr(
        lobby_start, "classify_watch",
        lambda _image: SimpleNamespace(state=WatchState.IN_GAME))
    monkeypatch.setattr(
        lobby_start, "waiting_room_visual_evidence",
        lambda _image: {"matched": False})

    class MeldedHand:
        last_meld_boxes = [(0, 0, 1, 1)] * 3

        def count_tiles(self, _image):
            return 13, [(index, 0, index + 1, 1) for index in range(13)]

    monkeypatch.setattr(lobby_start, "TileVision", MeldedHand)
    matched, evidence = lobby_start.game_started_evidence(image)
    assert matched is True
    assert evidence["hand_count"] == 16
    assert evidence["concealed_count"] == 13
    assert evidence["exposed_box_count"] == 3


def test_execute_post_click_uncertainty_is_terminal_and_never_retries(
        monkeypatch, tmp_path):
    _ready_patches(monkeypatch)
    image = np.zeros((900, 1600, 3), np.uint8)
    taps = []
    monkeypatch.setattr(
        lobby_start, "classify_watch",
        lambda _image: SimpleNamespace(state=WatchState.UNKNOWN))
    result = execute_auto_start(
        image,
        expected_account="玉姐", room_cards=9,
        capture_image=lambda: image,
        tap_point=lambda x, y: taps.append((x, y)),
        sleep_fn=lambda _seconds: None,
        lock_path=tmp_path / "start.lock",
    )
    assert result.status == "failed"
    assert result.clicked is True
    assert len(taps) == 1
    assert "不再点击" in result.detail


def test_execute_does_not_click_when_other_instance_holds_start_lock(
        monkeypatch):
    _ready_patches(monkeypatch)
    image = np.zeros((900, 1600, 3), np.uint8)

    @contextmanager
    def busy(_path):
        yield False

    monkeypatch.setattr(lobby_start, "_exclusive_start_lock", busy)
    taps = []
    result = execute_auto_start(
        image,
        expected_account="玉姐", room_cards=9,
        capture_image=lambda: image,
        tap_point=lambda x, y: taps.append((x, y)),
        sleep_fn=lambda _seconds: None,
    )
    assert result.status == "busy"
    assert taps == []


def test_real_reviewed_waiting_button_template_scores_above_gate():
    template = lobby_start._load_gray(lobby_start.TEMPLATE_PATH)
    assert template is not None
    assert template.shape == (100, 274)
    image = np.zeros((900, 1600, 3), np.uint8)
    image[525:625, 663:937] = cv2.cvtColor(template, cv2.COLOR_GRAY2BGR)
    assert lobby_start._button_score(
        image, tuple(BUTTON_BOX)) >= lobby_start.START_MIN_SCORE


def test_member_overlay_is_closed_by_tab_only(monkeypatch):
    button = lobby_start._load_gray(lobby_start.TEMPLATE_PATH)
    overlay = lobby_start._load_gray(lobby_start.MEMBER_OVERLAY_TEMPLATE)
    assert button is not None and overlay is not None
    covered = np.zeros((900, 1600, 3), np.uint8)
    covered[525:625, 663:937] = cv2.cvtColor(button, cv2.COLOR_GRAY2BGR)
    covered[0:120, 0:485] = cv2.cvtColor(overlay, cv2.COLOR_GRAY2BGR)
    clear = covered.copy()
    clear[0:120, 0:485] = 0
    assert lobby_start.waiting_room_visual_evidence(covered)["member_overlay"]
    taps = []
    result = lobby_start.close_member_overlay(
        covered,
        capture_image=lambda: clear if taps else covered,
        tap_point=lambda x, y: taps.append((x, y)),
        sleep_fn=lambda _seconds: None,
    )
    assert result.status == "closed"
    assert taps == [lobby_start.MEMBER_OVERLAY_CLOSE_POINT]
    assert "未点击任何邀请" in result.detail
