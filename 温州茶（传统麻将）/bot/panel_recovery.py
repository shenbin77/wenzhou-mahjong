# -*- coding: utf-8 -*-
"""胡提示面板「待二击」状态的持久化与重启恢复判定（纯逻辑，零 ADB）。

背景（Codex 交接日志 2026-10-25 明示未完成项）：出牌第一击点选建议牌后
会弹出中央「胡」提示面板，已选牌抬起（有时移出常规手牌 ROI），执行器在
同一次 ``execute()`` 内沿用第一击坐标发第二击确认出牌。若进程恰在两击
之间重启，新进程内存里没有原建议，只剩面板帧 → 旧行为只能一直等待。

本模块补两件事：

1. **状态持久化**：执行器在第一击确实改变目标列后，把待二击状态（牌面、
   第一击坐标与牌框、点击前手牌多重集、局身份指纹+财神、时间戳）写到按
   serial 固定的状态文件。监督器重拉一律用**新**会话目录（见
   session_supervisor 的 relaunch 计划），状态文件放进单次会话目录新进程
   根本找不到，所以固定放在 ``采集/dev_auto_play/_pending_second_tap/
   <serial>.json``，新进程凭 serial 找回。出牌验证成功 / 确认换局时清除，
   防跨局误用。

2. **恢复判定**（:func:`decide_recovery`，纯函数）：新进程在面板帧上只有
   同时满足下列全部条件才允许补发第二击，缺一即弃权：
   - 状态合法、serial 一致、未过期（≤ :data:`MAX_STATE_AGE_S`）；
   - 局身份一致：HUD 指纹容差比对（与 auto_play_loop 的 ``_same_hud``
     同口径 ≤8 字节差异），财神 label 硬冲突即否决；两项都无法核对时
     按不一致处理（跨进程恢复不许猜）；
   - 本帧暗手可完整读（diagnostics 里 accepted 暗手 ≥4 张，且与
     ``hand_ids`` 多重集一致）；
   - 「已选中抬起牌」唯一高置信定位：
     A. 暗手多重集与点击前完全一致，且恰有 1 张框比同帧中位数 y1 抬起
        ≥ :data:`LIFT_PX`，其 tile_id 等于待二击牌、且与第一击牌框 x
        重叠 ≥ 半宽 → 点它**当前**框的中心；
     B. 暗手恰少待二击牌 1 张（已移出常规 ROI），其余框无一抬起，且在
        全部 diagnostics 中恰有 1 条该牌记录抬起到手牌行之上 → 点它
        当前框的中心；找不到 / 不唯一 → 弃权。

判定只产出「允许 + 当前中心点」，真正的点击仍走 TapExecutor 的前台 /
几何白名单 / 点后 −1 验证，本模块不碰 ADB、不降任何门禁阈值
（:data:`LIFT_PX` 与 tap_executor.RAISED_BOX_LIFT_PX 同值同口径；本模块
不 import tap_executor / auto_play_loop，避免循环依赖，改值须同步）。
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from statistics import median

STATE_VERSION = 1
STATE_KIND = "pending_second_tap"
# 状态最长有效期：房间闲置约 4 分钟即被踢散，面板不可能活得更久；
# 给到 10 分钟只为容忍监督重拉的调度延迟，过期一律弃权 + 清除。
MAX_STATE_AGE_S = 600.0
# 与 tap_executor.RAISED_BOX_LIFT_PX 同值同口径（抬起判据），改值须同步。
LIFT_PX = 12
STATE_DIR_NAME = "_pending_second_tap"
_STATE_ROOT = (Path(__file__).resolve().parent.parent
               / "采集" / "dev_auto_play" / STATE_DIR_NAME)


def state_path_for(serial: str, root: str | Path | None = None) -> Path:
    """按 serial 固定的状态文件路径（跨会话目录、跨进程可找回）。"""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(serial or "unknown"))
    base = Path(root) if root is not None else _STATE_ROOT
    return base / f"{safe}.json"


def _int_list(values, count: int | None = None) -> list[int] | None:
    try:
        out = [int(value) for value in values]
    except (TypeError, ValueError):
        return None
    if count is not None and len(out) != count:
        return None
    return out


def build_state(*, serial: str, tile_id: int, tile_label: str,
                tap, box, before_count: int, hand_ids=None, hud_key=None,
                caishen: str | None = None, caishen_id: int | None = None,
                round_seq: int | None = None,
                saved_at: float | None = None) -> dict:
    """组装待二击状态 dict（字段口径见模块 docstring 与 validate_state）。"""
    if isinstance(hud_key, (bytes, bytearray)):
        hud_key = bytes(hud_key).hex()
    return {
        "version": STATE_VERSION,
        "kind": STATE_KIND,
        "serial": str(serial),
        "tile_id": int(tile_id),
        "tile_label": str(tile_label or ""),
        "tap": _int_list(tap, 2) if tap is not None else None,
        "box": _int_list(box, 4) if box is not None else None,
        "before_count": int(before_count),
        "hand_ids": _int_list(hand_ids) if hand_ids else None,
        "hud_key": str(hud_key) if hud_key else None,
        "caishen": str(caishen) if caishen else None,
        "caishen_id": int(caishen_id) if caishen_id is not None else None,
        "round_seq": int(round_seq) if round_seq is not None else None,
        "saved_at": float(saved_at if saved_at is not None else time.time()),
        "source": "tap_executor.execute",
    }


def validate_state(state) -> str | None:
    """状态 dict 的格式校验；返回问题短码，合法返回 ``None``。"""
    if not isinstance(state, dict):
        return "not_a_dict"
    if state.get("version") != STATE_VERSION:
        return "version"
    if state.get("kind") != STATE_KIND:
        return "kind"
    if not isinstance(state.get("serial"), str) or not state["serial"]:
        return "serial"
    tile_id = state.get("tile_id")
    if not isinstance(tile_id, int) or isinstance(tile_id, bool):
        return "tile_id"
    if _int_list(state.get("box"), 4) is None:
        return "box"
    before = state.get("before_count")
    if not isinstance(before, int) or isinstance(before, bool) or before < 2:
        return "before_count"
    saved_at = state.get("saved_at")
    if not isinstance(saved_at, (int, float)) or isinstance(saved_at, bool):
        return "saved_at"
    return None


def write_state(path: str | Path, state: dict) -> bool:
    """原子落盘（同目录 tmp + os.replace）；失败只返回 False，绝不抛。"""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, target)
        return True
    except OSError:
        return False


def read_state(path: str | Path) -> tuple[dict | None, str | None]:
    """读状态文件：``(state, None)`` / ``(None, None)`` 文件不存在 /
    ``(None, 错误说明)`` 文件存在但不可读或格式无效。"""
    target = Path(path)
    if not target.is_file():
        return None, None
    try:
        state = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"状态文件不可读: {exc}"
    problem = validate_state(state)
    if problem:
        return None, f"状态文件格式无效: {problem}"
    return state, None


def clear_state(path: str | Path) -> bool:
    """删除状态文件（不存在不算失败）；IO 异常返回 False，绝不抛。"""
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _same_hud_bytes(first: bytes, second: bytes) -> bool:
    """与 auto_play_loop._same_hud 同口径：等长且 ≤8 个字节不同。"""
    if len(first) != len(second):
        return False
    return sum(a != b for a, b in zip(first, second)) <= 8


def identity_mismatch(state: dict, hud_key: bytes | None,
                      caishen: str | None) -> str | None:
    """局身份核对：``None`` = 一致；否则返回不一致原因。

    硬冲突：财神双方都有且不同 → 不一致（哪怕指纹相同）。
    正核对：指纹双方都有 → 容差比对；指纹缺一 → 用财神相等兜底。
    两项都核对不了 → 按不一致处理（跨进程恢复不许猜）。
    """
    state_caishen = state.get("caishen") or None
    if state_caishen and caishen and state_caishen != caishen:
        return f"caishen {state_caishen!r} != {caishen!r}"
    state_hud = None
    hud_hex = state.get("hud_key")
    if hud_hex:
        try:
            state_hud = bytes.fromhex(str(hud_hex))
        except ValueError:
            return "state hud_key 非法"
    if state_hud is not None and hud_key is not None:
        if not _same_hud_bytes(state_hud, bytes(hud_key)):
            return "hud 指纹不一致"
        return None
    if state_caishen and caishen and state_caishen == caishen:
        return None
    return "局身份无法核对（指纹/财神至少缺一项）"


def _diagnostic_items(result, *, concealed_only: bool) -> list[dict]:
    """从 analyze 结果的 vision_diagnostics 抽 (tile_id, box) 列表。

    ``concealed_only=True`` 时只收 accepted 的暗手条目（口径与
    auto_play_loop.locate_drawn_tile 一致）；按 x0 排序返回。
    """
    items: list[dict] = []
    for entry in (result or {}).get("vision_diagnostics") or []:
        if not isinstance(entry, dict):
            continue
        if concealed_only:
            if entry.get("region") not in (None, "concealed"):
                continue
            if not entry.get("accepted"):
                continue
        tile_id = entry.get("tile_id")
        box = entry.get("box")
        if not isinstance(tile_id, int) or isinstance(tile_id, bool):
            continue
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        items.append({"tile_id": int(tile_id),
                      "box": [int(value) for value in box]})
    items.sort(key=lambda item: item["box"][0])
    return items


def accepted_concealed_items(result) -> list[dict]:
    """本帧 accepted 暗手条目 ``[{tile_id, box}]``（x0 升序）。"""
    return _diagnostic_items(result, concealed_only=True)


def all_diagnostic_items(result) -> list[dict]:
    """本帧全部 diagnostics 条目（不限 region / accepted）。"""
    return _diagnostic_items(result, concealed_only=False)


@dataclass(frozen=True)
class RecoveryDecision:
    """恢复判定结果：allowed 时 box/tap_point 是当前帧重定位的牌框与中心。"""

    allowed: bool
    reason: str
    case: str | None = None
    tile_id: int | None = None
    box: list[int] | None = None
    tap_point: tuple[int, int] | None = None


def decide_recovery(state: dict, result: dict, *,
                    hud_key: bytes | None = None,
                    caishen: str | None = None,
                    now: float | None = None,
                    max_age_s: float = MAX_STATE_AGE_S) -> RecoveryDecision:
    """面板帧上的待二击恢复判定（纯函数，条件清单见模块 docstring）。"""

    def deny(reason: str) -> RecoveryDecision:
        return RecoveryDecision(False, reason)

    problem = validate_state(state)
    if problem:
        return deny(f"state_invalid:{problem}")
    moment = float(now if now is not None else time.time())
    age = moment - float(state["saved_at"])
    if age < -5.0 or age > max_age_s:
        return deny(f"state_stale age={age:.0f}s")
    mismatch = identity_mismatch(state, hud_key, caishen)
    if mismatch:
        return deny(f"identity_mismatch:{mismatch}")
    hand_ids = state.get("hand_ids")
    if not hand_ids:
        return deny("state_hand_unknown")
    tile_id = int(state["tile_id"])
    state_box = [int(value) for value in state["box"]]
    items = accepted_concealed_items(result)
    if len(items) < 4:
        return deny("hand_not_readable")
    result_ids = (result or {}).get("hand_ids")
    if result_ids is not None:
        try:
            diagnostics_ids = sorted(item["tile_id"] for item in items)
            if sorted(int(tile) for tile in result_ids) != diagnostics_ids:
                return deny("diagnostics_hand_mismatch")
        except (TypeError, ValueError):
            return deny("diagnostics_hand_mismatch")
    median_y1 = float(median([item["box"][3] for item in items]))
    raised = [item for item in items
              if item["box"][3] <= median_y1 - LIFT_PX]
    hand_counter = Counter(int(tile) for tile in hand_ids)
    item_counter = Counter(item["tile_id"] for item in items)
    # 情形 A：整手仍在（含待二击牌），恰有一张抬起。
    if item_counter == hand_counter:
        if len(raised) != 1:
            return deny(f"raised_not_unique count={len(raised)}")
        target = raised[0]
        if target["tile_id"] != tile_id:
            return deny("raised_tile_mismatch")
        overlap = (min(target["box"][2], state_box[2])
                   - max(target["box"][0], state_box[0]))
        if overlap * 2 < (state_box[2] - state_box[0]):
            return deny("raised_column_drift")
        box = list(target["box"])
        return RecoveryDecision(
            True, "唯一抬起牌与待二击状态一致", case="in_roi_lifted",
            tile_id=tile_id, box=box,
            tap_point=((box[0] + box[2]) // 2, (box[1] + box[3]) // 2))
    # 情形 B：待二击牌已移出常规暗手，其余整手守恒且无一抬起，
    # 在全部 diagnostics 里把那张牌唯一定位出来才许点。
    expected = hand_counter - Counter([tile_id])
    if item_counter == expected and not raised:
        lifted = [item for item in all_diagnostic_items(result)
                  if item["tile_id"] == tile_id
                  and item["box"][3] <= median_y1 - LIFT_PX]
        if len(lifted) != 1:
            return deny(
                f"raised_tile_not_uniquely_located count={len(lifted)}")
        box = list(lifted[0]["box"])
        return RecoveryDecision(
            True, "移出 ROI 的待二击牌被唯一定位", case="out_of_roi_located",
            tile_id=tile_id, box=box,
            tap_point=((box[0] + box[2]) // 2, (box[1] + box[3]) // 2))
    return deny("hand_mismatch")
