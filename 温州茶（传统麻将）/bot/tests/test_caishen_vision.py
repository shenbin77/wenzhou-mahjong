"""Held-out rooms and dates for the small visible 财 marker."""

import json
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import caishen_vision  # noqa: E402
from caishen_vision import detect_caishen  # noqa: E402

CAPTURES = BOT_DIR.parent / "采集"
TEMPLATE_DIR = BOT_DIR / "templates" / "caishen"
# Lossless ADB screenshots of a real session; kept so real play frames stay covered.
LIVE_SESSION = CAPTURES / "assist_sessions" / "20261023_201640" / "live" / "tea_2p"


def _read(relative: str) -> np.ndarray:
    raw = np.fromfile(CAPTURES / relative, dtype=np.uint8)
    return cv2.imdecode(raw, cv2.IMREAD_COLOR)


@pytest.mark.parametrize(("relative", "expected"), [
    ("2026-10-19/833667/r01/frame_15s.png", "w5"),
    ("2026-10-19/833667/r07/room833667_round7_f3facec6_2.png", "s4"),
    ("2026-10-19/833667/r09/room833667_round9_d4a6f992_2.png", "xi"),
    ("2026-10-19/833667/r10/room833667_round10_67ea9201_2.png", "w2"),
    ("2026-10-19/984226/r01/room984226_round1_3d58eb17_2.png", "t3"),
    ("2026-10-16/740195/r08/frame_02.png", "s9"),
    # 2026-10-25 纠错：两帧同帧手牌的"财"角标牌分别是 6筒 / 7筒（手牌模板独立读出），
    # 小牌也分别是"上二绿横排"/"上三绿斜排"。旧断言把 6/7 对调了。
    ("2026-10-15/291755/r05/frame_01.png", "t6"),
    ("2026-10-13/207547/r03/frame_01.png", "t7"),
])
def test_marker_from_other_room_or_date(relative, expected):
    observed = detect_caishen(_read(relative))
    assert observed.label == expected
    assert observed.score >= .98
    assert observed.score - observed.runner_up >= .02


def test_missing_badge_or_unknown_size_abstains():
    image = _read("2026-10-19/833667/r01/frame_15s.png")
    image[348:370, 622:652] = 0
    assert detect_caishen(image).label is None
    assert detect_caishen(image[:800]).label is None


def test_every_manifest_entry_survives_loading():
    """A label may own one replay crop and one live-screenshot crop."""
    manifest = json.loads((TEMPLATE_DIR / "manifest.json").read_text(encoding="utf-8"))
    _, loaded = caishen_vision._templates()
    declared = Counter(item["label"] for item in manifest["templates"])
    assert sum(len(tiles) for tiles in loaded.values()) == len(manifest["templates"])
    assert {label: len(tiles) for label, tiles in loaded.items()} == dict(declared)
    assert len(loaded) < len(manifest["templates"]), "expected at least one label with 2+ templates"


def test_thresholds_are_never_relaxed_to_pass_live_frames():
    assert caishen_vision._MIN_SCORE == .98
    assert caishen_vision._MIN_MARGIN == .02


@pytest.mark.parametrize(("frame", "expected"), [
    ("20261023_205404_403250_abb9d7.png", "t3"),
    ("20261023_205716_405083_d96078.png", "dong"),
    ("20261023_205858_483921_22f71d.png", "nan"),
    ("20261023_210043_608900_da5d98.png", "w1"),
    ("20261023_205630_775235_879693.png", "s5"),
    ("20261023_210151_647099_108bd2.png", "w9"),
    ("20261023_205345_749900_23750c.png", "s4"),
])
def test_lossless_adb_frame_clears_the_same_strict_thresholds(frame, expected):
    path = LIVE_SESSION / frame
    if not path.exists():
        pytest.skip("实战截屏样本已不在此机器上")
    observed = detect_caishen(_read(f"assist_sessions/20261023_201640/live/tea_2p/{frame}"))
    assert observed.label == expected
    assert observed.score >= .98
    assert observed.score - observed.runner_up >= .02


def test_round_settlement_frame_without_a_caishen_tile_abstains():
    """The 牌局结束 overlay shows other tiles where the 财 tile normally sits."""
    frame = "20261023_210211_157265_7be7e3.png"
    if not (LIVE_SESSION / frame).exists():
        pytest.skip("实战截屏样本已不在此机器上")
    observed = detect_caishen(_read(f"assist_sessions/20261023_201640/live/tea_2p/{frame}"))
    assert observed.label is None


# 2026-10-24 实战会话：为「实时渲染变体」补齐 live 模板后，两个曾失败段必须
# 在 0.98/0.02 严格阈值下读对；t9 段由操作者现场确认（9筒），s7 段经裁框
# 目视复核为 7 条（同一裁框曾被误标为 bei，已在 pending 复核环节驳回）。
LIVE_SESSION_1024 = CAPTURES / "assist_sessions" / "20261024_035412" / "live" / "tea_2p"


@pytest.mark.parametrize(("frame", "expected"), [
    ("20261024_110911_867393_f1fe70.png", "t9"),
    ("20261024_104839_549465_396b27.png", "s7"),
    # 2026-10-25 纠错：该帧财字小牌为 3x3 竖条、中列红 = 9条；旧断言 s3 只是因为
    # s3_live2 模板被误标（其实也是 9条）才"通过"的。
    ("20261024_104001_144544_5526f7.png", "s9"),
])
def test_20261024_live_variant_frames_clear_strict_thresholds(frame, expected):
    if not (LIVE_SESSION_1024 / frame).exists():
        pytest.skip("实战截屏样本已不在此机器上")
    observed = detect_caishen(
        _read(f"assist_sessions/20261024_035412/live/tea_2p/{frame}"))
    assert observed.label == expected
    assert observed.score >= .98
    assert observed.score - observed.runner_up >= .02


def test_20261024_fail_frame_is_not_bei():
    """曾被人工误标为北的帧必须读成 7 条：相同裁框不允许混入 bei 模板库。"""
    frame = "20261024_104839_549465_396b27.png"
    if not (LIVE_SESSION_1024 / frame).exists():
        pytest.skip("实战截屏样本已不在此机器上")
    observed = detect_caishen(
        _read(f"assist_sessions/20261024_035412/live/tea_2p/{frame}"))
    assert observed.label == "s7"
    assert observed.label != "bei"


# --- 2026-10-24 下午：s6（6条）live 变体 + 自动候选提名 ---

def test_20261024_s6_live_frame_clears_strict_thresholds():
    frame = "20261024_120001_000000_s6probe.png"
    if not (LIVE_SESSION_1024 / frame).exists():
        pytest.skip("实战截屏样本已不在此机器上")
    observed = detect_caishen(
        _read(f"assist_sessions/20261024_035412/live/tea_2p/{frame}"))
    assert observed.label == "s6"
    assert observed.score >= .98
    assert observed.score - observed.runner_up >= .02


def _live_frame(relative: str) -> np.ndarray:
    path = LIVE_SESSION_1024 / relative
    if not path.exists():
        pytest.skip("实战截屏样本已不在此机器上")
    return _read(f"assist_sessions/20261024_035412/live/tea_2p/{relative}")


def test_rank_labels_orders_scores_descending():
    from caishen_collector import rank_labels
    ranked = rank_labels(_live_frame("20261024_120001_000000_s6probe.png"))
    assert ranked, "应至少返回一个 label 的分数"
    scores = [score for _, score in ranked]
    assert scores == sorted(scores, reverse=True)
    assert ranked[0][0] == "s6"
    assert ranked[0][1] >= .98


def test_auto_candidate_returns_matched_when_gate_passes(tmp_path):
    from caishen_collector import collect_auto_candidate
    result = collect_auto_candidate(
        _live_frame("20261024_120001_000000_s6probe.png"),
        pending_dir=tmp_path)
    assert result["status"] == "matched"
    assert result["label"] == "s6"
    assert not list(tmp_path.iterdir()), "识别成功时不应落任何候选文件"


def test_auto_candidate_nominates_on_miss_and_requires_review(tmp_path, monkeypatch):
    """门禁不过但角标在：自动提名 top1，且侧车标注 auto_suggested。"""
    from pathlib import Path
    import caishen_collector
    import caishen_vision
    frame = _live_frame("20261024_120001_000000_s6probe.png")
    # 临时抬到 1.01 制造一次"必 miss"（top1≈1.0 但永远过不了门禁），
    # 阈值本身在另一条测试里锁定，这里只是触发提名路径；
    # 同时抬覆盖阈值，避免因 s6 已在库中被判 already_covered。
    monkeypatch.setattr(caishen_vision, "_MIN_SCORE", 1.01)
    monkeypatch.setattr(caishen_collector, "_COVERED_THRESHOLD", 1.1)
    result = caishen_collector.collect_auto_candidate(
        frame, frame_path="dummy.png", pending_dir=tmp_path)
    assert result["status"] == "pending"
    assert result["label"] is None
    assert result.get("suggested_label") == "s6"
    sidecar = tmp_path / (Path(result["file"]).stem + ".json")
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    assert record["label"] is None
    assert record.get("suggested_label") == "s6"
    # 分级：top1≈1.0 只差实时变体，记为 auto_high（复核即可入库）
    assert record["visual_check"] == "pending: auto_high"
    assert record["pending"] is True


def test_auto_candidate_never_writes_on_a_non_tile_frame(tmp_path):
    """结算/无财神画面：无论走 no_badge 还是 too_uncertain，都不落文件。"""
    import caishen_collector
    frame = _read("assist_sessions/20261023_201640/live/tea_2p/"
                  "20261023_210211_157265_7be7e3.png")
    if frame is None:
        pytest.skip("结算帧样本已不在此机器上")
    result = caishen_collector.collect_auto_candidate(frame, pending_dir=tmp_path)
    assert result["status"] in {"no_badge", "unknown", "noise"}
    assert not list(tmp_path.iterdir())


# 2026-10-24 晚间验收会话 Round1：财神为 7筒(t7)，缺 live 变体模板
# 导致 小牌匹配不够确定；补 live 模板后，0.98/0.02 必须读对。
LIVE_SESSION_1024_EVE = CAPTURES / "assist_sessions" / "20261024_211352" / "live" / "tea_2p"


@pytest.mark.parametrize(("frame", "expected"), [
    ("20261024_225442_037340_67061a.png", "t7"),
    ("20261024_225500_432052_b46480.png", "t7"),
    ("20261024_230118_528549_2fca86.png", "t9"),
])
def test_20261024_211352_round1_t7_live_frame_clears_strict_thresholds(frame, expected):
    if not (LIVE_SESSION_1024_EVE / frame).exists():
        pytest.skip("实战截屏样本已不在此机器上")
    observed = detect_caishen(
        _read(f"assist_sessions/20261024_211352/live/tea_2p/{frame}"))
    assert observed.label == expected
    assert observed.score >= .98
    assert observed.score - observed.runner_up >= .02


def test_match_fail_exposes_candidate_diagnostic_not_as_label():
    """When gated out, candidate is diagnostic only; label stays None."""
    # Synthetic: badge-like red patch but random gray face → low match.
    image = np.zeros((900, 1600, 3), dtype=np.uint8)
    image[:] = (40, 80, 40)
    # Paint a red badge inside default ROI top-left so badge gate passes.
    (x0, y0, x1, y1), _ = caishen_vision._templates()
    image[y0:y0 + 22, x0:x0 + 30] = (20, 40, 200)  # BGR strong red
    # Fill rest of crop with noise so no template clears 0.98.
    rng = np.random.default_rng(0)
    image[y0:y1, x0:x1] = rng.integers(0, 255, size=(y1 - y0, x1 - x0, 3), dtype=np.uint8)
    image[y0:y0 + 22, x0:x0 + 30] = (20, 40, 200)
    observed = detect_caishen(image)
    if observed.reason == "财神小牌匹配不够确定":
        assert observed.label is None
        assert observed.candidate is not None
        assert observed.score < .98 or (observed.score - observed.runner_up) < .02
