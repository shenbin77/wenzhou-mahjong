"""Replay ETL invariants: text must not be mistaken for tile evidence."""
from scripts.replay_to_gamelog import (
    FrameObservation, RoundTracker, load_manifest_segments, map_tile_list,
    normalize_tile_name,
    summarize_manifest,
)
import scripts.replay_to_gamelog as replay_module
from scripts.replay_to_db import source_fingerprint


def test_rule_banner_is_not_red_joker():
    assert normalize_tile_name("放炮麻将：点炮胡/庄闲算分/红中赖子/可胡七对/4人") == (None, None)


def test_tile_names_and_out_of_deck_are_explicit():
    assert normalize_tile_name("红中") == (55, None)
    assert normalize_tile_name("九条") == (49, None)
    assert normalize_tile_name("一索") == (41, None)
    assert normalize_tile_name("1s") == (41, None)
    assert normalize_tile_name(41) == (41, None)
    assert normalize_tile_name("发财") == (None, "发")
    assert normalize_tile_name("北") == (None, "北")


def test_common_ocr_tile_aliases_are_canonicalized():
    assert normalize_tile_name("一萬") == (1, None)
    assert normalize_tile_name("幺鸡") == (41, None)
    assert normalize_tile_name("么雞") == (41, None)


def test_mapping_keeps_unparsed_names_for_review():
    ids, bad = map_tile_list(["一万", "红中赖子", "北"])
    assert ids == [1]
    assert bad == ["红中赖子", "北"]


def test_repeated_tile_violation_is_collapsed_to_max_count():
    tracker = RoundTracker(1, "a", "sample.mp4", None)
    tracker.cum["up"][8] = 5
    tracker._check_tile_counts()
    tracker.cum["up"][8] = 8
    tracker._check_tile_counts()
    assert tracker.violations == [{
        "type": "tile_over_four",
        "tile": 8,
        "tile_name": "8万",
        "count": 8,
    }]


def test_tile_count_check_includes_visible_hand():
    tracker = RoundTracker(1, "a", "sample.mp4", None)
    tracker._check_tile_counts(extra=[8, 8, 8, 8, 8])
    assert tracker.violations[0]["tile"] == 8
    assert tracker.violations[0]["count"] == 5


def test_frame_selection_prefers_table_scene_and_keeps_time_bins(monkeypatch):
    scores = [0.01, 0.02, 0.80, 0.03, 0.75, 0.02, 0.70, 0.01, 0.65, 0.01]
    monkeypatch.setattr(replay_module, "_green_table_score",
                        lambda path: scores[int(path)])
    paths = [str(i) for i in range(len(scores))]
    picked = replay_module._pick_frame_indices(len(paths), 4, paths)
    assert picked == [2, 6, 8]


def test_frame_selection_falls_back_when_no_table_scene(monkeypatch):
    monkeypatch.setattr(replay_module, "_green_table_score", lambda _path: 0.01)
    paths = [str(i) for i in range(10)]
    assert replay_module._pick_frame_indices(10, 4, paths) == [0, 3, 6, 9]


def test_frame_selection_rejects_non_positive_budget():
    import pytest
    with pytest.raises(ValueError, match="max_frames"):
        replay_module._uniform_frame_indices(10, 0)
    assert replay_module._uniform_frame_indices(10, 1) == [0]
    assert replay_module._uniform_frame_indices(0, 1) == []


def test_directory_source_fingerprint_is_stable_and_changes_on_clip_update(tmp_path):
    root = tmp_path / "clips"
    root.mkdir()
    (root / "manifest.json").write_text('{"clips":[]}', encoding="utf-8")
    (root / "round01.mp4").write_bytes(b"clip-a")
    first = source_fingerprint(root)
    assert len(first) == 64 and first == source_fingerprint(root)
    (root / "round02.mp4").write_bytes(b"clip-b")
    assert source_fingerprint(root) != first


def test_legacy_round_manifest_is_normalized(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(
        '{"rounds":[{"round_no":7,"seat":"b","outcome":"自摸",'
        '"start":12,"end":19,"file":"r07.mp4"}]}',
        encoding="utf-8",
    )
    assert load_manifest_segments(str(path)) == [{
        "round": 7, "take": "b", "result": "自摸",
        "start_seconds": 12.0, "end_seconds": 19.0,
        "duration_seconds": 7.0, "file": "r07.mp4",
    }]
    assert summarize_manifest({"rounds": [{"round_no": 7}, {"round_no": 9}]}) == {
        "clip_count": 2, "unique_rounds": 2,
        "missing_rounds": [8], "duplicate_rounds": [],
    }


def test_legacy_csv_manifest_is_normalized(tmp_path):
    path = tmp_path / "manifest.csv"
    path.write_text(
        "round_no,seat,outcome,start,end,file\n"
        "2,a,点炮,3,8,r02.mp4\n", encoding="utf-8"
    )
    assert load_manifest_segments(str(path))[0]["round"] == 2
    assert load_manifest_segments(str(path))[0]["result"] == "点炮"
    assert load_manifest_segments(str(path))[0]["duration_seconds"] == 5.0


def test_tracker_preserves_duplicate_river_tiles_for_downstream_counts():
    tracker = RoundTracker(1, "a", "sample.mp4", None)
    hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 55]
    tracker.feed(FrameObservation(1.0, {
        "hand": hand, "river_up": ["一万", "一万"],
        "river_opposite": [], "river_down": [],
    }, None), None)
    point = tracker.finish("r1", None, "none")[0]
    assert point["rivers"]["up"] == [1, 1]


def test_frame_observation_normalizes_last_discard():
    obs = FrameObservation(1.0, {"hand": [], "last_discard": "五筒"}, None)
    assert obs.last_discard == 25


def test_vision_ocr_disables_hidden_reasoning_by_default(monkeypatch, tmp_path):
    import cv2, numpy as np
    frame = tmp_path / "frame.jpg"
    cv2.imwrite(str(frame), np.full((8, 8, 3), 255, dtype=np.uint8))
    captured = {}

    class Response:
        def json(self):
            return {"choices": [{"message": {"content": "{\"hand\": []}"}}]}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return Response()

    import requests
    monkeypatch.delenv("VISION_ENABLE_THINKING", raising=False)
    monkeypatch.setattr(requests, "post", fake_post)
    recognizer = replay_module.VisionRecognizer(api_key="test", retries=0)
    assert recognizer.recognize_frame(str(frame)) == {"hand": []}
    assert captured["json"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured["json"]["max_tokens"] == 1536
    assert recognizer.stats["enable_thinking"] is False
