"""牌局结束（局数打满）→ 返回大厅。

## 实机现象（2026-10-25 22:58，房间 604624 局数 16/16）

局数打满后画面变成「牌局结束」标题 + 冠军横幅 + 底部三个按钮：
**返回大厅 / 分享战绩 / 继续游戏**。旧行为是 ``turn_gate`` 判 blocked →
直接停机，整场代打就此结束，必须人工点返回大厅才能再来一局。

## 判定与整场事务编排；实际点击仍归专用执行器

判定用三组互相独立的证据，**任何一组不成立就 ``present=False``**，调用方
保持原有停机语义（fail-closed）：

1. ``settled``：复用 :func:`turn_gate.detect_settled_screen` 的双信号口径
   （中央奶油面板 + 手牌带压暗），不自创阈值；
2. ``buttons``：底部按钮带内恰好检出 3 个亮色圆角按钮，宽度/高度/纵向中心
   落在实测区间内；
3. ``layout``：三个按钮横向排开、互不重叠，最左那个就是「返回大厅」。

点击与点后验证走 ``TapExecutor.execute_leave_table`` 的独立白名单通道，
**不复用出牌通道**：出牌通道只校验"点在某个手牌框内"，而 (399,825) 这类
按钮中心恰好会落在手牌框内，复用等于把安全边界让给了巧合。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import cv2
import numpy as np

from turn_gate import VERIFIED_FRAME_SIZE, detect_settled_screen

# ---------------------------------------------------------------------------
# 实测几何（1600x900，来自 BOSS 20261025 22:58 真帧，1105x622 等比换算）
#   返回大厅 box=(246,777,554,873) 中心=(399,825)
#   分享战绩 中心≈(700,825)
#   继续游戏 box=(1045,764,1413,890) 中心=(1229,826)
# ---------------------------------------------------------------------------
BUTTON_BAND_Y = (740, 900)
BUTTON_MIN_W = 180
BUTTON_MAX_W = 520
BUTTON_MIN_H = 70
BUTTON_MAX_H = 150
BUTTON_MIN_AREA = 11000
BUTTON_COUNT = 3
CENTER_Y_TOLERANCE = 30
#: 实测三按钮中心（1600x900）。牌局结束是固定版式，用它钉住"是不是这一屏"，
#: 比"有没有结算感"可靠得多：这一屏的底部手牌并没有被压暗，
#: ``detect_settled_screen`` 只命中 1/2 条信号，不能当必要条件。
#: 三个按钮的宽容 ROI（左右各留 ~30px 余量），在 ROI 内定位真实按钮框。
BUTTON_ROIS = (
    ("leave", (230, 760, 585, 885)),
    ("share", (600, 750, 1025, 885)),
    ("continue", (1030, 750, 1430, 895)),
)
ROI_MIN_FILL = 0.30
#: 按钮填充率上限：整片同色（大厅地板、纯色背景）会把 ROI 填满到 1.0。
ROI_MAX_FILL = 0.85
#: 按钮框必须离 ROI 四边各留至少这么多像素；贴边说明命中的是整片背景。
ROI_MARGIN = 6
EXPECTED_LEAVE_CENTER = (399, 825)
EXPECTED_RIGHT_CENTER = (1229, 826)
CENTER_X_TOLERANCE = 80
CENTER_Y_RANGE = (780, 870)


@dataclass(frozen=True)
class MatchEndState:
    present: bool
    buttons: tuple[tuple[int, int, int, int], ...] = ()
    leave_box: tuple[int, int, int, int] | None = None
    reason: str = ""
    settled: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)

    def ledger(self) -> dict:
        return {
            "present": self.present,
            "buttons": [list(box) for box in self.buttons],
            "leave_box": list(self.leave_box) if self.leave_box else None,
            "leave_center": list(center(self.leave_box)) if self.leave_box else None,
            "reason": self.reason,
            "settled_signals": self.settled.get("settled_signals"),
            "panel_area_pct": self.settled.get("panel_area_pct"),
            "bottom_mean_v": self.settled.get("bottom_mean_v"),
            **self.evidence,
        }


def center(box) -> tuple[int, int]:
    x0, y0, x1, y1 = (int(v) for v in box[:4])
    return ((x0 + x1) // 2, (y0 + y1) // 2)


def _button_fill_mask(crop: np.ndarray) -> np.ndarray:
    """按钮填充色：亮（V≥150）且有饱和度（S≥40）。奶油色手牌面 S 低，
    绿色桌面 V 低，两者都落不进这个区间。"""
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    return ((hsv[:, :, 2] >= 150) & (hsv[:, :, 1] >= 40)).astype(np.uint8)


def _layout_buttons(image: np.ndarray) -> tuple[dict, list[dict]]:
    """在三个固定 ROI 里各找一个按钮，返回 (证据, 命中列表)。"""
    evidence: dict = {"rois": [], "fills": []}
    found: list[dict] = []
    height, width = image.shape[:2]
    for kind, (x0, y0, x1, y1) in BUTTON_ROIS:
        x1 = min(x1, width)
        y1 = min(y1, height)
        crop = image[y0:y1, x0:x1]
        if crop.size == 0:
            evidence["rois"].append({"kind": kind, "empty": True})
            continue
        mask = _button_fill_mask(crop)
        fill = round(float(mask.mean()), 3)
        count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
            mask * 255, 8)
        best = None
        for index in range(1, count):
            area = int(stats[index][cv2.CC_STAT_AREA])
            if best is None or area > best[0]:
                best = (area, index)
        row = {"kind": kind, "fill": fill}
        if best is None or not (ROI_MIN_FILL <= fill <= ROI_MAX_FILL):
            row["ok"] = False
            evidence["rois"].append(row)
            evidence["fills"].append(fill)
            continue
        _area, index = best
        bx = int(stats[index][cv2.CC_STAT_LEFT])
        by = int(stats[index][cv2.CC_STAT_TOP])
        bw = int(stats[index][cv2.CC_STAT_WIDTH])
        bh = int(stats[index][cv2.CC_STAT_HEIGHT])
        box = (x0 + bx, y0 + by, x0 + bx + bw, y0 + by + bh)
        margins = (bx, by, (x1 - x0) - bx - bw, (y1 - y0) - by - bh)
        row.update({"margins": list(margins)})
        if min(margins) < ROI_MARGIN:
            row["ok"] = False
            evidence["rois"].append(row)
            evidence["fills"].append(fill)
            continue
        row.update({"ok": True, "box": list(box), "center": list(center(box))})
        evidence["rois"].append(row)
        evidence["fills"].append(fill)
        found.append({"kind": kind, "box": box, "fill": fill})
    return evidence, found


def read_match_end(image: np.ndarray | None) -> MatchEndState:
    """识别「牌局结束」画面，并定位最左的「返回大厅」按钮。"""
    if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
        return MatchEndState(False, reason="画面不可用")
    height, width = image.shape[:2]
    if (height, width) != VERIFIED_FRAME_SIZE:
        return MatchEndState(False, reason=f"帧尺寸未验证 {width}x{height}")
    _settled_hit, settled = detect_settled_screen(image)
    evidence, found = _layout_buttons(image)
    if len(found) != BUTTON_COUNT:
        return MatchEndState(
            False, buttons=tuple(item["box"] for item in found), settled=settled,
            reason=(f"固定版式命中 {len(found)}/{BUTTON_COUNT} 个底部按钮 "
                    f"（填充率 {evidence['fills']}），不是牌局结束画面"),
            evidence=evidence)
    centres = [center(item["box"]) for item in found]
    if not (CENTER_Y_RANGE[0] <= centres[0][1] <= CENTER_Y_RANGE[1]):
        return MatchEndState(
            False, buttons=tuple(item["box"] for item in found), settled=settled,
            reason=f"左侧按钮中心 y={centres[0][1]} 不在牌局结束按钮带",
            evidence=evidence)
    left_dx = abs(centres[0][0] - EXPECTED_LEAVE_CENTER[0])
    right_dx = abs(centres[2][0] - EXPECTED_RIGHT_CENTER[0])
    if left_dx > CENTER_X_TOLERANCE or right_dx > CENTER_X_TOLERANCE:
        evidence.update({"left_offset": left_dx, "right_offset": right_dx})
        return MatchEndState(
            False, buttons=tuple(item["box"] for item in found), settled=settled,
            reason=(f"按钮横向位置与实测版式不符（左偏 {left_dx}、右偏 "
                    f"{right_dx}，容差 {CENTER_X_TOLERANCE}）"),
            evidence=evidence)
    leave = found[0]["box"]
    evidence.update({"button_centres": [list(c) for c in centres],
                     "left_offset": left_dx, "right_offset": right_dx,
                     "settled_signals": settled.get("settled_signals")})
    return MatchEndState(
        True, buttons=tuple(item["box"] for item in found),
        leave_box=leave, settled=settled,
        reason=(f"牌局结束画面：三个底部按钮与实测版式一致，"
                f"返回大厅中心 {center(leave)}"),
        evidence=evidence,
    )


def precise_settlement_evidence(image, *, round_probe=None,
                                check_match: bool = True) -> tuple[bool, dict]:
    """Positive settlement proof, excluding generic modal/dim overlays.

    A full match requires the exact same-frame 16/16 HUD plus the three-button
    layout. Between rounds requires the complete button/template/scene probe;
    round_end_scene().ok alone is deliberately insufficient.
    """
    if check_match:
        state = read_match_end(image)
        if state.present:
            from match_score import read_full_match_round
            final_round = read_full_match_round(image)
            if final_round.ok:
                return True, {"settlement_kind": "match_end",
                              "match_end": state.ledger(),
                              "full_match_round": final_round.ledger()}
    if round_probe is None:
        from round_transition import find_next_round_button, round_end_scene
        button = find_next_round_button(image, _next_round_template())
        scene = round_end_scene(image)
        matched = bool(button.get("ok") and scene.get("ok"))
        evidence = {"button": button, "scene": scene}
    else:
        matched, evidence = round_probe(image)
    return bool(matched), {"settlement_kind": "next_round" if matched else None,
                           "round_transition_probe": evidence}


@lru_cache(maxsize=1)
def _next_round_template():
    from round_transition import load_template
    return load_template()


def finish_match_summary(image, *, player_name: str, serial: str,
                         frame_path, score_reader, persist_result, execute_leave,
                         authorized, dry_run: bool = False,
                         already_recorded: bool = False) -> dict:
    """Shared 16/16 transaction: verified score -> durable row -> guarded exit.

    Unknown pre-click score evidence is observation, not a replayable action.
    All executor failures stay terminal: this function never retries a click.
    """
    from datetime import datetime
    from tap_executor import TapAborted, TapUnverified

    result = {"recorded": already_recorded}
    def outcome(status, gate, reason, **extra):
        return {**result, "status": status, "gate": gate,
                "reason": reason, **extra}

    if not authorized():
        return outcome("blocked", "match_complete", "返回大厅未获当前授权；零点击")
    try:
        score_state = score_reader(image)
    except Exception as exc:
        return outcome("blocked", "match_score_failed",
                       f"16/16 整场积分识别异常，未返回大厅：{exc}")
    result["match_result"] = score_state.ledger()
    if not score_state.ok:
        return outcome("observe", "match_score_unverified",
                       f"16/16 整场积分证据暂不完整，未点击并继续观察：{score_state.reason}")
    player_score = score_state.score_for(player_name)
    result.update(player_name=player_name or None, player_score=player_score)
    if not player_name or player_score is None:
        return outcome("blocked", "match_score_player_unverified",
                       f"当前昵称 {player_name!r} 无法与总结栏唯一对应；未落账、未返回大厅")
    row = {
        "ts": datetime.now().astimezone().isoformat(), "event": "match_result",
        "mode": "tea_2p", "serial": serial, "round_no": 16, "round_total": 16,
        "player_name": player_name, "player_score": int(player_score),
        "left_player": score_state.left_player, "left_score": score_state.left_score,
        "right_player": score_state.right_player, "right_score": score_state.right_score,
        "confidence": score_state.confidence, "frame": str(frame_path),
        "source": "verified_16_of_16_summary",
    }
    result["match_result_record"] = row
    if not authorized():
        return outcome("blocked", "user_stop", "结算授权已撤销；未返回大厅")
    if not dry_run and not already_recorded:
        try:
            persist_result(row)
        except Exception as exc:
            return outcome("blocked", "match_score_persist_failed",
                           f"16/16 积分持久化失败，未返回大厅：{exc}")
        result["recorded"] = True
    if not authorized():
        return outcome("blocked", "user_stop", "积分已保存但授权已撤销；未返回大厅")
    try:
        tap = execute_leave(image)
        if not dry_run and (not tap.get("executed") or not tap.get("verified")):
            return outcome("blocked", "leave_table_unverified",
                           "返回大厅执行结果未验证；停手不补点", leave_table=tap)
    except TapUnverified as exc:
        return outcome("blocked", "leave_table_unverified",
                       f"点返回大厅后无法证明版式消失，停手不补点：{exc}")
    except TapAborted as exc:
        return outcome("blocked", "leave_table_aborted", f"返回大厅被拒绝：{exc}")
    except Exception as exc:
        return outcome("blocked", "leave_table_failed", f"返回大厅结果未知：{exc}")
    if dry_run:
        return outcome("dry_run_plan", "", "dry_run 只记录16/16积分与返回计划，不写正式积分、不点击",
                       leave_table=tap)
    return outcome("left_table", "",
                   f"16/16 整场积分已按昵称落账：{player_name} {player_score:+d}；"
                   "已返回大厅并验证，本场状态作废",
                   leave_table=tap)
