# -*- coding: utf-8 -*-
"""会话看门狗 + 停顿熔断 + 自动战报 + 场次管家骨架（session watch）。

定位（硬边界，任何后续集成不得放宽）：
- **只观察**：读注入的帧与会话台账（actions.jsonl），不碰 ADB、不启停
  进程、不发任何点击。本模块不 import 任何执行层（无 adb/subprocess）。
- **只统计、只出计划**：熔断与战报是纯统计；场次管家只产出 dry-run
  计划 dict（``execute=False`` 恒定），本模块内没有任何执行实现。
- **读不准就 None / unknown**：房卡读数与登录页判定只认正面证据，
  证据不足一律 fail-closed，绝不猜。

与 session_supervisor 的分工：监督器管「循环停手后的恢复计划」；本模块管
账号/场次层健康（登录页、房卡）、停顿熔断（同一类停顿反复出现就别再
无限重拉）、「一场一报」的自动战报，以及跨场次衔接的管家骨架。

分类阈值沿用 session_supervisor 已用存帧标定过的口径（大厅暖色/绿毡、
结算双信号复用 turn_gate.detect_settled_screen），不在这里另拍一套。
台账解析口径：每行是一个完整 JSON（整帧大字典 + 尾部 event/gate/reason），
必须逐行 json.loads，只看行首字段会误判事件类型（2026-10-25 实证教训）。
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import cv2
import numpy as np

from mode_vision import score_tea_player_count
from turn_gate import detect_settled_screen

__all__ = [
    "WatchState",
    "WatchResult",
    "classify_watch",
    "detect_waiting_room",
    "detect_login_screen",
    "read_room_cards",
    "room_cards_evidence",
    "load_frame",
    "load_ledger_events",
    "StallPolicy",
    "extract_stall_occurrences",
    "analyze_stalls",
    "ProgressSuspicionPolicy",
    "derive_progress_suspicions",
    "build_match_report",
    "build_match_report_root",
    "render_match_report_md",
    "write_match_report",
    "ManagerState",
    "SessionManager",
    "NOMINAL_TARGETS",
    "EXECUTION_ENABLED",
]

BOT_DIR = Path(__file__).resolve().parent
EXPECTED_HW = (900, 1600)  # 已验证帧尺寸 (height, width)

# --- 画面分类阈值（与 session_supervisor 同口径，存帧实测分隔值） ---
LOBBY_WARM_MIN = 0.45   # 大厅暖色占比下限（实测 0.72；对局帧最高 0.19）
LOBBY_GREEN_MAX = 0.15  # 大厅绿毡占比上限（实测 0.02；对局帧最低 0.62）
TABLE_GREEN_MIN = 0.35  # 「仍在牌桌」绿毡占比下限（实测对局 0.62~0.73）

# --- 等候室“开始游戏”只读检测（1600x900 实测） ---
# 只在茶 2 人规则文字高置信成立时，检查中央固定区域里的大橙色按钮连通域。
# 这只用于把 waiting_room 与 in_game 分开；永不发布点击坐标给执行层。
WAITING_BUTTON_ROI = (500, 430, 1100, 700)  # x0,y0,x1,y1
WAITING_ORANGE_HSV_LOW = (5, 100, 140)
WAITING_ORANGE_HSV_HIGH = (30, 255, 255)
WAITING_BUTTON_MIN_AREA = 15000
WAITING_BUTTON_WIDTH = (220, 340)
WAITING_BUTTON_HEIGHT = (70, 130)

# --- 登录页模板门（与财神模板同风格：高分 + 边距，防误报） ---
LOGIN_TEMPLATE_MIN_SCORE = 0.90
LOGIN_TEMPLATE_MIN_MARGIN = 0.05
DEFAULT_LOGIN_TEMPLATE_DIR = BOT_DIR / "templates" / "login"

# --- 房卡读数（大厅第二行「可用房卡：N」，1600x900 标定） ---
# 带外有标题行（含「(888)」与口令数字），故只取第二行 y 带；带内自左向右
# 第一个连续数字串即房卡数（其后是 人数 a/b、桌数 c/d，均在其右侧），
# 串长 >3 视为不是房卡（口令是 6 位），拒绝。
CARD_BAND = (70, 122, 240, 820)  # (y0, y1, x0, x1) @1600x900
CARD_MAX_DIGITS = 3
CARD_MIN_SCORE = 0.90
CARD_MIN_MARGIN = 0.05
DEFAULT_DIGIT_TEMPLATE_DIR = BOT_DIR / "templates" / "room_hud_digits"


class WatchState(str, Enum):
    """看门狗会话状态。str 枚举，可直接落台账/战报。"""

    IN_GAME = "in_game"
    SETTLEMENT = "settlement"
    LOBBY = "lobby"
    WAITING_ROOM = "waiting_room"
    LOGIN_SCREEN = "login_screen"
    UNKNOWN = "unknown"


@dataclass
class WatchResult:
    """一次看门狗分类的完整结论：状态 + 证据 + 告警 + 房卡读数。"""

    state: WatchState
    evidence: dict = field(default_factory=dict)
    alerts: list = field(default_factory=list)
    room_cards: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "room_cards": self.room_cards,
            "alerts": self.alerts,
            "evidence": self.evidence,
        }


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def load_frame(path: str | Path) -> Optional[np.ndarray]:
    """读一帧（中文路径安全：np.fromfile + imdecode）。失败返回 None。"""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except (OSError, ValueError):
        return None


def _load_gray_template(path: Path) -> Optional[np.ndarray]:
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
    except (OSError, ValueError):
        return None


def load_ledger_events(path: str | Path) -> list[dict]:
    """逐行 JSON 解析台账（path 可为 actions.jsonl 文件或会话目录）。

    坏行/非 dict 行跳过；会话头（无 event、带 started_at）保留在结果里，
    调用方靠字段区分。文件缺失返回 []。
    """
    p = Path(path)
    if p.is_dir():
        p = p / "actions.jsonl"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _color_stats(image: np.ndarray) -> dict:
    """绿毡/暖色占比（HSV 口径与 session_supervisor 标定一致）。"""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    h = hsv[:, :, 0].astype(np.int16)
    s = hsv[:, :, 1].astype(np.int16)
    v = hsv[:, :, 2].astype(np.int16)
    green = float(((h >= 35) & (h <= 95) & (s >= 60) & (v >= 30) & (v <= 170)).mean())
    warm = float(((h <= 25) & (s >= 40) & (v >= 120)).mean())
    return {"green_ratio": round(green, 3), "warm_ratio": round(warm, 3)}


def detect_waiting_room(image: Optional[np.ndarray]) -> dict:
    """正面识别茶 2 人等候室；只读，不推断是否已坐满、不提供点击计划。"""
    result: dict[str, Any] = {
        "matched": False,
        "player_count": None,
        "mode_score": 0.0,
        "mode_margin": 0.0,
        "start_button_box": None,
        "start_button_area": 0,
        "status": "not_checked",
    }
    if (image is None or not isinstance(image, np.ndarray)
            or image.ndim != 3 or tuple(image.shape[:2]) != EXPECTED_HW):
        result["status"] = "frame_missing_or_unverified"
        return result
    players, score, margin = score_tea_player_count(image)
    result.update({
        "player_count": players,
        "mode_score": round(float(score), 4),
        "mode_margin": round(float(margin), 4),
    })
    if players != 2:
        result["status"] = "tea_2p_rule_not_confirmed"
        return result

    x0, y0, x1, y1 = WAITING_BUTTON_ROI
    hsv = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    lower = np.array(WAITING_ORANGE_HSV_LOW, dtype=np.uint8)
    upper = np.array(WAITING_ORANGE_HSV_HIGH, dtype=np.uint8)
    mask = cv2.inRange(hsv, lower, upper)
    count, _labels, stats, _centers = cv2.connectedComponentsWithStats(mask, 8)
    components = [tuple(int(v) for v in stats[index])
                  for index in range(1, count)]
    if not components:
        result["status"] = "start_button_not_found"
        return result
    bx, by, width, height, area = max(components, key=lambda item: item[4])
    result.update({
        "start_button_box": [x0 + bx, y0 + by,
                             x0 + bx + width, y0 + by + height],
        "start_button_area": area,
    })
    if not (WAITING_BUTTON_MIN_AREA <= area
            and WAITING_BUTTON_WIDTH[0] <= width <= WAITING_BUTTON_WIDTH[1]
            and WAITING_BUTTON_HEIGHT[0] <= height <= WAITING_BUTTON_HEIGHT[1]):
        result["status"] = "start_button_geometry_rejected"
        return result
    result["matched"] = True
    result["status"] = "matched"
    return result


def _is_header(item: Mapping[str, Any]) -> bool:
    """会话头：进程每次启动写一行（无 event、带 started_at/serial）。"""
    return "event" not in item and ("started_at" in item or "session" in item)


# ---------------------------------------------------------------------------
# 登录页检测（只认模板正面证据；无模板 = 明确报缺口，绝不猜）
# ---------------------------------------------------------------------------

def detect_login_screen(
    image: Optional[np.ndarray],
    templates_dir: str | Path | None = None,
) -> dict:
    """登录页模板检测。返回证据 dict，``matched`` 为 True 才算登录页。

    模板目录缺失/为空时 status=no_templates、matched=False——这是
    fail-closed 的正确行为：没有标定过的登录模板，宁可报 unknown，
    也不许凭颜色猜登录页（误报会触发停手，漏报靠 unknown 兜底告警）。
    """
    result: dict[str, Any] = {
        "matched": False,
        "status": "not_checked",
        "template": None,
        "score": None,
        "margin": None,
    }
    if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
        result["status"] = "frame_missing_or_invalid"
        return result
    tdir = Path(templates_dir) if templates_dir is not None else DEFAULT_LOGIN_TEMPLATE_DIR
    if not tdir.is_dir():
        result["status"] = "no_templates"
        return result
    pngs = sorted(tdir.glob("*.png"))
    if not pngs:
        result["status"] = "no_templates"
        return result
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    scores: list[tuple[str, float]] = []
    for png in pngs:
        tpl = _load_gray_template(png)
        if tpl is None or tpl.shape[0] > gray.shape[0] or tpl.shape[1] > gray.shape[1]:
            continue
        match = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
        scores.append((png.name, float(match.max())))
    if not scores:
        result["status"] = "templates_unusable"
        return result
    scores.sort(key=lambda pair: pair[1], reverse=True)
    best_name, best = scores[0]
    runner_up = scores[1][1] if len(scores) > 1 else 0.0
    margin = best - runner_up
    result.update(
        {"template": best_name, "score": round(best, 4), "margin": round(margin, 4)}
    )
    if best >= LOGIN_TEMPLATE_MIN_SCORE and margin >= LOGIN_TEMPLATE_MIN_MARGIN:
        result["matched"] = True
        result["status"] = "matched"
    else:
        result["status"] = "below_gate"
    return result


# ---------------------------------------------------------------------------
# 房卡读数（大厅 HUD；模板不全/分数不够一律 None）
# ---------------------------------------------------------------------------

def _load_digit_templates(templates_dir: Path) -> dict[str, list[np.ndarray]]:
    """按 index.json（或 d<digit>_*.png 命名）加载数字字形模板。"""
    digits: dict[str, list[np.ndarray]] = {}
    index_path = templates_dir / "index.json"
    names: dict[str, list[str]] = {}
    if index_path.is_file():
        try:
            raw = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                names = {str(k): list(v) for k, v in raw.items()}
        except (OSError, json.JSONDecodeError):
            names = {}
    if not names:
        for png in sorted(templates_dir.glob("d*_*.png")):
            stem = png.stem  # d7_0 -> digit '7'
            if len(stem) >= 2 and stem[1].isdigit():
                names.setdefault(stem[1], []).append(png.name)
    for digit, files in names.items():
        if not digit.isdigit() or len(digit) != 1:
            continue
        glyphs = []
        for name in files:
            tpl = _load_gray_template(templates_dir / name)
            if tpl is not None:
                glyphs.append(tpl)
        if glyphs:
            digits[digit] = glyphs
    return digits


def room_cards_evidence(
    image: Optional[np.ndarray],
    templates_dir: str | Path | None = None,
) -> dict:
    """房卡读数的完整证据：value 读不出时为 None，status 说明卡在哪一步。"""
    result: dict[str, Any] = {"value": None, "status": "not_checked", "digits": None}
    if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
        result["status"] = "frame_missing_or_invalid"
        return result
    if tuple(image.shape[:2]) != EXPECTED_HW:
        result["status"] = "frame_size_unverified"
        return result
    tdir = Path(templates_dir) if templates_dir is not None else DEFAULT_DIGIT_TEMPLATE_DIR
    if not tdir.is_dir():
        result["status"] = "no_templates"
        return result
    templates = _load_digit_templates(tdir)
    missing = [d for d in "0123456789" if d not in templates]
    result["template_digits"] = sorted(templates.keys())
    result["template_missing"] = missing
    if missing:
        # 现行 room_hud_digits 缺 3/8（玉姐 78、拾则 8 都含 8），字形不全
        # 时任何读数都可能是错的——直接 fail-closed，不进匹配。
        result["status"] = "digit_templates_incomplete"
        return result

    y0, y1, x0, x1 = CARD_BAND
    band = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    # 逐数字算分数图（多变体取 max）。
    score_maps: dict[str, np.ndarray] = {}
    tpl_size: dict[str, tuple[int, int]] = {}
    for digit, glyphs in templates.items():
        best_map: Optional[np.ndarray] = None
        for glyph in glyphs:
            if glyph.shape[0] > band.shape[0] or glyph.shape[1] > band.shape[1]:
                continue
            m = cv2.matchTemplate(band, glyph, cv2.TM_CCOEFF_NORMED)
            best_map = m if best_map is None else np.maximum(best_map, m)
            tpl_size[digit] = (int(glyph.shape[1]), int(glyph.shape[0]))
        if best_map is not None:
            score_maps[digit] = best_map
    if not score_maps:
        result["status"] = "templates_unusable"
        return result

    # 迭代取全局峰值：每取一个峰，要求分数过门且对其它数字有边距，
    # 否则整次读数作废（歧义不猜）。取后只抑制该字形自身的足迹
    # （±1px），避免误杀间距 ≥2px 的相邻数字；与已取峰位置重叠的
    # 候选峰（同一字形的偏移伪峰）跳过并局部抑制。
    work = {d: m.copy() for d, m in score_maps.items()}
    picks: list[dict[str, Any]] = []

    def _suppress(cx: int, cy: int, half_w: int, half_h: int) -> None:
        for m in work.values():
            yy0, yy1 = max(0, cy - half_h), min(m.shape[0], cy + half_h + 1)
            xx0, xx1 = max(0, cx - half_w), min(m.shape[1], cx + half_w + 1)
            if yy1 > yy0 and xx1 > xx0:
                m[yy0:yy1, xx0:xx1] = -1.0

    while True:
        best_digit, best_xy, best_score = None, None, -1.0
        for digit, m in work.items():
            idx = int(np.argmax(m))
            score = float(m.flat[idx])
            if score > best_score:
                best_digit, best_xy, best_score = digit, np.unravel_index(idx, m.shape), score
        if best_digit is None or best_score < CARD_MIN_SCORE:
            break
        y, x = int(best_xy[0]), int(best_xy[1])
        w, h = tpl_size[best_digit]
        if any(abs(p["x"] - x) < 0.6 * max(w, p["w"]) and abs(p["y"] - y) <= 4
               for p in picks):
            _suppress(x, y, 4, 4)
            continue
        others = [
            float(score_maps[d][y, x])
            for d in score_maps
            if d != best_digit
            and y < score_maps[d].shape[0]
            and x < score_maps[d].shape[1]
        ]
        margin = best_score - (max(others) if others else 0.0)
        if margin < CARD_MIN_MARGIN:
            result["status"] = "ambiguous_digit"
            result["ambiguous"] = {"digit": best_digit, "score": round(best_score, 4),
                                    "margin": round(margin, 4), "x": x, "y": y}
            return result
        picks.append({"digit": best_digit, "x": x, "y": y, "w": w, "h": h,
                      "score": round(best_score, 4), "margin": round(margin, 4)})
        for m in work.values():
            yy0, yy1 = max(0, y - 1), min(m.shape[0], y + h + 1)
            xx0, xx1 = max(0, x - 1), min(m.shape[1], x + w + 1)
            m[yy0:yy1, xx0:xx1] = -1.0
    if not picks:
        result["status"] = "no_digit_above_gate"
        return result

    picks.sort(key=lambda p: p["x"])
    # 按 y 一致 + x 间距连续分组，取最左一组为房卡串。
    runs: list[list[dict]] = [[picks[0]]]
    for pick in picks[1:]:
        prev = runs[-1][-1]
        gap = pick["x"] - (prev["x"] + prev["w"])
        if abs(pick["y"] - prev["y"]) <= 4 and -2 <= gap <= int(prev["w"] * 1.5):
            runs[-1].append(pick)
        else:
            runs.append([pick])
    run = runs[0]
    if len(run) > CARD_MAX_DIGITS:
        result["status"] = "run_too_long"
        result["digits"] = "".join(p["digit"] for p in run)
        return result
    result["digits"] = "".join(p["digit"] for p in run)
    result["picks"] = run
    result["value"] = int(result["digits"])
    result["status"] = "ok"
    return result


def read_room_cards(
    image: Optional[np.ndarray],
    templates_dir: str | Path | None = None,
) -> Optional[int]:
    """大厅房卡读数；读不准返回 None（原因见 room_cards_evidence）。"""
    return room_cards_evidence(image, templates_dir)["value"]


# ---------------------------------------------------------------------------
# 看门狗分类器
# ---------------------------------------------------------------------------

def classify_watch(
    image: Optional[np.ndarray] = None,
    events: Optional[Sequence[Mapping[str, Any]]] = None,
    *,
    login_templates_dir: str | Path | None = None,
    digit_templates_dir: str | Path | None = None,
) -> WatchResult:
    """把一帧（和/或一页台账）判成一个看门狗状态。纯读、无副作用。

    判定顺序（正面证据优先）：登录页模板 → 结算双信号 → 大厅暖色场景
    （顺带读房卡）→ 对局绿毡。无帧时退到台账口径：近期 tick 带牌桌事实
    且无终止 stop → in_game；gate_turn 的 stop → settlement（结算/模态
    停手，证据里注明来源）；其余 stop → unknown + loop_stopped 证据。
    登录页一旦命中，alerts 里必有 critical 级 login_screen 告警——
    红线：绝不在登录页做任何操作，先停手找人。
    """
    evidence: dict[str, Any] = {}
    alerts: list[dict] = []

    if events is not None:
        evidence["ledger_events"] = len(events)
        for item in events:
            if item.get("event") == "stop" and item.get("gate") == "card_exhausted":
                alerts.append({
                    "type": "room_cards_exhausted",
                    "severity": "high",
                    "message": "台账出现 card_exhausted 停手：房卡耗尽，先停手找人，不要重拉。",
                })
                break

    if image is not None:
        if not isinstance(image, np.ndarray) or image.ndim != 3:
            evidence["frame_error"] = "frame_missing_or_invalid"
            return WatchResult(WatchState.UNKNOWN, evidence, alerts)
        if tuple(image.shape[:2]) != EXPECTED_HW:
            evidence["frame_error"] = "frame_size_unverified"
            evidence["frame_size"] = [int(image.shape[1]), int(image.shape[0])]
            return WatchResult(WatchState.UNKNOWN, evidence, alerts)

        # 1) 登录页：最高告警级，只认模板正面证据，先于一切颜色启发式。
        login = detect_login_screen(image, login_templates_dir)
        evidence["login"] = login
        if login["matched"]:
            alerts.append({
                "type": "login_screen",
                "severity": "critical",
                "message": "检测到登录页：账号已掉登录态。立即停手找人，绝不在登录页点击（防风控）。",
                "template": login.get("template"),
                "score": login.get("score"),
            })
            return WatchResult(WatchState.LOGIN_SCREEN, evidence, alerts)

        stats = _color_stats(image)
        evidence.update(stats)

        # 2) 结算：与 turn_gate 完全一致的双信号口径。
        settled_hit, settled_ev = detect_settled_screen(image)
        evidence["settled_signals"] = settled_ev.get("settled_signals")
        evidence["panel_area_pct"] = settled_ev.get("panel_area_pct")
        evidence["bottom_mean_v"] = settled_ev.get("bottom_mean_v")
        if settled_hit:
            evidence["screen"] = "settled_dual_signal"
            return WatchResult(WatchState.SETTLEMENT, evidence, alerts)

        # 3) 等候室：茶 2 人规则文字 + 中央大橙色“开始游戏”按钮。
        # 必须先于通用绿毡对局判定，否则等候室会被误报成 in_game。
        waiting = detect_waiting_room(image)
        evidence["waiting_room"] = waiting
        if waiting["matched"]:
            evidence["screen"] = "tea_2p_waiting_room"
            return WatchResult(WatchState.WAITING_ROOM, evidence, alerts)

        # 4) 大厅：暖色场景；顺带读房卡（读不出为 None，不影响分类）。
        if stats["warm_ratio"] >= LOBBY_WARM_MIN and stats["green_ratio"] <= LOBBY_GREEN_MAX:
            evidence["screen"] = "venue_lobby"
            cards = room_cards_evidence(image, digit_templates_dir)
            evidence["room_cards"] = cards
            return WatchResult(WatchState.LOBBY, evidence, alerts, cards["value"])

        # 5) 对局：绿毡主导。
        if stats["green_ratio"] >= TABLE_GREEN_MIN:
            evidence["screen"] = "table_in_game"
            return WatchResult(WatchState.IN_GAME, evidence, alerts)

        evidence["screen"] = "unrecognized"
        return WatchResult(WatchState.UNKNOWN, evidence, alerts)

    # 无帧：台账口径（证据强度低，结论都注明 source=ledger）。
    if events:
        timed = [item for item in events if _parse_ts(item.get("ts")) is not None]
        last = timed[-1] if timed else events[-1]
        evidence["source"] = "ledger"
        evidence["last_event"] = last.get("event")
        evidence["last_status"] = last.get("status")
        if last.get("event") == "stop":
            evidence["loop_stopped"] = True
            evidence["stop_gate"] = last.get("gate")
            if last.get("gate") == "gate_turn":
                evidence["screen"] = "stop_gate_turn_settled_or_modal"
                return WatchResult(WatchState.SETTLEMENT, evidence, alerts)
            return WatchResult(WatchState.UNKNOWN, evidence, alerts)
        if any("frame_facts" in item or "table_state" in item for item in events[-20:]):
            evidence["screen"] = "ledger_ticks_with_table_facts"
            return WatchResult(WatchState.IN_GAME, evidence, alerts)
    evidence["frame_error"] = "frame_missing"
    return WatchResult(WatchState.UNKNOWN, evidence, alerts)


# ---------------------------------------------------------------------------
# 停顿熔断
# ---------------------------------------------------------------------------

@dataclass
class StallPolicy:
    """熔断口径：同一类停顿在一场内出现次数 > threshold 即熔断告警。

    abstain_run_min：abstain tick 是 3 帧预算式的常态噪声，连续同类
    abstain 满 abstain_run_min 帧才记 1 次停顿发生，避免把帧级抖动
    当成停顿次数。
    """

    threshold: int = 3
    abstain_run_min: int = 5


def _abstain_key(item: Mapping[str, Any]) -> str:
    gate = item.get("turn_gate")
    if isinstance(gate, Mapping):
        return str(gate.get("code") or gate.get("state") or "unspecified")
    return "unspecified"


def extract_stall_occurrences(
    events: Sequence[Mapping[str, Any]],
    policy: StallPolicy | None = None,
) -> list[dict]:
    """从台账事件流里抽出「停顿发生」序列（每次一条，可 JSON 序列化）。

    类别定义：
    - stop 事件 → ``stop:<gate>``（硬停手，重拉监督器的主要对象）；
    - error 事件 → ``error:<gate|unknown>``；
    - abstain 连续段（同 turn_gate code/state、长度 ≥ abstain_run_min）
      → ``abstain:<key>`` 记 1 次（段长记在 run_length）。
    """
    policy = policy or StallPolicy()
    occurrences: list[dict] = []
    run_key: Optional[str] = None
    run_len = 0
    run_ts: Any = None

    def flush() -> None:
        nonlocal run_key, run_len, run_ts
        if run_key is not None and run_len >= policy.abstain_run_min:
            occurrences.append({
                "category": f"abstain:{run_key}",
                "ts": run_ts,
                "run_length": run_len,
            })
        run_key, run_len, run_ts = None, 0, None

    for item in events:
        if _is_header(item):
            flush()
            continue
        event = item.get("event")
        if event == "stop":
            flush()
            occurrences.append({
                "category": f"stop:{item.get('gate') or 'unknown'}",
                "ts": item.get("ts"),
                "gate": item.get("gate"),
                "reason": item.get("reason"),
            })
            continue
        if event == "error":
            flush()
            occurrences.append({
                "category": f"error:{item.get('gate') or 'unknown'}",
                "ts": item.get("ts"),
                "gate": item.get("gate"),
                "reason": item.get("reason"),
            })
            continue
        if item.get("status") == "abstain":
            key = _abstain_key(item)
            if key == run_key:
                run_len += 1
            else:
                flush()
                run_key, run_len, run_ts = key, 1, item.get("ts")
            continue
        flush()
    flush()
    return occurrences


def analyze_stalls(
    events: Sequence[Mapping[str, Any]],
    policy: StallPolicy | None = None,
) -> dict:
    """停顿统计 + 熔断判定（纯函数）。

    relaunches 口径：台账内会话头数 - 1（同一文件被多进程续写时成立；
    现行监督器每次重拉开新会话目录，跨目录的重拉数在战报 root 聚合里
    以 process_runs 呈现，见 build_match_report_root）。
    """
    policy = policy or StallPolicy()
    occurrences = extract_stall_occurrences(events, policy)
    counts = Counter(o["category"] for o in occurrences)
    headers = sum(1 for item in events if _is_header(item))
    session_ids = {
        item.get("session") for item in events if _is_header(item) and item.get("session")
    }
    process_runs = max(headers, len(session_ids))
    fuse_alerts = [
        {
            "type": "stall_fuse",
            "category": category,
            "count": count,
            "threshold": policy.threshold,
            "message": (
                f"同一类停顿「{category}」已出现 {count} 次（阈值 {policy.threshold}）："
                "停止无限重拉，留证找人。"
            ),
        }
        for category, count in sorted(counts.items())
        if count > policy.threshold
    ]
    return {
        "policy": {"threshold": policy.threshold,
                   "abstain_run_min": policy.abstain_run_min},
        "occurrences": occurrences,
        "category_counts": dict(counts),
        "total_stalls": len(occurrences),
        "process_runs": process_runs,
        "relaunches": max(0, process_runs - 1),
        "fuse_alerts": fuse_alerts,
    }


# ---------------------------------------------------------------------------
# 卡住 / 外部推进疑似事件（离线派生，不回写执行层）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProgressSuspicionPolicy:
    """旁路疑似事件阈值。

    ``min_frames`` 只数路径不同的已落盘帧；``min_duration_s`` 按台账时间戳
    计算。两项必须同时满足。这里产出的始终是 ``*_suspected``，不能作为
    谁点击了游戏的真值。
    """

    min_frames: int = 3
    # 我方已有可执行证据却没有动作：5 秒就是故障候选。
    min_duration_s: float = 5.0
    # 普通识别/门禁异常必须更久，避免一两次动画抖动误报。
    abnormal_duration_s: float = 15.0
    # “下一局”已经被客户端接收后，给发牌/请财神动画更长窗口。
    round_transition_duration_s: float = 20.0


def _event_seconds(item: Mapping[str, Any]) -> Optional[float]:
    raw = item.get("ts") or item.get("at")
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        # actions.jsonl 当前用 ``YYYY/MM/DD HH:MM:SS``；同时兼容 ISO 台账。
        return datetime.strptime(text, "%Y/%m/%d %H:%M:%S").timestamp()
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None


def _frame_id(item: Mapping[str, Any]) -> Optional[str]:
    value = item.get("frame")
    if isinstance(value, str) and value:
        return value
    return None


def _json_list(value: Any) -> Optional[list]:
    if not isinstance(value, (list, tuple)):
        return None
    result: list = []
    for part in value:
        if isinstance(part, (str, int, float, bool)) or part is None:
            result.append(part)
        elif isinstance(part, Mapping):
            # 副露/按钮只保留台账已有的稳定标识，不读取坐标猜语义。
            result.append({
                key: part.get(key)
                for key in ("type", "tile_id", "label", "name")
                if key in part
            })
        else:
            return None
    return result


def _candidate_action_state(item: Mapping[str, Any]) -> Optional[dict]:
    """返回可比较的「疑似可行动」状态；证据不足返回 None。

    一般建议态只接受 ``frame_facts.turn_actionable is True``。针对实机已见
    的 ``bright_subset_unconfirmed``，还要求多亮裁决已连续确认、物理框数
    与期望暗手一致。两条路都必须有完整 ``frame_facts.concealed``，因此
    ``wait``、发牌、结算过渡和单纯的 BOT 文本猜测不会进入候选。
    """
    if item.get("event") != "idle":
        return None
    facts = item.get("frame_facts")
    if not isinstance(facts, Mapping):
        return None
    concealed = _json_list(facts.get("concealed"))
    if concealed is None or not concealed:
        return None

    source: Optional[str] = None
    gate = item.get("gate")
    if facts.get("turn_actionable") is True and item.get("status") == "advise":
        source = "frame_facts.turn_actionable"
    elif gate == "bright_subset_unconfirmed":
        cut = item.get("bright_subset_cut")
        evidence = cut.get("evidence") if isinstance(cut, Mapping) else None
        physical = evidence.get("physical_boxes") if isinstance(evidence, Mapping) else None
        expected = evidence.get("expected_concealed") if isinstance(evidence, Mapping) else None
        if not (
            isinstance(cut, Mapping)
            and cut.get("hit") is True
            and cut.get("confirmed") is True
            and isinstance(physical, int)
            and physical > 0
            and physical == expected
        ):
            return None
        source = "confirmed_bright_subset_candidate"
    else:
        return None

    cut = item.get("bright_subset_cut")
    whitelist = cut.get("whitelist") if isinstance(cut, Mapping) else None
    bright_tiles = None
    if isinstance(whitelist, list):
        bright_tiles = [
            part.get("tile_id")
            for part in whitelist
            if isinstance(part, Mapping) and isinstance(part.get("tile_id"), int)
        ]

    return {
        "stall_class": "actionable",
        "threshold_s": 5.0,
        "candidate_source": source,
        "gate": gate,
        "round_seq": item.get("round_seq", facts.get("round_seq")),
        "caishen": facts.get("caishen"),
        "concealed": concealed,
        "melds": _json_list(facts.get("melds")) or [],
        "river_own": _json_list(facts.get("river_own")),
        "river_opp": _json_list(facts.get("river_opp")),
        "claim_buttons": _json_list(facts.get("claim_buttons")) or [],
        "bright_tiles": bright_tiles,
    }


_EXPECTED_WAIT_TOKENS = (
    "等待对方", "等待摸牌", "等待发牌", "发牌中", "正在发牌",
    "等候室", "等待玩家", "等待好友", "好友还在等你", "尚未开局", "计数冻结",
)


def _candidate_stall_state(
    item: Mapping[str, Any],
    policy: ProgressSuspicionPolicy,
) -> Optional[dict]:
    """按场景给出可比较的停滞签名与阈值；正常等待明确排除。"""
    actionable = _candidate_action_state(item)
    if actionable is not None:
        actionable["threshold_s"] = float(policy.min_duration_s)
        return actionable

    transition = item.get("round_transition")
    transition_status = (
        transition.get("status") if isinstance(transition, Mapping) else None)
    if transition_status in {"accepted_waiting", "accepted_waiting_timeout"}:
        return {
            "stall_class": "round_transition",
            "threshold_s": float(policy.round_transition_duration_s),
            "round_seq": item.get("round_seq"),
            "transition_status": "accepted_waiting",
            "clicked": bool(transition.get("clicked")),
        }

    if item.get("event") != "idle" or item.get("status") not in {
            "abstain", "guard"}:
        return None
    reason = str(item.get("reason") or "")
    if any(token in reason for token in _EXPECTED_WAIT_TOKENS):
        return None
    gate = item.get("gate")
    if not isinstance(gate, str) or gate.strip().lower() in {
            "", "-", "none", "wait"}:
        return None
    facts = item.get("frame_facts")
    facts = facts if isinstance(facts, Mapping) else {}
    table = item.get("table_state")
    table = table if isinstance(table, Mapping) else {}
    return {
        "stall_class": "abnormal_stable",
        "threshold_s": float(policy.abnormal_duration_s),
        "gate": gate,
        "round_seq": item.get("round_seq", facts.get("round_seq")),
        "caishen": facts.get("caishen", table.get("caishen")),
        "concealed": _json_list(facts.get("concealed")),
        "river_own": _json_list(facts.get("river_own")),
        "river_opp": _json_list(facts.get("river_opp")),
    }


def _own_river_progress(item: Mapping[str, Any], baseline: Mapping[str, Any]) -> Optional[dict]:
    """只认可信的本家牌河严格追加；对手推进和识别抖动都不算。"""
    if item.get("event") in ("round_transition", "round_reset"):
        return None
    facts = item.get("frame_facts")
    table = item.get("table_state")
    if not isinstance(facts, Mapping) or not isinstance(table, Mapping):
        return None
    if table.get("discards_trusted") is not True:
        return None
    before = baseline.get("river_own")
    after = _json_list(facts.get("river_own"))
    if before is None or after is None or len(after) <= len(before):
        return None
    if after[:len(before)] != before:
        return None
    baseline_round = baseline.get("round_seq")
    current_round = item.get("round_seq", facts.get("round_seq"))
    if baseline_round is not None and current_round != baseline_round:
        return None
    return {
        "kind": "trusted_own_river_append",
        "before": before,
        "after": after,
        "appended": after[len(before):],
    }


def derive_progress_suspicions(
    events: Sequence[Mapping[str, Any]],
    policy: ProgressSuspicionPolicy | None = None,
) -> list[dict]:
    """从现有台账旁路派生 ``stuck`` / ``external advance`` 疑似事件。

    该函数不把识别器或 BOT 决策当真值：卡住只在同一证据签名的至少三张
    新帧持续约五秒后报告；外部推进只认随后可信本家牌河严格追加，且中间
    没有任何 ``event=action``。会话头、用户 stop、换局与过渡均切断关联。
    """
    policy = policy or ProgressSuspicionPolicy()
    derived: list[dict] = []
    run_state: Optional[dict] = None
    run_frames: list[str] = []
    run_start_index: Optional[int] = None
    run_start_seconds: Optional[float] = None
    run_occurrence: Optional[dict] = None
    pending_stuck: Optional[dict] = None

    def clear_run() -> None:
        nonlocal run_state, run_frames, run_start_index
        nonlocal run_start_seconds, run_occurrence
        run_state = None
        run_frames = []
        run_start_index = None
        run_start_seconds = None
        run_occurrence = None

    for index, item in enumerate(events):
        if _is_header(item):
            clear_run()
            pending_stuck = None
            continue

        event = item.get("event")
        if event == "action":
            # actions.jsonl 的 action 是 BOT 执行层事件；无论验证结果如何，
            # 都不能再把后续推进归因为「无 BOT 点击」。
            clear_run()
            pending_stuck = None
            continue
        if event in ("stop", "error"):
            # user_stop 明确是人工结束；其它 stop/error 也可能发生在一次
            # 未完整落 action 的执行尝试后，均不跨边界推断外部推进。
            clear_run()
            pending_stuck = None
            continue
        if event in ("round_transition", "round_reset"):
            transition = item.get("round_transition")
            transition_status = (
                transition.get("status") if isinstance(transition, Mapping) else None)
            if not (event == "round_transition" and transition_status in {
                    "accepted_waiting", "accepted_waiting_timeout"}):
                clear_run()
                pending_stuck = None
                continue

        if pending_stuck is not None:
            progress = _own_river_progress(item, pending_stuck["state_signature"])
            if progress is not None:
                external = {
                    "type": "external_advance_suspected",
                    "event_index": index,
                    "ts": item.get("ts") or item.get("at"),
                    "frame": _frame_id(item),
                    "stuck_event_index": pending_stuck["event_index"],
                    "bot_action_between": False,
                    "progress_evidence": progress,
                    "reason": (
                        "卡住疑似段后，本家可信牌河严格追加，且台账中间无 "
                        "event=action；仅标记外部推进疑似，不判定操作者"
                    ),
                }
                derived.append(external)
                pending_stuck = None
                clear_run()
                continue

        state = _candidate_stall_state(item, policy)
        frame = _frame_id(item)
        seconds = _event_seconds(item)
        if state is None or frame is None or seconds is None:
            clear_run()
            continue
        if state != run_state:
            run_state = state
            run_frames = [frame]
            run_start_index = index
            run_start_seconds = seconds
            run_occurrence = None
            continue
        if frame in run_frames:
            continue
        if run_start_seconds is None or seconds < run_start_seconds:
            clear_run()
            continue
        run_frames.append(frame)
        duration = seconds - run_start_seconds
        threshold_s = float(state.get("threshold_s", policy.min_duration_s))
        if len(run_frames) < policy.min_frames or duration < threshold_s:
            continue
        if run_occurrence is None:
            run_occurrence = {
                "type": "stuck_suspected",
                "event_index": index,
                "start_index": run_start_index,
                "end_index": index,
                "ts_start": events[run_start_index].get("ts") if run_start_index is not None else None,
                "ts_end": item.get("ts") or item.get("at"),
                "frame": frame,
                "frame_count": len(run_frames),
                "duration_s": round(duration, 3),
                "threshold_s": threshold_s,
                "stall_class": state.get("stall_class"),
                "state_signature": state,
                "reason": (
                    "同一疑似可行动证据签名持续至少 "
                    f"{policy.min_frames} 张新帧 / {threshold_s:g} 秒；"
                    "仅为旁路疑似，不把 BOT 判断当真值"
                ),
            }
            derived.append(run_occurrence)
            pending_stuck = run_occurrence
        else:
            run_occurrence["event_index"] = index
            run_occurrence["end_index"] = index
            run_occurrence["ts_end"] = item.get("ts") or item.get("at")
            run_occurrence["frame"] = frame
            run_occurrence["frame_count"] = len(run_frames)
            run_occurrence["duration_s"] = round(duration, 3)

    return derived


# ---------------------------------------------------------------------------
# 自动战报
# ---------------------------------------------------------------------------

def _as_round_int(value: Any) -> Optional[int]:
    """局身份归一：int 直接用，纯数字字符串转 int，其余（None/bool）不用。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _event_caishen(item: Mapping[str, Any]) -> Optional[str]:
    for source in (item.get("frame_facts"), item.get("table_state")):
        if isinstance(source, Mapping):
            label = source.get("caishen")
            if isinstance(label, str) and label and label != "None":
                return label
    label = item.get("caishen")
    if isinstance(label, str) and label and label != "None":
        return label
    return None


def build_match_report(session_dir: str | Path) -> dict:
    """单会话目录（一个 serial）→ 战报 dict（JSON 可序列化）。

    数字全部来自 actions.jsonl 逐行解析；局数以局身份的不同取值计数：
    优先 frame_facts.round_no，缺失时用 round_seq（现行台账实测
    round_no/room_id 恒为 None，round_seq 才是实际局信号），再缺失
    才退回 round_reset 计数，source 字段注明实际来源。财神分布以
    tick 级解析标签计数（解析不出记 unknown_ticks，不混进分布）。
    """
    session_dir = Path(session_dir)
    events = load_ledger_events(session_dir)
    header = next((item for item in events if _is_header(item)), {})
    ticks = [item for item in events if not _is_header(item)]

    event_counts = Counter(item.get("event") or "<none>" for item in ticks)
    status_counts = Counter(
        item.get("status") for item in ticks if item.get("status")
    )
    action_events = [item for item in ticks if item.get("event") == "action"]
    verified = sum(1 for item in action_events if item.get("verified") is True)

    claim_actions: Counter = Counter()
    for item in ticks:
        claim = item.get("claim")
        if isinstance(claim, Mapping):
            action = claim.get("action")
            if isinstance(action, str) and action:
                claim_actions[action] += 1

    round_nos: set[int] = set()
    room_ids: set[str] = set()
    round_total: Optional[int] = None
    used_round_no = False
    used_round_seq = False
    for item in ticks:
        facts = item.get("frame_facts")
        if not isinstance(facts, Mapping):
            facts = {}
        total = facts.get("round_total")
        if isinstance(total, int):
            round_total = total
        room_id = facts.get("room_id")
        if isinstance(room_id, (str, int)) and str(room_id):
            room_ids.add(str(room_id))
        identity = _as_round_int(facts.get("round_no"))
        if identity is not None:
            used_round_no = True
        else:
            identity = _as_round_int(facts.get("round_seq"))
            if identity is None:
                identity = _as_round_int(item.get("round_seq"))
            if identity is not None:
                used_round_seq = True
        if identity is not None:
            round_nos.add(identity)
    if used_round_no and used_round_seq:
        round_source = "frame_facts.round_no+round_seq"
    elif used_round_no:
        round_source = "frame_facts.round_no"
    elif used_round_seq:
        round_source = "round_seq"
    else:
        round_source = "round_reset_events"
    round_resets = int(event_counts.get("round_reset", 0))

    caishen_dist: Counter = Counter()
    unknown_ticks = 0
    for item in ticks:
        label = _event_caishen(item)
        if label is None:
            unknown_ticks += 1
        else:
            caishen_dist[label] += 1

    frame_refs: list[str] = []
    seen_frames: set[str] = set()
    for item in ticks:
        for key in ("frame", "after_frame"):
            value = item.get(key)
            if isinstance(value, str) and value:
                if value not in seen_frames:
                    seen_frames.add(value)
                    frame_refs.append(value)
    existing = sum(1 for ref in frame_refs if Path(ref).exists())

    timed = [ts for ts in (_parse_ts(item.get("ts")) for item in ticks) if ts]
    duration_s = None
    if len(timed) >= 2:
        duration_s = round((max(timed) - min(timed)).total_seconds(), 1)

    stalls = analyze_stalls(events)
    serial = (
        header.get("serial")
        or next((item.get("device_serial") for item in ticks
                 if item.get("device_serial")), None)
        or session_dir.name
    )
    return {
        "session_dir": str(session_dir),
        "serial": serial,
        "mode": header.get("mode"),
        "dry_run": header.get("dry_run"),
        "enabled": header.get("enabled"),
        "started_at": header.get("started_at"),
        "lines_total": len(events),
        "ticks": len(ticks),
        "duration_s": duration_s,
        "events": dict(event_counts),
        "statuses": dict(status_counts),
        "actions": {
            "count": len(action_events),
            "verified": verified,
            "claim_actions": dict(claim_actions),
        },
        "rounds": {
            "source": round_source,
            "round_nos": sorted(round_nos),
            "rounds_observed": len(round_nos) if round_nos else round_resets,
            "round_total": round_total,
            "round_resets": round_resets,
            "room_ids": sorted(room_ids),
        },
        "caishen_distribution": dict(caishen_dist),
        "caishen_unknown_ticks": unknown_ticks,
        "stalls": {
            "category_counts": stalls["category_counts"],
            "total_stalls": stalls["total_stalls"],
            "process_runs": stalls["process_runs"],
            "relaunches": stalls["relaunches"],
            "fuse_alerts": stalls["fuse_alerts"],
        },
        "evidence_frames": {
            "unique_refs": len(frame_refs),
            "existing": existing,
            "samples": frame_refs[:8],
        },
    }


def build_match_report_root(root: str | Path) -> dict:
    """会话根目录 → 战报：根下有 actions.jsonl 则单会话；否则按子目录聚合。

    聚合口径：数值字段求和；process_runs 求和后 relaunches = runs -
    会话数（每个 serial 至少跑过 1 次，超出部分才是重拉）。
    """
    root = Path(root)
    if (root / "actions.jsonl").is_file():
        return {"root": str(root), "sessions": {root.name: build_match_report(root)},
                "aggregate": None}
    sessions: dict[str, dict] = {}
    for child in sorted(root.iterdir()) if root.is_dir() else []:
        if child.is_dir() and (child / "actions.jsonl").is_file():
            sessions[child.name] = build_match_report(child)
    aggregate = None
    if sessions:
        runs = sum(s["stalls"]["process_runs"] for s in sessions.values())
        aggregate = {
            "sessions": len(sessions),
            "lines_total": sum(s["lines_total"] for s in sessions.values()),
            "actions": sum(s["actions"]["count"] for s in sessions.values()),
            "verified": sum(s["actions"]["verified"] for s in sessions.values()),
            "total_stalls": sum(s["stalls"]["total_stalls"] for s in sessions.values()),
            "process_runs": runs,
            "relaunches": max(0, runs - len(sessions)),
            "fuse_alerts": [
                {"session": name, **alert}
                for name, s in sessions.items()
                for alert in s["stalls"]["fuse_alerts"]
            ],
        }
    return {"root": str(root), "sessions": sessions, "aggregate": aggregate}


def render_match_report_md(report: dict) -> str:
    """战报 dict → Markdown（单会话与 root 聚合两种形状都能渲染）。"""
    lines: list[str] = []
    if "sessions" in report and len(report.get("sessions", {})) == 1 \
            and report.get("aggregate") is None:
        report = next(iter(report["sessions"].values()))
    if "sessions" in report:
        lines.append(f"# 战报（多会话聚合）：{report.get('root')}")
        agg = report.get("aggregate") or {}
        lines += [
            "",
            f"- 会话数：{agg.get('sessions')}；台账行数：{agg.get('lines_total')}；"
            f"动作：{agg.get('actions')}（点后验证通过 {agg.get('verified')}）",
            f"- 停顿总计：{agg.get('total_stalls')}；进程运行：{agg.get('process_runs')}；"
            f"重拉：{agg.get('relaunches')}",
            f"- 熔断告警：{len(agg.get('fuse_alerts', []))} 条",
            "",
        ]
        for name, session in report["sessions"].items():
            lines.append(f"## 会话 {name}")
            lines.append(_render_session_md(session))
        return "\n".join(lines)
    lines.append(f"# 战报：{report.get('session_dir')}")
    lines.append(_render_session_md(report))
    return "\n".join(lines)


def _render_session_md(s: dict) -> str:
    rounds = s["rounds"]
    stalls = s["stalls"]
    frames = s["evidence_frames"]
    lines = [
        "",
        f"- serial：{s.get('serial')}；模式：{s.get('mode')}；"
        f"dry_run：{s.get('dry_run')}；开始：{s.get('started_at')}；"
        f"时长：{s.get('duration_s')}s",
        f"- 台账行数：{s['lines_total']}（tick {s['ticks']}）；"
        f"事件分布：{s['events']}；状态分布：{s['statuses']}",
        f"- 局数（{rounds['source']}）：{rounds['rounds_observed']}"
        f"（局号 {rounds['round_nos']}，总场次 {rounds['round_total']}，"
        f"round_reset {rounds['round_resets']}，房间 {rounds['room_ids']}）",
        f"- 动作：{s['actions']['count']}（点后验证通过 {s['actions']['verified']}）；"
        f"鸣牌决策：{s['actions']['claim_actions']}",
        f"- 财神分布：{s['caishen_distribution']}（未解析 tick {s['caishen_unknown_ticks']}）",
        f"- 停顿：总计 {stalls['total_stalls']}，分类 {stalls['category_counts']}；"
        f"进程运行 {stalls['process_runs']}，重拉 {stalls['relaunches']}",
        f"- 熔断告警：{len(stalls['fuse_alerts'])} 条",
    ]
    for alert in stalls["fuse_alerts"]:
        lines.append(f"  - ⚠️ {alert['message']}")
    lines.append(
        f"- 证据帧：引用 {frames['unique_refs']}（现存 {frames['existing']}），抽样："
    )
    for sample in frames["samples"]:
        lines.append(f"  - {sample}")
    lines.append("")
    return "\n".join(lines)


def write_match_report(
    session_dir: str | Path,
    out_dir: str | Path | None = None,
) -> tuple[Path, Path]:
    """把战报 JSON+MD 落盘（缺省写到会话目录下 report.json/report.md）。"""
    session_dir = Path(session_dir)
    report = build_match_report_root(session_dir)
    out = Path(out_dir) if out_dir is not None else session_dir
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "report.json"
    md_path = out / "report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    md_path.write_text(render_match_report_md(report), encoding="utf-8")
    return json_path, md_path


# ---------------------------------------------------------------------------
# 场次管家骨架（只出 dry-run 计划；EXECUTION_ENABLED 恒 False）
# ---------------------------------------------------------------------------

#: 场次管家执行开关：骨架阶段恒 False。本模块没有任何执行实现；
#: 后续启用必须另写 executor 并在本模块之外完成双开关授权与评审。
EXECUTION_ENABLED = False

#: 名义坐标（1600x900，实测来源见各条 note）。管家只把它们写进计划，
#: 执行层启用时必须以执行时帧逐项复核，复核不过不许点——不许盲点坐标。
NOMINAL_TARGETS = {
    "return_to_lobby": {
        "x": 544, "y": 815, "label": "返回大厅",
        "note": "2026-10-25 结算屏实测（1280 标定 435,652 ×1.25）",
    },
    "enter_table": {
        "x": 274, "y": 459, "label": "比赛场桌子图",
        "note": "2026-10-25 凌晨实测进桌点（点后有规则窗，需再走 dismiss_rules_x）",
    },
    "dismiss_rules_x": {
        "x": 1056, "y": 289, "label": "规则窗右上 X",
        "note": "2026-10-25 凌晨实测：点 X 关窗且房主已入桌开局",
    },
}

#: 点击白名单（骨架登记，执行层启用时只许这三项，且每项点后必须验状态迁移）：
#: W1 结算屏→返回大厅；W2 大厅→点桌子图；W3 规则窗→右上 X。
#: 本只读规划器仍不发布开局点击；开始游戏仅归 lobby_start 专用执行器。
#: 充值/分享战绩/离桌确认等不在任何白名单内，永久禁止。
CLICK_WHITELIST = ("return_to_lobby", "enter_table", "dismiss_rules_x")


class ManagerState(str, Enum):
    """场次管家状态（跨场次衔接，与局内状态分开）。"""

    SETTLEMENT = "settlement"
    LOBBY = "lobby"
    ENTER_TABLE = "enter_table"
    WAITING_ROOM = "waiting_room"
    IN_GAME = "in_game"
    HOLD = "hold"


class SessionManager:
    """场次管家骨架：吃看门狗分类，吐 dry-run 计划，维护跨场次状态机。

    状态机：settlement →（计划 return_to_lobby）→ lobby →（计划
    enter_table）→ enter_table →（分类变 in_game 确认）→ in_game
    →（分类变 settlement）→ settlement……enter_table 后仍见 lobby
    视为未生效，计数重试，超过 max_enter_retries 改出 alert_human
    计划。login_screen / unknown 一律 HOLD + 告警/等待计划。
    本类不执行任何计划（计划 dict 里 execute 恒 False）。
    """

    def __init__(self, sink: str | Path | None = None, max_enter_retries: int = 3):
        self.state = ManagerState.HOLD
        self.sink = Path(sink) if sink is not None else None
        self.max_enter_retries = max_enter_retries
        self.enter_retries = 0
        self.plan_log: list[dict] = []

    def _emit(self, plan: dict) -> dict:
        plan["manager_state"] = self.state.value
        plan["dry_run"] = True
        plan["execute"] = False
        plan["execution_enabled"] = EXECUTION_ENABLED
        self.plan_log.append(plan)
        if self.sink is not None:
            self.sink.parent.mkdir(parents=True, exist_ok=True)
            with self.sink.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(plan, ensure_ascii=False) + "\n")
        return plan

    def _tap_plan(self, action: str, reason: str) -> dict:
        target = NOMINAL_TARGETS[action]
        return {
            "action": action,
            "kind": "tap",
            "whitelist_id": action,
            "nominal_target": dict(target),
            "requires_frame_recheck": True,
            "post_verify": "点后必须以新帧复核状态迁移，未迁移不许连点",
            "reason": reason,
        }

    def step(self, watch: WatchResult) -> dict:
        """按一次看门狗分类推进状态机，返回本次 dry-run 计划。"""
        ws = watch.state
        if ws is WatchState.LOGIN_SCREEN:
            self.state = ManagerState.HOLD
            return self._emit({
                "action": "alert_human", "kind": "alert",
                "reason": "login_screen：账号掉登录态，停手找人，绝不操作登录页",
            })
        if ws is WatchState.SETTLEMENT:
            self.state = ManagerState.SETTLEMENT
            self.enter_retries = 0
            return self._emit(self._tap_plan(
                "return_to_lobby", "settlement：本场结束，计划返回大厅"))
        if ws is WatchState.LOBBY:
            if self.state is ManagerState.ENTER_TABLE:
                self.enter_retries += 1
                if self.enter_retries > self.max_enter_retries:
                    return self._emit({
                        "action": "alert_human", "kind": "alert",
                        "reason": f"enter_table 重试 {self.enter_retries} 次仍在大厅，停手找人",
                    })
                return self._emit(self._tap_plan(
                    "enter_table",
                    f"enter_table 第 {self.enter_retries} 次重试：仍在大厅"))
            self.state = ManagerState.LOBBY
            plan = self._tap_plan("enter_table", "lobby：计划点桌子图进桌")
            self.state = ManagerState.ENTER_TABLE
            plan["manager_state"] = self.state.value
            return self._emit(plan)
        if ws is WatchState.WAITING_ROOM:
            self.state = ManagerState.WAITING_ROOM
            self.enter_retries = 0
            return self._emit({
                "action": "wait_for_session_entry", "kind": "none",
                "reason": ("waiting_room：只读管家不点击；用户显式启动的专用场次入口"
                           "负责身份/玩法/对手/按钮复核与单次开始"),
            })
        if ws is WatchState.IN_GAME:
            self.state = ManagerState.IN_GAME
            self.enter_retries = 0
            return self._emit({
                "action": "monitor", "kind": "none",
                "reason": "in_game：局内由代打循环负责，管家只观察",
            })
        # UNKNOWN：不推进、不点击，只等待下一帧证据。
        return self._emit({
            "action": "wait", "kind": "none",
            "reason": "unknown：证据不足，等待下一帧，不做任何计划动作",
        })


if __name__ == "__main__":  # pragma: no cover - 手工离线出报入口
    import argparse

    parser = argparse.ArgumentParser(description="session_watch 离线战报")
    parser.add_argument("session", help="会话目录或会话根目录")
    parser.add_argument("--out", default=None, help="输出目录（缺省写会话目录）")
    args = parser.parse_args()
    _json_path, _md_path = write_match_report(args.session, args.out)
    print(f"report json: {_json_path}")
    print(f"report md:   {_md_path}")
