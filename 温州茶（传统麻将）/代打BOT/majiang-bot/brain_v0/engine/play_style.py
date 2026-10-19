# -*- coding: utf-8 -*-
"""打法分析器 —— 以「胜率」为核心 KPI（回答：我的真实打法画像，我只要胜率）。

术语钉死（避免算错指标，见第 4 条澄清）：
- 点炮 (deal-in)：你打出一张牌，让别人胡了 → 你「点炮 / 放炮 / 喂牌」。
  "少点炮" = 你喂牌少 = 防守稳。这是你本局赔付的来源。
- 别人点炮（接炮 / 荣和）：别人打出一张牌，你胡了 → 你「接炮胡」。
  这不是你点炮，是你赢。
- 自摸 (zimo)：你自己摸到胡牌张 → 你「自摸胡」。
- 结论：胜率 = (自摸 + 接炮) / 总参与局。「少点炮、多自摸」= 你喂牌少 + 你自摸多。

数据来源：rounds 表的结局字段（is_zimo / win_type / is_dealer / winner_seat /
losing_seat），由 tracker.set_outcome 在每局结束写入，或从回放文件名
（第NN局_a_自摸_*.mp4 / 第NN局_a_点炮_*.mp4）批量导入。

所有函数纯函数 + 异常兜底，绝不因单条脏数据崩溃。
"""

from .storage import get_recent_rounds

# 给前端/用户看的术语说明（胜率画像的一部分）
DEFINITION = (
    "胜率 = 你胡牌的局(自摸+接炮) / 你参与的总局数。"
    "点炮=你打出让别人胡的牌(你赔付)；别人点炮=别人打出让你胡的牌(你赢)。"
)

# 结局归类（与 tracker.set_outcome 语义一致）
def _classify(rec: dict) -> str:
    """把一条 rounds 记录归为四种结局之一。"""
    wt = (rec.get("win_type") or "").lower()
    if wt in ("zimo", "selfdraw"):
        return "zimo"          # 你自摸胡
    if wt in ("point", "接炮", "ron"):
        return "point"         # 你接炮胡（别人点炮给你）
    if wt in ("lose", "点炮", "deal"):
        return "lose"          # 你点炮给别人胡
    if wt in ("draw", "流局"):
        return "draw"          # 流局
    # win_type 缺失时回退到 is_win/is_zimo 启发式
    if rec.get("is_win") == 1:
        return "zimo" if rec.get("is_zimo") == 1 else "point"
    if rec.get("is_win") == 0 and rec.get("is_zimo") == 0:
        return "lose"
    return "unknown"


def win_rate(db_path: str = None, limit: int = 100000) -> dict:
    """只返回「胜率」这一件事（用户只要胜率）。

    返回：{"total", "win_count", "win_rate", "insufficient", "error"}。
    win_rate 为 0~1 的小数；insufficient=True 表示样本不足。
    """
    try:
        rounds = get_recent_rounds(limit, db_path)
        cats = {"zimo": 0, "point": 0, "lose": 0, "draw": 0, "unknown": 0}
        for r in rounds:
            cats[_classify(r)] += 1
        total = sum(v for k, v in cats.items() if k != "unknown")
        if total == 0:
            return {"total": 0, "win_count": 0, "win_rate": 0.0, "insufficient": True}
        win_count = cats["zimo"] + cats["point"]
        return {
            "total": total,
            "win_count": win_count,
            "win_rate": round(win_count / total, 3),
            "insufficient": False,
        }
    except Exception:  # pragma: no cover
        return {"total": 0, "win_count": 0, "win_rate": 0.0,
                "insufficient": True, "error": True}


def analyze_play_style(db_path: str = None, limit: int = 100000) -> dict:
    """以胜率为头条 KPI 的打法画像。

    返回结构示例：
    {
      "win_rate": 0.58,            # 头条：胜率
      "headline": "胜率 58.3%（7/12 局胡牌）",
      "total": 12, "win_count": 7,
      "zimo": 3, "point": 4, "lose": 3, "draw": 2,
      "zimo_rate": 0.43,           # 自摸局 / 胡牌局
      "deal_rate": 0.25,           # 你点炮(喂牌)局 / 总参与局
      "verdict": "...",
      "definition": DEFINITION
    }
    """
    wr = win_rate(db_path, limit)
    if wr.get("insufficient"):
        return {
            "win_rate": 0.0, "headline": "胜率：样本不足（需先积累带结局的牌局）",
            "total": 0, "win_count": 0,
            "zimo": 0, "point": 0, "lose": 0, "draw": 0,
            "zimo_rate": 0.0, "deal_rate": 0.0,
            "verdict": "样本不足（需先积累带结局的牌局）",
            "definition": DEFINITION,
        }
    try:
        rounds = get_recent_rounds(limit, db_path)
        cats = {"zimo": 0, "point": 0, "lose": 0, "draw": 0, "unknown": 0}
        for r in rounds:
            cats[_classify(r)] += 1
        total = sum(v for k, v in cats.items() if k != "unknown")
        win_count = cats["zimo"] + cats["point"]
        # 粗略净胡分：自摸赢 2 底分/局，接炮赢 1，点炮赔 1（仅作相对量纲）
        net = cats["zimo"] * 2 + cats["point"] * 1 - cats["lose"] * 1
        zimo_rate = round(cats["zimo"] / win_count, 3) if win_count else 0.0
        deal_rate = round(cats["lose"] / total, 3)
        win_rate_val = wr["win_rate"]
        # 结论：少点炮 = deal_rate 低；多自摸 = zimo_rate 高（在胡牌局中占比）
        if total < 8:
            verdict = "样本偏少，方向仅供参考"
        elif deal_rate <= 0.2 and zimo_rate >= 0.4:
            verdict = "符合「少点炮、多自摸」的防守型打法"
        elif deal_rate > 0.4:
            verdict = "点炮偏多，建议加强弃牌安全度"
        else:
            verdict = "打法均衡，未见明显偏向"
        return {
            "win_rate": win_rate_val,
            "headline": f"胜率 {win_rate_val * 100:.1f}%（{win_count}/{total} 局胡牌）",
            "total": total, "win_count": win_count,
            "zimo": cats["zimo"], "point": cats["point"],
            "lose": cats["lose"], "draw": cats["draw"],
            "zimo_rate": zimo_rate, "deal_rate": deal_rate,
            "net_hu_score": net, "verdict": verdict,
            "definition": DEFINITION,
        }
    except Exception:  # pragma: no cover
        return {"win_rate": 0.0, "headline": "分析失败（数据读取异常）",
                "total": 0, "definition": DEFINITION}
