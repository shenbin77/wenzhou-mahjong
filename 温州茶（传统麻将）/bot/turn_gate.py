# -*- coding: utf-8 -*-
"""turn_gate.py — 回合所有权门禁（纯函数，零副作用）。

用途
====
``auto_play_loop.tick()`` 在进入 dry_run 计划或 ``TapExecutor.execute()`` 之前，
必须先用本模块判定「当前这一帧确实轮到我方，且已经到了出牌态」。本模块只读
``result``（``assist_tea.analyze_snapshot`` 的返回体）、当前帧 ``image`` 与调用方
维护的 ``history``；**不截图、不调 ADB、不写文件、不做任何点击**，因此可以完全
用单元测试覆盖。

放行条件（``discard_ready``，缺一条都不放行）
============================================
判定顺序（前面的先判，顺序本身是安全语义的一部分）：

``settled → mode → status/advise/suggested_boxes → caishen → 亮度(压暗→wait)
→ 物理张数 → accepted<physical(→wait) → 稳定性 → ready``

0. **画面不是结算/结束/模态弹窗**（最优先，见下节）：命中即 ``blocked``；
1. ``result["mode"] == "tea_2p"``（4 人局未验收）；
2. ``result["status"] == "advise"`` 且 ``suggested_boxes`` 非空；
3. 本局财神已知（``caishen_id`` 可解析）；
4. **手牌未压暗**：手牌框区域面亮度 p70(V) 均值不得低于
   :data:`MIN_HAND_FACE_MEAN`。压暗帧必然伴随识别不全，所以它必须排在张数
   之前——否则"压暗"会被误标成"张数矛盾"（= 误停机）；
5. **张数守恒（用物理牌框数，不用 accepted 数）**：
   ``physical = len(concealed_boxes(result))``，``expected = 17 - meld_total``。

   * ``physical == expected - 1`` → ``wait``（未摸牌/不是出牌态）；
   * ``physical == expected`` 且 ``accepted < physical`` → **``wait``**
     （有牌未读出：这是"没读全"，绝不许 ``blocked``）；
   * ``physical == expected`` 且 ``accepted == physical`` → 继续；
   * ``expected < 0``，或 ``physical`` 与 ``expected`` 差 ≥2 → ``blocked``
     （真矛盾，fail closed）。

6. **画面稳定**：连续 ≥2 帧（含当前帧）的手牌多重集与暗手张数完全一致，
   用于抵抗摸牌/发牌动画。历史由调用方传入，本模块只读；
7. **换局第一帧不放行**：由调用方（``auto_play_loop``）在 ``round_reset`` 时清空
   历史并跳过该帧，本模块用 ``round_seq`` 隔离不同局的历史，绝不跨局比较。

台账证据里 ``physical_boxes``（物理牌框数）与 ``accepted_tiles``（已接受张数）
分开记，能一眼看出是"没读全"还是"真矛盾"。

亮度阈值实测依据
================
统计口径：对每个暗手框取 ``tile_vision.face_region`` 后的 ``p70(V)``，再对整手
取均值（与 ``tile_vision._FACE_BG_REFERENCE`` 同一套「面亮度」定义，**只用于
判回合，不参与识牌**，整手压暗帧的识牌弃权契约不受影响）。

实测样本：9 个真实采集目录共 **1074 帧** 1600×900 原生截图

* ``采集/dev_auto_play/takeover_live/*.png``
* ``采集/dev_auto_play/20261024_041701_0df7a7/frames/*.png``
* ``采集/assist_sessions/20261023_201640/live/tea_2p/*.png``
* ``采集/assist_sessions/20261023_235121/live/tea_2p/*.png``
* ``采集/assist_sessions/20261024_002903/live/tea_2p/*.png``
* ``采集/2026-10-19/833667/r01``、``r02``
* ``采集/2026-10-16/740195/r08``
* ``采集/2026-10-15/424821/r08``

结果呈双峰、中间无重叠：

* 压暗簇（「请出牌」整手压暗）55 帧，均值区间 **65.00 – 135.57**；例如
  ``采集/assist_sessions/20261023_201640/live/tea_2p/20261023_205534_475580_f4a34e.png``
  = 116.29（14 框）、``采集/2026-10-19/833667/r02/frame_10s.png`` = 105.06（16 框）、
  ``采集/2026-10-15/424821/r08/frame_01.png`` = 107.12（16 框）；
* 正常簇 1019 帧，均值区间 **168.40 – 230.00**；例如
  ``采集/2026-10-16/740195/r08/frame_02.png`` = 214.00（17 框）、
  ``采集/dev_auto_play/takeover_live/20261024_035718_000.png`` = 214.00（16 框）、
  ``采集/assist_sessions/20261023_201640/live/tea_2p/20261023_205345_749900_23750c.png``
  = 203.64（11 框 + 2 副露）。

取两簇中点为阈值 **150.0**：比最高压暗帧高 14.43，比最低正常帧低 18.40。
注意「财神高亮」只把**单张**压到 ~100，其余牌仍 214，17 张均值 207.29
（``采集/2026-10-15/424821/r08/frame_02.png``），不会误判成整手压暗。

结算/结束/模态弹窗判据（第 9 条，最优先）
=========================================
真实证据帧 ``采集/dev_auto_play/takeover_live/wb_probe_043707.png``（1600×900）：
顶部「牌局结束」横幅 + HUD「房间号: 869684 局数: 3/6」+ 中央模态弹窗
「提示 / 多数玩家同意解散比赛！/ 确定」+ 下方结算面板（−20/+20）+
底部「返回大厅」「分享战绩」。该帧**不存在可出牌手牌**，任何点击都可能落到
「确定」（解散/开局类动作），属明令禁止的盲点；而弹窗压下来时手牌仍可能被
数出 16/17 张，所以必须**先判画面类型、再判张数**。

两个互相独立、可测的强特征，**同时成立 2 条才判 blocked**（避免误伤）：

* ``central_panel``：中央 ROI ``x∈[18%,82%] y∈[18%,82%]`` 内，最大一块
  「亮奶油色」连通域（``V≥195 且 S≤90``）占**整幅画面**的比例 ≥ 4.5%。
  模态弹窗 / 结算面板 / 战绩页都是这种整块浅色大矩形。
* ``bottom_blackout``：手牌带 ``y≥85%`` 的平均 ``V`` < 45。整幅画面被半透明
  遮罩压暗时手牌区几乎全黑（正常出牌态该处 ~170-233，「请出牌」整手压暗也只
  掉到 ~60-135）；同一帧的 ``frame_mean_v`` 一并记入台账便于对照。

本证据帧实测：``panel_area_pct=14.19``、``bottom_mean_v=23.29``、
``frame_mean_v=67.0``（正常帧对应 0.22% / 180.05 / 113.9-162.6），两条特征
同时成立 → ``blocked``（``code="gate_settled"``）。只命中 1 条的帧记 ``wait``
并写明"疑似结算/非对局画面"，**任何情况下都不会是 discard_ready**。

实测样本：9 个采集目录共 **1138 帧** 1600×900 原生截图：

* ``central_panel``：正常对局帧最大 **2.79%**
  （``20261024_004330_506441_e544aa.png``），命中帧最小 **6.28%**
  （``20261023_205820_628578_3698f4.png``，「胡」结算面板）；阈值 4.5% 两侧
  各留 1.71 / 1.78 个百分点。全库命中 **52 帧**，逐帧核验全部是非对局画面：
  牌局结束+解散弹窗 14.19%、申请续桌 25.88%、胡面板 6.28%、比赛战绩 26.52%、
  我的比赛场大厅 9.24%、比赛场列表 9.10%。
* ``bottom_blackout``：正常对局帧最低 **60.00**
  （``20261024_003929_195772_acfd9b.png``），命中帧 23.30（本证据帧）与
  36.10（申请续桌）；阈值 45 两侧各留 8.90 / 9.00。
* 两信号并集同为这 52 帧，**正常对局帧 0 误报**；按"同时成立 2 条"定案时
  ``blocked`` 只有 2 帧（本证据帧 + 申请续桌），其余 50 帧只命中
  ``central_panel`` 一条 → 记 ``wait``（同样永不放行）。
* 顶部「牌局结束」横幅在遮罩下 V 只有 ~110-135（该 ROI 内 V≥140 占比实测
  **0.0%**），单独当判据会漏报，故不采用——已由上面两条覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import cv2
import numpy as np

from tile_vision import TILE_IDS, face_background

# ---------------------------------------------------------------- 常量
MODE_TEA_2P = "tea_2p"
TILES_PER_HAND = 17          # 温州茶 2 人局：轮到出牌时暗手满张
MELD_TILE_COUNT = 3          # 吃 / 碰 各 3 张
KONG_TILE_COUNT = 4          # 杠 4 张（含叠杠：3 个可见框代表 4 张）
STABLE_FRAMES = 2            # 抗摸牌/发牌动画：连续 2 帧手牌一致才算稳定
MAX_HISTORY = 4              # 只留最近几帧，够判稳定即可
MIN_HAND_FACE_MEAN = 150.0   # 手牌面亮度 p70(V) 均值下限（实测见模块 docstring）

# 结算/结束/模态弹窗判据（实测见模块 docstring）
SETTLED_PANEL_MIN_PCT = 4.5       # 中央最大浅色连通域占整幅画面的最小比例（%）
SETTLED_DARK_BOTTOM_BELOW = 45.0  # 手牌带 y≥85% 平均 V 低于此值 = 被遮罩压黑
REQUIRED_SETTLED_SIGNALS = 2      # 至少两条特征同时成立才判 blocked（防误伤）
CREAM_V_MIN = 195                 # 「亮奶油色」= 弹窗/结算面板底色
CREAM_S_MAX = 90
CENTRE_ROI = (0.18, 0.18, 0.82, 0.82)   # x0, y0, x1, y1（画面比例）
BOTTOM_ROI_Y = 0.85
VERIFIED_FRAME_SIZE = (900, 1600)       # (height, width)，与 capture 的校验一致

STATE_DISCARD_READY = "discard_ready"
HINT_PANEL_CODE = "hint_panel"   # 疑似胡提示面板（听牌提示条）
STATE_WAIT = "wait"
STATE_BLOCKED = "blocked"


@dataclass(frozen=True)
class TurnVerdict:
    """门禁判定结果。

    Attributes:
        state: ``discard_ready`` / ``wait`` / ``blocked``。
        reason: 人类可读的依据，直接进台账，便于事后复核。
        evidence: 本帧实测值（张数、``meld_total``、稳定性、亮度、弹窗信号等）。
        code: 停机归类（如 ``gate_settled``）；空串表示用调用方的默认归类。
    """

    state: str
    reason: str
    evidence: dict = field(default_factory=dict)
    code: str = ""

    @property
    def ready(self) -> bool:
        """是否为可执行出牌态。"""
        return self.state == STATE_DISCARD_READY

    def ledger(self) -> dict:
        """台账用摘要（JSON 可序列化，不含整手牌表）。"""
        return {"state": self.state, "reason": self.reason, "code": self.code,
                "evidence": dict(self.evidence)}


@dataclass(frozen=True)
class HandObservation:
    """一帧的手牌观测，用于判定「连续帧画面稳定」。

    Attributes:
        tiles: 已接受的暗手牌 id（升序，即手牌多重集）。
        count: 暗手张数。
        round_seq: 所属局序号；跨局历史永不参与比较。
        tick: 循环的帧序号；只有相邻帧才算「连续」。
    """

    tiles: tuple[int, ...]
    count: int
    round_seq: int = 1
    tick: int = 0


def _as_int(value) -> int | None:
    """把可能是 numpy / str 的计数安全转成 int，失败返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def logical_meld_slots(result: dict) -> int:
    """Exposed-meld **logical** tile slots for tea rules (each group = 3).

    A kong still occupies one meld slot of 3 for hand-count accounting
    (P0-1 / P0-2), even when four physical faces are visible.
    """
    melds = result.get("melds")
    if isinstance(melds, (list, tuple)) and melds:
        groups = [g for g in melds if isinstance(g, (list, tuple)) and g]
        return MELD_TILE_COUNT * len(groups)
    count = _as_int(result.get("meld_count")) or 0
    return MELD_TILE_COUNT * max(0, count)


def meld_total(result: dict) -> int:
    """各副露的**实际张数**之和（吃/碰 3、杠 4）。

    优先按 ``result["melds"]`` 的每组长度计；叠杠只暴露 3 个框却代表 4 张，
    由 ``result["stacked_kongs"]`` 的 ``logical_count == 4`` 补 1。
    ``melds`` 缺失时回落 ``3 * meld_count``（旧返回体口径）。
    """
    melds = result.get("melds")
    if isinstance(melds, (list, tuple)) and melds:
        total = 0
        for group in melds:
            if not isinstance(group, (list, tuple)):
                continue
            size = len(group)
            # 超过 4 张的组只按 4 计（同种牌最多 4 张），异常组照实计入总数，
            # 让张数守恒去判 blocked，而不是在这里"修"成合理值。
            total += KONG_TILE_COUNT if size >= KONG_TILE_COUNT else size
        for kong in result.get("stacked_kongs") or []:
            if isinstance(kong, dict) and _as_int(kong.get("logical_count")) == KONG_TILE_COUNT:
                total += 1
        return total
    count = _as_int(result.get("meld_count")) or 0
    return MELD_TILE_COUNT * max(0, count)


def known_caishen_id(result: dict) -> int | None:
    """解析本局财神牌 id；``None`` 表示财神未知。

    ``analyze_snapshot`` 的返回体目前给的是 ``caishen`` label（如 ``"t2"``）与
    ``caishen_source``，而回放/离线结果里可能直接带 ``caishen_id``。两者都认：
    ``caishen_id`` 优先，其次按 label 查 :data:`tile_vision.TILE_IDS`。
    """
    explicit = _as_int(result.get("caishen_id"))
    if explicit is not None:
        return explicit
    label = result.get("caishen")
    if isinstance(label, str) and label:
        return TILE_IDS.get(label)
    return None


def concealed_boxes(result: dict) -> list[list[int]]:
    """从 ``vision_diagnostics`` 取出暗手牌框（``region`` 为 exposed 的不算）。"""
    boxes: list[list[int]] = []
    for item in result.get("vision_diagnostics") or []:
        if not isinstance(item, dict):
            continue
        if item.get("region") not in (None, "concealed"):
            continue
        box = item.get("box")
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        try:
            boxes.append([int(value) for value in box])
        except (TypeError, ValueError):
            continue
    return boxes


def hand_face_brightness(image, boxes: Sequence[Sequence[int]]) -> float | None:
    """手牌框区域面亮度 p70(V) 的**均值**；无可用证据时返回 ``None``。

    Args:
        image: 当前帧（BGR ``numpy`` 数组）。
        boxes: 暗手牌框（xyxy 像素坐标）。

    Returns:
        均值（0-255），或 ``None``（图/框不可用）。
    """
    if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
        return None
    if boxes is None or len(boxes) == 0:
        return None
    height, width = image.shape[:2]
    values: list[float] = []
    for box in boxes:
        x0, y0, x1, y1 = box
        x0, y0 = max(0, min(int(x0), width)), max(0, min(int(y0), height))
        x1, y1 = max(0, min(int(x1), width)), max(0, min(int(y1), height))
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        crop = image[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        try:
            # 与 tile_vision 的亮度补偿同一把尺子：face_region 之后的 p70(V)。
            # 这里**只测亮度判回合**，绝不把补偿后的像素喂给识牌。
            values.append(float(face_background(crop)))
        except (ValueError, cv2.error):
            continue
    if not values:
        return None
    return float(np.mean(values))


def detect_settled_screen(image) -> tuple[bool, dict]:
    """识别「结算 / 牌局结束 / 模态弹窗」画面（只读像素统计，不 OCR）。

    这类画面上手牌仍可能被识别出 16/17 张，所以不能用张数判断；这里用两个
    互相独立的强特征，**同时成立 ≥ ``REQUIRED_SETTLED_SIGNALS``(2) 条**才判
    「结算类画面」（避免正常牌局误伤）：

    1. ``central_panel``：中央 ROI 内最大一块亮奶油色连通域 ≥ 整幅的 4.5%；
    2. ``bottom_blackout``：手牌带 ``y≥85%`` 平均 V < 45（整幅被半透明遮罩
       压暗，可与 :data:`frame_mean_v` 对照）。

    只命中 1 条时返回 ``False``（不判结算），由 :func:`evaluate_turn` 记
    ``wait``——**永远不会**因此放行。

    Args:
        image: 当前帧 BGR 图（只读）。

    Returns:
        ``(hit, evidence)``；``evidence`` 里带实测值，可直接进台账。
        画面不可用/尺寸未验证时返回 ``(False, {"settled_unknown": True, ...})``，
        由后续判据继续把关（亮度不可测 → ``wait``，绝不会放行）。
    """
    evidence: dict = {"settled_signals": [], "panel_area_pct": None,
                      "bottom_mean_v": None, "frame_mean_v": None,
                      "settled_unknown": False,
                      "settled_required_signals": REQUIRED_SETTLED_SIGNALS}
    if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
        evidence["settled_unknown"] = True
        return False, evidence
    height, width = image.shape[:2]
    if (height, width) != VERIFIED_FRAME_SIZE:
        evidence["settled_unknown"] = True
        evidence["frame_size"] = [width, height]
        return False, evidence
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    value = hsv[:, :, 2].astype(np.int16)
    saturation = hsv[:, :, 1].astype(np.int16)
    cream = ((value >= CREAM_V_MIN) & (saturation <= CREAM_S_MAX)).astype(np.uint8)
    x0, y0 = int(width * CENTRE_ROI[0]), int(height * CENTRE_ROI[1])
    x1, y1 = int(width * CENTRE_ROI[2]), int(height * CENTRE_ROI[3])
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        cream[y0:y1, x0:x1], 8)
    largest = max((int(stats[index][cv2.CC_STAT_AREA]) for index in range(1, count)),
                  default=0)
    panel_pct = round(largest / float(height * width) * 100, 2)
    bottom_mean = round(float(value[int(height * BOTTOM_ROI_Y):, :].mean()), 2)
    frame_mean = round(float(value.mean()), 2)
    signals: list[str] = []
    if panel_pct >= SETTLED_PANEL_MIN_PCT:
        signals.append("central_panel")
    if bottom_mean < SETTLED_DARK_BOTTOM_BELOW:
        signals.append("bottom_blackout")
    evidence.update({
        "settled_signals": signals,
        "panel_area_pct": panel_pct,
        "bottom_mean_v": bottom_mean,
        "frame_mean_v": frame_mean,
        "settled_panel_min_pct": SETTLED_PANEL_MIN_PCT,
        "settled_bottom_below": SETTLED_DARK_BOTTOM_BELOW,
    })
    return len(signals) >= REQUIRED_SETTLED_SIGNALS, evidence


def _as_float(value: object, digits: int = 6):
    """JSON-safe float conversion; ``None`` for anything unreadable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def action_ui_evidence(image) -> dict:
    """Per-frame record of what the button area showed and how the mode gate ran.

     watchdog 要求逐帧留一条"这一帧按钮区发生了什么"。这里把两件互相独立的事
    拼成一条扁平字典，直接 ``evidence["action_ui"]`` 进台账：

    * **按钮区**（``claim_vision.read_claims``，每次对本帧重算）：有没有检出行动
      圆盘、几个、在什么位置、按既有的保守规则该读出什么决策。这里**不改变**任
      何门禁结论，只记录。
    * **玩法门禁**（``mode_vision.RULE_GATES`` 的当前状态，两把锁都记）：玩法人数
      锁没锁、锁的是几人、上次确认的分值/次名差；牌集（无中发）锁没锁、上次确认
      的分值；以及各自锁了多久/多少帧、被重置过几次。``mode_source`` /
      ``deck_source`` 为 ``"latched"`` 表示这一帧是**吃粘滞**过关的，而不是逐帧
      重新判定出来的 —— 按钮圆盘压住规则文字时正是这种情况。

    只读：不截图、不点击、不改任何锁状态（``MODE_LATCH.snapshot()`` 是纯读）。
    """
    record: dict = {
        "action_discs_seen": False,
        "action_disc_count": 0,
        "action_discs": [],
        "claim_decision": None,
        "claim_reason": "",
        "countdown_status": "absent",
        "countdown_seconds": None,
        "countdown_evidence": {},
        "action_gold_pixel_ratio": None,
        "mode_latched": False,
        "mode_players": None,
        "mode_source": "unavailable",
        "mode_score": None,
        "mode_margin": None,
        "mode_latch_age_frames": None,
        "mode_latch_age_s": None,
        "mode_latch_resets": None,
        "mode_latch_available": False,
        "deck_latched": False,
        "deck_value": None,
        "deck_score": None,
        "deck_source": "unavailable",
        "deck_score_min": None,
        "deck_latch_available": False,
    }
    if image is not None and isinstance(image, np.ndarray) and image.ndim == 3:
        try:
            from claim_vision import read_claims
        except Exception:  # noqa: BLE001 - 读不出就只出玩法那一半
            read_claims = None
        if read_claims is not None:
            try:
                state = read_claims(image)
            except Exception as exc:  # noqa: BLE001 - 异常 ≠ "没有按钮"
                record["claim_reason"] = f"行动行读取失败：{exc}"
            else:
                record["action_discs_seen"] = bool(state.known)
                record["action_disc_count"] = len(state.buttons)
                record["action_discs"] = [
                    {"kind": str(button.kind),
                     "box": [int(v) for v in button.box],
                     "center": [int(v) for v in button.center]}
                    for button in state.buttons]
                record["claim_decision"] = state.decision
                record["claim_reason"] = str(state.reason)
                ratios = (state.evidence or {}).get("pixel_ratio") or {}
                record["action_gold_pixel_ratio"] = _as_float(ratios.get("gold"))
    # P0-3: real countdown detector (never leave the permanent unknown stub).
    try:
        from countdown_vision import detect_countdown
        reading = detect_countdown(image if isinstance(image, np.ndarray) else None)
        record["countdown_status"] = reading.status
        record["countdown_seconds"] = reading.seconds
        record["countdown_evidence"] = dict(reading.evidence)
    except Exception as exc:  # noqa: BLE001 - detector failure => unreadable
        record["countdown_status"] = "unreadable"
        record["countdown_seconds"] = None
        record["countdown_evidence"] = {"error": f"{exc}"}

    try:
        from mode_vision import DECK_LATCH, DECK_SCORE_MIN, MODE_LATCH
    except Exception:  # noqa: BLE001 - 门禁不可用时如实标注
        return record
    deck_score_min = DECK_SCORE_MIN
    try:
        snapshot = MODE_LATCH.snapshot()
        deck = DECK_LATCH.snapshot()
    except Exception as exc:  # noqa: BLE001 - 可观测性失败不影响门禁结论
        record["claim_reason"] = f"{record['claim_reason']}；玩法门禁状态不可读：{exc}"
        return record
    record.update({
        "mode_latched": bool(snapshot.get("latched")),
        "mode_players": snapshot.get("players"),
        "mode_score": _as_float(snapshot.get("score")),
        "mode_margin": _as_float(snapshot.get("margin")),
        "mode_latch_age_frames": snapshot.get("age_frames"),
        "mode_latch_age_s": _as_float(snapshot.get("age_s"), 3),
        "mode_latch_resets": snapshot.get("resets"),
        "mode_latch_available": True,
        "deck_latched": bool(deck.get("latched")),
        "deck_value": deck.get("value"),
        "deck_score": _as_float(deck.get("score")),
        "deck_score_min": deck_score_min,
        "deck_latch_available": True,
    })
    # ``source`` 只在门禁这一帧真的被问过时才可信；锁存"过期"时标注 expired。
    if snapshot.get("latched"):
        record["mode_source"] = "latched"
    elif snapshot.get("expired"):
        record["mode_source"] = "latch_expired"
    elif snapshot.get("players") is None:
        record["mode_source"] = "not_confirmed"
    else:
        record["mode_source"] = "unavailable"
    if deck.get("latched"):
        record["deck_source"] = "latched"
    elif deck.get("expired"):
        record["deck_source"] = "latch_expired"
    elif deck.get("value") is None:
        record["deck_source"] = "not_confirmed"
    else:
        record["deck_source"] = "unavailable"
    return record


def hand_observation(result: dict, *, round_seq: int = 1,
                     tick: int = 0) -> HandObservation | None:
    """把一帧的暗手牌整理成可比较的观测；不可读时返回 ``None``。"""
    ids = result.get("hand_ids")
    if not isinstance(ids, (list, tuple)):
        return None
    tiles: list[int] = []
    for value in ids:
        tile = _as_int(value)
        if tile is None:
            return None
        tiles.append(tile)
    return HandObservation(tiles=tuple(sorted(tiles)), count=len(tiles),
                           round_seq=_as_int(round_seq) or 1,
                           tick=_as_int(tick) or 0)


def _stable_frames(observation: HandObservation,
                   history: Sequence[HandObservation]) -> int:
    """从最近一帧往回数，连续一致的帧数（含当前帧）。"""
    streak = 1
    for item in reversed(list(history)):
        if (item.round_seq != observation.round_seq
                or item.tick != observation.tick - streak
                or item.tiles != observation.tiles
                or item.count != observation.count):
            break
        streak += 1
    return streak


def evaluate_turn(result: dict, image, history: Sequence[HandObservation] | None = None,
                  *, round_seq: int | None = None,
                  tick: int | None = None) -> TurnVerdict:
    """判定本帧是否轮到我方出牌。

    Args:
        result: ``assist_tea.analyze_snapshot`` 的返回体（只读）。
        image: 当前帧 BGR 图（只读，用于测手牌面亮度）。
        history: 调用方维护的历史观测（最近的在后），只用于判「连续帧稳定」。
        round_seq: 本帧所属局序号；缺省取 ``result["round_seq"]``，再缺省 1。
        tick: 本帧的帧序号；缺省按「历史最后一帧 + 1」推导。只有真正相邻的帧
            才算「连续」——中间夹了一帧不可读画面就不算。

    Returns:
        :class:`TurnVerdict`；``discard_ready`` 才允许进入计划/点击流程。
    """
    history_list = list(history or [])
    sequence = _as_int(round_seq)
    if sequence is None:
        sequence = _as_int(result.get("round_seq")) or 1
    if tick is None:
        frame_index = history_list[-1].tick + 1 if history_list else 0
    else:
        frame_index = _as_int(tick) or 0
    observation = hand_observation(result, round_seq=sequence, tick=frame_index)

    melds = result.get("melds")
    meld_sizes = ([len(group) for group in melds if isinstance(group, (list, tuple))]
                  if isinstance(melds, (list, tuple)) else [])
    melds_used = meld_total(result)
    logical_slots = logical_meld_slots(result)
    meld_count = len(meld_sizes) if meld_sizes else (_as_int(result.get("meld_count")) or 0)
    # Tea rules: expected concealed uses logical slots (kong=3), not physical 4.
    expected = TILES_PER_HAND - logical_slots
    boxes = concealed_boxes(result)
    # 张数守恒用「物理牌框数」而不是「已接受张数」：识别不全（accepted < physical）
    # 是"没读全"，只该 wait，绝不能硬停机。两者分开记进台账便于复核。
    physical = len(boxes)
    accepted = observation.count if observation is not None else 0
    brightness = hand_face_brightness(image, boxes)
    streak = _stable_frames(observation, history_list) if observation is not None else 0
    settled, settled_evidence = detect_settled_screen(image)
    # 看门狗用的按钮区 + 玩法门禁证据（只读，不改变任何判定）。
    action_ui = action_ui_evidence(image)

    evidence: dict = {
        "mode": result.get("mode"),
        "status": result.get("status"),
        "caishen_id": known_caishen_id(result),
        "meld_count": meld_count,
        "meld_total": melds_used,
        "logical_meld_slots": logical_slots,
        "meld_group_sizes": meld_sizes,
        "expected_concealed": expected,
        "physical_boxes": physical,
        "accepted_tiles": accepted,
        "concealed_count": accepted,
        "vision_boxes": physical,
        "hand_face_mean_p70": (round(brightness, 2) if brightness is not None else None),
        "min_hand_face_mean_p70": MIN_HAND_FACE_MEAN,
        "stable_frames": streak,
        "required_stable_frames": STABLE_FRAMES,
        "history_len": len(history_list),
        "round_seq": sequence,
        "action_ui": action_ui,
    }
    evidence.update(settled_evidence)

    def verdict(state: str, reason: str, code: str = "") -> TurnVerdict:
        return TurnVerdict(state=state, reason=reason,
                           evidence=dict(evidence), code=code)

    # 第 0 条：结算/牌局结束/模态弹窗 —— 最先判，且**绝不可能是 discard_ready**。
    # 两条强特征同时成立 → blocked（停机，不轮询）。
    if settled:
        return verdict(
            STATE_BLOCKED,
            "牌局已结束/结算或模态弹窗，停止代打（"
            + "+".join(settled_evidence["settled_signals"])
            + f"；中央浅色面板 {settled_evidence['panel_area_pct']}%、"
            f"手牌带亮度 {settled_evidence['bottom_mean_v']}）："
            "不点确定/返回大厅/分享战绩/开始游戏",
            code="gate_settled")
    # 只命中 1 条：证据不足以定案为结算画面，但也绝不继续轮询放行。
    if settled_evidence["settled_signals"]:
        # 胡提示面板细分：听牌时游戏弹出的奶油色提示条（大红「胡」字 + 听口）
        # 只命中 central_panel 单信号、手牌带仍可读、且本帧读数已被面板
        # 干扰（analyze 非 advise）。它不是结算：仍记 wait 永不放行，但用
        # 独立 code 交给循环计数，持续多帧后走「只摸切刚摸的牌」兜底路径。
        # 双信号结算在上面已 blocked，bottom_blackout 单信号仍走通用支。
        if (settled_evidence["settled_signals"] == ["central_panel"]
                and brightness is not None
                and brightness >= MIN_HAND_FACE_MEAN
                and result.get("status") != "advise"):
            return verdict(
                STATE_WAIT,
                "疑似胡提示面板（听牌提示条）：中央奶油色面板单信号命中"
                "(central_panel)，手牌带仍可读、结算双信号未成立；"
                "本轮不出牌，等待循环的面板兜底判定",
                code=HINT_PANEL_CODE)
        return verdict(
            STATE_WAIT,
            "疑似结算/非对局画面（"
            + "+".join(settled_evidence["settled_signals"])
            + f" 单项命中 < {REQUIRED_SETTLED_SIGNALS} 条）：本轮不出牌")
    if result.get("mode") != MODE_TEA_2P:
        return verdict(STATE_WAIT, f"模式不是 tea_2p（{result.get('mode')!r}），4 人局未验收")
    boxes_suggestion = result.get("suggested_boxes") or []
    if result.get("status") != "advise" or not boxes_suggestion:
        return verdict(STATE_WAIT, f"本帧没有可执行建议（status={result.get('status')!r}）")
    if known_caishen_id(result) is None:
        return verdict(STATE_WAIT, "本局财神未知：不出牌")
    # 亮度先于张数：压暗帧必然伴随识别不全，若先判张数会把"压暗"误标成"张数矛盾"。
    if brightness is None:
        return verdict(STATE_WAIT, "手牌亮度不可测（牌框缺失）：不出牌")
    if brightness < MIN_HAND_FACE_MEAN:
        return verdict(STATE_WAIT,
                       f"手牌压暗，非我方回合（面亮度均值 {brightness:.2f} < "
                       f"{MIN_HAND_FACE_MEAN:.1f}）：不出牌")
    if expected < 0:
        return verdict(STATE_BLOCKED,
                       f"副露张数 {melds_used} 超过一手 17 张，证据自相矛盾")
    if physical == expected - 1:
        return verdict(STATE_WAIT,
                       f"物理 {physical} 张 = 16 - 副露 {melds_used}：未摸牌/不是出牌态")
    if physical != expected:
        return verdict(STATE_BLOCKED,
                       f"张数自相矛盾：物理 {physical} 张牌框，副露 {melds_used} 张，"
                       f"出牌态应为 {expected} 张")
    if accepted > physical:
        # 已接受张数多于物理牌框：证据本身不一致（真实链路不会出现），不硬停机。
        return verdict(STATE_WAIT,
                       f"识别张数 {accepted} 多于物理牌框 {physical}，证据不一致：不出牌")
    if accepted < physical:
        # 有牌没读出来：这是"没读全"，不是"张数矛盾"，绝不许 blocked。
        return verdict(STATE_WAIT,
                       f"有牌未读出（accepted {accepted}/{physical}），不出牌")
    if streak < STABLE_FRAMES:
        return verdict(STATE_WAIT,
                       f"画面尚未稳定（连续一致 {streak} 帧 < {STABLE_FRAMES} 帧，"
                       "可能在摸牌/发牌动画中）")
    # P0-3: claim row visible => do not discard.
    # Countdown low/expired/unreadable => urgency note (still wait, never tap).
    if action_ui.get("action_discs_seen"):
        cd = str(action_ui.get("countdown_status") or "unreadable")
        sec = action_ui.get("countdown_seconds")
        if cd in {"low", "expired", "unreadable"}:
            detail = f"倒计时{cd}" + (f"={sec}s" if sec is not None else "")
            return verdict(
                STATE_WAIT,
                f"行动圆盘可见且{detail}：本帧只许弃权/过，不出牌",
                code="gate_claim_ui_urgent")
        extra = f"（倒计时{cd}" + (f"={sec}s）" if sec is not None else "）")
        return verdict(
            STATE_WAIT,
            "行动圆盘可见：本帧先处理吃碰杠胡/过，不出牌" + extra,
            code="gate_claim_ui")
    return verdict(STATE_DISCARD_READY,
                   f"轮到我方出牌：暗手 {physical} 张 + 副露 {melds_used} 张 = 17，"
                   f"面亮度 {brightness:.2f}，连续 {streak} 帧一致")
