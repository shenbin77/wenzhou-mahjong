# -*- coding: utf-8 -*-
"""温州麻将长期数据层：SQLite 持久化与对手倾向。

设计要点（与用户确认过）：
- 长期库只服务「你自己的数据」：每场出牌流水(plays) + 场次数(rounds)。
  由于没有实时比分 OCR，胜率/放炮率无数据源，故长期层聚焦「你的弃牌习惯」，
  这部分你不会随机变成别人，永远成立、永远有用。
- 对手倾向只做「本场/本会话」实时聚合（session_opponent_profiles），
  明确标注「对手若轮换仅供参考」，不落长期对手表，避免跨场噪声误导。
"""

import os
import sqlite3
import json

from .tiles import TILE_NAMES, CAISHEN_ID

# 四个座位：bottom=自己，top/left/right=三家对手
VALID_SEATS = ("bottom", "top", "left", "right")

# 默认库路径：优先用 src/config.py 的 DB_PATH，导入失败时回退到项目 data/ 目录
try:  # pragma: no cover - 取决于运行环境是否 import 得到 config
    from config import DB_PATH as _CFG_DB_PATH
    DEFAULT_DB_PATH = _CFG_DB_PATH
except Exception:  # pragma: no cover
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DEFAULT_DB_PATH = os.path.join(_ROOT, "data", "mahjong.db")


def _suit_of(tile_id):
    """返回牌的花色键，非法牌返回 None。1-9万 / 21-29筒 / 41-49条 / 55红中。"""
    if isinstance(tile_id, int) or (isinstance(tile_id, str) and tile_id.isdigit()):
        tid = int(tile_id)
        if 1 <= tid <= 9:
            return "man"
        if 21 <= tid <= 29:
            return "pin"
        if 41 <= tid <= 49:
            return "sou"
        if tid == CAISHEN_ID:  # 红中
            return "honor"
    return None


# rounds 表可后加的结局字段（迁移时按需补列，保证旧库兼容）
_ROUND_OUTCOME_COLS = {
    "is_zimo": "INTEGER",      # 1=自摸胡；0=非自摸（接炮胡/点炮/流局）
    "win_type": "TEXT",        # zimo / point(接炮胡) / lose(点炮) / draw(流局)
    "is_dealer": "INTEGER",    # 1=本局你是庄家
    "winner_seat": "TEXT",     # 胡牌者座位
    "losing_seat": "TEXT",     # 喂牌家（你点炮时=你；接炮胡时=点炮方）
}
_ROUND_EXTRA_COLS = {
    "deltas": "TEXT",          # JSON: {seat: 该座位得分变化}; 焦点座位净 EV 来源
}


def outcome_to_deltas(win_type, winner_seat=None, losing_seat=None, focus="bottom"):
    """由结局推导「每座位得分变化」(焦点视角净 EV 的代理)。

    真实对局应从引擎/识别拿到精确 deltas；此处给出基于 win_type 的默认代理：
    - zimo(自摸胡)  焦点 +3（三家各付1）
    - point(接炮胡)  焦点 +1（放炮方付1）
    - lose(点炮放炮) 焦点 -1（你付赢家）
    - 其他(流局等)   0
    已知 winner_seat/losing_seat 时用于更精确的跨座位分摊（本工具只关心焦点列）。
    """
    d = {s: 0.0 for s in VALID_SEATS}
    wt = (win_type or "")
    if wt in ("zimo", "point"):
        d[focus] = 3.0 if wt == "zimo" else 1.0
    elif wt == "lose":
        d[focus] = -1.0
    return d


def init_db(db_path: str) -> sqlite3.Connection:
    """建库并返回连接；表已存在则跳过（幂等）。"""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            ended_at TEXT,
            self_score INTEGER,
            is_win INTEGER,
            result TEXT,
            rounds_played INTEGER,
            note TEXT
        )
        """
    )
    # 迁移：补结局字段（旧库已有 rounds 表也不会报错）
    for col, ctype in _ROUND_OUTCOME_COLS.items():
        try:
            conn.execute(f"ALTER TABLE rounds ADD COLUMN {col} {ctype}")
        except sqlite3.OperationalError:
            pass  # 列已存在
    # 迁移：补 deltas 列（每座位得分变化 JSON）
    for col, ctype in _ROUND_EXTRA_COLS.items():
        try:
            conn.execute(f"ALTER TABLE rounds ADD COLUMN {col} {ctype}")
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id INTEGER,
            seq INTEGER,
            tile_id INTEGER,
            tile_name TEXT,
            player TEXT,
            turn_no INTEGER,
            is_caishen INTEGER,
            timestamp TEXT,
            FOREIGN KEY(round_id) REFERENCES rounds(id)
        )
        """
    )
    conn.commit()
    return conn


def save_round(record: dict, plays: list, db_path: str = None, outcome: dict = None,
               deltas: dict = None) -> int | None:
    """落库一整场：插入 rounds 一行 + 多条 plays；返回新 round_id。

    outcome（可选）：本局结局，键见 _ROUND_OUTCOME_COLS。
        {is_zimo, win_type, is_dealer, winner_seat, losing_seat}
    deltas（可选）：{seat: 得分变化}；缺省时由 outcome 的 win_type 推导代理值
        （见 outcome_to_deltas），保证净 EV 报表始终有数可读。
    校验：tile_id 必须合法、player 必须在 VALID_SEATS，否则跳过该条并记日志。
    任何异常均兜底返回 None，绝不向上抛出导致主程序崩溃。
    """
    db = db_path or DEFAULT_DB_PATH
    try:
        conn = init_db(db)
        cur = conn.cursor()
        rec = record or {}
        oc = outcome or {}
        if deltas is None:
            deltas = outcome_to_deltas(
                oc.get("win_type"), oc.get("winner_seat"), oc.get("losing_seat"))
        deltas_json = json.dumps(deltas, ensure_ascii=False) if deltas is not None else None
        cur.execute(
            "INSERT INTO rounds(started_at, ended_at, self_score, is_win, result, rounds_played, note, "
            "is_zimo, win_type, is_dealer, winner_seat, losing_seat, deltas) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec.get("started_at"),
                rec.get("ended_at"),
                rec.get("self_score"),
                rec.get("is_win"),
                rec.get("result", "unknown"),
                rec.get("rounds_played"),
                rec.get("note"),
                oc.get("is_zimo"),
                oc.get("win_type"),
                oc.get("is_dealer"),
                oc.get("winner_seat"),
                oc.get("losing_seat"),
                deltas_json,
            ),
        )
        round_id = cur.lastrowid
        seq = 0
        for p in plays or []:
            tid = p.get("tile_id")
            if _suit_of(tid) is None:
                print(f"[storage] 跳过非法 tile_id: {tid}")
                continue
            player = p.get("player")
            if player not in VALID_SEATS:
                print(f"[storage] 跳过非法 player: {player}")
                continue
            seq += 1
            cur.execute(
                "INSERT INTO plays(round_id, seq, tile_id, tile_name, player, turn_no, is_caishen, timestamp) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    round_id,
                    seq,
                    int(tid),
                    p.get("tile_name", TILE_NAMES.get(int(tid), "")),
                    player,
                    p.get("turn_no", 0),
                    1 if int(tid) == CAISHEN_ID else 0,
                    p.get("timestamp"),
                ),
            )
        conn.commit()
        conn.close()
        return round_id
    except Exception as e:  # pragma: no cover - 兜底
        print(f"[storage] save_round 失败(已忽略): {e}")
        return None


def save_play(round_id: int, play: dict, db_path: str = None) -> bool:
    """单条落库 helper；失败返回 False 不崩。"""
    db = db_path or DEFAULT_DB_PATH
    try:
        if _suit_of(play.get("tile_id")) is None:
            return False
        if play.get("player") not in VALID_SEATS:
            return False
        conn = init_db(db)
        tid = int(play["tile_id"])
        conn.execute(
            "INSERT INTO plays(round_id, seq, tile_id, tile_name, player, turn_no, is_caishen, timestamp) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                round_id,
                play.get("seq", 0),
                tid,
                play.get("tile_name", TILE_NAMES.get(tid, "")),
                play.get("player"),
                play.get("turn_no", 0),
                1 if tid == CAISHEN_ID else 0,
                play.get("timestamp"),
            ),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:  # pragma: no cover
        print(f"[storage] save_play 失败(已忽略): {e}")
        return False


def _profile_from_tile_sequence(tile_ids) -> dict:
    """从有序的 tile_id 序列聚合出弃牌画像（花色分布 / 安全偏好 / 激进度）。

    安全偏好 = 打出「已见过的熟张」比例；激进度 = 打出「生张(此前未见)」比例。
    """
    suit = {"man": 0, "pin": 0, "sou": 0, "honor": 0}
    running = {}
    safe = 0
    live = 0
    for tid in tile_ids:
        s = _suit_of(tid)
        if s is None:
            continue
        suit[s] += 1
        before = running.get(int(tid), 0)
        if before >= 1:
            safe += 1
        else:
            live += 1
        running[int(tid)] = before + 1
    total = sum(suit.values())
    if total == 0:
        return {"total_discards": 0, "suit_counts": suit, "safety_pref": 0.0, "aggression": 0.0}
    return {
        "total_discards": total,
        "suit_counts": suit,
        "safety_pref": round(safe / total, 3),
        "aggression": round(live / total, 3),
    }


def session_opponent_profiles(plays: list) -> list:
    """本场/本会话对手倾向：按座位实时聚合当前会话出牌（纯函数，不落库）。

    注意：仅反映「当前这一桌」的倾向；若对手轮换则为不同人的混合，仅供参考。
    """
    seats = {s: [] for s in VALID_SEATS}
    for p in plays or []:
        seat = p.get("player")
        if seat in seats:
            seats[seat].append(p.get("tile_id"))
    out = []
    for s in VALID_SEATS:
        out.append({"player": s, **_profile_from_tile_sequence(seats[s])})
    return out


def get_longterm_stats(db_path: str = None) -> dict:
    """长期统计：你的场次数 + 总出牌数 + 你自己的弃牌习惯（永远成立）。"""
    db = db_path or DEFAULT_DB_PATH
    try:
        conn = init_db(db)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM rounds")
        total_rounds = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM plays")
        total_plays = cur.fetchone()[0]
        cur.execute(
            "SELECT tile_id FROM plays WHERE player=? ORDER BY timestamp ASC",
            ("bottom",),
        )
        self_tiles = [r[0] for r in cur.fetchall()]
        conn.close()
        ev = get_net_ev(db)
        return {
            "total_rounds": total_rounds,
            "total_plays": total_plays,
            "self": _profile_from_tile_sequence(self_tiles),
            "focus_net": ev["focus_net"],
            "net_ev_per_round": ev["net_ev_per_round"],
        }
    except Exception:  # pragma: no cover - 空库/损坏均返回空结构
        return {"total_rounds": 0, "total_plays": 0, "self": {},
                "focus_net": 0.0, "net_ev_per_round": 0.0}


def get_recent_rounds(limit: int = 20, db_path: str = None) -> list:
    """近期牌局（按 id 倒序），含结局字段与 deltas(已解析为 dict)。"""
    db = db_path or DEFAULT_DB_PATH
    try:
        conn = init_db(db)
        cur = conn.cursor()
        cur.execute(
            "SELECT id, started_at, ended_at, self_score, is_win, result, rounds_played, "
            "is_zimo, win_type, is_dealer, winner_seat, losing_seat, deltas "
            "FROM rounds ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )
        cols = ["id", "started_at", "ended_at", "self_score", "is_win", "result",
                "rounds_played", "is_zimo", "win_type", "is_dealer",
                "winner_seat", "losing_seat", "deltas"]
        rows = []
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            raw = row.get("deltas")
            if raw:
                try:
                    row["deltas"] = json.loads(raw)
                except Exception:
                    row["deltas"] = None
            else:
                row["deltas"] = None
            rows.append(row)
        conn.close()
        return rows
    except Exception:  # pragma: no cover
        return []


def get_net_ev(db_path: str = None, seat: str = "bottom") -> dict:
    """焦点座位的净 EV 统计（取代胜率的核心指标）。

    优先用存储的 deltas；缺失时回退到 win_type 代理(outcome_to_deltas)。
    返回 {rounds, focus_net, net_ev_per_round}。
    """
    db = db_path or DEFAULT_DB_PATH
    try:
        conn = init_db(db)
        cur = conn.cursor()
        cur.execute("SELECT win_type, deltas FROM rounds")
        rows = cur.fetchall()
        conn.close()
        n = len(rows)
        if n == 0:
            return {"rounds": 0, "focus_net": 0.0, "net_ev_per_round": 0.0}
        total = 0.0
        for win_type, deltas_json in rows:
            d = None
            if deltas_json:
                try:
                    d = json.loads(deltas_json)
                except Exception:
                    d = None
            if not d:
                d = outcome_to_deltas(win_type or "")
            total += float(d.get(seat, 0.0))
        return {"rounds": n, "focus_net": round(total, 3),
                "net_ev_per_round": round(total / n, 4)}
    except Exception:  # pragma: no cover
        return {"rounds": 0, "focus_net": 0.0, "net_ev_per_round": 0.0}
