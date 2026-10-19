"""Gold checks from the archived, four-player red-Zhong replay."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from assist_shu import (analyze_shu_manual_hand, analyze_shu_snapshot,
                        import_shu_and_analyze)  # noqa: E402

FRAMES = BOT_DIR.parents[1] / "阿法算牌分析" / "熟识牌对照_20261018"


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
