#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线回测入口：HandRecord v0 / decisions.jsonl / harvest+analyze → 固定报告。

不触碰实盘、ADB、模拟器。actual 缺失时标记 actual_missing，一致率 N/A。
frame_diff 规则见 schemas/README.md（禁止用河推 actual；manual_gold 可覆盖）。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BRAIN = ROOT / "brain_v0"
PC_BOT = ROOT / "pc_bot"
sys.path.insert(0, str(BRAIN))

from engine.advisor import Advisor  # noqa: E402
from engine.shanten import calc_shanten, calc_ting_tiles, calc_ukeire  # noqa: E402
from engine.tiles import CAISHEN_ID, TILE_NAMES  # noqa: E402

SCHEMA_ID = "hand-record/v0"
REPORT_SCHEMA_ID = "backtest-report/v0"
TARGET_IDS = list(range(1, 10)) + list(range(21, 30)) + list(range(41, 50)) + [55]
DEFAULT_DECISIONS = PC_BOT / "logs" / "decisions.jsonl"
DEFAULT_OUT_DIR = BRAIN / "data" / "backtest"
REPORT_REQUIRED_KEYS = (
    "schema",
    "generated_at",
    "data_sources",
    "n_rounds",
    "n_decision_points",
    "n_actual_missing",
    "agree_rate",
    "agree_rate_display",
    "shanten_summary",
    "ukeire_summary",
    "points",
    "blockers",
)

# 识牌对齐 hand_record.v0 后接入钩子（已落地 frame_diff / seats；open 听牌信息仍预留）
# TODO(vision-align): open_info / 非 bottom tiles 到位后写入 decision.open_melds(unknown) 与 seat 快照


def load_json_schema(path: Path | None = None) -> dict:
    """加载 hand_record.v0.json；缺文件时返回最小内嵌描述。"""
    schema_path = path or (BRAIN / "schemas" / "hand_record.v0.json")
    if schema_path.is_file():
        return json.loads(schema_path.read_text(encoding="utf-8"))
    return {"$id": "hand_record.v0.inline", "title": "HandRecord v0", "schema": SCHEMA_ID}


def _parse_played_counts(raw: Any) -> dict[int, int]:
    if not raw:
        return {}
    out: dict[int, int] = {}
    for k, v in dict(raw).items():
        out[int(k)] = int(v)
    return out


def _dealer_seat_from_meta(meta: dict) -> tuple[str | None, str | None]:
    """返回 (dealer_name, dealer_seat)。seat 由 seats 反查。"""
    dealer_name = meta.get("dealer_name") or meta.get("dealer")
    if isinstance(dealer_name, str) and "红中" in dealer_name and "赖" in dealer_name:
        # meta.wild 误写入 dealer 时忽略
        dealer_name = None
    seats = meta.get("seats")
    if not isinstance(seats, dict):
        seats = {}
    dealer_seat = meta.get("dealer_seat")
    if dealer_seat in ("bottom", "right", "top", "left"):
        return dealer_name if isinstance(dealer_name, str) else None, dealer_seat
    if dealer_name and seats:
        for seat, nick in seats.items():
            if nick == dealer_name and seat in ("bottom", "right", "top", "left"):
                return dealer_name, seat
    return (dealer_name if isinstance(dealer_name, str) else None), None


def _normalize_user_seat(meta: dict) -> str | None:
    us = meta.get("user_seat")
    if isinstance(us, str):
        us = us.strip().lower()
        for s in ("bottom", "right", "top", "left"):
            if us == s or us.startswith(s):
                return s
    if meta.get("seat") in ("bottom", "right", "top", "left"):
        return meta["seat"]
    return "bottom" if meta.get("user_account") else None


def round_from_meta(meta: dict, *, user_account: str = "晓红") -> dict:
    """harvest meta → HandRecord.round（识牌对齐字段）。"""
    dealer_name, dealer_seat = _dealer_seat_from_meta(meta)
    seats = meta.get("seats")
    if not isinstance(seats, dict):
        seats = None
    else:
        seats = {
            k: v
            for k, v in seats.items()
            if k in ("bottom", "right", "top", "left") and isinstance(v, str)
        } or None
    players = meta.get("players")
    # UI 列表序；多数局可 null（无 seats 时保留 players 供人工）
    if seats is not None:
        # 有座位映射时 players 非权威，可保留原列表或 null；此处保留原值供对照
        pass
    return {
        "round_uid": meta.get("round_uid") or "unknown-round",
        "date": meta.get("date"),
        "room_id": meta.get("room_id"),
        "session_start": meta.get("session_start"),
        "players": players if isinstance(players, list) else None,
        "seats": seats,
        "user_seat": _normalize_user_seat(meta),
        "round_index": meta.get("round_index"),
        "replay_code": meta.get("replay_code"),
        "user_account": meta.get("user_account") or user_account,
        "seat": _normalize_user_seat(meta),
        "dealer_name": dealer_name,
        "dealer_seat": dealer_seat,
        "status": meta.get("status"),
        "frames": list(meta.get("frames") or []),
        "capture_timestamps": list(meta.get("capture_timestamps") or []),
    }


def multiset_missing_one(prev: list[int], nxt: list[int]) -> int | None:
    """若 prev 多重集合恰好比 next 多 1 张，返回该 tile；否则 None。"""
    if len(prev) != len(nxt) + 1:
        return None
    cp, cn = Counter(int(t) for t in prev), Counter(int(t) for t in nxt)
    diff = cp - cn
    if sum(diff.values()) != 1:
        return None
    if cn - cp:
        return None
    return next(iter(diff.elements()))


def infer_actual_by_frame_diff(prev: dict, nxt: dict) -> tuple[int | None, str]:
    """稳定 frame_diff 规则。禁止用河推 actual。

    条件：complete+gameplay、open_meld_count 不变、concealed 多重集合恰好少1、
    len(prev)==len(next)+1。
    """
    if not (prev.get("complete") and nxt.get("complete")):
        return None, "none"
    if not (prev.get("gameplay") and nxt.get("gameplay")):
        return None, "none"
    if int(prev.get("open_meld_count") or 0) != int(nxt.get("open_meld_count") or 0):
        return None, "none"
    prev_c = list(prev.get("concealed") or [])
    next_c = list(nxt.get("concealed") or [])
    tile = multiset_missing_one(prev_c, next_c)
    if tile is None:
        return None, "none"
    return int(tile), "frame_diff"


def open_melds_transition(melds_tiles: list[int] | None, open_meld_count: int) -> list:
    """副露过渡：仅 unknown 或 []；禁止臆造 peng/chi/gang。"""
    if open_meld_count <= 0 and not melds_tiles:
        return []
    tiles = [int(t) for t in (melds_tiles or [])]
    if not tiles and open_meld_count <= 0:
        return []
    # 有 count 或 tiles 时用 unknown 占位（不臆造 kind）
    return [{"kind": "unknown", "tiles": tiles}] if (tiles or open_meld_count > 0) else []


def decisions_jsonl_to_hand_record(
    path: Path,
    *,
    user_account: str = "晓红",
    round_uid: str | None = None,
) -> dict:
    """从 decisions.jsonl 适配为 HandRecord v0（无 actual → actual_missing）。"""
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    decisions = []
    for i, row in enumerate(rows):
        hand = [int(t) for t in (row.get("hand14") or row.get("hand") or [])]
        open_n = int(row.get("open_meld_count") or 0)
        wait = 13 - 3 * open_n
        is_hand14 = len(hand) == wait + 1
        if row.get("hand14") is not None:
            is_hand14 = len(hand) == wait + 1
        suggested = row.get("suggested_discard")
        decisions.append({
            "turn_no": row.get("turn_no"),
            "hand": hand,
            "open_melds": open_melds_transition(None, open_n),
            "open_meld_count": open_n,
            "played_counts": {
                str(k): int(v) for k, v in _parse_played_counts(row.get("played_counts")).items()
            },
            "is_hand14": bool(is_hand14),
            "joker": CAISHEN_ID,
            "hongzhong_count": hand.count(CAISHEN_ID),
            "suggested_discard": None if suggested is None else int(suggested),
            "actual_discard": row.get("actual_discard"),
            "actual_discard_source": row.get("actual_discard_source") or "none",
            "actual_action": row.get("actual_action"),
            "actual_missing": True,
            "shanten_before": row.get("shanten"),
            "source": "decisions.jsonl",
            "ts": row.get("ts"),
            "_line_index": i,
        })
        ad = decisions[-1]["actual_discard"]
        aa = decisions[-1]["actual_action"]
        src = decisions[-1]["actual_discard_source"]
        if src == "manual_gold" and ad is not None:
            decisions[-1]["actual_missing"] = False
        elif ad is not None or (isinstance(aa, dict) and aa.get("type")):
            decisions[-1]["actual_missing"] = False
            if src == "none":
                decisions[-1]["actual_discard_source"] = "manual_gold"
        else:
            decisions[-1]["actual_discard"] = None
            decisions[-1]["actual_action"] = None
            decisions[-1]["actual_discard_source"] = "none"
            decisions[-1]["actual_missing"] = True

    return {
        "schema": SCHEMA_ID,
        "round": {
            "round_uid": round_uid or f"decisions-adapt-{path.stem}",
            "date": None,
            "room_id": None,
            "session_start": None,
            "players": None,
            "seats": None,
            "user_seat": "bottom",
            "round_index": None,
            "replay_code": None,
            "user_account": user_account,
            "seat": "bottom",
            "dealer_name": None,
            "dealer_seat": None,
            "status": None,
            "frames": [],
            "capture_timestamps": [],
        },
        "decisions": decisions,
        "frames": [],
        "events": [],
        "notes": f"adapted from {path}; n={len(decisions)}; actual usually missing",
    }


def hand_record_from_analyze(
    meta: dict,
    analyze: dict,
    *,
    user_account: str = "晓红",
) -> dict:
    """harvest meta + replay_analyze JSON → HandRecord（含 frame_diff actual）。"""
    round_meta = round_from_meta(meta, user_account=user_account)
    frames_raw = list(analyze.get("frames") or [])
    # 时间序：按 name / 列表序（analyze 已按 mtime 排）
    frame_snaps = []
    for fr in frames_raw:
        concealed = [int(t) for t in (fr.get("concealed") or [])]
        open_n = int(fr.get("open_meld_count") or 0)
        melds = [int(t) for t in (fr.get("melds") or [])]
        # 非 bottom：自动导出默认只填 bottom
        seats_snap = None
        if concealed or fr.get("gameplay"):
            seats_snap = [{"seat": "bottom", "tiles": concealed, "unknown_count": 0}]
        frame_snaps.append({
            "name": fr.get("name"),
            "concealed": concealed,
            "melds": open_melds_transition(melds, open_n),
            "shanten": fr.get("shanten"),
            "hongzhong": fr.get("hongzhong"),
            "is_hand14": bool(fr.get("is_hand14")),
            "seats": seats_snap,
            "_complete": bool(fr.get("complete")),
            "_gameplay": bool(fr.get("gameplay")),
            "_open_meld_count": open_n,
            "_advise": fr.get("advise"),
        })

    decisions = []
    # frame_diff：相邻 complete+gameplay
    gp_idx = [i for i, fr in enumerate(frames_raw) if fr.get("complete") and fr.get("gameplay")]
    actual_by_prev_name: dict[str, tuple[int, str]] = {}
    for a, b in zip(gp_idx, gp_idx[1:]):
        tile, src = infer_actual_by_frame_diff(frames_raw[a], frames_raw[b])
        if tile is not None and src == "frame_diff":
            # actual 归属「打出前」的手牌帧（prev）
            actual_by_prev_name[frames_raw[a].get("name")] = (tile, src)

    for i, fr in enumerate(frames_raw):
        if not (fr.get("complete") and fr.get("gameplay")):
            continue
        concealed = [int(t) for t in (fr.get("concealed") or [])]
        open_n = int(fr.get("open_meld_count") or 0)
        wait = 13 - 3 * open_n
        is_hand14 = bool(fr.get("is_hand14")) or len(concealed) == wait + 1
        is_hand13 = bool(fr.get("is_hand13")) or len(concealed) == wait
        if not (is_hand14 or is_hand13):
            continue
        advise = fr.get("advise") or {}
        suggested = advise.get("suggested") if isinstance(advise, dict) else None
        name = fr.get("name")
        actual = None
        actual_src = "none"
        if name in actual_by_prev_name:
            actual, actual_src = actual_by_prev_name[name]
        # manual_gold 预留覆盖：meta 级 gold 未接时跳过
        decisions.append({
            "turn_no": i + 1,
            "hand": concealed,
            "open_melds": open_melds_transition(fr.get("melds"), open_n),
            "open_meld_count": open_n,
            "played_counts": {},
            "is_hand14": bool(is_hand14),
            "joker": CAISHEN_ID,
            "hongzhong_count": concealed.count(CAISHEN_ID),
            "suggested_discard": None if suggested is None else int(suggested),
            "actual_discard": actual,
            "actual_discard_source": actual_src,
            "actual_action": (
                {"type": "discard", "tile": actual} if actual is not None else None
            ),
            "actual_missing": actual is None,
            "shanten_before": fr.get("shanten"),
            "source": "replay_analyze",
            "ts": None,
            "_frame_name": name,
        })

    notes = (
        f"from harvest meta + analyze; gameplay_decisions={len(decisions)}; "
        f"frame_diff_actuals={sum(1 for d in decisions if d['actual_discard_source']=='frame_diff')}"
    )
    # 去掉内部 _ 字段再写入 frames 公开面
    public_frames = []
    for fs in frame_snaps:
        public_frames.append({
            k: v for k, v in fs.items() if not k.startswith("_")
        })
    return {
        "schema": SCHEMA_ID,
        "round": round_meta,
        "decisions": decisions,
        "frames": public_frames,
        "events": [],
        "notes": notes,
    }


def load_hand_record(path: Path, *, user_account: str = "晓红") -> dict:
    """加载 HandRecord JSON/JSONL，或自动识别 decisions.jsonl。"""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return decisions_jsonl_to_hand_record(path, user_account=user_account)

    first = text.splitlines()[0].strip()
    obj0 = json.loads(first)
    if obj0.get("schema") == SCHEMA_ID or ("decisions" in obj0 and "round" in obj0):
        if text.lstrip().startswith("{") and "\n{" not in text[1:]:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        if obj0.get("kind") == "decision" or "hand14" in obj0:
            return decisions_jsonl_to_hand_record(path, user_account=user_account)
        return obj0

    if "hand14" in obj0 or obj0.get("kind") == "decision":
        return decisions_jsonl_to_hand_record(path, user_account=user_account)

    return json.loads(text)


def _resolve_actual_tile(dp: dict) -> int | None:
    if dp.get("actual_discard_source") == "manual_gold" and dp.get("actual_discard") is not None:
        return int(dp["actual_discard"])
    if dp.get("actual_discard") is not None:
        return int(dp["actual_discard"])
    aa = dp.get("actual_action")
    if isinstance(aa, dict) and aa.get("type") == "discard" and aa.get("tile") is not None:
        return int(aa["tile"])
    return None


def evaluate_decision(dp: dict, advisor: Advisor | None = None) -> dict:
    """对单个决策点调用与线上一致的 Advisor.local_advise。"""
    advisor = advisor or Advisor()
    hand = [int(t) for t in dp.get("hand") or []]
    open_n = int(dp.get("open_meld_count") or 0)
    played = _parse_played_counts(dp.get("played_counts"))
    turn_no = int(dp.get("turn_no") or 0)
    actual_missing = bool(dp.get("actual_missing", True))
    actual_tile = _resolve_actual_tile(dp)
    if actual_tile is not None:
        actual_missing = False

    result: dict[str, Any] = {
        "turn_no": turn_no,
        "hand": hand,
        "open_meld_count": open_n,
        "is_hand14": bool(dp.get("is_hand14")),
        "joker": CAISHEN_ID,
        "actual_missing": actual_missing,
        "actual_discard": actual_tile,
        "actual_discard_source": dp.get("actual_discard_source")
        or ("none" if actual_missing else "manual_gold"),
        "suggested_discard_log": dp.get("suggested_discard"),
        "bot_action": None,
        "agree": None,
        "shanten_before": None,
        "shanten_after_bot": None,
        "shanten_after_actual": None,
        "ting_count": None,
        "ukeire_count_bot": None,
        "ukeire_count_actual": None,
        "error": None,
        "round_uid": dp.get("_round_uid"),
        "source": dp.get("source"),
        "frame_name": dp.get("_frame_name"),
    }

    if not hand:
        result["error"] = "empty_hand"
        return result

    try:
        sh_before = calc_shanten(hand, open_meld_count=open_n)
        result["shanten_before"] = sh_before
        wait_n = 13 - 3 * open_n
        discard_n = wait_n + 1

        if len(hand) == wait_n:
            ting = calc_ting_tiles(hand, open_meld_count=open_n)
            result["ting_count"] = len(ting)
            result["bot_action"] = None
            result["ukeire_count_bot"] = sum(
                calc_ukeire(hand, played, open_meld_count=open_n).values()
            )
            return result

        if len(hand) != discard_n:
            result["error"] = f"bad_hand_len:{len(hand)} expected {wait_n}|{discard_n}"
            return result

        advice = advisor.local_advise(hand, played, turn_no, open_n)
        opts = advice.get("options") or []
        if not opts:
            result["error"] = "no_options"
            return result
        top = opts[0]
        bot_tile = int(top["tile"])
        result["bot_action"] = {"type": "discard", "tile": bot_tile}
        result["shanten_after_bot"] = top.get("shanten_after")
        result["ukeire_count_bot"] = top.get("ukeire_count")
        result["bot_name"] = TILE_NAMES.get(bot_tile, str(bot_tile))
        if "fangpao_risk" in top:
            result["bot_fangpao_risk"] = top.get("fangpao_risk")

        rest_bot = list(hand)
        rest_bot.remove(bot_tile)
        if len(rest_bot) == wait_n:
            result["ting_count"] = len(calc_ting_tiles(rest_bot, open_meld_count=open_n))

        if not actual_missing and actual_tile is not None:
            result["agree"] = bot_tile == actual_tile
            if actual_tile in hand:
                rest_a = list(hand)
                rest_a.remove(actual_tile)
                result["shanten_after_actual"] = calc_shanten(rest_a, open_meld_count=open_n)
                if len(rest_a) == wait_n:
                    uk = calc_ukeire(rest_a, played, open_meld_count=open_n)
                    result["ukeire_count_actual"] = sum(uk.values())
            else:
                result["error"] = f"actual_tile_not_in_hand:{actual_tile}"
        else:
            result["agree"] = None
            result["actual_missing"] = True
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}:{exc}"
    return result


def run_backtest(record: dict, advisor: Advisor | None = None) -> dict:
    """对 HandRecord 全部决策点回测并汇总（单局，兼容旧测）。"""
    advisor = advisor or Advisor()
    round_uid = (record.get("round") or {}).get("round_uid")
    decisions = record.get("decisions") or []
    tagged = []
    for dp in decisions:
        d = dict(dp)
        d["_round_uid"] = round_uid
        tagged.append(d)
    rows = [evaluate_decision(dp, advisor) for dp in tagged]

    n = len(rows)
    n_err = sum(1 for r in rows if r.get("error"))
    comparable = [r for r in rows if not r.get("actual_missing") and r.get("agree") is not None]
    n_comparable = len(comparable)
    n_agree = sum(1 for r in comparable if r["agree"])
    n_missing = sum(1 for r in rows if r.get("actual_missing"))

    def _avg(key: str, subset: list | None = None) -> float | None:
        xs = [r[key] for r in (subset or rows) if r.get(key) is not None]
        if not xs:
            return None
        return round(sum(xs) / len(xs), 4)

    sh_act = [r["shanten_after_actual"] for r in comparable if r.get("shanten_after_actual") is not None]
    uk_act = [r["ukeire_count_actual"] for r in comparable if r.get("ukeire_count_actual") is not None]

    agreement_rate: float | None
    if n_comparable == 0:
        agreement_rate = None
    else:
        agreement_rate = round(n_agree / n_comparable, 4)

    summary = {
        "schema": SCHEMA_ID,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S CST"),
        "round_uid": round_uid,
        "user_account": (record.get("round") or {}).get("user_account"),
        "n_decisions": n,
        "n_errors": n_err,
        "n_actual_missing": n_missing,
        "n_comparable": n_comparable,
        "n_agree": n_agree,
        "agreement_rate": agreement_rate,
        "agreement_rate_note": (
            None
            if n_comparable
            else "N/A: 无 actual_discard/actual_action 真值（actual_missing）"
        ),
        "shanten_after_bot_avg": _avg("shanten_after_bot"),
        "shanten_after_actual_avg": (
            round(sum(sh_act) / len(sh_act), 4) if sh_act else None
        ),
        "ukeire_count_bot_avg": _avg("ukeire_count_bot"),
        "ukeire_count_actual_avg": (
            round(sum(uk_act) / len(uk_act), 4) if uk_act else None
        ),
        "ting_count_avg": _avg("ting_count"),
        "TARGET_IDS_note": "1-9万/21-29筒/41-49条/55红中",
    }
    return {"summary": summary, "rows": rows, "record_notes": record.get("notes")}


def _avg_key(rows: list[dict], key: str) -> float | None:
    xs = [r[key] for r in rows if r.get(key) is not None]
    if not xs:
        return None
    return round(sum(xs) / len(xs), 4)


def build_fixed_report(
    *,
    data_sources: list[str],
    round_results: list[dict],
    user_account: str | None = "晓红",
    blockers: list[str] | None = None,
    n_rounds_override: int | None = None,
) -> dict:
    """组装 backtest-report/v0（字段顺序与必填段写死）。"""
    all_rows: list[dict] = []
    round_summaries = []
    for rr in round_results:
        s = rr["summary"]
        rows = rr["rows"]
        all_rows.extend(rows)
        round_summaries.append({
            "round_uid": s.get("round_uid"),
            "n_decision_points": s.get("n_decisions"),
            "n_actual_missing": s.get("n_actual_missing"),
            "n_hand14": sum(1 for r in rows if r.get("is_hand14")),
            "n_hand13": sum(1 for r in rows if not r.get("is_hand14")),
            "agreement_rate": s.get("agreement_rate"),
            "notes": rr.get("record_notes"),
        })

    comparable = [r for r in all_rows if not r.get("actual_missing") and r.get("agree") is not None]
    n_comparable = len(comparable)
    n_agree = sum(1 for r in comparable if r["agree"])
    agree_rate = None if n_comparable == 0 else round(n_agree / n_comparable, 4)
    if agree_rate is None:
        agree_display = "N/A"
        agree_note = "N/A: 无 actual_discard/actual_action 真值（actual_missing）"
    else:
        agree_display = f"{agree_rate:.2%} ({n_agree}/{n_comparable})"
        agree_note = None

    points = []
    for i, r in enumerate(all_rows):
        bot = (r.get("bot_action") or {}).get("tile")
        points.append({
            "round_uid": r.get("round_uid"),
            "idx": i,
            "turn_no": r.get("turn_no"),
            "is_hand14": bool(r.get("is_hand14")),
            "bot_tile": bot,
            "actual_discard": r.get("actual_discard"),
            "actual_missing": bool(r.get("actual_missing")),
            "agree": r.get("agree"),
            "shanten_before": r.get("shanten_before"),
            "shanten_after_bot": r.get("shanten_after_bot"),
            "ukeire_count_bot": r.get("ukeire_count_bot"),
            "error": r.get("error"),
            "source": r.get("source"),
            "frame_name": r.get("frame_name"),
        })

    auto_blockers = list(blockers or [])
    n_hand14 = sum(1 for r in all_rows if r.get("is_hand14"))
    if n_hand14 == 0:
        auto_blockers.append("三局帧均无 hand14：无法抽出弃牌决策点（仅 hand13 听牌态可评）")
    if all(r.get("actual_missing") for r in all_rows) if all_rows else True:
        auto_blockers.append("全部决策点 actual_missing：无 frame_diff/manual_gold 真值，agree_rate=N/A")
    if not any(
        r.get("actual_discard_source") == "frame_diff"
        for rr in round_results
        for r in rr.get("rows") or []
    ):
        # check from points' underlying — rows may not carry source of actual
        pass
    n_frame_diff = sum(
        1
        for r in all_rows
        if r.get("actual_discard") is not None
        and r.get("actual_discard_source") == "frame_diff"
    )
    if n_frame_diff == 0:
        auto_blockers.append(
            "frame_diff 未命中：相邻 gameplay 帧无「多重集合恰好少1张」的 14→13 过渡"
        )

    # 去重保序
    seen = set()
    blockers_out = []
    for b in auto_blockers:
        if b not in seen:
            seen.add(b)
            blockers_out.append(b)

    report = {
        "schema": REPORT_SCHEMA_ID,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S CST"),
        "data_sources": list(data_sources),
        "user_account": user_account,
        "n_rounds": n_rounds_override if n_rounds_override is not None else len(round_results),
        "n_decision_points": len(all_rows),
        "n_actual_missing": sum(1 for r in all_rows if r.get("actual_missing")),
        "n_comparable": n_comparable,
        "n_agree": n_agree,
        "n_errors": sum(1 for r in all_rows if r.get("error")),
        "agree_rate": agree_rate,
        "agree_rate_display": agree_display,
        "agreement_rate_note": agree_note,
        "shanten_summary": {
            "before_avg": _avg_key(all_rows, "shanten_before"),
            "after_bot_avg": _avg_key(all_rows, "shanten_after_bot"),
            "after_actual_avg": _avg_key(comparable, "shanten_after_actual"),
        },
        "ukeire_summary": {
            "bot_avg": _avg_key(all_rows, "ukeire_count_bot"),
            "actual_avg": _avg_key(comparable, "ukeire_count_actual"),
            "ting_count_avg": _avg_key(all_rows, "ting_count"),
        },
        "points": points,
        "blockers": blockers_out,
        "rounds": round_summaries,
        "rows_detail": all_rows,
    }
    # 校验必填键
    for k in REPORT_REQUIRED_KEYS:
        if k not in report:
            raise KeyError(f"backtest report missing required key: {k}")
    return report


def write_report(result: dict, out_dir: Path, stem: str = "offline_backtest_latest") -> tuple[Path, Path]:
    """兼容旧单局 result{summary,rows}；亦支持固定 report schema。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"

    if result.get("schema") == REPORT_SCHEMA_ID:
        report = result
    else:
        # 旧格式 → 包一层固定报告
        s = result["summary"]
        report = build_fixed_report(
            data_sources=[result.get("input") or s.get("round_uid") or "unknown"],
            round_results=[result],
            user_account=s.get("user_account"),
        )
        # 保留旧 summary 以便兼容
        report["legacy_summary"] = s

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    rate_s = report["agree_rate_display"]
    lines = [
        f"# 离线回测报告 ({report['generated_at']})",
        "",
        f"- schema: `{report['schema']}`",
        f"- user_account: {report.get('user_account')}",
        f"- 数据源: {report['data_sources']}",
        f"- 局数 n_rounds: **{report['n_rounds']}**",
        f"- 决策点数: **{report['n_decision_points']}**"
        f"（actual_missing={report['n_actual_missing']}, errors={report.get('n_errors', 0)}）",
        f"- bot vs actual 一致率: **{rate_s}**",
    ]
    if report.get("agreement_rate_note"):
        lines.append(f"- 一致率说明: {report['agreement_rate_note']}")
    sh = report["shanten_summary"]
    uk = report["ukeire_summary"]
    lines += [
        f"- 向听 before 均值: {sh.get('before_avg')}",
        f"- 向听 after bot 均值: {sh.get('after_bot_avg')}",
        f"- 向听 after actual 均值: {sh.get('after_actual_avg')}",
        f"- ukeire bot 均值: {uk.get('bot_avg')}",
        f"- ukeire actual 均值: {uk.get('actual_avg')}",
        f"- ting_count 均值: {uk.get('ting_count_avg')}",
        "",
        "## 卡点",
        "",
    ]
    for b in report.get("blockers") or []:
        lines.append(f"- {b}")
    if not report.get("blockers"):
        lines.append("- （无）")
    lines += ["", "## 分局", ""]
    for rs in report.get("rounds") or []:
        lines.append(
            f"- `{rs.get('round_uid')}`: decisions={rs.get('n_decision_points')} "
            f"hand14={rs.get('n_hand14')} hand13={rs.get('n_hand13')} "
            f"missing={rs.get('n_actual_missing')} rate={rs.get('agreement_rate')}"
        )
    lines += ["", "## 逐点", ""]
    for p in report.get("points") or []:
        lines.append(
            f"- [{p['idx']}] round={p.get('round_uid')} turn={p.get('turn_no')} "
            f"h14={p.get('is_hand14')} bot={p.get('bot_tile')} actual={p.get('actual_discard')} "
            f"agree={p.get('agree')} missing={p.get('actual_missing')} "
            f"sh_before={p.get('shanten_before')} sh_bot={p.get('shanten_after_bot')} "
            f"ukeire_bot={p.get('ukeire_count_bot')} err={p.get('error')}"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def load_analyze_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def discover_analyze_latest(dir_path: Path) -> Path | None:
    latest = dir_path / "replay_latest.json"
    if latest.is_file():
        return latest
    cands = sorted(dir_path.glob("replay_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def run_three_rounds_backtest(
    harvest_dirs: list[Path],
    analyze_dirs: list[Path] | None = None,
    *,
    decisions_path: Path | None = None,
    user_account: str = "晓红",
    include_decisions_jsonl: bool = True,
) -> dict:
    """对 3 局 harvest + analyze 跑回测，并可选并入 decisions.jsonl。"""
    advisor = Advisor()
    data_sources: list[str] = []
    round_results: list[dict] = []
    blockers: list[str] = []

    for i, hdir in enumerate(harvest_dirs):
        meta_path = hdir / "meta.json"
        if not meta_path.is_file():
            blockers.append(f"缺 meta.json: {hdir}")
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        analyze_path = None
        if analyze_dirs and i < len(analyze_dirs):
            analyze_path = discover_analyze_latest(analyze_dirs[i])
        if analyze_path is None:
            # 同级 data/backtest/analyze_* 约定
            blockers.append(f"无 analyze 输出: {hdir}")
            # 仍产出空决策局
            record = {
                "schema": SCHEMA_ID,
                "round": round_from_meta(meta, user_account=user_account),
                "decisions": [],
                "frames": [],
                "events": [],
                "notes": "no analyze",
            }
        else:
            analyze = load_analyze_json(analyze_path)
            record = hand_record_from_analyze(meta, analyze, user_account=user_account)
            data_sources.append(str(analyze_path))
        data_sources.append(str(meta_path))
        rr = run_backtest(record, advisor)
        round_results.append(rr)

    if include_decisions_jsonl and decisions_path and decisions_path.is_file():
        data_sources.append(str(decisions_path))
        rec = decisions_jsonl_to_hand_record(decisions_path, user_account=user_account)
        rr = run_backtest(rec, advisor)
        round_results.append(rr)

    # n_rounds 以 harvest 局为准；decisions.jsonl 只并入决策点
    n_harvest = len(harvest_dirs)
    # 帧侧 hand14 卡点
    harvest_rows = []
    for rr in round_results[:n_harvest]:
        harvest_rows.extend(rr.get("rows") or [])
    if harvest_rows and not any(r.get("is_hand14") for r in harvest_rows):
        blockers.append(
            "3局 harvest 帧均无 hand14：弃牌决策点仅来自 decisions.jsonl 或 hand13 听牌态"
        )
    report = build_fixed_report(
        data_sources=data_sources,
        round_results=round_results,
        user_account=user_account,
        blockers=blockers,
        n_rounds_override=n_harvest,
    )
    return report


def main(argv: list[str] | None = None) -> dict:
    p = argparse.ArgumentParser(description="Offline backtest HandRecord / decisions / harvest")
    p.add_argument("--input", type=Path, default=None, help="单文件 HandRecord / decisions.jsonl")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--stem", default="offline_backtest_latest")
    p.add_argument("--user-account", default="晓红")
    p.add_argument(
        "--harvest-rounds",
        nargs="*",
        type=Path,
        default=None,
        help="3 局 harvest 目录（含 meta.json）",
    )
    p.add_argument(
        "--analyze-dirs",
        nargs="*",
        type=Path,
        default=None,
        help="与 harvest-rounds 对齐的 analyze 输出目录",
    )
    p.add_argument(
        "--decisions",
        type=Path,
        default=DEFAULT_DECISIONS,
        help="并入的 decisions.jsonl（默认 pc_bot/logs）",
    )
    p.add_argument("--no-decisions", action="store_true", help="不并入 decisions.jsonl")
    args = p.parse_args(argv)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    if args.harvest_rounds:
        analyze_dirs = args.analyze_dirs
        if not analyze_dirs:
            # 默认约定
            base = BRAIN / "data" / "backtest"
            analyze_dirs = [
                base / "analyze_r01_1015",
                base / "analyze_r01_1014",
                base / "analyze_r02_1014",
            ]
        report = run_three_rounds_backtest(
            list(args.harvest_rounds),
            list(analyze_dirs),
            decisions_path=None if args.no_decisions else args.decisions,
            user_account=args.user_account,
            include_decisions_jsonl=not args.no_decisions,
        )
    elif args.input:
        schema = load_json_schema()
        record = load_hand_record(args.input, user_account=args.user_account)
        result = run_backtest(record)
        result["schema_id"] = schema.get("$id") or SCHEMA_ID
        result["input"] = str(args.input)
        report = build_fixed_report(
            data_sources=[str(args.input)],
            round_results=[result],
            user_account=args.user_account,
        )
    else:
        # 默认：3 局 + decisions
        harvest = [
            Path("/workspace/majiang-data/replay_harvest/2026-10-15/r01"),
            Path("/workspace/majiang-data/replay_harvest/2026-10-14/r01"),
            Path("/workspace/majiang-data/replay_harvest/2026-10-14/r02"),
        ]
        base = BRAIN / "data" / "backtest"
        analyze_dirs = [
            base / "analyze_r01_1015",
            base / "analyze_r01_1014",
            base / "analyze_r02_1014",
        ]
        report = run_three_rounds_backtest(
            harvest,
            analyze_dirs,
            decisions_path=None if args.no_decisions else args.decisions,
            user_account=args.user_account,
            include_decisions_jsonl=not args.no_decisions,
        )

    json_path, md_path = write_report(report, args.out_dir, args.stem)
    # 时间戳副本
    ts_stem = f"offline_backtest_{stamp}"
    write_report(report, args.out_dir, ts_stem)

    print(json.dumps({
        "schema": report["schema"],
        "generated_at": report["generated_at"],
        "n_rounds": report["n_rounds"],
        "n_decision_points": report["n_decision_points"],
        "n_actual_missing": report["n_actual_missing"],
        "agree_rate_display": report["agree_rate_display"],
        "shanten_summary": report["shanten_summary"],
        "ukeire_summary": report["ukeire_summary"],
        "blockers": report["blockers"],
    }, ensure_ascii=False, indent=2))
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return report


if __name__ == "__main__":
    main()
