"""Gold checks from the archived, four-player red-Zhong replay."""

import hashlib
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from assist_shu import (analyze_shu_manual_hand, analyze_shu_snapshot,
                        import_shu_and_analyze)  # noqa: E402
from mode_vision import detect_shu_rule_text  # noqa: E402
from tile_vision import TILE_IDS, TileVision  # noqa: E402

FRAMES = BOT_DIR.parents[1] / "阿法算牌分析" / "熟识牌对照_20261018"
SHU_GOLD = BOT_DIR.parent / "采集" / "box_replay_harvest_missing"


def test_shu_rule_text_accepts_red_zhong_replay_and_rejects_tea(tmp_path):
    for name in ("frame_233s_14.png", "frame_261s_14.png"):
        image = cv2.imdecode(np.fromfile(str(FRAMES / name), np.uint8), cv2.IMREAD_COLOR)
        assert detect_shu_rule_text(image)

    hidden_zhong = cv2.imdecode(np.fromfile(str(FRAMES / "frame_233s_14.png"),
                                           np.uint8), cv2.IMREAD_COLOR)
    hidden_zhong[195:250, 810:990] = 130
    assert not detect_shu_rule_text(hidden_zhong)
    hidden_path = tmp_path / "hidden_red_zhong_rule.png"
    ok, encoded = cv2.imencode(".png", hidden_zhong)
    assert ok
    hidden_path.write_bytes(encoded.tobytes())
    hidden_result = analyze_shu_snapshot(hidden_path, confirmed_four_player=True)
    assert hidden_result["status"] == "abstain" and hidden_result["hand"] == []

    tea = BOT_DIR.parent / "采集" / "2026-10-13" / "323028" / "r01" / "frame_01.png"
    image = cv2.imdecode(np.fromfile(str(tea), np.uint8), cv2.IMREAD_COLOR)
    assert not detect_shu_rule_text(image)
    marked = tmp_path / "wrong_mode.png"
    result = analyze_shu_snapshot(tea, confirmed_four_player=True, output_path=marked)
    assert result["status"] == "abstain" and "未确认温州熟" in result["reason"]
    assert result["hand"] == [] and marked.is_file()


@pytest.mark.parametrize(
    ("filename", "gold", "suggestion"),
    [
        ("frame_233s_14.png",
         "w1 w1 w6 w6 w8 s1 s6 t3 t3 t4 t5 t6 t9 t9".split(), "s1"),
        ("frame_261s_14.png",
         "w2 w5 w6 w7 w9 s1 s3 s5 t5 t5 t6 t8 t9 t1".split(), "w9"),
    ],
)
def test_real_four_player_draw_frame_has_exact_hand_and_located_advice(
    filename, gold, suggestion, tmp_path,
):
    frame = FRAMES / filename
    before = hashlib.sha256(frame.read_bytes()).hexdigest()
    marked = tmp_path / "marked.png"
    result = analyze_shu_snapshot(frame, confirmed_four_player=True,
                                  output_path=marked)
    assert result["hand"] == gold
    assert result["status"] == "advise"
    assert result["suggestion"] == suggestion
    assert result["suggested_boxes"]
    assert marked.is_file() and result["dry_run"] is True
    assert hashlib.sha256(frame.read_bytes()).hexdigest() == before


def test_shu_vision_is_told_hongzhong_is_the_caishen(monkeypatch):
    """熟局财神按规则固定是红中；识别器必须拿到它，否则角标/金牌整帧弃权。"""
    import assist_shu

    assert assist_shu._SHU_CAISHEN_LABEL in TILE_IDS
    original = TileVision.recognize_hand
    observed: list[object] = []

    def spy(self, image, *args, **kwargs):
        observed.append(self.caishen_label)
        return original(self, image, *args, **kwargs)

    monkeypatch.setattr(TileVision, "recognize_hand", spy)
    result = analyze_shu_snapshot(FRAMES / "frame_233s_14.png",
                                  confirmed_four_player=True)
    assert observed == ["hongzhong"]
    assert result["status"] == "advise"


def test_uncertain_and_incomplete_replay_frames_abstain(tmp_path):
    for filename in ("frame_180s.png", "frame_120s.png"):
        result = analyze_shu_snapshot(
            FRAMES / filename, confirmed_four_player=True,
            output_path=tmp_path / f"{filename}.marked.png",
        )
        assert result["status"] == "abstain"
        assert result["suggested_boxes"] == []
    with pytest.raises(ValueError, match="4 人"):
        analyze_shu_snapshot(FRAMES / "frame_233s_14.png",
                             confirmed_four_player=False)


def test_import_and_manual_correction_save_separate_local_records(tmp_path):
    frame = FRAMES / "frame_233s_14.png"
    result = import_shu_and_analyze(
        frame, session_dir=tmp_path, confirmed_four_player=True,
    )
    assert result["status"] == "advise"
    assert Path(result["image"]).read_bytes() == frame.read_bytes()
    assert json.loads(Path(result["record"]).read_text(encoding="utf-8")) == result
    corrected = analyze_shu_manual_hand(
        " ".join(result["hand"]), confirmed_four_player=True,
        session_dir=tmp_path, image_path=result["image"],
        corrected_from=result["record"],
    )
    assert corrected["status"] == "advise"
    assert corrected["suggested_boxes"] == []
    assert corrected["corrected_from"] == result["record"]
    assert json.loads(Path(corrected["record"]).read_text(encoding="utf-8")) == corrected


def test_manual_correction_requires_verified_shu_screenshot(tmp_path):
    labels = "w1 w1 w6 w6 w8 s1 s6 t3 t3 t4 t5 t6 t9 t9"
    with pytest.raises(ValueError, match="必须关联"):
        analyze_shu_manual_hand(
            labels, confirmed_four_player=True, session_dir=tmp_path,
        )
    tea = BOT_DIR.parent / "采集" / "2026-10-13" / "323028" / "r01" / "frame_01.png"
    with pytest.raises(ValueError, match="未确认温州熟"):
        analyze_shu_manual_hand(
            labels, confirmed_four_player=True, session_dir=tmp_path,
            image_path=tea,
        )
    assert not list(tmp_path.glob("shu_manual_*.json"))


def test_native_900px_shu_gold_hands_are_exact_and_incomplete_hand_abstains():
    checked_frames = checked_tiles = 0
    for manifest in sorted(SHU_GOLD.rglob("gold_hands.json")):
        document = json.loads(manifest.read_text(encoding="utf-8"))
        for frame in document["frames"]:
            source = manifest.parent / Path(frame["name"]).name
            expected = next(seat["tiles"] for seat in frame["seats"]
                            if seat["seat"] == "bottom")
            result = analyze_shu_snapshot(source, confirmed_four_player=True)
            from pc_bot.tile_vision import TILE_IDS
            observed = [TILE_IDS[label] for label in result["hand"]]
            assert observed == expected, f"{document['round_uid']}/{source.name}"
            assert result["mode"] == "shu_4p"
            assert result["status"] == ("wait" if len(expected) == 13 else "abstain")
            checked_frames += 1
            checked_tiles += len(expected)
    assert (checked_frames, checked_tiles) == (8, 103)
