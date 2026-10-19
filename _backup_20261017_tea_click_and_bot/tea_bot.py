# -*- coding: utf-8 -*-
"""
温州茶代打Bot v0 - 最小可打版本。

链路：vision(截图识别) → shanten(向听/决策) → pattern_analyzer(胡牌判定) → ADB(执行)

红线：
1. 默认 dry_run=True，只打印决策不真点。用户明确批准前绝不真点。
2. 遵守 D:\\温州麻将\\温州茶（传统麻将）\\HANDOVER.md 的所有红线。
3. 不进雷电游戏中心、不充值、弹窗只点右上X。

模块依赖：
- tea_shanten.py: calc_shanten, is_win, best_discard_by_efficiency (引擎编码，白=73)
- tea_pattern_analyzer.py: analyze (引擎编码，可选)
- tile_vision: 截图→牌ID列表 (vision编码，白=67)

编码转换：vision_to_engine 在 see() 里做。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

# 向听模块（独立，无相对导入）
try:
    from tea_shanten import (
        calc_shanten,
        is_win,
        calc_ting_tiles,
        best_discard_by_efficiency,
        discard_analysis,
    )
    _HAS_SHANTEN = True
except ImportError:
    _HAS_SHANTEN = False

# 牌型分析器（需要tea包，相对导入；失败则降级用shanten.is_win）
try:
    from tea_pattern_analyzer import analyze as _pa_analyze
    _HAS_PA = True
except ImportError:
    try:
        # D盘包路径：brain_v0.engine.tea.pattern_analyzer
        from brain_v0.engine.tea.pattern_analyzer import analyze as _pa_analyze
        _HAS_PA = True
    except ImportError:
        _HAS_PA = False
        _pa_analyze = None


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
# pc-server HTTP客户端（用户PC上8099端口）
# ---------------------------------------------------------------------------
class PcServerClient:
    """pc-server HTTP API：/shot、/tap、/swipe。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8099", timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def shot(self) -> bytes:
        """截图，返回PNG字节。"""
        import urllib.request
        with urllib.request.urlopen(
            f"{self.base_url}/shot", timeout=self.timeout
        ) as r:
            return r.read()

    def tap(self, x: int, y: int) -> dict:
        """点击坐标。"""
        import urllib.request
        data = json.dumps({"x": x, "y": y}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/tap", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> dict:
        import urllib.request
        data = json.dumps(
            {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "ms": ms}
        ).encode()
        req = urllib.request.Request(
            f"{self.base_url}/swipe", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())


# ---------------------------------------------------------------------------
# TeaBot
# ---------------------------------------------------------------------------
class TeaBot:
    """
    温州茶代打Bot。

    dry_run=True（默认）：只打印决策，不调用ADB。
    用户明确批准真实代打前，保持dry_run。
    """

    def __init__(
        self,
        dry_run: bool = True,
        caishen_id: Optional[int] = None,
        pc_server_url: str = "http://127.0.0.1:8099",
        log_dir: Optional[str] = None,
        # 可注入的vision函数：screenshot_bytes -> vision编码牌ID列表
        # 离线测试时注入mock；实战时接tile_vision
        vision_fn: Optional[Callable[[bytes], list[int]]] = None,
        # 可注入的截图函数：() -> PNG字节；默认走pc-server
        shot_fn: Optional[Callable[[], bytes]] = None,
    ):
        if not _HAS_SHANTEN:
            raise RuntimeError("tea_shanten 模块未加载，bot无法工作")
        self.dry_run = dry_run
        self.caishen_id = caishen_id  # 引擎编码；None=无财神/未知
        self.vision_fn = vision_fn
        self.shot_fn = shot_fn
        self._pc = None
        if shot_fn is None:
            # 延迟创建，dry_run离线测试时不需要
            self._pc_url = pc_server_url
        else:
            self._pc_url = pc_server_url

        # 日志
        self.log_dir = Path(log_dir) if log_dir else Path("logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"tea_bot_{ts}.log"
        self._setup_logger()

        self.round_no = 0
        self.step_no = 0

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
        if self._pc is None:
            self._pc = PcServerClient(self._pc_url)
        return self._pc.shot()

    # -- see: 截图→识别→转码 --
    def see(self, screenshot: Optional[bytes] = None) -> list[int]:
        """
        返回引擎编码的手牌ID列表。
        screenshot为None时实时截图；传入字节则用传入的（离线测试用）。
        """
        png = screenshot if screenshot is not None else self._shot()
        if self.vision_fn is None:
            raise RuntimeError(
                "vision_fn未注入。实战时接tile_vision，离线测试时注入mock。"
            )
        vision_ids = self.vision_fn(png)
        engine_ids = vision_hand_to_engine(vision_ids)
        self.log(f"[see] vision={hand_str(vision_ids)} → engine={hand_str(engine_ids)}")
        return engine_ids

    # -- think: 决策 --
    def think(self, hand: list[int]) -> ThinkResult:
        t0 = time.time()
        n = len(hand)
        result = ThinkResult(hand=list(hand), n_tiles=n, shanten=99,
                             is_hu=False)

        # 1. 胡牌判定（pattern_analyzer优先，shanten兜底）
        hu_info = {}
        if _HAS_PA and n == 17:
            try:
                hu_info = _pa_analyze(hand, caishen_id=self.caishen_id) or {}
            except Exception as e:
                self.log(f"[think] pattern_analyzer异常: {e}，降级用shanten")
        result.hu_info = hu_info

        sh = calc_shanten(hand, caishen_id=self.caishen_id)
        result.shanten = sh
        result.is_hu = bool(hu_info.get("hu", False)) if hu_info else (sh == -1)

        if result.is_hu:
            kind_zh = hu_info.get("hu_kind_zh", "")
            mult = hu_info.get("multiplier", "")
            tags = hu_info.get("tags", [])
            result.reason = f"胡牌！{kind_zh} x{mult} tags={tags}"
            result.elapsed_ms = (time.time() - t0) * 1000
            self.log(f"[think] {hand_str(hand)} | {result.reason}")
            return result

        # 2. 听牌时算进张
        if n == 16 and sh == 0:
            try:
                result.ting_tiles = sorted(
                    calc_ting_tiles(hand, caishen_id=self.caishen_id)
                )
            except Exception as e:
                self.log(f"[think] calc_ting_tiles异常: {e}")

        # 3. 打牌决策（17张：刚摸牌，打一张）
        if n == 17:
            cands = best_discard_by_efficiency(
                hand, caishen_id=self.caishen_id
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
        elif n == 16:
            ting = ",".join(tile_name(t) for t in result.ting_tiles)
            result.reason = f"向听{sh}（听牌），进张[{ting}]，等待摸牌"
        else:
            result.reason = f"张数异常{n}，向听{sh}"

        result.elapsed_ms = (time.time() - t0) * 1000
        self.log(
            f"[think] {hand_str(hand)} | {result.reason} "
            f"({result.elapsed_ms:.1f}ms)"
        )
        return result

    # -- act: 执行 --
    def act(self, result: ThinkResult) -> dict:
        """
        dry_run=True：只打印，不真点。
        dry_run=False：调ADB tap（需要tile坐标，调用方提供）。
        """
        if result.is_hu:
            msg = f"[act] 胡牌！{result.reason}"
            self.log(msg)
            return {"action": "hu", "dry_run": self.dry_run, "msg": msg}

        if result.discard is None:
            msg = f"[act] 无打牌动作（{result.reason}）"
            self.log(msg)
            return {"action": "wait", "dry_run": self.dry_run, "msg": msg}

        tile = result.discard
        if self.dry_run:
            msg = f"[act][DRY_RUN] 打 {tile_name(tile)}（引擎ID={tile}）| {result.reason}"
            self.log(msg)
            print(msg)
            return {"action": "discard", "tile": tile,
                    "dry_run": True, "msg": msg}

        # 真实执行（用户批准后）
        msg = f"[act][LIVE] 打 {tile_name(tile)}"
        self.log(msg)
        # 注意：需要tile在屏幕上的坐标，由调用方通过tap_tile实现
        # 这里只做决策，坐标映射在play_one_round的执行层处理
        return {"action": "discard", "tile": tile,
                "dry_run": False, "msg": msg}

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

            result = self.think(hand)
            act_r = self.act(result)
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
    vision_fn: Callable[[bytes], list[int]],
    caishen_id: Optional[int] = None,
    max_files: int = 0,
    log_dir: str = "logs",
) -> dict:
    """
    用已采集的截图做离线测试（不碰模拟器）。

    screenshot_dir: 截图目录（递归找*.png）
    vision_fn: 截图字节 → vision编码牌ID列表
    返回统计报告。
    """
    shot_dir = Path(screenshot_dir)
    pngs = sorted(shot_dir.rglob("*.png"))
    if max_files:
        pngs = pngs[:max_files]

    bot = TeaBot(dry_run=True, caishen_id=caishen_id,
                 vision_fn=vision_fn, log_dir=log_dir,
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
    if not _HAS_SHANTEN:
        print("tea_shanten未加载，自检失败")
        raise SystemExit(1)

    def mock_vision(png: bytes) -> list[int]:
        # 17张：1-9万各1 + 1筒2筒3筒 + 1条1条1条 + 东东
        return [1, 2, 3, 4, 5, 6, 7, 8, 9,
                21, 22, 23, 41, 41, 41, 61, 61]

    bot = TeaBot(dry_run=True, vision_fn=mock_vision,
                 shot_fn=lambda: b"mock")
    s = bot.play_one_round(screenshot=b"mock")
    print(json.dumps(s, ensure_ascii=False, indent=2))
