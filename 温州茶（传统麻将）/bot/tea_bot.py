# -*- coding: utf-8 -*-
"""
温州茶 Bot：离线识牌与决策入口。

链路：vision(截图识别) → shanten(向听/决策) → pattern_analyzer(胡牌判定)。

红线：
1. 只允许 dry_run=True，不触发模拟器输入。
2. 图像识别必须完整且为闭手 16/17 张，才进入决策。

模块依赖：
- brain_v0.engine.tea.shanten: 向听/弃牌效率（引擎编码，白=73）
- brain_v0.engine.tea.pattern_analyzer: 胡牌牌型
- tile_vision: 截图→(牌ID,边界框)列表（视觉编码，白=67）

编码转换：vision_to_engine 在 see() 里做。
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

_TEA_ROOT = Path(__file__).resolve().parent.parent
_ENGINE_ROOT = _TEA_ROOT / "代打BOT" / "majiang-bot"
_TEMPLATE_DIR = _TEA_ROOT / "采集" / "templates" / "tea_tiles"
if not _ENGINE_ROOT.is_dir():
    raise RuntimeError(f"茶规则引擎目录不存在: {_ENGINE_ROOT}")
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from brain_v0.engine.tea.shanten import (  # noqa: E402
    best_discard_by_efficiency,
    calc_shanten,
    calc_ting_tiles,
    expected_concealed_count,
    validate_tiles,
)
from brain_v0.engine.tea.pattern_analyzer import analyze as _pa_analyze  # noqa: E402


# ---------------------------------------------------------------------------
# 编码转换（vision → 引擎）
# ---------------------------------------------------------------------------
def vision_to_engine(vid: int) -> int:
    """tile_vision ID → 茶引擎 ID。发 66→72，白 67→73，其余不变。"""
    return {66: 72, 67: 73}.get(vid, vid)


def engine_to_vision(eid: int) -> int:
    """茶引擎 ID → tile_vision ID。"""
    return {72: 66, 73: 67}.get(eid, eid)


def vision_hand_to_engine(vision_ids: Sequence[int]) -> list[int]:
    """整手牌转换。"""
    return [vision_to_engine(v) for v in vision_ids]


# ---------------------------------------------------------------------------
# 牌名（日志用）
# ---------------------------------------------------------------------------
_TILE_NAMES = {}
for i in range(1, 10):
    _TILE_NAMES[i] = f"{i}万"
for i in range(21, 30):
    _TILE_NAMES[i] = f"{i - 20}筒"
for i in range(41, 50):
    _TILE_NAMES[i] = f"{i - 40}条"
_TILE_NAMES.update({61: "东", 62: "南", 63: "西", 64: "北", 71: "中", 72: "发", 73: "白"})
# vision编码的发/白也加上，方便日志
_TILE_NAMES.update({66: "发(v)", 67: "白(v)"})


def tile_name(tid: int) -> str:
    return _TILE_NAMES.get(tid, f"?{tid}")


def hand_str(tiles: Sequence[int]) -> str:
    return " ".join(tile_name(t) for t in sorted(tiles))


# ---------------------------------------------------------------------------
# 决策结果
# ---------------------------------------------------------------------------
@dataclass
class ThinkResult:
    hand: list[int]              # 引擎编码手牌
    n_tiles: int                 # 张数
    shanten: int                 # 向听数（-1=已胡）
    is_hu: bool                  # 是否胡牌
    hu_info: dict = field(default_factory=dict)  # pattern_analyzer结果
    discard: Optional[int] = None     # 建议打出的牌（引擎编码）
    discard_candidates: list = field(default_factory=list)  # [(score, tile)]
    ting_tiles: list = field(default_factory=list)  # 听牌时的进张
    reason: str = ""             # 决策理由（中文）
    elapsed_ms: float = 0.0      # think耗时


# ---------------------------------------------------------------------------
# TeaBot
# ---------------------------------------------------------------------------
class TeaBot:
    """
    温州茶代打Bot。

    仅离线观察/建议，不调用模拟器输入。
    """

    def __init__(
        self,
        dry_run: bool = True,
        caishen_id: Optional[int] = None,
        log_dir: Optional[str] = None,
        # 可注入的vision函数：screenshot_bytes -> vision编码牌ID列表
        # 默认接茶模板识牌；离线测试可注入可信函数
        vision_fn: Optional[Callable[[bytes], list[int]]] = None,
        # 可注入的离线截图函数：() -> PNG字节
        shot_fn: Optional[Callable[[], bytes]] = None,
        player_count: int = 2,
    ):
        if not dry_run:
            raise ValueError("茶 Bot 尚未通过实机定位和回读验证，只允许 dry_run=True")
        if player_count not in (2, 4):
            raise ValueError("茶玩法人数只能是 2 或 4")
        self.dry_run = dry_run
        self.player_count = player_count
        self.caishen_id = caishen_id  # 引擎编码；None=无财神/未知
        self.vision_fn = vision_fn or self._default_vision
        self._vision = None
        self.shot_fn = shot_fn

        # 日志
        self.log_dir = Path(log_dir) if log_dir else Path("logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"tea_bot_{ts}.log"
        self._setup_logger()

        self.round_no = 0
        self.step_no = 0
        self.meld_count = 0
        self.meld_groups: list[list[int]] = []
        # 最近一帧的按钮区 + 玩法门禁证据（看门狗用的可观测出口）。
        self.last_action_ui: dict = {}

    def _setup_logger(self):
        self.logger = logging.getLogger(f"TeaBot-{id(self)}")
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            fh = logging.FileHandler(self.log_file, encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s"
            ))
            self.logger.addHandler(fh)
            sh = logging.StreamHandler()
            sh.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(sh)

    def log(self, msg: str):
        self.logger.info(msg)

    # -- 截图 --
    def _shot(self) -> bytes:
        if self.shot_fn:
            return self.shot_fn()
        raise RuntimeError("未提供离线截图；请传 screenshot 或注入 shot_fn")

    # 两道玩法门禁粘滞都不可用时的 action_ui 兜底骨架：字段一个不少，值全是
    # "没结论"，让下游按 key 取数永远不会 KeyError。
    _NEUTRAL_ACTION_UI: dict = {
        "action_discs_seen": False, "action_disc_count": 0, "action_discs": [],
        "claim_decision": None, "claim_reason": "", "action_gold_pixel_ratio": None,
        "deck_score": 0.0, "deck_score_min": 0.94, "deck_latched": False,
        "deck_source": "unknown",
        "mode_score": 0.0, "mode_margin": 0.0, "mode_score_min": 0.92,
        "mode_margin_min": 0.08, "mode_raw_players": None, "mode_players": None,
        "mode_source": "per_frame", "mode_latched": False, "mode_latch_resets": 0,
        "mode_latch_age_frames": 0, "mode_latch_age_s": 0.0,
    }

    def _read_tea_rules(self, image, detect_players,
                        detect_no_zhong_fa) -> tuple[Optional[int], bool, dict]:
        """一次读透本帧的两道玩法规则，返回 ``(players, no_zhong_fa, action_ui)``。

        两道门禁（人数 / 牌集无中发）判的都是"这间房用什么规则"，在一局里都不会变，
        但它们的取样区会被吃/碰/杠/胡 的行动圆盘压住，导致逐帧判定一时读不出 →
        整帧弃权。这里统一走 ``mode_vision.RULE_GATES`` 的粘滞结论。

        ``assist_tea.analyze_snapshot`` 每帧 new 一个 TeaBot，所以粘滞状态放在
        ``mode_vision`` 的进程级 **gates 容器**里，不放在 self 上。**两把锁共用
        同一份局身份和同一次作废动作**——不会出现"人数锁重_confirmed了、牌集锁
        还拿着上一局结论"这种最危险的组合。

        fail-closed 不变：粘滞给不出结论时一律退回逐帧口径。粘滞只会**补**上
        "读不出但其实早确认过"的帧，永远不会把"没确认过"的画面变成已确认。

        兼容：部分测试用 ``SimpleNamespace`` 顶替整个 ``mode_vision`` 模块（只有
        那两个 detect 函数），那时取不到 gates，就老实走逐帧口径，行为与修改前
        完全一致。

        Args:
            image: 已解出的 BGR 帧。
            detect_players: 逐帧人数判定函数（粘滞不可用/无结论时的事实来源）。
            detect_no_zhong_fa: 逐帧牌集规则判定函数，同上。

        Returns:
            ``(players, no_zhong_fa, action_ui)``；``players`` 为 2/4 或 ``None``，
            ``no_zhong_fa`` 为 True/False（False 一律当作"未确认"去弃权）。
        """
        raw_players = detect_players(image)
        raw_deck = bool(detect_no_zhong_fa(image))
        action_ui = dict(self._NEUTRAL_ACTION_UI)
        action_ui.update({"mode_raw_players": raw_players,
                          "mode_players": raw_players,
                          "deck_score": 0.0})
        try:
            from mode_vision import RULE_GATES
        except Exception:  # noqa: BLE001 - 模块被替身顶掉：按逐帧口径走
            return raw_players, raw_deck, action_ui
        try:
            mode, deck = RULE_GATES.observe(image)
        except Exception as exc:  # noqa: BLE001 - 粘滞出错不许拖垮整帧
            action_ui["claim_reason"] = f"玩法粘滞门禁异常，退回逐帧：{exc}"
            return raw_players, raw_deck, action_ui
        action_ui = dict(mode.action_ui)
        action_ui["mode_raw_players"] = raw_players
        # 粘滞没结论 → 按逐帧口径：此刻两者理应都是"没确认"（兼任替身兜底）。
        players = raw_players if mode.players is None else mode.players
        no_zhong_fa = raw_deck if deck.value is None else bool(deck.value)
        action_ui["mode_players"] = players
        action_ui["deck_latched"] = deck.latched
        action_ui["deck_source"] = deck.source
        return players, no_zhong_fa, action_ui

    def _default_vision(self, png: bytes) -> list[int]:
        """对采集截图做保守识别；任何漏牌、明牌组或未知牌均停止。"""
        import cv2
        import numpy as np

        bot_dir = str(Path(__file__).resolve().parent)
        if bot_dir not in sys.path:
            sys.path.insert(0, bot_dir)
        from tile_vision import TileVision
        from mode_vision import detect_tea_no_zhong_fa, detect_tea_player_count

        image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("截图不是有效 PNG 图像")
        # 玩法门禁（人数 + 牌集无中发）：走 mode_vision 的粘滞结论。粘滞/逐帧
        # 两种口径都读不出来的帧，行为跟修改前一样是弃权，绝不放宽门槛。
        # action_ui 是本帧按钮区 + 两道门禁的分值/是否走了粘滞，给看门狗逐帧留证。
        players, no_zhong_fa, action_ui = self._read_tea_rules(
            image, detect_tea_player_count, detect_tea_no_zhong_fa)
        self.last_action_ui = action_ui
        self.log(f"[mode] players={players} source={action_ui.get('mode_source')} "
                 f"score={action_ui.get('mode_score')} "
                 f"latched={action_ui.get('mode_latched')} "
                 f"deck={no_zhong_fa}/{action_ui.get('deck_source')} "
                 f"deck_latched={action_ui.get('deck_latched')} "
                 f"discs={action_ui.get('action_disc_count')}")
        if players != self.player_count:
            raise ValueError(
                f"画面为茶 {players} 人局，停止 {self.player_count} 人决策"
                if players in (2, 4) else
                f"画面未确认茶 {self.player_count} 人局，停止决策"
            )
        if not no_zhong_fa:
            raise ValueError("未确认无中发规则，当前茶牌集停止决策")
        if self._vision is None:
            self._vision = TileVision(template_dir=_TEMPLATE_DIR)
        # 本局财神已知时告诉识别器：带"财"角标的那张牌可以按财神计入闭手。
        # caishen_id 为 None（未确认财神）时不传 label，角标牌仍按未识别处理。
        caishen_label = None
        if self.caishen_id is not None:
            from tile_vision import TILE_IDS
            want = engine_to_vision(self.caishen_id)
            caishen_label = next((name for name, tid in TILE_IDS.items()
                                  if tid == want), None)
        self._vision.caishen_label = caishen_label
        recognized = self._vision.recognize_hand(image)
        if not self._vision.last_complete:
            raise ValueError("识牌不完整，停止决策")
        self.meld_count = 0
        self.meld_groups = []
        if self._vision.last_meld_boxes:
            melds = getattr(self._vision, "last_melds", [])
            ids = [row[0] for row in melds]
            boxes = [tuple(int(v) for v in box)
                     for box in self._vision.last_meld_boxes]
            if len(ids) < 3 or len(ids) != len(boxes) \
                    or any(tid is None for tid in ids):
                raise ValueError("明牌组不完整，停止决策")
            sizes = list((getattr(self._vision, "detection_diagnostics", {}) or {})
                         .get("meld_groups") or [])
            if sizes:
                hand_left = (min(int(row[1]) for row in recognized)
                             if recognized else None)
                self.meld_groups = self._meld_groups_by_geometry(
                    ids, boxes, sizes, hand_left=hand_left)
            else:
                # 视觉层没发布组边界（只可能出现在测试替身或旧适配器上）：
                # 退回旧的全组搜索，同时记一行日志，便于发现真正缺字段的适配器。
                self.log("[meld] 视觉层未发布组边界，退回全组搜索")
                self.meld_groups = self._meld_groups_by_global_partition(ids)
            self.meld_count = len(self.meld_groups)
            self.log(f"[meld] groups={self.meld_groups} geometry={sizes}")
        return [row[0] for row in recognized]

    # -- 副露分组：几何组边界是硬约束，组内只判 3 张还是 4 张 --
    @staticmethod
    def _meld_group_shape_ok(group: Sequence[int]) -> bool:
        """副露组牌型：同牌 3/4 张（碰/杠），或同花色连续三张（吃）。"""
        if len(set(group)) == 1:
            return len(group) in (3, 4)
        if len(group) != 3 or len(set(group)) != 3:
            return False
        suits = (range(1, 10), range(21, 30), range(41, 50))
        return any(all(tile in suit for tile in group) for suit in suits) \
            and max(group) - min(group) == 2

    @staticmethod
    def _stacked_kong_center(boxes: Sequence[Sequence[int]]) -> bool:
        """Three face positions with a full middle tile stacked one tile up.

        The 005400 native frames contain both flat 西碰 and stacked 西杠.
        A label triplet alone cannot distinguish them; require the complete
        middle tile to translate vertically while both neighbours stay level.
        """
        if len(boxes) != 3:
            return False
        left, middle, right = boxes
        heights = [box[3] - box[1] for box in boxes]
        widths = [box[2] - box[0] for box in boxes]
        height = min(heights)
        width = min(widths)
        if height <= 0 or width <= 0:
            return False
        rise = min(left[3], right[3]) - middle[3]
        tolerance = max(4.0, height * 0.05)
        return (abs(left[3] - right[3]) <= tolerance
                and rise >= max(16.0, height * 0.15)
                and abs((left[1] - middle[1]) - rise) <= tolerance
                and abs((right[1] - middle[1]) - rise) <= tolerance
                and max(widths) - width <= width * 0.15
                and 0 <= middle[0] - left[2] <= width * 0.15
                and 0 <= right[0] - middle[2] <= width * 0.15)

    @classmethod
    def _meld_groups_by_geometry(cls, ids: Sequence[int],
                                 boxes: Sequence[Sequence[int]],
                                 sizes: Sequence[int],
                                 hand_left: Optional[int] = None) -> list[list[int]]:
        """按视觉层组边界逐组判定，禁止跨组自由组合。

        视觉层 `_split_exposed_prefix` 已按组间间隙把台面切好（组内相邻约 4px、
        组间约 15px、台面到手中断 21-30px），`meld_groups` 就是每组的框数。
        这里只回答"这一组是 3 张还是 4 张、牌型合不合法"，不做全局重排，
        所以同一张牌的多份副本不会被拆到不同组，也不会拼出"3+1"这类非法重组。

        组内张数不是 3/4、或牌型非法时一律停止决策：这种形态说明视觉层漏了
        或错切了牌（例如副露组只有 2 张），宁可弃权也不补一张不存在的牌。
        `hand_left` 是闭手最左框的 x，用来在弃权理由里报出"台面到手中断"的
        真实间距——漏检一张牌时这个间距会比正常值多出约一个牌宽，运维一眼可查。
        """
        if sum(sizes) != len(ids):
            raise ValueError(
                f"明牌组边界{sizes}与识别张数{len(ids)}不符，停止决策")
        groups: list[list[int]] = []
        cursor = 0
        for index, size in enumerate(sizes, start=1):
            left = boxes[cursor][0] if cursor < len(boxes) else -1
            last = cursor + size - 1
            right = boxes[last][2] if 0 <= last < len(boxes) else -1
            chunk = [int(t) for t in ids[cursor:cursor + size]]
            group_boxes = boxes[cursor:cursor + size]
            cursor += size
            if size not in (3, 4):
                detail = ""
                if index == len(sizes) and hand_left is not None and right >= 0:
                    detail = (f"，台面到手间隙{hand_left - right}px，正常 21-30px")
                raise ValueError(
                    f"明牌第{index}组仅{size}张（x≈{left}..{right}{detail}），"
                    f"疑似漏检，停止决策")
            if not cls._meld_group_shape_ok(chunk):
                raise ValueError(f"明牌第{index}组牌型非法{chunk}，停止决策")
            if (size == 3 and len(set(chunk)) == 1
                    and cls._stacked_kong_center(group_boxes)):
                # The fourth physical tile is under the raised middle tile:
                # account for it in the rule engine, not as a fabricated box.
                chunk.append(chunk[0])
            groups.append(chunk)
        return groups

    @classmethod
    def _meld_groups_by_global_partition(cls, ids: Sequence[int]) -> list[list[int]]:
        """旧路径：全组自由搜索，仅当视觉层不发布组边界时兜底使用。"""
        def partitions(offset):
            if offset == len(ids):
                return {()}
            found = set()
            for width in (3, 4):
                group = ids[offset:offset + width]
                if len(group) == width and cls._meld_group_shape_ok(group):
                    found.update((tuple(group),) + rest
                                 for rest in partitions(offset + width))
            return found

        possible = partitions(0)
        if len(possible) != 1:
            raise ValueError("明牌组数量或牌型未确认，停止决策")
        return [list(group) for group in possible.pop()]

    # -- see: 截图→识别→转码 --
    def see(self, screenshot: Optional[bytes] = None) -> list[int]:
        """
        返回引擎编码的手牌ID列表。
        screenshot为None时实时截图；传入字节则用传入的（离线测试用）。
        """
        png = screenshot if screenshot is not None else self._shot()
        vision_ids = self.vision_fn(png)
        engine_ids = vision_hand_to_engine(vision_ids)
        engine_melds = [vision_hand_to_engine(group) for group in self.meld_groups]
        # 张数守恒按逻辑位：每组副露（含杠）只占一个面子=3 逻辑位。
        # 杠的第 4 张不回灌、不额外扣暗手；等待=16-3n，刚摸=17-3n。
        concealed_wait = expected_concealed_count(melds=engine_melds, drawn=False)
        concealed_turn = expected_concealed_count(melds=engine_melds, drawn=True)
        expected = (concealed_wait, concealed_turn)
        if len(engine_ids) not in expected:
            raise ValueError(f"暗手应为{expected[0]}或{expected[1]}张，识别到{len(engine_ids)}张")
        validate_tiles(engine_ids, caishen_id=self.caishen_id,
                       melds=engine_melds)
        self.log(f"[see] vision={hand_str(vision_ids)} → engine={hand_str(engine_ids)}")
        return engine_ids

    # -- think: 决策 --
    def think(self, hand: list[int]) -> ThinkResult:
        t0 = time.time()
        n = len(hand)
        result = ThinkResult(hand=list(hand), n_tiles=n, shanten=99,
                             is_hu=False)
        # Public meld_groups remain in visual encoding for the UI and action
        # ledger; the engine must receive the same encoding as the hand.
        engine_melds = [vision_hand_to_engine(group) for group in self.meld_groups]

        # 与 see()/validate_tiles 同一套逻辑位：杠也只占 3，不按实物 4 扣暗手。
        concealed_wait = expected_concealed_count(melds=engine_melds, drawn=False)
        concealed_turn = expected_concealed_count(melds=engine_melds, drawn=True)
        if n not in (concealed_wait, concealed_turn):
            raise ValueError(f"暗手应为{concealed_wait}或{concealed_turn}张，得到{n}张")
        validate_tiles(hand, caishen_id=self.caishen_id,
                       melds=engine_melds)
        hu_info = (_pa_analyze(hand, caishen_id=self.caishen_id)
                   if n == 17 and not self.meld_groups else {})
        result.hu_info = hu_info

        sh = calc_shanten(hand, caishen_id=self.caishen_id,
                          melds=engine_melds)
        result.shanten = sh
        if self.meld_groups and sh == -1:
            raise RuntimeError("副露胡牌尚未独立验收，停止决策")
        if not self.meld_groups and n == 17 and bool(hu_info.get("hu", False)) != (sh == -1):
            raise RuntimeError("胡牌分析器与向听引擎结论不一致，停止决策")
        result.is_hu = n == 17 and bool(hu_info.get("hu", False))

        if result.is_hu:
            kind_zh = hu_info.get("hu_kind_zh", "")
            mult = hu_info.get("multiplier", "")
            tags = hu_info.get("tags", [])
            result.reason = f"胡牌！{kind_zh} x{mult} tags={tags}"
            result.elapsed_ms = (time.time() - t0) * 1000
            self.log(f"[think] {hand_str(hand)} | {result.reason}")
            return result

        # 2. 听牌时算进张
        if n == concealed_wait and sh == 0:
            result.ting_tiles = sorted(
                calc_ting_tiles(hand, caishen_id=self.caishen_id,
                                melds=engine_melds)
            )

        # 3. 打牌决策（刚摸牌：打一张）
        if n == concealed_turn:
            cands = best_discard_by_efficiency(
                hand, caishen_id=self.caishen_id, melds=engine_melds
            )
            result.discard_candidates = cands
            if cands:
                result.discard = cands[0][1]
                top3 = ", ".join(
                    f"{tile_name(t)}({s:.1f})" for s, t in cands[:3]
                )
                result.reason = (
                    f"向听{sh}，打{tile_name(result.discard)}，"
                    f"候选[{top3}]"
                )
            else:
                result.reason = f"向听{sh}，无候选（异常）"
        elif n == concealed_wait:
            if sh == 0:
                ting = ",".join(tile_name(t) for t in result.ting_tiles)
                result.reason = f"向听0（听牌），进张[{ting}]，等待摸牌"
            else:
                result.reason = f"向听{sh}，等待摸牌"
        else:
            result.reason = f"张数异常{n}，向听{sh}"

        # 副露局面的胡牌/计分判定未经验收：显式标注，绝不静默当成"没胡"。
        if self.meld_groups and not result.is_hu:
            result.reason += "；副露局面胡牌判定未验证"
            if sh == -1:
                result.reason = "疑似胡牌（副露局面胡牌判定未验证，不计分）"

        result.elapsed_ms = (time.time() - t0) * 1000
        self.log(
            f"[think] {hand_str(hand)} | {result.reason} "
            f"({result.elapsed_ms:.1f}ms)"
        )
        return result

    # -- act: 执行 --
    def act(self, result: ThinkResult) -> dict:
        """只输出建议；没有点击入口。"""
        if result.is_hu:
            msg = f"[act] 胡牌！{result.reason}"
            self.log(msg)
            return {"action": "hu", "dry_run": self.dry_run, "msg": msg}

        if result.discard is None:
            msg = f"[act] 无打牌动作（{result.reason}）"
            self.log(msg)
            return {"action": "wait", "dry_run": self.dry_run, "msg": msg}

        tile = result.discard
        msg = f"[act][DRY_RUN] 建议打 {tile_name(tile)}（引擎ID={tile}）| {result.reason}"
        self.log(msg)
        print(msg)
        return {"action": "discard", "tile": tile,
                "dry_run": True, "msg": msg}

    # -- 主循环 --
    def play_one_round(self, max_steps: int = 60,
                       screenshot: Optional[bytes] = None) -> dict:
        """
        打一局。screenshot传入则用固定截图做单步决策（离线测试用）。
        返回本局摘要。
        """
        self.round_no += 1
        self.step_no = 0
        self.log(f"===== 第{self.round_no}局开始（dry_run={self.dry_run}）=====")
        summary = {
            "round": self.round_no,
            "steps": 0,
            "hu": False,
            "final_shanten": None,
            "decisions": [],
        }

        for _ in range(max_steps):
            self.step_no += 1
            try:
                hand = self.see(screenshot)
            except Exception as e:
                self.log(f"[round] see()失败: {e}")
                summary["error"] = str(e)
                break

            if not hand:
                self.log("[round] 未识别到手牌，等待")
                summary["decisions"].append({"step": self.step_no,
                                             "action": "no_hand"})
                if screenshot is not None:
                    break  # 离线单步测试，只跑一次
                time.sleep(2)
                continue

            try:
                result = self.think(hand)
                act_r = self.act(result)
            except Exception as e:
                self.log(f"[round] 决策失败，停止本局: {e}")
                summary["error"] = str(e)
                break
            summary["decisions"].append({
                "step": self.step_no,
                "hand": hand_str(hand),
                "shanten": result.shanten,
                "action": act_r["action"],
                "tile": act_r.get("tile"),
                "reason": result.reason,
                "ms": round(result.elapsed_ms, 1),
            })
            summary["steps"] = self.step_no
            summary["final_shanten"] = result.shanten

            if result.is_hu:
                summary["hu"] = True
                self.log(f"===== 第{self.round_no}局结束：胡牌 =====")
                break

            if screenshot is not None:
                break  # 离线单步测试，只跑一次see→think→act

            # 实战：等对手/自己摸牌，张数变化后再下一轮
            time.sleep(3)

        return summary


# ---------------------------------------------------------------------------
# 离线回放测试
# ---------------------------------------------------------------------------
def offline_replay_test(
    screenshot_dir: str,
    vision_fn: Optional[Callable[[bytes], list[int]]] = None,
    caishen_id: Optional[int] = None,
    max_files: int = 0,
    log_dir: str = "logs",
    player_count: int = 2,
) -> dict:
    """
    用已采集的截图做离线测试（不碰模拟器）。

    screenshot_dir: 截图目录（递归找*.png）
    vision_fn: 截图字节 → vision编码牌ID列表；省略时使用茶模板识别
    player_count: 目标茶局人数，仅支持 2 或 4；画面规则必须与之相符
    返回统计报告。
    """
    shot_dir = Path(screenshot_dir)
    pngs = sorted(shot_dir.rglob("*.png"))
    if max_files:
        pngs = pngs[:max_files]

    bot = TeaBot(dry_run=True, caishen_id=caishen_id,
                 vision_fn=vision_fn, log_dir=log_dir,
                 player_count=player_count,
                 shot_fn=lambda: b"")  # 不用真实截图

    stats = {
        "total": len(pngs),
        "ok": 0,
        "fail": 0,
        "no_hand": 0,
        "hu": 0,
        "shanten_sum": 0,
        "shanten_n": 0,
        "ms_sum": 0.0,
        "ms_n": 0,
        "errors": [],
    }

    for i, p in enumerate(pngs):
        try:
            png_bytes = p.read_bytes()
            summary = bot.play_one_round(screenshot=png_bytes)
            if "error" in summary:
                stats["fail"] += 1
                if len(stats["errors"]) < 10:
                    stats["errors"].append(f"{p.name}: {summary['error']}")
                continue
            stats["ok"] += 1
            for d in summary["decisions"]:
                if d["action"] == "no_hand":
                    stats["no_hand"] += 1
                else:
                    if isinstance(d.get("shanten"), int) and d["shanten"] >= 0:
                        stats["shanten_sum"] += d["shanten"]
                        stats["shanten_n"] += 1
                    if d.get("ms"):
                        stats["ms_sum"] += d["ms"]
                        stats["ms_n"] += 1
                if d["action"] == "hu":
                    stats["hu"] += 1
        except Exception as e:
            stats["fail"] += 1
            if len(stats["errors"]) < 10:
                stats["errors"].append(f"{p.name}: {e}")
        if (i + 1) % 100 == 0:
            print(f"  ... {i + 1}/{len(pngs)}")

    report = {
        "total": stats["total"],
        "ok": stats["ok"],
        "fail": stats["fail"],
        "no_hand": stats["no_hand"],
        "hu_detected": stats["hu"],
        "avg_shanten": round(stats["shanten_sum"] / stats["shanten_n"], 2)
        if stats["shanten_n"] else None,
        "avg_think_ms": round(stats["ms_sum"] / stats["ms_n"], 1)
        if stats["ms_n"] else None,
        "sample_errors": stats["errors"],
    }
    return report


if __name__ == "__main__":
    # 自检：构造一手牌跑think
    from tempfile import TemporaryDirectory
    def mock_vision(png: bytes) -> list[int]:
        # 17张：1-9万各1 + 1筒2筒3筒 + 1条1条1条 + 东东
        return [1, 2, 3, 4, 5, 6, 7, 8, 9,
                21, 22, 23, 41, 41, 41, 61, 61]

    with TemporaryDirectory() as tmp:
        bot = TeaBot(dry_run=True, vision_fn=mock_vision,
                     shot_fn=lambda: b"mock", log_dir=tmp)
        s = bot.play_one_round(screenshot=b"mock")
        print(json.dumps(s, ensure_ascii=False, indent=2))
        for handler in list(bot.logger.handlers):
            bot.logger.removeHandler(handler)
            handler.close()
