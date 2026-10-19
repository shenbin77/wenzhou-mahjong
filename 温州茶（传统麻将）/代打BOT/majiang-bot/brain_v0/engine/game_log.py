# -*- coding: utf-8 -*-
"""牌谱结构化记录(JSONL)与长期战绩统计。

一局一条记录,schema 稳定可扩展;统计函数纯函数化便于回归。
"""

import json
import os
import time


def new_round_record(*, ts=None, room=None, round_no=None, dealer=None,
                     winner=None, fangpao_by=None, win_type=None,
                     fans=(), deltas=None, hands=None, rivers=None,
                     engine=None, notes=None):
    return {
        "ts": ts or time.time(),
        "room": room,
        "round_no": round_no,
        "dealer": dealer,
        "winner": winner,
        "fangpao_by": fangpao_by,
        "win_type": win_type,
        "fans": list(fans),
        "deltas": deltas or {},
        "hands": hands,
        "rivers": rivers,
        "engine": engine,
        "notes": notes,
    }


class GameLogger:
    def __init__(self, path):
        self.path = path

    def append(self, record):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load_all(self):
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out


def stats_from_records(records, focus_seat=0):
    n = len(records)
    if n == 0:
        return {"rounds": 0}
    hu = sum(1 for r in records if r.get("winner") == focus_seat)
    fp = sum(1 for r in records if r.get("fangpao_by") == focus_seat)
    net = 0
    for r in records:
        d = r.get("deltas") or {}
        net += int(d.get(str(focus_seat), d.get(focus_seat, 0)))
    sessions = {}
    for r in records:
        key = r.get("room")
        sessions.setdefault(key, []).append(r)
    session_stats = []
    for room, rs in sessions.items():
        s_net = 0
        for r in rs:
            d = r.get("deltas") or {}
            s_net += int(d.get(str(focus_seat), d.get(focus_seat, 0)))
        session_stats.append({
            "room": room,
            "rounds": len(rs),
            "net": s_net,
            "win20_walk": s_net >= 20,
        })
    return {
        "rounds": n,
        "focus_hu": hu,
        "focus_hu_rate": round(hu / n, 4),
        "focus_fangpao": fp,
        "focus_fangpao_rate": round(fp / n, 4),
        "focus_net": net,
        "avg_net_per_round": round(net / n, 3),
        "sessions": session_stats,
    }


def rng_fairness_report(records, focus_seat=0, expected_rate=0.25, min_rounds=100):
    """粗粒度 RNG 公平性检验:胡牌率对期望的偏离(卡方近似,z 分数)。"""
    s = stats_from_records(records, focus_seat)
    n = s["rounds"]
    if n < min_rounds:
        return {"verdict": "insufficient_data", "rounds": n}
    p_hat = s["focus_hu_rate"]
    se = (expected_rate * (1 - expected_rate) / n) ** 0.5
    z = (p_hat - expected_rate) / se if se > 0 else 0.0
    verdict = "normal"
    if abs(z) >= 3:
        verdict = "suspicious"
    elif abs(z) >= 2:
        verdict = "watch"
    return {"verdict": verdict, "rounds": n, "hu_rate": p_hat, "z": round(z, 3)}


def new_decision_record(*, hand14, played_counts, turn_no, open_meld_count=0,
                        suggested_discard=None, suggested_name=None,
                        shanten=None, options_top=None, source="local",
                        ts=None, extra=None):
    """单次出牌决策记录(写入 decisions.jsonl)。

    字段稳定可扩展;不改变决策算法本身,仅供离线复盘。
    """
    rec = {
        "kind": "decision",
        "ts": ts or time.time(),
        "hand14": [int(t) for t in (hand14 or [])],
        "played_counts": {str(k): int(v) for k, v in (played_counts or {}).items()},
        "turn_no": int(turn_no) if turn_no is not None else None,
        "open_meld_count": int(open_meld_count or 0),
        "suggested_discard": (
            None if suggested_discard is None else int(suggested_discard)
        ),
        "suggested_name": suggested_name,
        "shanten": shanten,
        "options_top": list(options_top or []),
        "source": source,
    }
    if extra:
        rec["extra"] = extra
    return rec
