# -*- coding: utf-8 -*-
"""tools/frame_corpus.py 的回归：分类口径、断点续跑/抽样、真实帧状态标注。

为什么要有这个测试
==================
``tools/frame_corpus.py`` 的全部阈值都来自生产模块（action_guard /
claim_vision / single_bright_cut / turn_gate / round_transition），它自己只做
组合。所以这里锁死的是**两件容易悄悄坏掉的事**：

1. 组合顺序（结算 > 行动行 > 压暗子集 > 正常 > 未知）——顺序一乱，BOT 卡死
   的统计口径就变了，报告数字会全部失真；
2. 对已验证真帧的判定结果——``20261025_010644``（17 框、16 暗 100.0 + 亮
   214.0）必须标 ``dim_bright_subset``，``20261024_140602``（吃/过 圆盘）
   必须标 ``claim_panel``，邀请弹窗帧必须标 ``settled``，等候室帧必须标
   ``unknown``。这四帧就是「BOT 老卡在哪」的四种现场。

另锁：``--limit/--stride`` 抽样与 ``--resume`` 断点续跑的文件清单语义。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
TOOLS_DIR = BOT_DIR.parents[1] / "tools"          # 仓库根 tools（frame_corpus.py 所在）
for path in (BOT_DIR, TOOLS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import frame_corpus as fc  # noqa: E402

CORPUS = BOT_DIR.parent / "采集" / "assist_sessions"

# 五类现场各自的一帧真帧（均已人工看图确认画面内容）
FRAME_DIM_BRIGHT = (CORPUS / "20261024_232316" / "live" / "tea_2p"
                    / "20261025_010644_188963_43d691.png")
FRAME_CLAIM = (CORPUS / "20261024_133129" / "live" / "tea_2p"
               / "20261024_140602_770708_d6322e.png")
FRAME_SETTLED = CORPUS / "20261018_072200" / "20261018_072201_378696_fd5d64.png"
FRAME_LOBBY = (CORPUS / "20261024_035412" / "live" / "tea_2p"
               / "20261024_083110_918860_1c2efe.png")
#: 牌局结束（21:02，局数 16/16，底部 返回大厅/分享战绩/继续游戏 三按钮）
FRAME_MATCH_END = (CORPUS / "20261023_201640" / "live" / "tea_2p"
                   / "20261023_210215_334182_0b7c12.png")

needs_frame = pytest.mark.skipif(
    not FRAME_DIM_BRIGHT.is_file(), reason="本机没有归档真帧语料")


@pytest.fixture(scope="module")
def probe() -> fc.CorpusProbe:
    return fc.CorpusProbe()


def _record(**overrides) -> dict:
    """最小记录骨架，与 CorpusProbe.classify 的字段同名同义。"""
    base = {
        "w": 1600, "h": 900, "tiles_box_count": 16,
        "bright_count": 0, "dim_count": 0, "mid_count": 0,
        "gap": None, "loose_dim_bright": False,
        "action_visible": False, "action_state": "clear",
        "claim_kinds": [], "settled": False, "settled_signals": [],
        "waiting_room": False, "match_end": False,
        "state": fc.STATE_UNKNOWN, "reason": "",
        "_pattern_hit": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- 路径与口径
def test_template_dir_is_the_production_one() -> None:
    """TileVision 默认目录 templates_hongzhong_v2 不存在，必须显式指 tea_tiles。"""
    assert fc.DEFAULT_TEMPLATE_DIR.name == "tea_tiles"
    assert (fc.DEFAULT_TEMPLATE_DIR / "manifest.json").is_file()


def test_state_vocabulary_is_exactly_the_six_contracted_states() -> None:
    assert fc.STATES == ("normal", "dim_bright_subset", "claim_panel",
                         "settled", "match_end", "unknown")


@pytest.mark.parametrize("overrides, expected", [
    # 牌局结束是独立固定版式，压过一切（结算信号通常只有 1 条）
    ({"match_end": True, "settled": True, "_pattern_hit": True}, "match_end"),
    # 结算压过一切：结算页上偶尔还能读到圆盘/手牌，届时仍应记 settled
    ({"settled": True, "claim_kinds": ["pass"], "_pattern_hit": True}, "settled"),
    # 行动行压过压暗图案：production 的 _claim_gate 同序
    ({"claim_kinds": ["gold", "pass"], "_pattern_hit": True}, "claim_panel"),
    ({"action_visible": True, "action_state": "action_visible",
      "_pattern_hit": True}, "claim_panel"),
    # 只有压暗图案
    ({"_pattern_hit": True, "bright_count": 1, "dim_count": 15},
     "dim_bright_subset"),
    # 有手牌框、什么都没命中
    ({}, "normal"),
    # 没有手牌框
    ({"tiles_box_count": 0}, "unknown"),
])
def test_decide_state_priority(overrides, expected) -> None:
    state, reason = fc._decide_state(_record(**overrides))
    assert state == expected
    assert isinstance(reason, str) and reason


def test_decide_state_labels_waiting_room() -> None:
    state, reason = fc._decide_state(_record(tiles_box_count=0, waiting_room=True))
    assert state == "unknown"
    assert "等候室" in reason


def test_unreadable_frame_is_unknown_and_never_raises(probe) -> None:
    record = probe.classify(None, "missing.png")
    assert record["state"] == "unknown"
    assert record["w"] is None
    assert "读不出" in record["reason"]
    assert record["ms"] >= 0


def test_wrong_frame_size_is_unknown(probe) -> None:
    record = probe.classify(np.zeros((720, 1280, 3), dtype=np.uint8), "small.png")
    assert record["state"] == "unknown"
    assert "1600x900" in record["reason"]


# ---------------------------------------------------------------- 真实帧
@needs_frame
def test_dim_bright_frame_is_labelled(probe) -> None:
    """17 框、16 暗 100.0 + 1 亮 214.0 的压暗子集帧（BOT 卡死的现场之一）。"""
    image = fc.read_frame(FRAME_DIM_BRIGHT)
    assert image is not None, "读帧失败：中文路径必须走 np.fromfile + imdecode"
    record = probe.classify(image, str(FRAME_DIM_BRIGHT))
    assert record["state"] == "dim_bright_subset", record["reason"]
    assert record["tiles_box_count"] == 17
    assert record["bright_count"] == 1
    assert record["dim_count"] == 16
    assert record["loose_dim_bright"] is True
    assert record["gap"] == pytest.approx(114.0)
    assert record["dim_mean"] == pytest.approx(100.0)


@needs_frame
def test_claim_frame_is_labelled(probe) -> None:
    """吃/过 圆盘同框帧：claim_panel，且 kinds 只由生产模块决定。"""
    record = probe.classify(fc.read_frame(FRAME_CLAIM), str(FRAME_CLAIM))
    assert record["state"] == "claim_panel", record["reason"]
    assert "gold" in record["claim_kinds"]
    assert "pass" in record["claim_kinds"]
    assert record["claim_known"] is True


@needs_frame
def test_settled_frame_is_labelled(probe) -> None:
    """邀请弹窗压在等候室上的帧：生产口径里属于「结算/模态弹窗」类。"""
    record = probe.classify(fc.read_frame(FRAME_SETTLED), str(FRAME_SETTLED))
    assert record["state"] == "settled", record["reason"]
    assert record["settled"] is True
    assert record["settled_signals"]


@needs_frame
def test_lobby_frame_is_unknown(probe) -> None:
    """等候室帧（每小时健康快照的常态画面）不得被算成 normal。"""
    record = probe.classify(fc.read_frame(FRAME_LOBBY), str(FRAME_LOBBY))
    assert record["state"] == "unknown", record["reason"]
    assert record["tiles_box_count"] == 0


@needs_frame
def test_match_end_frame_is_labelled(probe) -> None:
    """局数打满的「牌局结束」页：三按钮版式 + 返回大厅中心 (400,825)。"""
    assert FRAME_MATCH_END.is_file(), "缺少牌局结束真帧"
    record = probe.classify(fc.read_frame(FRAME_MATCH_END), str(FRAME_MATCH_END))
    assert record["state"] == "match_end", record["reason"]
    assert record["match_end"] is True
    assert record["leave_center"] == [400, 825]


@needs_frame
def test_match_end_beats_settled_on_the_same_frame(probe) -> None:
    """这一屏的中央面板也会命中结算信号，但状态必须是 match_end 而不是 settled。"""
    record = probe.classify(fc.read_frame(FRAME_MATCH_END), str(FRAME_MATCH_END))
    assert record["settled"] is True, "该帧应带 central_panel 证据（否则本用例失去意义）"
    assert record["state"] == "match_end"


@needs_frame
def test_every_record_carries_the_contracted_fields(probe) -> None:
    record = probe.classify(fc.read_frame(FRAME_CLAIM), str(FRAME_CLAIM))
    for field in ("path", "w", "h", "tiles_box_count", "bright_count",
                  "dim_count", "action_visible", "claim_kinds", "state"):
        assert field in record
    json.dumps(record, ensure_ascii=False)          # 必须可直接落 JSONL


# ---------------------------------------------------------------- 清单语义
def _touch(root: Path, names: list[str]) -> list[Path]:
    made = []
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        made.append(path)
    return made


def test_iter_pngs_stride_and_limit(tmp_path) -> None:
    root = tmp_path / "corpus"
    _touch(root, [f"s{i:02d}/f{i}.png" for i in range(10)])
    _touch(root, ["s00/ignore.txt"])

    everything = fc.iter_pngs(root)
    assert len(everything) == 10
    assert everything == sorted(everything), "字典序必须是时间序"

    assert len(fc.iter_pngs(root, stride=5)) == 2       # 抽样：更均匀地铺满全程
    assert len(fc.iter_pngs(root, limit=3)) == 3
    assert fc.iter_pngs(root, stride=1, limit=0) == []


def test_iter_pngs_rejects_missing_root(tmp_path) -> None:
    with pytest.raises(SystemExit):
        fc.iter_pngs(tmp_path / "nope")


def test_load_done_paths_tolerates_broken_lines(tmp_path) -> None:
    out = tmp_path / "index.jsonl"
    out.write_text(
        json.dumps({"path": "a.png"}, ensure_ascii=False) + "\n"
        + "{ not json\n"
        + "\n"
        + json.dumps({"path": "b.png"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    assert fc.load_done_paths(out) == {"a.png", "b.png"}
    assert fc.load_done_paths(tmp_path / "absent.jsonl") == set()


def test_summarize_counts_and_shares() -> None:
    records = [
        {"state": "normal", "tiles_box_count": 16},
        {"state": "normal", "tiles_box_count": 16},
        {"state": "claim_panel", "claim_kinds": ["pass"], "action_visible": True},
        {"state": "dim_bright_subset", "loose_dim_bright": True},
        {"state": "unknown", "tiles_box_count": 0},
        {"state": "bogus"},                        # 未登记状态不得撑爆统计
    ]
    summary = fc.summarize(records)
    assert summary["total"] == 6
    assert summary["counts"]["unknown"] == 2
    assert summary["counts"]["normal"] == 2
    assert summary["shares"]["normal"] == pytest.approx(33.33)
    assert summary["extras"]["claim_kind_frames"]["pass"] == 1
    assert summary["extras"]["action_visible"] == 1
    assert summary["extras"]["loose_dim_bright"] == 1
    assert sum(summary["counts"].values()) == 6


def test_resume_appends_instead_of_rewriting(tmp_path) -> None:
    """--resume 的两次运行必须只追加，且与不续跑的结果等价。"""
    root = tmp_path / "corpus"
    made = _touch(root, [f"f{i}.png" for i in range(4)])
    for path in made:
        path.write_bytes(b"")                       # 空文件 = 读不出的帧
    out = tmp_path / "index.jsonl"

    assert fc.main(["--root", str(root), "--out", str(out)]) == 0
    first = out.read_text(encoding="utf-8").splitlines()
    assert len(first) == 4
    assert all(json.loads(line)["state"] == "unknown" for line in first)

    assert fc.main(["--root", str(root), "--out", str(out), "--resume"]) == 0
    assert out.read_text(encoding="utf-8").splitlines() == first, "续跑不该重复写"


def test_parallel_scan_matches_sequential(tmp_path) -> None:
    """workers>1 只是吞吐手段，结论必须与串行完全一致。"""
    root = tmp_path / "corpus"
    for path in _touch(root, [f"f{i}.png" for i in range(3)]):
        path.write_bytes(b"")
    paths = fc.iter_pngs(root)
    parallel = fc.scan(paths, workers=2)
    assert {r["path"] for r in parallel} == {str(p) for p in paths}
    assert all(r["state"] == "unknown" for r in parallel)
