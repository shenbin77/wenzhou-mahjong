# -*- coding: utf-8 -*-
"""弱牌恢复路径（recover_triplet）与正常路径的独立锁定。

背景：`tile_vision.recognize_hand` 里有一条兜底——三张同牌里恰好一张低于阈值时，
用左右两帧牌面的相关性把它补回来，reason 为 `{region}_triplet_correlated`
（f-string 拼出，源码里搜不到完整字符串，所以只能靠行为断言来锁）。

这条路径以前由 `test_assist_tea.py` 里那张实战帧"顺带"覆盖：一旦某个 label 补了
更强的模板（例如 t9 补了实战模板后首轮 score 0.9698），该帧直接 accepted，兜底就
不再触发，用例从"覆盖兜底"退化成"断言旧值"，看起来像回归。

这里把两条路径拆开、各自独立锁定：
1. 人为把单张副露牌的分数压到阈值以下（只影响那一张，另两张保持 accepted），
   断言兜底确实抓到、reason 是兜底专用值、相似度达到相关阈值；
2. 用当前生产模板原样跑，断言走的是正常 accepted 路径，且**结果与走兜底时一致**
   （牌面序列相同、整手完整）——这样"路径切换"与"识别退化"可以区分。

不修改任何生产代码与阈值；threshold 在本文件里显式断言，防止被悄悄放宽。
"""

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import tile_vision  # noqa: E402
from tile_vision import TileVision  # noqa: E402

# 原生实战截图：左侧三张九筒为副露，右侧三张一筒为另一组副露。
TRIPLET_FRAME = (BOT_DIR.parent / "采集" / "assist_sessions" / "diagnostics"
                 / "codex_live_20261023_233630.png")
EXPECTED_MELDS = [29, 29, 29, 1, 1, 1]     # 九筒×3 + 一筒×3
RECOVER_REASON = "exposed_triplet_correlated"
LOW_SCORE = 0.70                            # 低于 min_score 0.82，仍高于兜底下限 0.5


def _vision() -> TileVision:
    vision = TileVision(template_dir=BOT_DIR.parent / "采集" / "templates" / "tea_tiles")
    vision.caishen_label = "w2"             # 本局财神，与该帧实际一致
    return vision


def _frame():
    if not TRIPLET_FRAME.is_file():
        pytest.skip("实战副露截图样本缺失")
    return cv2.imdecode(np.fromfile(str(TRIPLET_FRAME), np.uint8), cv2.IMREAD_COLOR)


def _concealed_calls(vision: TileVision, image) -> int:
    """试跑一次，得到闭手框数量=闭手循环的 classify_crop 调用次数。

    用它把"第一次副露调用"的下标自校准出来，避免硬编码牌框数而随识别变化失效。
    """
    vision.recognize_hand(image)
    return len(vision.diagnostics)


def _force_one_weak_meld_tile(vision: TileVision, index: int) -> None:
    """只把第 index 次 classify_crop（即第一张副露牌）的分数压到阈值以下。

    保留 label/tile_id 之外的原始读数，模拟"这一张恰好没认出来、左右两张认出来了"
    的真实弱牌场景；不触碰其它任何牌。
    """
    original = vision.classify_crop
    counter = {"calls": 0}

    def wrapped(crop, caishen_label=None):
        result = original(crop, caishen_label=caishen_label)
        current = counter["calls"]
        counter["calls"] += 1
        if current == index:
            doctored = dict(result)
            doctored.update(score=LOW_SCORE, accepted=False, tile_id=None,
                            reason="low_score")
            return doctored
        return result

    vision.classify_crop = wrapped


def test_weak_meld_tile_is_recovered_by_the_triplet_path():
    """单张低于阈值时，兜底必须抓到，并给出兜底专用 reason。"""
    image = _frame()
    vision = _vision()
    assert vision.min_score == 0.82 and vision.min_margin == 0.055  # 阈值不许被放宽

    concealed = _concealed_calls(vision, image)
    _force_one_weak_meld_tile(vision, concealed)
    vision.recognize_hand(image)

    weak = vision.meld_diagnostics[0]
    assert weak["score"] < vision.min_score          # 确实低于阈值
    assert weak["score"] >= 0.5                      # 仍在兜底的受理区间
    assert weak["accepted"] is True                  # 兜底把它补回来了
    assert weak["reason"] == RECOVER_REASON          # 兜底专用 reason
    assert weak["group_similarity"] >= 0.95          # 相关性判据达标
    assert [row[0] for row in vision.last_melds] == EXPECTED_MELDS
    assert vision.last_complete is True


def test_strong_meld_tiles_skip_the_recovery_path():
    """分数够时不得走兜底：reason 必须是 accepted，且结果与兜底路径一致。"""
    image = _frame()
    vision = _vision()
    vision.recognize_hand(image)

    melds = vision.meld_diagnostics[:3]
    assert [m["label"] for m in melds] == ["t9", "t9", "t9"]
    for meld in melds:
        assert meld["score"] >= vision.min_score
        assert meld["accepted"] is True
        assert meld["reason"] == "accepted"          # 正常路径
        assert meld.get("group_similarity") is None  # 没用到兜底判据
    # 关键：路径不同、结论相同——换模板只会切换路径，不该改变牌面结论
    assert [row[0] for row in vision.last_melds] == EXPECTED_MELDS
    assert vision.last_complete is True


def test_recover_reason_is_assembled_not_literal():
    """兜底 reason 由 f-string 拼出，这正是源码 grep 不到完整字符串的原因。

    锁住这一点，防止有人因为"搜不到字符串"而误判该分支已废弃并删掉它。
    """
    source = Path(tile_vision.__file__).read_text(encoding="utf-8")
    assert "triplet_correlated" in source
    assert RECOVER_REASON not in source, (
        "若源码出现完整字符串，说明写法变了，请同步更新本文件说明")
    assert RECOVER_REASON == f"{'exposed'}_triplet_correlated"
