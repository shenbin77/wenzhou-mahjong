"""温州茶 2 人整场（16/16）总结页积分识别。

这个模块只读固定版式里的左右总分，不负责保存积分或点击「返回大厅」。模块
必须在同一张原生帧里同时确认顶部局数 ``16/16`` 与三按钮总结版式；不信任
调用方传入的布尔值，避免把提前/异常结算误记成整场总分。

积分还有三层 fail-closed 门禁：固定 ROI 内的符号/数字几何、蓝负橙正的颜色
语义、左右绝对值相等且总和为零。数字模板来自 1600x900 原生实机帧中的同款
结算字体；任何数字置信度或类别间隔不足都拒绝返回分数。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from match_end import read_match_end
from turn_gate import VERIFIED_FRAME_SIZE


# 1600x900 原生帧固定积分区。留白覆盖 1～3 位数字，但不跨入人物统计行。
SCORE_ROIS = {
    "left": (510, 650, 690, 720),
    "right": (910, 650, 1090, 720),
}
SCORE_CENTERS_X = {"left": 600, "right": 1000}
SCORE_CENTER_TOLERANCE = 48

# 完整总结页左上角的“局数: 16/16”数值区。该 HUD 在总结遮罩下仍保留，
# 但通用 room_round_vision 会因左侧头像遮挡主动弃权，因此这里仅为整场结算
# 做更窄、更严格的固定版式读取。ROI 内 16/16 的“/”与后一个“1”的相对
# 位置随抗锯齿浮动：标定帧里两者相连、形成四个连通域（1、6、/1、6）；
# 2026-10-27 实机（991671 场，双端同几何）两者相距 1px、独立成五个
# 连通域（1、6、/、1、6，斜杠域分类器判 None、填充率约 0.27）。两种形态
# 都必须逐字通过各自的几何门 + 数字分类，见 read_full_match_round。
FULL_MATCH_ROUND_ROI = (270, 55, 365, 100)

_NORMALIZED_SHAPE = (40, 28)
_MIN_DIGIT_CONFIDENCE = 0.80
_MIN_DIGIT_MARGIN = 0.02


@dataclass(frozen=True)
class MatchScoreState:
    """一次整场积分读取结果。"""

    ok: bool
    left_player: str | None = None
    right_player: str | None = None
    left_score: int | None = None
    right_score: int | None = None
    confidence: float = 0.0
    reason: str = ""
    evidence: dict = field(default_factory=dict)

    def ledger(self) -> dict:
        return {
            "ok": self.ok,
            "left_player": self.left_player,
            "right_player": self.right_player,
            "left_score": self.left_score,
            "right_score": self.right_score,
            "confidence": round(float(self.confidence), 3),
            "reason": self.reason,
            **self.evidence,
        }

    def score_for(self, player_name: str) -> int | None:
        """Return the verified score for an exact reviewed nickname."""
        name = str(player_name or "").strip()
        if name and name == self.left_player:
            return self.left_score
        if name and name == self.right_player:
            return self.right_score
        return None


@dataclass(frozen=True)
class FullMatchRoundState:
    """同帧顶部 16/16 局数的只读门禁。"""

    ok: bool
    round_no: int | None = None
    round_total: int | None = None
    reason: str = ""
    evidence: dict = field(default_factory=dict)

    def ledger(self) -> dict:
        return {
            "ok": self.ok,
            "round_no": self.round_no,
            "round_total": self.round_total,
            "reason": self.reason,
            **self.evidence,
        }


# 每个十六进制串是一个 40x28 的二值字形（np.packbits）。小字形取自总结页
# 五行统计数字，大字形取自总分；同一数字保留多尺度样本以抵抗抗锯齿差异。
_PACKED_DIGIT_TEMPLATES: dict[int, tuple[str, ...]] = {
    0: (
        "0000000000000000000000000000003fc00003fc0000fff8001fffc001fffc007fffc007e07f007e07f00fe03f00fc01f80fc01f80fc01f80fc01f80fc01fc0fc01fc3fc01fc3fc01fc3fc01fc3fc01fc3fc01fc0fc01f80fc01f80fe03f80fe03f807e07f007e07f007fffc001fffc001fffc000fff80000e00000000000000000000000000000000000000",
    ),
    1: (
        "00000000000000007ff00007ff0001fff0001fff0001fff0001fff00007ff000007f800007f800007f800007f800007f800007f800007f800007f800007f800007f800007f800007f800003f800003f800003f800003f800003f800003f800003f800003f800003f800003f800003f800003f800003f000003f000003f000003f000003f0000000000000000",
    ),
    2: (
        "00000000000000007fc00007fc0001fffc001fffc003fffe00ffffe00ffffe00ff1ff83fc07f83fc07f83f807f83f807f83f807f80e007f80e007f80001fe00001fe00001fe00003fc00003fc0000ff00003ff00003ff00007fc0001ff80001ff80003fe00003fe00003fc0000ffffe00ffffe03fffffc3fffffc3fffffc3fffff8000000000000000000000",
        "000000000000000006000003fc0000fff0001fff8003fffc007fffe00fffff01ff1ff01fe0ff81fe07f81fc07f83f807f83f807f83f007f81f00ff00600ff00000fe00001fc00003fc00003f800007f80000ff00001fe00003fc00003fc00007f80001ff00001fe00003fc00007fc0000ff80001fffff83fffff83fffff83fffff83fffff800000000000000",
    ),
    3: (
        "000000000000000007000003fe0000fff0001fffc003fffc007fffe00fffff00ff0ff00fe07f00fc07f80fc03f802007f000007f000007f00000fe00003fe0000ffc0001ff80001ffc0000ffe0000fff000007fc00007fc00003fc00001fc02001fc1f001fc3f801fc3fc01fc1fe03fc1ff07fc0fffff807ffff007fffe001fffc001fffc000000000000000",
    ),
    4: (
        "000000000000000000000000060000006000003fc00007fc00007fc00007fe0001ffe0001ffe0001ffe0003ffe0003ffe0003cfe000f8fe000f8fe000f87e001f87e001f87e007e07e007e0fe007e0fe00fe0fe00fffff80fffff80fffffc3fffffc3fffffc01ffff80000fe00000fe000007e000007e000007e000007c000001c0000000000000000000000",
    ),
    5: (
        "00000000000000007fff0007fff003ffff803ffff803ffff003fff8003fff8003fc00003fc00003f000003fc00003fc00003ffe0003ffe0003fff8003fffc003fffc003ffff003ffff000007f800007f800007f800007f800007f800007f800007f800007f800007f80f80ff00f80ff03fffff03fffff03ffffc00fffe000fffe0001ff80000000000000000",
        "00000000000000007fff800ffffc01ffffc01ffffc01ffffc03ffff803f800003f000003f000003f000003f000003f000003ff00007fff0007fff8007ffff007ffff807f0ff803c03fc00003fc00003fc00001fc00001fc00001fc00001fc00001fc00001fc00003fc00003fc3fc07f83fe1ff83fffff03fffff01ffffe007fff8003fff0000000000000000",
    ),
    6: (
        "00000000000000000070000007000001fc00001fc0000ff00000ff00000ff00003fc00007f800007f80001ff80001ff80001ff80003fffc003fffc003fffe003ffff803ffff80ffc3fc0ffc3fc0ff00fc0ff00fc0ff00fc3fc00fc0fc00fc0fc00fc0fc03fc0fc03fc0ff07fc03ffff803ffff803ffff801fffc001fffc0000f800000000000000000000000",
    ),
    7: (
        "000000000000000fffff80fffff80fffff80fffff83fffffc0fffff80fffff800007f800007f800007f800007e000007e00001fe00001fe00001fe00001fc00003fc00003fc00003f000003f00000ff00003fc00003fc00003fc00007f800007f800007f800007f80001fe00001fe00001fe00003fc00003fc00003fc00001f0000000000000000000000000",
    ),
    8: (
        "00000000000000007ff00007ff0001fffc001fffc003fffe00ffffe00ffffe00ff07f80fc03f80fc03f80fc03f80fc03f80ff07e003fffe003fffe003fffe003fffe003fffe00ffdff80ffdff83fc07f83fc03fc3fc03fc3fc03fc3fc03fc3fc03fc3fc03fc3fc03fc3fc07fc0fffff80fffff803fffe001fffc001fffc0000f800000000000000000000000",
    ),
    9: (
        "00000000000000000000001fffc001fffc003fffe003fffe00fffff83ff1ff83ff1ff83f803fc3f803fc3f803fc3f803fc3f803fc3fc03fc3fc03fc3fc03fc3ff07fc3ff07fc0fffff803ffff803ffff801fffe001fffe0000ffc0000ffc0000ffc0000ff00003fc00003fc00003fc00003fc00007f800007e000007e000007c000000000000000000000000",
        "00000000000000000f800007ff0001fff8001fffc007fffe00fffff00ff8ff80ff07f81fe03fc1fc01fc3f801fc3f801fc3f801fc3f801fc3f801fc1fc03fc1fe03fc0ff07f80fffff807ffff001ffff000fffe00003fe00001fc00001fc00007f80000ff00000ff00000fe00001fc00007f800007f80000ff00000fc0000078000000000000000000000000",
    ),
}


def _decode_templates() -> dict[int, tuple[np.ndarray, ...]]:
    height, width = _NORMALIZED_SHAPE
    count = height * width
    decoded: dict[int, tuple[np.ndarray, ...]] = {}
    for digit, packed_rows in _PACKED_DIGIT_TEMPLATES.items():
        decoded[digit] = tuple(
            np.unpackbits(np.frombuffer(bytes.fromhex(row), dtype=np.uint8))[:count]
            .reshape(height, width).astype(bool)
            for row in packed_rows
        )
    return decoded


_DIGIT_TEMPLATES = _decode_templates()


# 总结页两侧昵称不是系统控件，仍只能走像素。模板来自当前三个已复核账号
# 在 1600x900 原生总结页的橙色昵称字形；同一昵称可登记多个模板行（取
# 最高分），「烘熊瞎」第二行来自 2026-10-27 实机终局帧——旧行与该帧字形
# 不合（0.163），新行由双端同帧互证（observed 互 Dice=1.0）后登记；
# 未知昵称必须拒绝落账/离桌，不能
# 靠左右座次猜当前玩家。玉姐保留两张独立帧模板以覆盖抗锯齿差异。
PLAYER_NAME_ROIS = {
    "left": (600, 205, 730, 250),
    "right": (950, 205, 1165, 250),
}
_PLAYER_SHAPE = (32, 96)
_PLAYER_SCORE_MIN = 0.70
_PLAYER_MARGIN_MIN = 0.30
_PACKED_PLAYER_TEMPLATES: dict[str, tuple[str, ...]] = {
    "玉姐": (
        "0000000000000000000000000000000000000000000000000000000000000700000000000000000000000701fe000000000007fffff80707ffc0000000000ffffffc0607ffe000000000038ffb00060701c0000000000001c0000e0701e0000000000001c0007fe701e0000000000001c0007fe700c0000000000001c0007fe701c0000000000001c0001c67ffc0000000000001c0001c67ffc0000000000001c0001c67ffc0000000000181ef00186700c00000000003fffff018e701c00000000003fffff038e700c0000000000003e36038e700c0000000000001c00038c701c0000000000001c70039c7ffc0000000000001c7801fc7ffc0000000000001c3c00f87f5c0000000000001c1e0078701c0000000000001c0f003c700c0000000000001c07007e701c0000000000001c0000fe701c0000000000001c0001e6701e0000000001ffffffe3c3ffff0000000001ffffffc783ffff8000000000ffffffc381ffff80000000000000000000000000000000000000000000000000000",
        "000000000000000000000000000000000000000000000000000000000000060000000000000000000000060000000000000007fffff80607ffc00000000007fffff80607ffc0000000000001c000060700c0000000000001c0000e0700c0000000000001c0003fe700c0000000000001c0007fe700c0000000000001c0003fe700c0000000000001c0000c67ffc0000000000001c0001c67ffc0000000000001c0001c6701c0000000000001c000186700c00000000003fffff018e700c00000000003fffff018e700c0000000000001c00038c700c0000000000001c00038c700c0000000000001c60039c7ffc0000000000001c7001fc7ffc0000000000001c3800f8701c0000000000001c1c0078700c0000000000001c0e003c700c0000000000001c07007e700c0000000000001c02007e700c0000000000001c0000e6700c0000000001ffffffc1c3ffff0000000001ffffffc383ffff8000000000ffffffc303ffff80000000000000000000000000000000000000000000000000000",
    ),
    "拾则": (
        "000000000000000000000000000000000000000000000000000000e00200000000600000000000e0070003de00600000000000e00f801fff18600000000000e00f801fff1c600000000000e01fc01c071c600000000000e03ce018471c60000000000ffc38e018e71c60000000000ffe78f018e71c60000000000ffcf07818e71c600000000000e1f07c18e31c600000000000e3fffe18e71c600000000000e3fffe18e71c600000000000e3004018e71c600000000000c0000038e71c600000000000fe000038e71c600000000003fc407018e71c60000000000ff8fff818e71c60000000000fe0fff818e71c60000000000ee0e13818e71c600000000000e0e03818c71c600000000000e0c03801c11c600000000000e0c03801b018600000000000e0e03803b800600000000000e0e03807bc00600000000000e0fff80f1f00600000000002e0fff83e0f04e00000000007e0fff87c078fe00000000007c0f03878010fc00000000000000000000000000000000000000000000000000000",
    ),
    "烘熊瞎": (
        "00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000010000001020880020000000020000001021c91c200000000200000010232e13fc000000020000001923f89324000000020000005ff1ef9efc000000c00000005023281220000000000000001023eb12f8000000000000003023ec1e200000000000000037fb2893fc0000000000000030036f92fc000000000000002920001e840000000000000062116910840000000000000046136c80fc0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000",
        "00000000000000000000000000000000000000000000000000180c0800e018000000600000180c1800c038000000700000180c1801cc38f07f80700000180c18038e3ff07fdfffc000180c1807073f8061ffffc000180c180fffb80061f8004000198e1c0fffb80c61f83040019bffff8e01b80c61d87040019b7fff83ff1ffc7fcfffc0019f0e1c07ff1ff87fcfffc0019e0c180707000071c07000019e0c180707380061c07000031c0c1807ff383061cfffc003180c1807ff39f061c7ff8000180c1807073fc061c0700000380c1807ff3e007fc0700000380e1c07ff38007fdfffc00038ffffc707380c61dfffc00038ffffc707380c61c07000003c0000073f1ffc61c07000003e0000073e1ffc61cfffc000770e18000000007fcfffc000630e1c000000007fcc01c000e31c0e038c18607f8c01c000e03807038e1870600c01c001c07007070e1838600c01c00380f003870e1c1c000fffc00380e0018e060c1c000fffc0000000000000000000000000000000000000000000000000",
    ),
}


def _decode_player_templates() -> dict[str, tuple[np.ndarray, ...]]:
    height, width = _PLAYER_SHAPE
    count = height * width
    return {
        name: tuple(
            np.unpackbits(np.frombuffer(bytes.fromhex(row), dtype=np.uint8))
            [:count].reshape(height, width).astype(bool)
            for row in rows
        )
        for name, rows in _PACKED_PLAYER_TEMPLATES.items()
    }


_PLAYER_TEMPLATES = _decode_player_templates()


def _normalise_glyph(mask: np.ndarray) -> np.ndarray | None:
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    glyph = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.uint8)
    out_h, out_w = _NORMALIZED_SHAPE
    scale = min((out_h - 4) / glyph.shape[0], (out_w - 4) / glyph.shape[1])
    width = max(1, round(glyph.shape[1] * scale))
    height = max(1, round(glyph.shape[0] * scale))
    resized = cv2.resize(glyph, (width, height), interpolation=cv2.INTER_NEAREST)
    output = np.zeros((out_h, out_w), dtype=bool)
    x0 = (out_w - width) // 2
    y0 = (out_h - height) // 2
    output[y0:y0 + height, x0:x0 + width] = resized.astype(bool)
    return output


def _dice(left: np.ndarray, right: np.ndarray) -> float:
    denominator = int(left.sum()) + int(right.sum())
    if denominator == 0:
        return 0.0
    return 2.0 * float(np.logical_and(left, right).sum()) / denominator


def _normalise_player_mask(mask: np.ndarray) -> np.ndarray | None:
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    glyph = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.uint8)
    out_h, out_w = _PLAYER_SHAPE
    scale = min((out_h - 4) / glyph.shape[0], (out_w - 4) / glyph.shape[1])
    width = max(1, round(glyph.shape[1] * scale))
    height = max(1, round(glyph.shape[0] * scale))
    resized = cv2.resize(glyph, (width, height), interpolation=cv2.INTER_NEAREST)
    output = np.zeros((out_h, out_w), dtype=bool)
    x0 = (out_w - width) // 2
    y0 = (out_h - height) // 2
    output[y0:y0 + height, x0:x0 + width] = resized.astype(bool)
    return output


def _player_score(observed: np.ndarray, template: np.ndarray) -> float:
    best = 0.0
    height, width = observed.shape
    for dy in range(-1, 2):
        for dx in range(-1, 2):
            matrix = np.float32([[1, 0, dx], [0, 1, dy]])
            shifted = cv2.warpAffine(
                template.astype(np.uint8), matrix, (width, height),
                flags=cv2.INTER_NEAREST, borderValue=0,
            ).astype(bool)
            best = max(best, _dice(observed, shifted))
    return best


def _read_side_player(image: np.ndarray, side: str) -> tuple[str | None, dict, str]:
    x0, y0, x1, y1 = PLAYER_NAME_ROIS[side]
    crop = image[y0:y1, x0:x1]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = (
        (hsv[:, :, 0] >= 2) & (hsv[:, :, 0] <= 25)
        & (hsv[:, :, 1] >= 100)
        & (hsv[:, :, 2] >= 60) & (hsv[:, :, 2] <= 220)
    ).astype(np.uint8)
    # 名称胶囊的上边框固定占前 8 行；只保留文字及其分离笔画。
    mask[:8, :] = 0
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    components = []
    for index in range(1, count):
        width = int(stats[index][cv2.CC_STAT_WIDTH])
        height = int(stats[index][cv2.CC_STAT_HEIGHT])
        area = int(stats[index][cv2.CC_STAT_AREA])
        if area >= 8 and (height >= 4 or width >= 4):
            components.append(
                (index, int(stats[index][cv2.CC_STAT_LEFT]),
                 int(stats[index][cv2.CC_STAT_TOP]), width, height, area))
    # 归属选择：胶囊边框/装饰的零星橙墨会落在 ROI 边缘，若混进归一化
    # 包围盒，包围盒被撑大、字形整体缩小错位，模板 Dice 全面塌方
    # （2026-10-27 实机：玉姐本名只剩 0.13）。把连通域按「框扩张 6px
    # 后相交」单链聚类，只保留墨量最大的一簇：字形笔画彼此贴近同属
    # 一簇，边框碎片离得远、单独成簇被丢弃，结果记进证据。
    # （首版按字形高+横向窗选簇，会误伤标定帧「拾则」边缘笔画使包围
    # 盒收缩、分数由 1.0 掉到 0.534，故改为纯聚类，不设字形尺寸窗。）
    roots = list(range(len(components)))

    def find(a: int) -> int:
        while roots[a] != a:
            roots[a] = roots[roots[a]]
            a = roots[a]
        return a

    for i, (_i1, x1, y1, w1, h1, _a1) in enumerate(components):
        for j in range(i + 1, len(components)):
            _i2, x2, y2, w2, h2, _a2 = components[j]
            if (x1 - 6 < x2 + w2 and x2 - 6 < x1 + w1
                    and y1 - 6 < y2 + h2 and y2 - 6 < y1 + h1):
                ri, rj = find(i), find(j)
                if ri != rj:
                    roots[ri] = rj
    clusters: dict[int, list] = {}
    for i, row in enumerate(components):
        clusters.setdefault(find(i), []).append(row)
    if clusters:
        kept = max(clusters.values(),
                   key=lambda rows: sum(r[5] for r in rows))
    else:
        kept = components
    cleaned = np.zeros_like(mask)
    for index, _x, _y, _w, _h, _a in kept:
        cleaned[labels == index] = 1
    observed = _normalise_player_mask(cleaned)
    evidence = {"side": side, "roi": list(PLAYER_NAME_ROIS[side]),
                "ink_pixels": int(cleaned.sum()),
                "dropped_components": len(components) - len(kept)}
    if observed is None:
        return None, evidence, f"{side} 昵称字形为空"
    ranked = sorted(
        (
            (max(_player_score(observed, template) for template in templates),
             name)
            for name, templates in _PLAYER_TEMPLATES.items()
        ),
        reverse=True,
    )
    best_score, best_name = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    margin = best_score - runner_up
    evidence.update({
        "detected": best_name,
        "score": round(float(best_score), 3),
        "margin": round(float(margin), 3),
        "ranked": [
            {"player": name, "score": round(float(score), 3)}
            for score, name in ranked
        ],
    })
    if best_score < _PLAYER_SCORE_MIN or margin < _PLAYER_MARGIN_MIN:
        return None, evidence, f"{side} 昵称模板置信不足"
    return best_name, evidence, ""


def _read_digit(mask: np.ndarray) -> tuple[int | None, float, float, list[dict]]:
    glyph = _normalise_glyph(mask)
    if glyph is None:
        return None, 0.0, 0.0, []
    scores = []
    for digit, templates in _DIGIT_TEMPLATES.items():
        confidence = max(_dice(glyph, template) for template in templates)
        scores.append((confidence, digit))
    scores.sort(reverse=True)
    best_confidence, best_digit = scores[0]
    runner_up = scores[1][0]
    margin = best_confidence - runner_up
    ranked = [
        {"digit": int(digit), "confidence": round(float(confidence), 3)}
        for confidence, digit in scores[:3]
    ]
    if (best_confidence < _MIN_DIGIT_CONFIDENCE
            or margin < _MIN_DIGIT_MARGIN):
        return None, best_confidence, margin, ranked
    return best_digit, best_confidence, margin, ranked


def read_full_match_round(image: np.ndarray | None) -> FullMatchRoundState:
    """Strictly verify the same-frame top-left ``16/16`` HUD value.

    The fixed 1600x900 layout joins the slash and denominator's ``1`` into one
    component.  Requiring the exact four-component geometry plus independent
    digit classification prevents a three-button early/abnormal settlement
    from borrowing the full-match permission.
    """
    if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
        return FullMatchRoundState(False, reason="画面不可用")
    height, width = image.shape[:2]
    if (height, width) != VERIFIED_FRAME_SIZE:
        return FullMatchRoundState(
            False, reason=f"帧尺寸未验证 {width}x{height}")

    x0, y0, x1, y1 = FULL_MATCH_ROUND_ROI
    crop = image[y0:y1, x0:x1]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _threshold, mask = cv2.threshold(gray, 140, 255, cv2.THRESH_BINARY)
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    boxes: list[tuple[int, int, int, int, int]] = []
    for index in range(1, count):
        x = int(stats[index][cv2.CC_STAT_LEFT])
        y = int(stats[index][cv2.CC_STAT_TOP])
        w = int(stats[index][cv2.CC_STAT_WIDTH])
        h = int(stats[index][cv2.CC_STAT_HEIGHT])
        area = int(stats[index][cv2.CC_STAT_AREA])
        if area < 18 or w < 3 or h < 8 or w > 28:
            continue
        if h > int(mask.shape[0] * 0.95):
            continue
        boxes.append((x, y, w, h, area))
    boxes.sort(key=lambda row: row[0])
    evidence: dict = {
        "round_roi": list(FULL_MATCH_ROUND_ROI),
        "threshold": 140,
        "components": [list(row) for row in boxes],
        "mask_pixels": int(np.count_nonzero(mask)),
    }
    if len(boxes) == 5:
        return _verify_round_five_components(boxes, mask, evidence)
    if len(boxes) != 4:
        return FullMatchRoundState(
            False, reason=f"16/16 局数字形连通域为 {len(boxes)}/4",
            evidence=evidence)

    xs = [row[0] for row in boxes]
    ys = [row[1] for row in boxes]
    widths = [row[2] for row in boxes]
    heights = [row[3] for row in boxes]
    gaps = [boxes[i + 1][0] - (boxes[i][0] + boxes[i][2])
            for i in range(3)]
    geometry_ok = (
        15 <= xs[0] <= 20
        and max(ys) - min(ys) <= 2
        and all(15 <= value <= 18 for value in heights)
        and 5 <= widths[0] <= 8
        and 9 <= widths[1] <= 13
        and 13 <= widths[2] <= 18
        and 10 <= widths[3] <= 13
        and all(1 <= value <= 4 for value in gaps)
    )
    evidence.update({"gaps": gaps, "geometry_ok": geometry_ok})
    if not geometry_ok:
        return FullMatchRoundState(
            False, reason="16/16 局数字形几何与实测固定版式不符",
            evidence=evidence)

    digit_rows = []
    digits = []
    for component_index in (0, 1, 3):
        x, y, w, h, _area = boxes[component_index]
        digit, confidence, margin, ranked = _read_digit(
            mask[y:y + h, x:x + w])
        digits.append(digit)
        digit_rows.append({
            "component_index": component_index,
            "digit": digit,
            "confidence": round(float(confidence), 3),
            "margin": round(float(margin), 3),
            "ranked": ranked,
        })
    evidence["digits"] = digit_rows
    if digits != [1, 6, 6]:
        return FullMatchRoundState(
            False, reason=f"局数字形不是 16/16（可判数字 {digits}）",
            evidence=evidence)

    # 第三个域必须仍是“斜杠+1”的复合字形，而不是一个可被高置信识别的
    # 普通数字；这是分母 16 的独立结构证据。
    x, y, w, h, _area = boxes[2]
    compound_digit, compound_confidence, compound_margin, compound_ranked = (
        _read_digit(mask[y:y + h, x:x + w]))
    evidence["slash_one"] = {
        "digit": compound_digit,
        "confidence": round(float(compound_confidence), 3),
        "margin": round(float(compound_margin), 3),
        "ranked": compound_ranked,
    }
    if compound_digit is not None:
        return FullMatchRoundState(
            False, reason="16/16 分隔斜杠与分母1未形成实测复合字形",
            evidence=evidence)
    return FullMatchRoundState(
        True, round_no=16, round_total=16,
        reason="同帧顶部局数字形严格确认为 16/16",
        evidence=evidence)


def _verify_round_five_components(boxes, mask, evidence) -> FullMatchRoundState:
    """五连通域形态：``1``、``6``、``/``、``1``、``6`` 各自独立。

    与四域形态同一安全口径：固定几何（起始 x、基线、字高、逐位宽、
    逐个间距）+ 四个数字逐个模板分类必须恰为 1、6、1、6 + 中间域
    必须是斜杠（分类器判不出数字、且填充率是细笔画的低值）。
    任一不成立即拒绝——非 16/16 的局数（如 15/16）在数字分类处出局。
    """
    evidence = {**evidence, "form": "five_component"}
    xs = [row[0] for row in boxes]
    ys = [row[1] for row in boxes]
    widths = [row[2] for row in boxes]
    heights = [row[3] for row in boxes]
    gaps = [boxes[i + 1][0] - (boxes[i][0] + boxes[i][2]) for i in range(4)]
    geometry_ok = (
        15 <= xs[0] <= 20
        and max(ys) - min(ys) <= 2
        and all(15 <= value <= 18 for value in heights)
        and 5 <= widths[0] <= 8
        and 9 <= widths[1] <= 13
        and 7 <= widths[2] <= 11
        and 5 <= widths[3] <= 8
        and 10 <= widths[4] <= 13
        and all(1 <= value <= 4 for value in gaps)
    )
    evidence.update({"gaps": gaps, "geometry_ok": geometry_ok})
    if not geometry_ok:
        return FullMatchRoundState(
            False, reason="16/16 局数字形几何与实测固定版式不符（五域形态）",
            evidence=evidence)
    digit_rows = []
    digits = []
    for component_index in (0, 1, 3, 4):
        x, y, w, h, _area = boxes[component_index]
        digit, confidence, margin, ranked = _read_digit(mask[y:y + h, x:x + w])
        digits.append(digit)
        digit_rows.append({
            "component_index": component_index,
            "digit": digit,
            "confidence": round(float(confidence), 3),
            "margin": round(float(margin), 3),
            "ranked": ranked,
        })
    evidence["digits"] = digit_rows
    if digits != [1, 6, 1, 6]:
        return FullMatchRoundState(
            False, reason=f"局数字形不是 16/16（可判数字 {digits}）",
            evidence=evidence)
    x, y, w, h, area = boxes[2]
    slash_digit, slash_confidence, slash_margin, slash_ranked = _read_digit(
        mask[y:y + h, x:x + w])
    fill_ratio = area / float(w * h)
    evidence["slash"] = {
        "digit": slash_digit,
        "confidence": round(float(slash_confidence), 3),
        "margin": round(float(slash_margin), 3),
        "ranked": slash_ranked,
        "fill_ratio": round(fill_ratio, 3),
    }
    if slash_digit is not None or fill_ratio > 0.45:
        return FullMatchRoundState(
            False, reason="16/16 分隔斜杠形态不成立（五域形态）",
            evidence=evidence)
    return FullMatchRoundState(
        True, round_no=16, round_total=16,
        reason="同帧顶部局数字形严格确认为 16/16（斜杠独立五域形态）",
        evidence=evidence)


def _score_colour_masks(crop: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    blue = cv2.inRange(hsv, (95, 140, 40), (110, 255, 255)) > 0
    orange = cv2.inRange(hsv, (4, 140, 50), (18, 255, 255)) > 0
    return blue, orange


def _read_side_score(image: np.ndarray, side: str) -> tuple[int | None, float, dict, str]:
    x0, y0, x1, y1 = SCORE_ROIS[side]
    crop = image[y0:y1, x0:x1]
    blue, orange = _score_colour_masks(crop)
    combined = (blue | orange).astype(np.uint8)
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        combined * 255, 8)

    components = []
    for index in range(1, count):
        x = int(stats[index][cv2.CC_STAT_LEFT])
        y = int(stats[index][cv2.CC_STAT_TOP])
        width = int(stats[index][cv2.CC_STAT_WIDTH])
        height = int(stats[index][cv2.CC_STAT_HEIGHT])
        area = int(stats[index][cv2.CC_STAT_AREA])
        if area < 80 or width < 8 or height < 5:
            continue
        component_mask = labels == index
        blue_count = int(np.logical_and(component_mask, blue).sum())
        orange_count = int(np.logical_and(component_mask, orange).sum())
        tone = "blue" if blue_count > orange_count else "orange"
        components.append({
            "x": x, "y": y, "width": width, "height": height,
            "area": area, "tone": tone, "mask": component_mask,
            "box": [x0 + x, y0 + y, x0 + x + width, y0 + y + height],
        })
    components.sort(key=lambda row: row["x"])
    public_components = [
        {key: value for key, value in row.items() if key != "mask"}
        for row in components
    ]
    evidence = {"side": side, "roi": list(SCORE_ROIS[side]),
                "components": public_components}
    if not (2 <= len(components) <= 4):
        return None, 0.0, evidence, f"{side} 积分区字形数 {len(components)} 不在 2~4"

    sign_component = components[0]
    width = sign_component["width"]
    height = sign_component["height"]
    if height <= 12 and width >= 20:
        sign = -1
        sign_name = "minus"
    elif 22 <= height <= 40 and 22 <= width <= 42:
        sign = 1
        sign_name = "plus"
    else:
        return None, 0.0, evidence, f"{side} 最左字形不是已验证正负号"

    digit_components = components[1:]
    if not (1 <= len(digit_components) <= 3):
        return None, 0.0, evidence, f"{side} 数字位数未验证"
    tones = {row["tone"] for row in components}
    if len(tones) != 1:
        return None, 0.0, evidence, f"{side} 符号与数字颜色不一致"
    tone = next(iter(tones))
    if (sign < 0 and tone != "blue") or (sign > 0 and tone != "orange"):
        return None, 0.0, evidence, f"{side} 分数颜色与正负号冲突"

    group_left = min(row["box"][0] for row in components)
    group_right = max(row["box"][2] for row in components)
    group_center = (group_left + group_right) / 2.0
    center_offset = abs(group_center - SCORE_CENTERS_X[side])
    evidence.update({"sign": sign_name, "tone": tone,
                     "group_center_x": round(group_center, 1),
                     "center_offset": round(center_offset, 1)})
    if center_offset > SCORE_CENTER_TOLERANCE:
        return None, 0.0, evidence, f"{side} 积分组偏离固定 ROI 中心"

    digits: list[int] = []
    confidences: list[float] = []
    digit_evidence = []
    for row in digit_components:
        if not (30 <= row["height"] <= 52 and 7 <= row["width"] <= 42
                and row["area"] >= 140):
            return None, 0.0, evidence, f"{side} 数字几何不在实测范围"
        digit, confidence, margin, ranked = _read_digit(row["mask"])
        digit_evidence.append({
            "box": row["box"], "digit": digit,
            "confidence": round(float(confidence), 3),
            "margin": round(float(margin), 3), "ranked": ranked,
        })
        if digit is None:
            evidence["digits"] = digit_evidence
            return None, 0.0, evidence, f"{side} 数字模板置信不足"
        digits.append(digit)
        confidences.append(confidence)

    magnitude = int("".join(str(digit) for digit in digits))
    if len(digits) > 1 and digits[0] == 0:
        return None, 0.0, evidence, f"{side} 积分存在前导零"
    score = sign * magnitude
    evidence.update({"digits": digit_evidence, "magnitude": magnitude,
                     "score": score})
    return score, min(confidences), evidence, ""


def read_match_score(
    image: np.ndarray | None,
) -> MatchScoreState:
    """读取 16/16 总结页两侧带符号总分。

    本函数自行复核三按钮固定版式与同帧顶部 ``16/16`` 局数。成功结果保证
    两分数绝对值相等、总和为零；调用方不能用布尔值绕过局数证据。
    """
    if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
        return MatchScoreState(False, reason="画面不可用")
    height, width = image.shape[:2]
    if (height, width) != VERIFIED_FRAME_SIZE:
        return MatchScoreState(False, reason=f"帧尺寸未验证 {width}x{height}")

    match_end = read_match_end(image)
    if not match_end.present:
        return MatchScoreState(
            False, reason="固定三按钮版式未确认，不读取整场积分",
            evidence={"match_end": match_end.ledger()})
    full_round = read_full_match_round(image)
    if not full_round.ok:
        return MatchScoreState(
            False,
            reason=("同帧顶部 16/16 局数未通过门禁，不读取整场积分："
                    f"{full_round.reason}"),
            evidence={"match_end": match_end.ledger(),
                      "full_match_round": full_round.ledger()})

    left, left_confidence, left_evidence, left_error = _read_side_score(
        image, "left")
    if left is None:
        return MatchScoreState(
            False, reason=left_error,
            evidence={"match_end": match_end.ledger(),
                      "full_match_round": full_round.ledger(),
                      "left": left_evidence})
    right, right_confidence, right_evidence, right_error = _read_side_score(
        image, "right")
    if right is None:
        return MatchScoreState(
            False, left_score=left, reason=right_error,
            evidence={"match_end": match_end.ledger(),
                      "full_match_round": full_round.ledger(),
                      "left": left_evidence,
                      "right": right_evidence})

    left_player, left_player_evidence, left_player_error = _read_side_player(
        image, "left")
    if left_player is None:
        return MatchScoreState(
            False, left_score=left, right_score=right,
            reason=left_player_error,
            evidence={"match_end": match_end.ledger(),
                      "full_match_round": full_round.ledger(),
                      "left": left_evidence,
                      "right": right_evidence,
                      "left_player": left_player_evidence})
    right_player, right_player_evidence, right_player_error = _read_side_player(
        image, "right")
    if right_player is None or right_player == left_player:
        reason = (right_player_error or
                  f"左右昵称重复为 {left_player}，拒绝按座次猜测")
        return MatchScoreState(
            False, left_player=left_player,
            left_score=left, right_score=right, reason=reason,
            evidence={"match_end": match_end.ledger(),
                      "full_match_round": full_round.ledger(),
                      "left": left_evidence,
                      "right": right_evidence,
                      "left_player": left_player_evidence,
                      "right_player": right_player_evidence})

    symmetric = abs(left) == abs(right)
    zero_sum = left + right == 0
    evidence = {
            "match_end": match_end.ledger(),
            "full_match_round": full_round.ledger(),
        "left": left_evidence,
        "right": right_evidence,
        "left_player_evidence": left_player_evidence,
        "right_player_evidence": right_player_evidence,
        "absolute_values_equal": symmetric,
        "sum_zero": zero_sum,
        "score_sum": left + right,
    }
    if not symmetric or not zero_sum:
        return MatchScoreState(
            False, left_score=left, right_score=right,
            left_player=left_player, right_player=right_player,
            confidence=min(left_confidence, right_confidence),
            reason=f"两侧积分不守恒：{left:+d} / {right:+d}",
            evidence=evidence)

    confidence = min(left_confidence, right_confidence)
    return MatchScoreState(
        True, left_score=left, right_score=right, confidence=confidence,
        left_player=left_player, right_player=right_player,
        reason=(f"16/16 总结积分已核验：{left_player} {left:+d}，"
                f"{right_player} {right:+d}"),
        evidence=evidence)


# 兼容复数命名，便于调用方表达“读取两侧分数”。
read_match_scores = read_match_score
