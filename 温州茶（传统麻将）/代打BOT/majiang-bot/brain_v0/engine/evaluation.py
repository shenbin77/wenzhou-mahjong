# -*- coding: utf-8 -*-
"""策略评测与上线闸门。所有统计都保留样本量和模型限制。"""

import math

from .advisor import local_strategy, random_strategy
from .simulator import Simulator


def _mean_ci95(values):
    values = [float(v) for v in values]
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "ci95": [None, None]}
    mean = sum(values) / n
    if n == 1:
        return {"n": 1, "mean": round(mean, 6), "ci95": [None, None]}
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    margin = 1.96 * math.sqrt(variance / n)
    return {
        "n": n,
        "mean": round(mean, 6),
        "ci95": [round(mean - margin, 6), round(mean + margin, 6)],
    }


def compare_local_to_random(rounds=100, seed=20260825, focus_seat=0):
    """同一洗牌种子下做配对 A/B，减少牌运方差。"""
    if rounds <= 0:
        raise ValueError("rounds 必须大于0")
    score_deltas = []
    win_deltas = []
    fangpao_deltas = []
    local_totals = {"wins": 0, "fangpao": 0, "net": 0}
    random_totals = {"wins": 0, "fangpao": 0, "net": 0}

    for index in range(rounds):
        trial_seed = seed + index
        dealer = index % 4
        local = Simulator(seed=trial_seed).play_round(
            dealer=dealer, focus_seat=focus_seat, focus_strategy=local_strategy
        )
        baseline = Simulator(seed=trial_seed).play_round(
            dealer=dealer, focus_seat=focus_seat, focus_strategy=random_strategy
        )
        local_score = local["deltas"][focus_seat]
        random_score = baseline["deltas"][focus_seat]
        local_win = int(local.get("winner") == focus_seat)
        random_win = int(baseline.get("winner") == focus_seat)
        local_fp = int(local.get("fangpao_by") == focus_seat)
        random_fp = int(baseline.get("fangpao_by") == focus_seat)
        score_deltas.append(local_score - random_score)
        win_deltas.append(local_win - random_win)
        fangpao_deltas.append(local_fp - random_fp)
        local_totals["wins"] += local_win
        local_totals["fangpao"] += local_fp
        local_totals["net"] += local_score
        random_totals["wins"] += random_win
        random_totals["fangpao"] += random_fp
        random_totals["net"] += random_score

    report = {
        "rounds": rounds,
        "seed": seed,
        "local": {
            "win_rate": round(local_totals["wins"] / rounds, 6),
            "fangpao_rate": round(local_totals["fangpao"] / rounds, 6),
            "avg_score": round(local_totals["net"] / rounds, 6),
        },
        "random": {
            "win_rate": round(random_totals["wins"] / rounds, 6),
            "fangpao_rate": round(random_totals["fangpao"] / rounds, 6),
            "avg_score": round(random_totals["net"] / rounds, 6),
        },
        "paired_delta_local_minus_random": {
            "win_rate": _mean_ci95(win_deltas),
            "fangpao_rate": _mean_ci95(fangpao_deltas),
            "score": _mean_ci95(score_deltas),
        },
        "model_limits": [
            "no_peng", "no_gang", "single_winner", "random_opponents",
            "seat_priority_fangpao_bias",
        ],
        "known_biases": [
            {
                "name": "seat_priority_fangpao_bias",
                "where": "engine/simulator.py:Simulator.play_round 接炮判定",
                "description": (
                    "多人同时可接炮时按 offset=1(下家)→2(对家)→3(上家) 顺序取第一个命中者，"
                    "是建模简化而非真实规则（真实规则为抢胡顺序/多家可同时胡），"
                    "靠近下家座位的玩家被系统性判为接炮方。"
                ),
                "affected_metrics": [
                    "local.fangpao_rate", "random.fangpao_rate",
                    "paired_delta_local_minus_random.fangpao_rate",
                    "paired_delta_local_minus_random.score",
                ],
                "mitigation": (
                    "同一洗牌种子下的配对 A/B 只做相对比较；本报告的绝对放炮率与座位分布不可外推，"
                    "需真实牌谱反事实评测校验。"
                ),
            }
        ],
    }
    score_ci = report["paired_delta_local_minus_random"]["score"]["ci95"]
    report["verdict"] = (
        "simulation_positive" if score_ci[0] is not None and score_ci[0] > 0
        else "simulation_negative" if score_ci[1] is not None and score_ci[1] < 0
        else "simulation_inconclusive"
    )
    report["production_claim"] = False
    return report


def afa_release_gate(*, availability, evaluated_decisions, paired_score_ci95,
                     min_decisions=500, min_availability=0.995):
    """阿法能否从 shadow 升为主引擎；必须由真实牌谱反事实评测填入。"""
    reasons = []
    if evaluated_decisions < min_decisions:
        reasons.append(f"真实决策样本不足: {evaluated_decisions} < {min_decisions}")
    if availability < min_availability:
        reasons.append(f"可用率不足: {availability:.3%} < {min_availability:.3%}")
    low, high = paired_score_ci95
    if low is None or low <= 0:
        reasons.append(f"相对本地引擎的得分提升未显著大于0: CI95={paired_score_ci95}")
    return {
        "promote_afa": not reasons,
        "mode": "blend" if not reasons else "shadow",
        "reasons": reasons,
        "requirements": {
            "min_decisions": min_decisions,
            "min_availability": min_availability,
            "paired_score_ci95_lower_gt": 0,
        },
    }
