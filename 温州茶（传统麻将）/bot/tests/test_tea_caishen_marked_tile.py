"""安全回归：单张"财"角标牌可补全闭手（task #4 契约的落盘固化）。

此前只口头验证过，本文件把契约锁死，防止识别器改动后回退：
- 角标命中 + 本局财神已知 → 该牌位按财神 label 计入（reason=caishen_marked）；
- 财神未知 → 角标牌仍按未识别处理，整手弃权；
- 补偿只作用于角标命中的那一张，其余牌必须走正常模板匹配。

实测帧（真实对局，只读）：20261023_205345 闭手 11 张含被"财"角标高亮的
四条，另有 6 张副露（吃+吃）。亮帧金标见 test_tea_2p_gold_manifest.py。
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
MARKED_FRAME = (TEA_ROOT / "采集" / "assist_sessions" / "20261023_201640" /
                "live" / "tea_2p" / "20261023_205345_749900_23750c.png")


def _vision():
    return TileVision(template_dir=TEMPLATES)


def _image(path=MARKED_FRAME):
    image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None, f"复现帧缺失: {path}"
    return image


def test_caishen_marked_tile_completes_concealed_hand():
    """角标 + 已知财神 → 单张按财神计入，闭手 11/11 全认出。"""
    vision = _vision()
    vision.recognize_hand(_image(), caishen_label="s4")
    assert vision.detection_diagnostics["concealed_count"] == 11
    assert vision.last_complete is True
    assert len(vision.last_meld_boxes) == 6  # 副露不混入闭手
    marked = [d for d in vision.diagnostics if d.get("caishen_marked")]
    assert len(marked) == 1
    assert marked[0]["label"] == "s4"
    assert marked[0]["reason"] == "caishen_marked"
    assert marked[0]["accepted"] is True


def test_unknown_caishen_keeps_marked_tile_unaccepted():
    """财神未知时角标不能凭空填牌：整手仍弃权。"""
    vision = _vision()
    vision.recognize_hand(_image(), caishen_label=None)
    assert vision.last_complete is False
    marked = [d for d in vision.diagnostics if d.get("caishen_marked")]
    assert len(marked) == 1  # 角标被检出，但不用于填牌
    assert marked[0]["accepted"] is False


def test_brightness_compensation_is_limited_to_the_marked_tile():
    """亮度补偿只允许作用于角标牌：其余牌必须走正常模板匹配路径。"""
    vision = _vision()
    vision.recognize_hand(_image(), caishen_label="s4")
    others = [d for d in vision.diagnostics if not d.get("caishen_marked")]
    assert others
    for d in others:
        assert d["reason"] in {"accepted", "low_score", "ambiguous", "no_glyph"}
        if d["accepted"]:
            assert d["reason"] == "accepted"
