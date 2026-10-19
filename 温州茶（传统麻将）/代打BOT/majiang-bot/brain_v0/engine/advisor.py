# -*- coding: utf-8 -*-
"""出牌建议聚合层:本地引擎即时兜底,阿法 API 可选增强,两者交叉校验。

综合评分 = 效率分(进张宽度) - λ × 危险度。
阿法可用时以其评分为主、本地危险度为修正;不可用时纯本地。
"""

from .danger import DangerModel
from .shanten import calc_shanten, calc_ting_tiles, discard_analysis
from .tiles import CAISHEN_ID, TILE_NAMES


class Advisor:
    def __init__(self, danger_model=None, afa_client=None,
                 danger_lambda=0.6, use_qidui=True, afa_mode="shadow",
                 dealer_seat=None, decision_logger=None):
        if afa_mode not in ("shadow", "blend"):
            raise ValueError("afa_mode 必须是 shadow 或 blend")
        self.dealer_seat = dealer_seat
        self.danger = danger_model or DangerModel(dealer_seat=dealer_seat)
        self.afa = afa_client
        self.danger_lambda = danger_lambda
        self.use_qidui = use_qidui
        self.afa_mode = afa_mode
        # Optional GameLogger; when set, every local_advise appends a decision row.
        self.decision_logger = decision_logger

    def _log_decision(self, hand14, played_counts, turn_no, open_meld_count, result):
        """Append one decision row via GameLogger; never raise into callers."""
        if self.decision_logger is None:
            return
        try:
            from .game_log import new_decision_record
            opts = result.get("options") or []
            top = opts[0] if opts else None
            top3 = [
                {
                    "tile": o.get("tile"),
                    "name": o.get("name"),
                    "final_score": o.get("final_score"),
                    "shanten_after": o.get("shanten_after"),
                }
                for o in opts[:3]
            ]
            rec = new_decision_record(
                hand14=hand14,
                played_counts=played_counts,
                turn_no=turn_no,
                open_meld_count=open_meld_count,
                suggested_discard=None if top is None else top.get("tile"),
                suggested_name=None if top is None else top.get("name"),
                shanten=result.get("shanten"),
                options_top=top3,
                source=result.get("source", "local"),
            )
            self.decision_logger.append(rec)
        except Exception:
            pass

    def local_advise(self, hand14, played_counts, turn_no, open_meld_count=0):
        """暗手建议; played_counts 仅含暗手以外已见牌,副露按固定面子计。"""
        hand = [int(t) for t in hand14]
        cur_shanten = calc_shanten(hand, self.use_qidui, open_meld_count)
        wait_count = 13 - 3 * open_meld_count
        discard_count = wait_count + 1
        if len(hand) == wait_count:
            result = {
                "source": "local",
                "shanten": cur_shanten,
                "ting": calc_ting_tiles(hand, self.use_qidui, open_meld_count),
                "options": [],
            }
            self._log_decision(hand, played_counts, turn_no, open_meld_count, result)
            return result
        if len(hand) != discard_count:
            raise ValueError(f"建议入口只接受{wait_count}或{discard_count}张,得到{len(hand)}张")
        from .fangpao_risk import fangpao_risk as _fangpao_risk
        options = []
        for row in discard_analysis(hand, played_counts, self.use_qidui, open_meld_count):
            tile = row["tile"]
            if tile == CAISHEN_ID and len(set(hand)) > 1:
                continue
            d = self.danger.rate(tile, played_counts, turn_no)
            score = row["eff_score"]
            fp = _fangpao_risk(
                tile, played_counts, turn_no,
                dealer_seat=self.dealer_seat, danger_model=self.danger,
            )
            options.append({
                "tile": tile,
                "name": TILE_NAMES.get(tile, str(tile)),
                "eff_score": round(score, 1),
                "shanten_after": row["shanten_after"],
                "ukeire": row["ukeire"],
                "ukeire_count": row["ukeire_count"],
                "danger": round(d, 1),
                "level": self.danger.level(d),
                # 放炮风险（温州红中）：结构化分，与 danger 同源但可含庄家加权
                "fangpao_risk": fp["fangpao_risk"],
                "fangpao_level": fp["level"],
                # 效果评分缩放因子：danger 为 0-100，eff_score 以 -1000*向听差+进张数
                # 计，两者量纲不同；*10 将危险度放大到与效率分可比的粗量级，再按
                # danger_lambda 加权合并为 final_score。
                "final_score": round(score - self.danger_lambda * d * 10, 1),
                "source": "local",
            })
        result = {
            "source": "local",
            "shanten": cur_shanten,
            "ting": [],
            "options": sorted(options, key=lambda x: -x["final_score"]),
        }
        self._log_decision(hand, played_counts, turn_no, open_meld_count, result)
        return result

    def ev_advise(self, hand14, played_counts, turn_no, seat_unseen=None,
                  dealer_seat=None):
        """真·1 步 EV 估算（决策层升级）。

        返回按 EV 降序的候选弃牌: [
          {tile, shanten_after, ukeire_count, danger(期望损失), ev}, ...]
        EV 最高者即"期望失分最小"的近似最优打法。
        """
        from .danger import EvEstimator
        if not hasattr(self, "_ev_estimator"):
            self._ev_estimator = EvEstimator(
                danger_model=self.danger, dealer_seat=self.dealer_seat)
        return self._ev_estimator.ev_of_discard(
            hand14, played_counts, turn_no, seat_unseen=seat_unseen,
            dealer_seat=dealer_seat or self.dealer_seat
            or getattr(self.danger, "dealer_seat", None))

    def advise(self, hand14, played_counts, turn_no, rivers=(), dealer_seat=None,
               open_meld_count=0):
        """主入口。返回统一结构的建议,附 disagreement 警告。"""
        local = self.local_advise(hand14, played_counts, turn_no, open_meld_count)
        result = {
            "engine": "local",
            "shanten": local["shanten"],
            "ting": local["ting"],
            "options": local["options"],
            "warnings": [],
        }
        if len(hand14) == 13 - 3 * open_meld_count:
            result["is_ting"] = local["shanten"] == 0

        # 真·1 步 EV 估算（仅 14 张手牌可算）
        if len(hand14) == 14 and not open_meld_count:
            try:
                result["ev_options"] = self.ev_advise(
                    hand14, played_counts, turn_no, dealer_seat=dealer_seat)
            except Exception:
                result["ev_options"] = []

        afa_result = None
        if self.afa is not None and len(hand14) == 14 and not open_meld_count:
            afa_result = self.afa.recommend(hand14, rivers=rivers)
            if afa_result is None:
                result["warnings"].append(
                    f"afa_unavailable: {getattr(self.afa, 'last_error', 'unknown')} → 已降级本地引擎"
                )

        if afa_result:
            result["afa"] = afa_result
            by_tile = {o["tile"]: o for o in result["options"]}
            # 先校验阿法的向听口径。向听不一致通常意味着远端规则/请求字段
            # 不匹配；这类结果只能作为 shadow 证据，不能静默混入主建议。
            afa_shanten = afa_result.get("min_shanten")
            try:
                afa_shanten_num = int(afa_shanten)
            except (TypeError, ValueError):
                afa_shanten_num = None
            if afa_shanten_num is None:
                result["warnings"].append("afa_invalid_shanten: 阿法未返回可解析向听数")
            elif afa_shanten_num != local["shanten"]:
                result["warnings"].append(
                    f"afa_shanten_disagreement: 本地{local['shanten']} vs 阿法{afa_shanten_num}"
                )
            merged = []
            invalid_option_seen = False
            max_score = max((o["score"] for o in afa_result["options"]), default=1.0) or 1.0
            for o in afa_result["options"]:
                t = o["tile"]
                if t not in by_tile:
                    # 阿法可能返回财神/不可弃牌或其它规则下的牌；禁止把它
                    # 直接呈现为可执行选项，保留原始响应在 result["afa"] 供审计。
                    result["warnings"].append(
                        f"afa_invalid_option: {TILE_NAMES.get(t, t)} 不在本地可弃牌集合"
                    )
                    invalid_option_seen = True
                    continue
                norm = o["score"] / max_score * 100
                d_local = by_tile.get(t, {}).get("danger", 50)
                final = norm * 10 - self.danger_lambda * d_local * 10
                merged.append({
                    "tile": t,
                    "name": TILE_NAMES.get(t, str(t)),
                    "afa_score": round(o["score"], 2),
                    "afa_danger_rate": round(o["danger_rate"], 1),
                    "danger": d_local,
                    "level": self.danger.level(d_local),
                    "final_score": round(final, 1),
                    "source": "afa",
                })
                if t in by_tile:
                    ld = by_tile[t]["danger"]
                    ar = o["danger_rate"]
                    if abs(ld - ar) >= 40:
                        result["warnings"].append(
                            f"disagreement: {TILE_NAMES.get(t, t)} 本地危险{ld:.0f} vs 阿法{ar:.0f}"
                        )
            result["afa_min_shanten"] = afa_result.get("min_shanten")
            # Blend is only safe when both engines use the same shanten
            # convention and every remote candidate is executable locally.
            # Afa has been observed to return a different shanten for the
            # same corrected hand; allowing blend in that state would replace
            # a validated local recommendation with an incompatible rule set.
            blend_compatible = (
                afa_shanten_num is not None
                and afa_shanten_num == local["shanten"]
                and not invalid_option_seen
            )
            if self.afa_mode == "blend" and merged and blend_compatible:
                result["engine"] = "afa+local"
                result["options"] = sorted(merged, key=lambda x: -x["final_score"])
            else:
                if self.afa_mode == "blend":
                    if not merged:
                        reason = "无合法阿法候选"
                    elif not blend_compatible:
                        reason = "向听口径不一致或含不可执行候选"
                    else:
                        reason = "阿法候选为空"
                    result["warnings"].append(
                        f"afa_blend_blocked: {reason}，保持本地建议"
                    )
                result["engine"] = "local+afa-shadow"
                local_top = result["options"][0]["tile"] if result["options"] else None
                afa_top = afa_result["options"][0]["tile"] if afa_result["options"] else None
                result["afa_agrees_with_local"] = local_top == afa_top
        fold, reason = self.should_fold_uncle(local, turn_no)
        result["fold"] = fold
        result["fold_reason"] = reason
        return result

    def should_fold(self, advise_result, turn_no, fold_turn=12):
        """弃胡判断:过 fold_turn 轮仍 2 向听以上 → 建议防守。"""
        if turn_no < fold_turn:
            return False
        sh = advise_result.get("shanten", 8)
        return sh >= 2

    def _best_ukeire(self, advise_result):
        """从建议结果取最大进张数(无候选则 0)。"""
        opts = advise_result.get("options") or []
        if not opts:
            return 0
        return max((o.get("ukeire_count", 0) for o in opts), default=0)

    def should_fold_uncle(self, advise_result, turn_no,
                          early_fold_turn=3, min_shanten=2, min_ukeire=6):
        """舅舅规则#2(攻守转换):摸约 2 轮(early_fold_turn)后,若手牌仍无现实胡路
        (向听>=min_shanten 且 进张<min_ukeire)→ 立即转守拆牌,只为不点炮。

        第一性原理注记(舅舅方式也不一定):
        - 真触发应是『继续的 EV < 转守的 EV』,固定 2 轮只是粗代理;真正该看
          shanten/ukeire(胡路厚度),而非回合数。
        - 早拆是『损失厌恶优先』效用函数下的理性;若目标是 EV 最大化,2 轮阈值偏激
          进(会多弃能发展的边际牌)。故阈值做成可调,留待数据校准最优 fold timing。
        - 仅当派牌非平稳(有连赢连输之势)时,早拆的『截断劣势』才额外有理。
        """
        sh = advise_result.get("shanten", 8)
        if sh <= 0:
            return False, None  # 听牌即有胡路(接炮/自摸),不拆
        opts = advise_result.get("options") or []
        if not opts:
            # 13 张未打牌情境:不触发早拆,仅 late 兜底
            if self.should_fold(advise_result, turn_no):
                return True, "late_game_backstop"
            return False, None
        ukeire = self._best_ukeire(advise_result)
        # 早巡 viability 门槛:2 轮后仍无胡路 → 拆牌转守
        if turn_no >= early_fold_turn and sh >= min_shanten and ukeire < min_ukeire:
            return True, "uncle_early_fold"
        # late-game 兜底(原 should_fold)
        if self.should_fold(advise_result, turn_no):
            return True, "late_game_backstop"
        return False, None


def random_strategy(hand14, ctx):
    import random

    rng = ctx.get("rng", random)
    return rng.choice(list(dict.fromkeys(hand14)))


def local_strategy(hand14, ctx):
    advisor = ctx.get("advisor")
    if advisor is None:
        return random_strategy(hand14, ctx)
    res = advisor.local_advise(
        hand14, ctx["played_counts"], ctx["turn_no"], ctx.get("open_meld_count", 0)
    )
    return res["options"][0]["tile"]
