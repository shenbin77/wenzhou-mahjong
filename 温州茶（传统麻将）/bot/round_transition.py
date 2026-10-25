"""局间「下一局」自动续局（温州茶 2 人）。

只做一件事：在**新鲜帧正证据**下点一次局间结算页右下角橙色「下一局」，
然后确认新局已开始。绝不按固定坐标盲点，绝不重复点击。

三重门（全部成立才算候选）：

1. 模板分：``templates/round_transition/next_round.png``（必须裁自真实
   1600×900 局间帧；缺失时本模块拒绝点击，状态 ``template_missing``）
   在右下 ROI 内 ``TM_CCOEFF_NORMED`` ≥ :data:`TEMPLATE_MIN_SCORE`；
2. 几何：命中框落在右下区域、尺寸在 :data:`BUTTON_W_RANGE` /
   :data:`BUTTON_H_RANGE` 内、框内橙色像素占比 ≥ :data:`ORANGE_MIN_RATIO`；
3. 场景：``turn_gate.detect_settled_screen`` 至少一条结算信号，且不是
   等候室（``session_watch.detect_waiting_room``）。OCR 不参与判定。

点击前 100–250ms 再取一帧复核（同区域、尺寸相近、分数过门），否则不点。
点后有界轮询：按钮消失 **且** 观察到新局证据（等候室 / 结算遮罩消失且
手牌带变亮 / 调用方注入的探针），否则 ``post_verify_uncertain`` 并永久
停手（同一执行器不会再点）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import cv2
import numpy as np

BOT_DIR = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = BOT_DIR / "templates" / "round_transition" / "next_round.png"

FRAME_HW = (900, 1600)
# 右下 ROI（x0, y0, x1, y1），按钮必须整块落在里面。
SEARCH_ROI = (900, 560, 1600, 900)
TEMPLATE_MIN_SCORE = 0.90
BUTTON_W_RANGE = (150, 420)
BUTTON_H_RANGE = (50, 160)
ORANGE_MIN_RATIO = 0.30
RECHECK_DELAY_S = 0.18          # 100–250ms 区间内
RECHECK_MAX_SHIFT_PX = 8
GONE_MAX_SCORE = 0.60           # 点后按钮“消失”的上限分
POST_POLLS = 10
POST_INTERVAL_S = 0.5
MATCH_ROUND_TOTAL = 16

# 房卡策略（只管“开新局/开新房”，从不阻拦入座）。
ROOM_CARDS_LOW = 3


def _valid(image) -> bool:
    return (isinstance(image, np.ndarray) and image.ndim == 3
            and tuple(image.shape[:2]) == FRAME_HW)


def load_template(path: str | Path | None = None) -> Optional[np.ndarray]:
    p = Path(path) if path is not None else DEFAULT_TEMPLATE
    if not p.is_file():
        return None
    img = cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)
    return img


def orange_ratio(patch: np.ndarray) -> float:
    if patch is None or patch.size == 0:
        return 0.0
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    mask = ((hsv[..., 0] >= 5) & (hsv[..., 0] <= 25)
            & (hsv[..., 1] >= 110) & (hsv[..., 2] >= 160))
    return float(mask.mean())


def find_next_round_button(image, template: Optional[np.ndarray]) -> dict:
    """模板 + 几何两道门；不含场景门。返回证据 dict（``ok`` 为两道门结果）。"""
    ev: dict[str, Any] = {"ok": False, "score": 0.0, "box": None,
                          "center": None, "orange_ratio": 0.0, "reason": ""}
    if not _valid(image):
        ev["reason"] = "invalid_frame"
        return ev
    if template is None or template.ndim != 3:
        ev["reason"] = "template_missing"
        return ev
    th, tw = template.shape[:2]
    x0, y0, x1, y1 = SEARCH_ROI
    roi = image[y0:y1, x0:x1]
    if th > roi.shape[0] or tw > roi.shape[1]:
        ev["reason"] = "template_larger_than_roi"
        return ev
    res = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    bx0, by0 = x0 + int(loc[0]), y0 + int(loc[1])
    box = [bx0, by0, bx0 + tw, by0 + th]
    ev.update({"score": round(float(score), 4), "box": box,
               "center": [bx0 + tw // 2, by0 + th // 2]})
    if score < TEMPLATE_MIN_SCORE:
        ev["reason"] = f"template_score_low:{score:.3f}"
        return ev
    if not (BUTTON_W_RANGE[0] <= tw <= BUTTON_W_RANGE[1]
            and BUTTON_H_RANGE[0] <= th <= BUTTON_H_RANGE[1]):
        ev["reason"] = f"button_size_out_of_range:{tw}x{th}"
        return ev
    ratio = orange_ratio(image[box[1]:box[3], box[0]:box[2]])
    ev["orange_ratio"] = round(ratio, 4)
    if ratio < ORANGE_MIN_RATIO:
        ev["reason"] = f"orange_ratio_low:{ratio:.3f}"
        return ev
    ev["ok"] = True
    ev["reason"] = "button_ok"
    return ev


def round_end_scene(image) -> dict:
    """场景门：结算类画面（至少一条结算信号）且不是等候室。"""
    from turn_gate import detect_settled_screen
    ev: dict[str, Any] = {"ok": False, "settled_signals": [], "waiting_room": False,
                          "reason": ""}
    if not _valid(image):
        ev["reason"] = "invalid_frame"
        return ev
    _hit, settled = detect_settled_screen(image)
    signals = list(settled.get("settled_signals") or [])
    ev["settled_signals"] = signals
    ev["panel_area_pct"] = settled.get("panel_area_pct")
    ev["bottom_mean_v"] = settled.get("bottom_mean_v")
    try:
        from session_watch import detect_waiting_room
        ev["waiting_room"] = bool(detect_waiting_room(image).get("matched"))
    except Exception:  # noqa: BLE001
        ev["waiting_room"] = False
    if ev["waiting_room"]:
        ev["reason"] = "waiting_room"
        return ev
    if not signals:
        ev["reason"] = "no_settled_signal"
        return ev
    ev["ok"] = True
    ev["reason"] = "round_end_scene"
    return ev


def default_new_round_probe(image) -> tuple[bool, str]:
    """点后新局正证据：等候室，或结算遮罩已消失（无结算信号且手牌带亮）。"""
    if not _valid(image):
        return False, "invalid_frame"
    scene = round_end_scene(image)
    if scene["waiting_room"]:
        return True, "waiting_room"
    if not scene["settled_signals"] and (scene.get("bottom_mean_v") or 0) >= 100:
        return True, "table_dealt"
    return False, "still_settled_or_unknown"


def room_card_policy(value: Optional[int], status: str = "") -> dict:
    """房卡门：>0 允许、0 禁止开新局、偏少警告、读不出允许但记 room_cards_unknown。

    只用于“开新局/开新房”，从不阻拦入座或局间「下一局」。
    """
    if value is None:
        return {"allow_new_game": True, "level": "unknown",
                "log": "room_cards_unknown", "status": status or "unreadable"}
    if value <= 0:
        return {"allow_new_game": False, "level": "block",
                "log": "room_cards_zero", "status": status}
    if value <= ROOM_CARDS_LOW:
        return {"allow_new_game": True, "level": "warn",
                "log": "room_cards_low", "status": status}
    return {"allow_new_game": True, "level": "ok", "log": "", "status": status}


@dataclass
class TransitionResult:
    status: str             # advanced / terminal / no_click / post_verify_uncertain / halted
    reason: str
    clicked: bool = False
    evidence: dict | None = None

    def ledger(self) -> dict:
        return {"status": self.status, "reason": self.reason,
                "clicked": self.clicked, "evidence": self.evidence or {}}


class RoundTransitionExecutor:
    """单次、带证据的「下一局」执行器。

    ``capture()`` 返回 BGR 帧；``tap(x, y)`` 执行一次点击；``on_advanced``
    在新局确认后调用（接到 assist_window.next_round 做记录器/财神簿记）。
    """

    def __init__(self, *, capture: Callable[[], np.ndarray],
                 tap: Callable[[int, int], None],
                 sleep: Callable[[float], None] = time.sleep,
                 template: Optional[np.ndarray] = None,
                 template_path: str | Path | None = None,
                 new_round_probe: Callable[[np.ndarray], tuple[bool, str]] | None = None,
                 on_advanced: Callable[[dict], None] | None = None,
                 report_dir: str | Path | None = None,
                 recheck_delay_s: float = RECHECK_DELAY_S,
                 post_polls: int = POST_POLLS,
                 post_interval_s: float = POST_INTERVAL_S):
        self._capture = capture
        self._tap = tap
        self._sleep = sleep
        self._template = template if template is not None else load_template(template_path)
        self._probe = new_round_probe or default_new_round_probe
        self._on_advanced = on_advanced
        self._report_dir = Path(report_dir) if report_dir else None
        self._recheck_delay_s = min(0.25, max(0.10, float(recheck_delay_s)))
        self._post_polls = int(post_polls)
        self._post_interval_s = float(post_interval_s)
        self.halted = False
        self.clicks = 0
        self._clicked_rounds: set = set()

    # ------------------------------------------------------------------
    def _report(self, result: TransitionResult, round_no) -> TransitionResult:
        if self._report_dir is not None:
            try:
                self._report_dir.mkdir(parents=True, exist_ok=True)
                line = {"ts": datetime.now().astimezone().isoformat(),
                        "event": "round_transition", "round_no": round_no,
                        **result.ledger()}
                with open(self._report_dir / "round_transition.jsonl", "a",
                          encoding="utf-8") as fh:
                    fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
            except Exception:  # noqa: BLE001 - 报告失败不影响 fail-closed
                pass
        return result

    def _gates(self, image) -> tuple[bool, dict]:
        button = find_next_round_button(image, self._template)
        scene = round_end_scene(image)
        return bool(button["ok"] and scene["ok"]), {"button": button, "scene": scene}

    def attempt(self, *, round_no: Optional[int] = None,
                round_total: int = MATCH_ROUND_TOTAL) -> TransitionResult:
        if self.halted:
            return self._report(TransitionResult(
                "halted", "执行器已因点后状态不确定而停手，不再点击"), round_no)
        if round_no is not None and round_no in self._clicked_rounds:
            return self._report(TransitionResult(
                "no_click", f"第 {round_no} 局已点过下一局，绝不重复"), round_no)
        if round_no is not None and round_total and round_no >= round_total:
            return self._report(TransitionResult(
                "terminal", f"已到第 {round_no}/{round_total} 局，本房结束，不点下一局"),
                round_no)
        if self._template is None:
            return self._report(TransitionResult(
                "no_click", "template_missing：缺少真实帧裁出的下一局模板，拒绝点击"),
                round_no)
        first = self._capture()
        ok, ev1 = self._gates(first)
        if not ok:
            scene = ev1["scene"]
            if scene.get("waiting_room"):
                return self._report(TransitionResult(
                    "advanced_without_click", "已在等候室，无需点击", evidence=ev1), round_no)
            status = "terminal" if ev1["button"]["reason"] != "template_missing" else "no_click"
            return self._report(TransitionResult(
                status, "未见合格的下一局按钮/局间场景："
                f"{ev1['button']['reason']} / {scene['reason']}", evidence=ev1), round_no)
        self._sleep(self._recheck_delay_s)
        second = self._capture()
        ok2, ev2 = self._gates(second)
        b1, b2 = ev1["button"], ev2["button"]
        same = (ok2 and b2["box"] is not None
                and max(abs(a - b) for a, b in zip(b1["box"], b2["box"]))
                <= RECHECK_MAX_SHIFT_PX)
        evidence = {"first": ev1, "recheck": ev2}
        if not same:
            return self._report(TransitionResult(
                "no_click", "复核帧未再次确认同一按钮，不点击", evidence=evidence), round_no)
        cx, cy = b2["center"]
        # 单击：先登记再点，任何异常都不会导致第二击。
        self.clicks += 1
        if round_no is not None:
            self._clicked_rounds.add(round_no)
        try:
            self._tap(int(cx), int(cy))
        except Exception as exc:  # noqa: BLE001
            self.halted = True
            return self._report(TransitionResult(
                "post_verify_uncertain", f"点击异常，结果未知：{exc}", clicked=True,
                evidence=evidence), round_no)
        polls = []
        for i in range(max(1, self._post_polls)):
            self._sleep(self._post_interval_s)
            try:
                frame = self._capture()
            except Exception as exc:  # noqa: BLE001
                polls.append({"error": str(exc)})
                continue
            button = find_next_round_button(frame, self._template)
            gone = button["score"] < GONE_MAX_SCORE or not _valid(frame)
            new_ok, label = self._probe(frame) if gone else (False, "button_still_visible")
            polls.append({"score": button["score"], "gone": gone, "probe": label})
            if gone and new_ok:
                evidence["post"] = polls
                info = {"round_no": round_no, "evidence": label}
                if self._on_advanced is not None:
                    self._on_advanced(info)
                return self._report(TransitionResult(
                    "advanced", f"下一局已确认：{label}", clicked=True,
                    evidence=evidence), round_no)
        evidence["post"] = polls
        self.halted = True
        return self._report(TransitionResult(
            "post_verify_uncertain", "点下一局后未观察到新局证据，停手不再点击",
            clicked=True, evidence=evidence), round_no)


def crop_template(frame_path: str | Path, box, out: str | Path | None = None) -> Path:
    """从真实 1600×900 局间帧裁模板（人工确认框）。"""
    img = cv2.imdecode(np.fromfile(str(frame_path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if not _valid(img):
        raise ValueError("只能从 1600×900 原生帧裁模板")
    x0, y0, x1, y1 = [int(v) for v in box]
    dest = Path(out) if out else DEFAULT_TEMPLATE
    dest.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", img[y0:y1, x0:x1])
    if not ok:
        raise RuntimeError("PNG 编码失败")
    buf.tofile(str(dest))
    return dest


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="局间下一局：裁模板 / 离线检测")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crop")
    c.add_argument("frame")
    c.add_argument("box", nargs=4, type=int)
    d = sub.add_parser("detect")
    d.add_argument("frame")
    args = ap.parse_args(argv)
    if args.cmd == "crop":
        print(crop_template(args.frame, args.box))
        return 0
    img = cv2.imdecode(np.fromfile(args.frame, dtype=np.uint8), cv2.IMREAD_COLOR)
    print(json.dumps({"button": find_next_round_button(img, load_template()),
                      "scene": round_end_scene(img)}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
