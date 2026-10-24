"""安全回归：整手压暗帧必须弃权（防亮度补偿扩散的守门测试）。

背景：tile_vision 的亮度补偿只允许作用于"财"角标命中的单张牌
（reason=caishen_marked）。一旦补偿扩散到无角标的牌，"请出牌"整手
压暗帧会被误识别成完整手牌——把不确定局面当成可执行建议，比弃权
危险得多。本文件锁死两条底线：

1. 财神未知时，整手压暗帧继续弃权（424821/r08 回放帧，既有契约）；
2. 即使财神已知，只要缺的牌没有角标，也必须弃权
   （20261023_205534 实战帧：缺的二条只是被整手压暗，无角标，
    实测 13/14 —— 该帧永远不得补全）。
"""

import sys
from pathlib import Path

import cv2
import numpy as np

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from tile_vision import TileVision  # noqa: E402

TEA_ROOT = BOT_DIR.parent
TEMPLATES = TEA_ROOT / "采集" / "templates" / "tea_tiles"
DIMMED_REPLAY = TEA_ROOT / "采集" / "2026-10-15" / "424821" / "r08" / "frame_02.png"
DIMMED_PONG_FRAME = (TEA_ROOT / "采集" / "assist_sessions" / "20261023_201640" /
                     "live" / "tea_2p" / "20261023_205534_475580_f4a34e.png")


def _image(path):
    image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None, f"复现帧缺失: {path}"
    return image


def test_dimmed_replay_hand_abstains_without_known_caishen():
    """财神未知：整手压暗帧保持既有弃权契约。"""
    vision = TileVision(template_dir=TEMPLATES)
    vision.recognize_hand(_image(DIMMED_REPLAY), caishen_label=None)
    assert vision.detection_diagnostics["concealed_count"] == 17
    assert vision.last_complete is False
    assert any(not d["accepted"] for d in vision.diagnostics)


def test_known_caishen_only_rescues_the_single_marked_tile():
    """财神已知时也只允许救活角标命中的那一张（该帧确有角标牌）。"""
    vision = TileVision(template_dir=TEMPLATES)
    vision.recognize_hand(_image(DIMMED_REPLAY), caishen_label="s9")
    marked = [d for d in vision.diagnostics if d.get("caishen_marked")]
    assert len(marked) == 1
    assert marked[0]["reason"] == "caishen_marked"
    # 其余 16 张全部是正常模板匹配，无一带补偿痕迹
    others = [d for d in vision.diagnostics if not d.get("caishen_marked")]
    assert len(others) == 16
    assert all(d["reason"] == "accepted" for d in others)


def test_dimmed_unmarked_missing_tile_never_completes_the_hand():
    """关键守门：压暗帧缺的牌没有角标时，给已知财神也不许补全。"""
    vision = TileVision(template_dir=TEMPLATES)
    vision.recognize_hand(_image(DIMMED_PONG_FRAME), caishen_label="t3")
    assert vision.detection_diagnostics["concealed_count"] == 14
    assert vision.last_complete is False
    # 该帧没有任何角标：不得出现 caishen_marked 路径
    assert not any(d.get("caishen_marked") for d in vision.diagnostics)
    unaccepted = [d for d in vision.diagnostics if not d["accepted"]]
    assert len(unaccepted) == 1
    assert unaccepted[0]["reason"] != "caishen_marked"
