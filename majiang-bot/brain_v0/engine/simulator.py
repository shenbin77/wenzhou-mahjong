# -*- coding: utf-8 -*-
"""温州放炮麻将最小可复现实验环境。

已建模：112张牌、红中财神、自摸、接炮、庄闲计分、七对。
未建模：碰/杠/抢杠胡和多人同时胡。模拟结果只用于同环境策略 A/B，
不能替代真实牌谱的胜率结论。

座位优先权偏差（建模简化，机器可读名 `seat_priority_fangpao_bias`）：模拟器按
offset=1(下家)→2(对家)→3(上家) 顺序判定接炮，与实际规则"谁先胡谁胡"不等价，
靠近下家座位的玩家有统计优势，放炮率与配对手牌的得分差都会被它污染。
该标记同时出现在 play_round/run 的 `model_limits` 与
`evaluation.compare_local_to_random` 报告的 `known_biases` 中，引用结论前必须声明。
"""

import random

from .advisor import Advisor, local_strategy, random_strategy
from .danger import DangerModel
from .scoring import detect_fans, settle_hand
from .shanten import is_win
from .tiles import all_numeric_tiles


class Simulator:
    def __init__(self, strategy_a=local_strategy, strategy_b=random_strategy,
                 seed=None, use_qidui=True):
        self.strategy_a = strategy_a
        self.strategy_b = strategy_b
        self.rng = random.Random(seed)
        self.use_qidui = use_qidui

    def _deal(self):
        deck = all_numeric_tiles()
        self.rng.shuffle(deck)
        hands = [sorted(deck[i * 13:(i + 1) * 13]) for i in range(4)]
        return hands, deck[52:]

    def play_round(self, dealer=0, focus_seat=0, focus_strategy=None):
        if dealer not in range(4) or focus_seat not in range(4):
            raise ValueError("座位必须在0..3")
        hands, wall = self._deal()
        policy_rngs = [random.Random(self.rng.getrandbits(64)) for _ in range(4)]
        rivers = {i: [] for i in range(4)}
        played_counts = {}
        advisor = Advisor(danger_model=DangerModel())
        seat = dealer
        draws = 0

        result = {
            "winner": None,
            "fangpao_by": None,
            "win_type": None,
            "draws": 0,
            "deltas": {i: 0 for i in range(4)},
            "rivers": rivers,
            "model_limits": ["no_peng", "no_gang", "single_winner", "seat_priority_fangpao_bias"],
        }

        while wall:
            hand = hands[seat]
            drawn = wall.pop()
            hand.append(drawn)
            hand.sort()
            draws += 1

            if is_win(hand, self.use_qidui):
                before = list(hand)
                before.remove(drawn)
                fans = detect_fans(before, drawn)
                result.update({
                    "winner": seat,
                    "win_type": "zimo",
                    "fans": fans,
                    "deltas": settle_hand("zimo", seat, dealer, fan_types=fans),
                })
                break

            ctx = {
                "played_counts": dict(played_counts),
                "turn_no": draws,
                "advisor": advisor,
                "rng": policy_rngs[seat],
            }
            is_focus = seat == focus_seat
            strategy = focus_strategy if (is_focus and focus_strategy) else (
                self.strategy_a if is_focus else self.strategy_b
            )
            discard = int(strategy(list(hand), dict(ctx)))
            if discard not in hand:
                raise ValueError(f"策略返回不在手牌中的牌: {discard}")
            hand.remove(discard)

            # 座位优先权偏差（seat_priority_fangpao_bias）：按 offset=1..3 轮询判断，下家(offset=1)
            # 最先被检查、上家(offset=3)最后，放炮归属存在位置偏差——
            # 同环境同手牌下靠近下家的玩家更容易被判为接炮方。
            winner = None
            for offset in range(1, 4):
                other = (seat + offset) % 4
                if is_win(hands[other] + [discard], self.use_qidui):
                    winner = other
                    break

            rivers[seat].append(discard)
            played_counts[discard] = played_counts.get(discard, 0) + 1
            if winner is not None:
                fans = detect_fans(hands[winner], discard)
                result.update({
                    "winner": winner,
                    "fangpao_by": seat,
                    "win_type": "fangpao",
                    "fans": fans,
                    "deltas": settle_hand(
                        "fangpao", winner, dealer, loser_seat=seat, fan_types=fans
                    ),
                })
                break
            seat = (seat + 1) % 4

        result["draws"] = draws
        if result["winner"] is None:
            result["draw_game"] = True
        return result

    def run(self, n_rounds=200, focus_seat=0, focus_strategy=None):
        if n_rounds <= 0:
            raise ValueError("n_rounds 必须大于0")
        stats = {
            "rounds": 0,
            "focus_wins": 0,
            "focus_zimo": 0,
            "focus_fangpao": 0,
            "focus_net": 0,
            "opponent_net": 0,
            "draw_games": 0,
            "model_limits": ["no_peng", "no_gang", "single_winner", "seat_priority_fangpao_bias"],
        }
        for round_index in range(n_rounds):
            result = self.play_round(
                dealer=round_index % 4,
                focus_seat=focus_seat,
                focus_strategy=focus_strategy,
            )
            stats["rounds"] += 1
            deltas = result["deltas"]
            stats["focus_net"] += deltas[focus_seat]
            stats["opponent_net"] += sum(
                score for seat, score in deltas.items() if seat != focus_seat
            )
            if result.get("winner") == focus_seat:
                stats["focus_wins"] += 1
                if result.get("win_type") == "zimo":
                    stats["focus_zimo"] += 1
            if result.get("fangpao_by") == focus_seat:
                stats["focus_fangpao"] += 1
            if result.get("draw_game"):
                stats["draw_games"] += 1
        n = stats["rounds"]
        stats["focus_win_rate"] = round(stats["focus_wins"] / n, 4)
        stats["focus_fangpao_rate"] = round(stats["focus_fangpao"] / n, 4)
        stats["focus_avg_score"] = round(stats["focus_net"] / n, 4)
        stats["opponent_avg_score"] = round(stats["opponent_net"] / n, 4)
        return stats
