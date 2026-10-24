"""财神牌面覆盖自检 + 自动候选提名的分级把关。

2 人茶局没有红中和发财，所以有效需求是 32 个牌面；每个牌面还需要
"回放帧"与"实时帧"两套模板才能真正秒读。这两条正是这里要盯的口径。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import caishen_collector  # noqa: E402
import caishen_vision  # noqa: E402
from caishen_collector import collect_auto_candidate, rank_labels  # noqa: E402
from caishen_coverage import UNUSED_IN_TWO_PLAYER, scan  # noqa: E402

CAPTURES = BOT_DIR.parent / "采集"
LIVE_SESSION_1024 = CAPTURES / "assist_sessions" / "20261024_035412" / "live" / "tea_2p"
KNOWN_LIVE_FRAME = "20261024_120001_000000_s6probe.png"


def _read(relative: str) -> np.ndarray:
    raw = np.fromfile(CAPTURES / relative, dtype=np.uint8)
    return cv2.imdecode(raw, cv2.IMREAD_COLOR)


def _live_frame(name: str = KNOWN_LIVE_FRAME) -> np.ndarray:
    path = LIVE_SESSION_1024 / name
    if not path.exists():
        pytest.skip("实战截屏样本已不在此机器上")
    return _read(f"assist_sessions/20261024_035412/live/tea_2p/{name}")


# --- 覆盖统计口径 ---

def test_two_player_wall_drops_zhong_and_fa():
    """2 人局牌墙不含红中/发财，不能计入需求缺口。"""
    assert UNUSED_IN_TWO_PLAYER == {"hongzhong", "fa"}


def test_coverage_counts_match_the_two_player_wall():
    report = scan()
    assert report["total_vocabulary"] == 34
    assert report["required"] == 32
    assert report["covered"] == report["required"] - len(
        report["missing_live"]) - len(report["missing_completely"])
    # 红中/发财不算缺口
    gaps = [e["label"] for e in
            report["missing_live"] + report["missing_completely"]]
    assert "hongzhong" not in gaps
    assert "fa" not in gaps


def test_coverage_report_is_json_serialisable():
    report = scan()
    assert json.loads(json.dumps(report, ensure_ascii=False))["required"] == 32


def test_every_live_template_file_exists_on_disk():
    """清单里写的 live 模板必须在磁盘上，否则运行时加载会直接抛错。"""
    report = scan()
    for row in report["rows"]:
        if row["status"] == "ok":
            assert row["live"], f"{row['label']} 状态为 ok 但没有 live 模板"
        for name in row["live_files"]:
            assert (caishen_vision._DIR / name).is_file(), name


# --- 提名分级 ---

def _noise_frame() -> np.ndarray:
    """Red badge present, sprite region unrecognisable.

    The badge gate only needs >=70 red pixels in the top-left 30x22 of the
    ROI, so a plain red block there makes a valid, deliberately unmatched
    frame.
    """
    rng = np.random.default_rng(20261024)
    image = rng.integers(40, 200, size=(900, 1600, 3), dtype=np.uint8)
    x0, y0, x1, y1 = caishen_vision._templates()[0]
    image[y0:y0 + 22, x0:x0 + 30] = (0, 0, 255)  # BGR 纯红
    return image


def test_auto_candidate_marks_noise_below_floor(tmp_path):
    result = collect_auto_candidate(_noise_frame(), pending_dir=tmp_path)
    assert result["status"] == "noise"
    assert result["label"] is None
    assert not list(tmp_path.iterdir()), "噪声帧不应留下任何候选文件"


def test_auto_candidate_keeps_unknown_crop_when_no_label_is_close(tmp_path, monkeypatch):
    """分数够不上提名：裁图照样留下来（带 top-k），交给人工命名。"""
    # 放开下限让这张"看不出"的帧进入分级，再抬高提名线让它落入 unknown。
    monkeypatch.setattr(caishen_collector, "_AUTO_FLOOR", -1.0)
    monkeypatch.setattr(caishen_collector, "_AUTO_LOW", 1.1)
    result = collect_auto_candidate(_noise_frame(), frame_path="dummy.png",
                                    pending_dir=tmp_path)
    assert result["status"] == "unknown"
    assert result["label"] is None
    assert result["file"].startswith("unknown_")
    sidecar = tmp_path / (Path(result["file"]).stem + ".json")
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    assert record["label"] is None
    assert record["visual_check"] == "pending: auto_unknown"
    assert len(record["topk"]) == 3
    assert (tmp_path / result["file"]).is_file()


def test_auto_candidate_grades_high_confidence(tmp_path, monkeypatch):
    """缺实时变体但牌面确定：提名带 auto_high，复核即可入库。"""
    monkeypatch.setattr(caishen_vision, "_MIN_SCORE", 1.01)  # 强制 miss
    monkeypatch.setattr(caishen_collector, "_COVERED_THRESHOLD", 1.1)
    result = collect_auto_candidate(_live_frame(), frame_path="dummy.png",
                                    pending_dir=tmp_path)
    assert result["status"] == "pending"
    assert result["label"] is None
    assert result.get("suggested_label") == "s6"
    record = json.loads(
        (tmp_path / (Path(result["file"]).stem + ".json")).read_text(encoding="utf-8"))
    assert record["visual_check"] == "pending: auto_high"


def test_auto_candidate_grades_low_confidence(tmp_path, monkeypatch):
    """像但没到 high：标记 auto_low，复核时必须看放大图。"""
    monkeypatch.setattr(caishen_vision, "_MIN_SCORE", 1.01)
    monkeypatch.setattr(caishen_collector, "_AUTO_HIGH", 1.1)
    monkeypatch.setattr(caishen_collector, "_COVERED_THRESHOLD", 1.1)
    result = collect_auto_candidate(_live_frame(), frame_path="dummy.png",
                                    pending_dir=tmp_path)
    assert result["status"] == "pending"
    record = json.loads(
        (tmp_path / (Path(result["file"]).stem + ".json")).read_text(encoding="utf-8"))
    assert record["visual_check"] == "pending: auto_low"


def test_auto_candidate_does_not_duplicate_a_queued_label(tmp_path, monkeypatch):
    """同一牌面只排队一次，避免刷爆 pending 目录。"""
    monkeypatch.setattr(caishen_vision, "_MIN_SCORE", 1.01)
    monkeypatch.setattr(caishen_collector, "_COVERED_THRESHOLD", 1.1)
    frame = _live_frame()
    first = collect_auto_candidate(frame, frame_path="dummy.png",
                                   pending_dir=tmp_path)
    assert first["status"] == "pending"
    second = collect_auto_candidate(frame, frame_path="dummy.png",
                                    pending_dir=tmp_path)
    assert second["status"] in {"duplicate", "already_covered"}


def test_rank_labels_is_empty_for_a_wrong_sized_image():
    assert rank_labels(np.zeros((800, 1600, 3), dtype=np.uint8)) == []


# --- 复核闸：未命名候选不许入库 ---

@pytest.fixture
def review_sandbox(tmp_path, monkeypatch):
    """把复核工具指向临时目录，避免动到生产 manifest。"""
    import caishen_coverage
    import review_caishen_pending as review

    templates = tmp_path / "templates" / "caishen"
    pending = templates / "pending"
    pending.mkdir(parents=True)
    manifest = templates / "manifest.json"
    manifest.write_text(
        json.dumps({"roi": [622, 348, 677, 423], "templates": []},
                   ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(review, "TEMPLATE_DIR", templates)
    monkeypatch.setattr(review, "PENDING_DIR", pending)
    monkeypatch.setattr(review, "MANIFEST", manifest)
    monkeypatch.setattr(caishen_collector, "PENDING_DIR", pending)
    return review, pending, manifest


def test_review_refuses_unnamed_candidate(review_sandbox, monkeypatch):
    review, pending, manifest = review_sandbox
    monkeypatch.setattr(caishen_collector, "_AUTO_FLOOR", -1.0)
    monkeypatch.setattr(caishen_collector, "_AUTO_LOW", 1.1)
    result = collect_auto_candidate(_noise_frame(), frame_path="dummy.png",
                                    pending_dir=pending)
    assert result["status"] == "unknown"
    sidecar = Path(result["file"]).stem + ".json"

    assert review.confirm(sidecar) == 2, "未命名候选必须被拦下"
    assert (pending / sidecar).is_file(), "被拒的候选应留在 pending 等人工命名"
    assert json.loads(manifest.read_text(encoding="utf-8"))["templates"] == []


def test_review_promotes_unnamed_candidate_when_labelled(review_sandbox, monkeypatch):
    review, pending, manifest = review_sandbox
    monkeypatch.setattr(caishen_collector, "_AUTO_FLOOR", -1.0)
    monkeypatch.setattr(caishen_collector, "_AUTO_LOW", 1.1)
    result = collect_auto_candidate(_noise_frame(), frame_path="dummy.png",
                                    pending_dir=pending)
    sidecar = Path(result["file"]).stem + ".json"

    assert review.confirm(sidecar, "t6") == 0
    entries = json.loads(manifest.read_text(encoding="utf-8"))["templates"]
    assert len(entries) == 1
    assert entries[0]["label"] == "t6"
    assert (templates_dir_of(manifest) / entries[0]["file"]).is_file()
    assert not (pending / sidecar).exists()


def templates_dir_of(manifest: Path) -> Path:
    return manifest.parent


def test_review_rejects_an_unknown_label(review_sandbox, monkeypatch):
    review, pending, _ = review_sandbox
    monkeypatch.setattr(caishen_collector, "_AUTO_FLOOR", -1.0)
    monkeypatch.setattr(caishen_collector, "_AUTO_LOW", 1.1)
    result = collect_auto_candidate(_noise_frame(), frame_path="dummy.png",
                                    pending_dir=pending)
    sidecar = Path(result["file"]).stem + ".json"
    assert review.confirm(sidecar, "not_a_tile") == 2
    assert (pending / sidecar).is_file()
