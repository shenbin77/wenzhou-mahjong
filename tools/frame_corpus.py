# -*- coding: utf-8 -*-
"""离线帧语料分类器：把历史真帧按「画面态」分桶，给代打 BOT 的卡死现场做统计。

## 为什么要有这个工具

BOT 链路（截图 → 识牌 → 算牌 → 门禁 → 点击 → 点后验证）每次改动都只能在实机
上试错，因为 `采集/assist_sessions` 下几万张真帧从来没有被系统化地打过分。
本工具只做一件事：**给每张帧标一个画面态**，让"BOT 大多数时间卡在什么画面"
变成一个可复算的数字，而不是靠回忆。

## 分类口径（阈值全部来自生产模块，本文件不自创）

| state | 判据（按优先级从高到低） | 依据模块 |
| --- | --- | --- |
| ``match_end`` | 牌局结束固定版式：底部 返回大厅/分享战绩/继续游戏 三按钮 | ``match_end.read_match_end`` |
| ``settled`` | 中央奶油面板 / 手牌带压暗 至少一条信号 | ``turn_gate.detect_settled_screen`` |
| ``claim_panel`` | 右侧行动行可见，或圆盘被定位到（吃/碰/杠/胡/过/取消） | ``action_guard.detect_action_ui`` + ``claim_vision.read_claims`` |
| ``dim_bright_subset`` | 手牌框亮暗双峰：亮≥1 且 暗≥1，暗簇内聚、亮暗分离 | ``single_bright_cut.bright_subset_pattern`` |
| ``normal`` | 有手牌框，其余一条都不命中 | — |
| ``unknown`` | 没有手牌框（等候室/大厅/过场）、读不出、尺寸未验证 | — |

优先级理由：**结算压过行动行**（结算页偶尔还能读到圆盘，此时仍算结算），
**行动行压过压暗图案**（行动行在时手牌本就不该被压暗）。``match_end`` 排最前
是因为它是独立版式，且必须单独计数——它决定"一局打完后 BOT 是否停摆"。

``settled`` 分两档留证：``settled_signals`` 有 2 条 = ``settled_strict``（生产
``detect_settled_screen`` 的放行口径），只有 1 条 = 模态弹窗/结算页的弱形态，
仍然不是正常牌局，故同样记 ``settled``，但 ``settled_strict=False``。
两档都不放宽任何门禁：本工具只读像素，不点任何东西。

## 用法

    python tools/frame_corpus.py --limit 2000 --out _takeover/corpus_probe.jsonl
    python tools/frame_corpus.py --resume --workers 6
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, Sequence

import cv2
import numpy as np

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
BOT_DIR = REPO_ROOT / "温州茶（传统麻将）" / "bot"
CORPUS_ROOT = REPO_ROOT / "温州茶（传统麻将）" / "采集" / "assist_sessions"
DEFAULT_OUT = REPO_ROOT / "_takeover" / "corpus_index.jsonl"

#: TileVision 的默认模板目录 ``templates_hongzhong_v2`` 在本仓库不存在，
#: 茶局必须显式指到实测过的 tea_tiles，否则识牌器直接 NoTemplatesError。
DEFAULT_TEMPLATE_DIR = REPO_ROOT / "温州茶（传统麻将）" / "采集" / "templates" / "tea_tiles"

if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from action_guard import detect_action_ui                      # noqa: E402
from claim_vision import read_claims                           # noqa: E402
from match_end import read_match_end                           # noqa: E402
from single_bright_cut import box_face_values, bright_subset_pattern  # noqa: E402
from tile_vision import TileVision                             # noqa: E402
from turn_gate import VERIFIED_FRAME_SIZE                      # noqa: E402

STATE_NORMAL = "normal"
STATE_DIM_BRIGHT = "dim_bright_subset"
STATE_CLAIM = "claim_panel"
STATE_SETTLED = "settled"
STATE_MATCH_END = "match_end"
STATE_UNKNOWN = "unknown"

#: 契约里的六种画面态，顺序即统计表的输出顺序。
STATES = (STATE_NORMAL, STATE_DIM_BRIGHT, STATE_CLAIM, STATE_SETTLED,
          STATE_MATCH_END, STATE_UNKNOWN)

#: 结算类画面的生产口径（``detect_settled_screen`` 要求 2 条信号同时成立）。
SETTLED_REQUIRED_SIGNALS = 2


def read_frame(path: str | Path) -> np.ndarray | None:
    """读一帧 BGR 图。中文路径必须走 ``np.fromfile`` + ``imdecode``。"""
    try:
        buffer = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if buffer.size == 0:
        return None
    return cv2.imdecode(buffer, cv2.IMREAD_COLOR)


class CorpusProbe:
    """单线程探针。一个实例持有一个 ``TileVision``（模板只加载一次）。"""

    def __init__(self, template_dir: str | Path | None = None):
        self.template_dir = Path(template_dir or DEFAULT_TEMPLATE_DIR)
        self.vision = TileVision(template_dir=self.template_dir)

    # -------------------------------------------------------------- 单帧
    def classify(self, image: np.ndarray | None, path: str = "") -> dict:
        started = time.perf_counter()
        record: dict = {
            "path": str(path), "w": None, "h": None,
            "tiles_box_count": 0, "meld_box_count": 0,
            "bright_count": 0, "dim_count": 0, "mid_count": 0,
            "bright_indices": [], "dim_mean": None, "dim_spread": None,
            "gap": None, "pattern_error": None, "pattern_hit": False,
            "_pattern_hit": False, "dim_majority": False,
            "loose_dim_bright": False,
            "action_visible": False, "action_state": "clear", "action_boxes": 0,
            "claim_kinds": [], "claim_decision": None, "claim_known": False,
            "settled": False, "settled_strict": False, "settled_has_hand": False,
            "settled_signals": [],
            "panel_area_pct": None, "bottom_mean_v": None, "frame_mean_v": None,
            "match_end": False, "match_end_reason": "", "leave_center": None,
            "waiting_room": False,
            "state": STATE_UNKNOWN, "reason": "",
        }
        if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
            record["reason"] = "读不出这一帧（文件损坏或不是 3 通道图）"
            record["ms"] = round((time.perf_counter() - started) * 1000, 1)
            return record

        height, width = image.shape[:2]
        record["w"], record["h"] = width, height
        if (height, width) != VERIFIED_FRAME_SIZE:
            record["reason"] = (f"帧尺寸 {width}x{height} 不是已验证的 "
                                f"{VERIFIED_FRAME_SIZE[1]}x{VERIFIED_FRAME_SIZE[0]}")
            record["ms"] = round((time.perf_counter() - started) * 1000, 1)
            return record

        # --- 牌局结束（固定版式）；顺手复用它的结算证据，省一次全幅 HSV ---
        match = read_match_end(image)
        record["match_end"] = bool(match.present)
        record["match_end_reason"] = match.reason
        if match.leave_box is not None:
            x0, y0, x1, y1 = match.leave_box
            record["leave_center"] = [(x0 + x1) // 2, (y0 + y1) // 2]

        settled = match.settled or {}
        signals = list(settled.get("settled_signals") or [])
        required = int(settled.get("settled_required_signals")
                       or SETTLED_REQUIRED_SIGNALS)
        record["settled_signals"] = signals
        record["settled"] = bool(signals)          # 弱形态（弹窗/面板）也算结算类
        record["settled_strict"] = len(signals) >= required
        record["panel_area_pct"] = settled.get("panel_area_pct")
        record["bottom_mean_v"] = settled.get("bottom_mean_v")
        record["frame_mean_v"] = settled.get("frame_mean_v")

        # --- 手牌物理框（生产识牌器，不做任何自定义 ROI 猜测） ---
        boxes: list = []
        try:
            count, boxes = self.vision.count_tiles(image)
            record["tiles_box_count"] = int(count)
            boxes = list(boxes or [])
        except Exception as exc:                    # noqa: BLE001 - 单帧失败不拖垮全量
            record["pattern_error"] = f"识牌异常：{type(exc).__name__}: {exc}"
            boxes = []
        record["meld_box_count"] = len(getattr(self.vision, "last_meld_boxes", []) or [])

        # --- 亮暗双峰（同一条亮度尺：box_face_values 与 turn_gate 同源） ---
        if len(boxes) >= 2:
            values = box_face_values(image, boxes)
            bright_indices, stats = bright_subset_pattern(values)
            record["bright_count"] = int(stats.get("bright_count") or 0)
            record["dim_count"] = int(stats.get("dim_count") or 0)
            record["mid_count"] = int(stats.get("mid_count") or 0)
            record["dim_mean"] = stats.get("dim_mean")
            record["dim_spread"] = stats.get("dim_spread")
            record["gap"] = stats.get("gap")
            record["_pattern_hit"] = bright_indices is not None
            record["pattern_hit"] = record["_pattern_hit"]
            record["bright_indices"] = list(bright_indices or [])
            if stats.get("pattern_error") and record["pattern_error"] is None:
                record["pattern_error"] = stats["pattern_error"]
            # 宽判据：只要亮暗两簇都存在就算（不上双峰门禁），用来看"压暗态"总量
            record["loose_dim_bright"] = (record["bright_count"] >= 1
                                          and record["dim_count"] >= 1)
            # 暗簇多于亮簇 = 整手被压暗（正常出牌态是它的镜像：1 暗 + 其余全亮）
            record["dim_majority"] = (record["dim_count"] > record["bright_count"])
            record["settled_has_hand"] = bool(record["settled"]
                                             and record["tiles_box_count"])

        # --- 行动行 / 圆盘 ---
        guard = detect_action_ui(image)
        record["action_state"] = guard.state
        record["action_visible"] = (guard.state == "action_visible")
        record["action_boxes"] = len(guard.boxes)

        claims = read_claims(image)
        record["claim_kinds"] = sorted({button.kind for button in claims.buttons})
        record["claim_decision"] = claims.decision
        record["claim_known"] = bool(claims.known)

        record["waiting_room"] = (record["tiles_box_count"] == 0
                                  and not record["settled"]
                                  and not record["match_end"])

        state, reason = _decide_state(record)
        record["state"] = state
        record["reason"] = reason
        record["ms"] = round((time.perf_counter() - started) * 1000, 1)
        return record


def _decide_state(record: dict) -> tuple[str, str]:
    """把一帧的证据落成一个状态 + 一句人话。顺序即口径，改顺序=改统计。"""
    if record.get("match_end"):
        return STATE_MATCH_END, (record.get("match_end_reason")
                                 or "牌局结束画面（底部三按钮版式命中）")
    if record.get("settled"):
        signals = record.get("settled_signals") or []
        tag = "结算/模态画面" if len(signals) >= SETTLED_REQUIRED_SIGNALS else "弱结算信号"
        return STATE_SETTLED, f"{tag}命中（{'+'.join(signals) or '无信号'}）"
    if record.get("claim_kinds") or record.get("action_visible") \
            or record.get("action_state") == "uncertain":
        kinds = record.get("claim_kinds") or []
        if kinds:
            return STATE_CLAIM, f"圆盘可见：{'/'.join(kinds)}"
        return STATE_CLAIM, f"右侧行动行可见（action_state={record.get('action_state')}）"
    if record.get("_pattern_hit"):
        return STATE_DIM_BRIGHT, (f"手牌压暗+少数亮：亮 {record.get('bright_count')} 张、"
                                  f"暗 {record.get('dim_count')} 张，间距 {record.get('gap')}")
    if record.get("tiles_box_count"):
        return STATE_NORMAL, (f"手牌框 {record.get('tiles_box_count')} 张，"
                              "无行动行、无压暗图案、非结算")
    if record.get("waiting_room"):
        return STATE_UNKNOWN, "无手牌框（等候室/大厅/过场画面）"
    return STATE_UNKNOWN, "无手牌框，也无任何可识别信号"


# ------------------------------------------------------------------ 清单
def iter_pngs(root: str | Path, stride: int = 1, limit: int | None = None) -> list[Path]:
    """按字典序（= 时间序）列出 png；``stride`` 均匀抽样，``limit`` 取前 N 张。"""
    root = Path(root)
    if not root.is_dir():
        raise SystemExit(f"[error] 语料根目录不存在：{root}")
    stride = max(1, int(stride))
    paths = sorted((p for p in root.rglob("*")
                    if p.is_file() and p.suffix.lower() == ".png"),
                   key=lambda p: str(p))
    paths = paths[::stride]
    if limit is not None:
        paths = paths[:max(0, int(limit))]
    return paths


def load_done_paths(index_path: str | Path) -> set[str]:
    """读已有索引里的 path 集合（断点续跑用）。坏行/空行直接跳过。"""
    done: set[str] = set()
    path = Path(index_path)
    if not path.is_file():
        return done
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            value = item.get("path") if isinstance(item, dict) else None
            if isinstance(value, str) and value:
                done.add(value)
    return done


# ------------------------------------------------------------------ 扫描
_local = threading.local()


def _worker_probe(template_dir: str) -> CorpusProbe:
    """每个线程一个探针：``count_tiles`` 会写 ``self.last_meld_boxes``，不能共享。"""
    probe = getattr(_local, "probe", None)
    if probe is None:
        probe = CorpusProbe(template_dir)
        _local.probe = probe
    return probe


def scan(paths: Sequence[Path], *, workers: int = 1,
         template_dir: str | Path | None = None) -> list[dict]:
    """分类一批帧，返回与输入同序的记录（``workers>1`` 只影响吞吐）。"""
    template = str(Path(template_dir or DEFAULT_TEMPLATE_DIR))
    paths = list(paths)
    if workers <= 1:
        probe = CorpusProbe(template)
        return [probe.classify(read_frame(path), str(path)) for path in paths]

    def one(path: Path) -> dict:
        probe = _worker_probe(template)
        return probe.classify(read_frame(path), str(path))

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        return list(pool.map(one, paths))


# ------------------------------------------------------------------ 统计
def summarize(records: Iterable[dict]) -> dict:
    """按 state 汇总计数与占比，另附对 BOSS 有用的交叉计数。"""
    counts = {state: 0 for state in STATES}
    claim_frames: dict[str, int] = {}
    extras = {"action_visible": 0, "loose_dim_bright": 0, "pattern_hit": 0,
              "dim_majority": 0, "settled_strict": 0, "settled_has_hand": 0,
              "match_end": 0, "waiting_room": 0,
              "no_hand": 0, "unreadable": 0}
    box_hist: dict[int, int] = {}
    total = 0
    for record in records:
        total += 1
        state = record.get("state")
        counts[state if state in counts else STATE_UNKNOWN] += 1
        for kind in record.get("claim_kinds") or []:
            claim_frames[kind] = claim_frames.get(kind, 0) + 1
        if record.get("action_visible"):
            extras["action_visible"] += 1
        if record.get("loose_dim_bright"):
            extras["loose_dim_bright"] += 1
        if record.get("pattern_hit") or record.get("_pattern_hit") \
                or state == STATE_DIM_BRIGHT:
            extras["pattern_hit"] += 1
        if record.get("dim_majority"):
            extras["dim_majority"] += 1
        if record.get("settled_strict"):
            extras["settled_strict"] += 1
        if record.get("settled_has_hand"):
            extras["settled_has_hand"] += 1
        if record.get("match_end"):
            extras["match_end"] += 1
        if record.get("waiting_room"):
            extras["waiting_room"] += 1
        if record.get("w") is None:
            extras["unreadable"] += 1
        box_count = record.get("tiles_box_count")
        if isinstance(box_count, int):
            box_hist[box_count] = box_hist.get(box_count, 0) + 1
            if box_count == 0:
                extras["no_hand"] += 1
    shares = {state: (round(count / total * 100, 2) if total else 0.0)
              for state, count in counts.items()}
    extras["claim_kind_frames"] = claim_frames
    extras["box_hist"] = dict(sorted(box_hist.items()))
    return {"total": total, "counts": counts, "shares": shares, "extras": extras}


def format_summary(summary: dict) -> str:
    lines = [f"总计 {summary['total']} 帧"]
    for state in STATES:
        lines.append(f"  {state:<20}{summary['counts'][state]:<8}"
                     f"{summary['shares'][state]:.2f}%")
    extras = summary["extras"]
    lines.append(f"  -- match_end 帧            {extras['match_end']}")
    lines.append(f"  -- settled_strict(2 信号)  {extras['settled_strict']}")
    lines.append(f"  -- settled 且有手牌框      {extras['settled_has_hand']}")
    lines.append(f"  -- 行动行 action_visible   {extras['action_visible']}")
    lines.append(f"  -- 压暗双峰 pattern_hit    {extras['pattern_hit']}")
    lines.append(f"  -- 暗多于亮 dim_majority   {extras['dim_majority']}")
    lines.append(f"  -- 宽判据 loose_dim_bright {extras['loose_dim_bright']}")
    lines.append(f"  -- 等候室/无手牌框         {extras['waiting_room']}")
    lines.append(f"  -- 读不出                  {extras['unreadable']}")
    lines.append(f"  -- 圆盘 kind 出现帧数      {extras['claim_kind_frames']}")
    top = sorted(extras["box_hist"].items(), key=lambda kv: -kv[1])[:6]
    lines.append(f"  -- 手牌框数 Top6           {top}")
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="离线帧语料分类：给每张真帧标画面态，输出 JSONL + 计数占比")
    parser.add_argument("--root", default=str(CORPUS_ROOT), help="语料根目录（递归找 png）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出 JSONL")
    parser.add_argument("--template-dir", default=str(DEFAULT_TEMPLATE_DIR))
    parser.add_argument("--limit", type=int, default=None, help="只扫前 N 张")
    parser.add_argument("--stride", type=int, default=1, help="每 N 张取 1 张（均匀抽样）")
    parser.add_argument("--resume", action="store_true", help="跳过 --out 里已有的帧，追加写")
    parser.add_argument("--workers", type=int, default=1, help="并发线程数")
    parser.add_argument("--progress", type=int, default=500, help="每 N 帧打一行进度")
    parser.add_argument("--quiet", action="store_true", help="不打印进度")
    args = parser.parse_args(argv)

    paths = iter_pngs(args.root, stride=args.stride, limit=args.limit)
    out_path = Path(args.out)
    done = load_done_paths(out_path) if args.resume else set()
    if done:
        paths = [p for p in paths if str(p) not in done]
    _emit(f"[plan] 候选 {len(paths) + len(done)} 帧，已有 {len(done)}，"
          f"本次待扫 {len(paths)}")
    if not paths:
        records = []
        if args.resume and done:
            records = _reread(out_path)
        print(format_summary(summarize(records)))
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.resume else "w"
    started = time.perf_counter()
    scanned = 0
    total = len(paths)
    with out_path.open(mode, encoding="utf-8") as out:
        for start in range(0, total, 64):
            batch = paths[start:start + 64]
            scanned += _flush(batch, out, out_path, args, started, scanned, total)

    print(format_summary(summarize(_reread(out_path))))
    print(f"[out] {out_path}")
    return 0


def _flush(batch: Sequence[Path], out, out_path: Path, args, started: float,
           scanned: int, total: int) -> int:
    """分类一批、追加落盘、按需打进度。返回本批帧数。"""
    records = scan(batch, workers=args.workers, template_dir=args.template_dir)
    for record in records:
        out.write(json.dumps({k: v for k, v in record.items()
                              if not k.startswith("_")}, ensure_ascii=False) + "\n")
    out.flush()
    scanned += len(records)
    elapsed = max(1e-6, time.perf_counter() - started)
    rate = scanned / elapsed
    step = max(1, int(args.progress))
    if not args.quiet and step > 0 and (scanned // step) > ((scanned - len(records)) // step):
        remain = (total - scanned) / rate if rate else 0
        _emit(f"[scan] {scanned}/{total} ({scanned / max(1, total) * 100:.1f}%) "
              f"{rate:.1f} 帧/s 剩余约 {remain:.0f}s")
    return len(records)


def _reread(index_path: Path) -> list[dict]:
    records: list[dict] = []
    with index_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _emit(message: str) -> None:
    print(message, flush=True)


if __name__ == "__main__":
    sys.exit(main())
