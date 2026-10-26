"""手牌账本：把「每帧重新读全手」改成「事件驱动 + 可读帧校正」。

## 为什么需要它（2026-10-25 实机证据）

温州茶在听牌 / 吃碰之后会把整手牌压暗，只亮起**可以打的那几张**。
这个画面态下暗牌面亮度 p70 只有 100（正常 214），识牌只能读出 3~5 张，
``analyze`` 直接 ``abstain``（"识牌不完整，停止决策"）；而亮牌白名单本身
是读得出的（实测 17 框里 2 框亮，id 精确到 南/西），却因为
``bright_subset_unconfirmed`` 要求"有算牌建议可核对"而弃权。

实测后果：``_takeover/wb_live/d1_5558`` 22:13:51 起连续弃权，
直到 22:19:38 游戏高亮自己超时才恢复——**单次卡死约 6 分钟**。
这就是 BOSS 看到的"打着打着停了 / 时好时坏"。

## 本模块的立场

手牌在**可读帧**上本来就是已知的（正常帧 analyze 能读全 14~17 张）。
同局内只要张数不变，压暗帧没有必要重读——用账本 + 本帧亮牌白名单
就能决策。账本只在下面这些**可证伪的硬约束**同时成立时才提供读数：

1. 账本读数是**另一帧**写入的（帧标识可证且不等于本帧）；
2. 同一局（``round_seq`` 相同）且财神相同；
3. 张数与本帧手牌物理框数**完全相等**；
4. 本帧亮牌白名单是账本手牌的**子集**。

任一条不成立就 ``recall`` 返回 ``None``，调用方保持原有弃权行为。
账本从不猜牌面、从不放宽阈值，也不参与任何"点后验证"的判定。

## 为什么第 1 条是硬约束（2026-10-26 Codex P0）

``_takeover/wb_live/w9`` 的台账里 4 次「账本辅助动作」的 ``memory_frame``
与**当前帧相同**：那一帧自己先 ``remember`` 写进账本，同一个 tick 里再
``recall`` 出来当作"本帧手牌"去算该打哪张。这是循环论证——账本读数不是
独立证据，它只是本帧识牌结果的复读机，一旦本帧读残，账本会把残读当成
事实放大两遍（先污染账本，再用账本给建议）。

所以帧判据**双向 fail-closed**：

* ``memory.frame`` 与本帧 ``current_frame`` 相同 → 拒绝（``same_frame``）；
* 任一侧帧标识为空（证明不了"这是另一帧写入的"）→ 拒绝
  （``unknown_frame``）。调用方必须显式把本帧标识传进来，漏传即弃权。
"""

from __future__ import annotations

import ntpath
import time
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Sequence


#: 只接受这两种状态的读数进账本：``advise``（有完整手牌 + 建议）与
#: ``hu``（胡牌帧同样已读全手）。``abstain`` / ``wait`` 一律不进账本，
#: 否则会把"读残的手牌"当成事实反复复用。
TRUSTED_STATUS = frozenset({"advise", "hu"})


@dataclass(frozen=True)
class HandMemory:
    """一帧可信读数。``tiles`` 用视觉 id（与 ``result["hand_ids"]`` 一致）。"""

    round_seq: int
    caishen: str | None
    caishen_id: int | None
    tiles: tuple[int, ...]
    melds: tuple[tuple[int, ...], ...]
    source: str
    ts: float
    frame: str = ""

    @property
    def count(self) -> int:
        return len(self.tiles)

    def ledger(self) -> dict:
        return {
            "round_seq": self.round_seq,
            "caishen": self.caishen,
            "count": self.count,
            "tiles": list(self.tiles),
            "meld_groups": [list(group) for group in self.melds],
            "source": self.source,
            "frame": self.frame,
            "age_s": round(max(0.0, time.time() - self.ts), 1),
        }


def _clean_ids(values: Iterable[object]) -> tuple[int, ...]:
    out: list[int] = []
    for value in values or ():
        if isinstance(value, bool) or not isinstance(value, int):
            return ()
        out.append(int(value))
    return tuple(out)


def _clean_groups(groups: Iterable[Sequence[object]]) -> tuple[tuple[int, ...], ...]:
    out: list[tuple[int, ...]] = []
    for group in groups or ():
        try:
            ids = tuple(sorted(int(v) for v in group))
        except (TypeError, ValueError):
            return ()
        if ids:
            out.append(ids)
    return tuple(out)


def _frame_key(value: object) -> str:
    """按 Windows 路径语义归一帧标识，斜杠/大小写差异不能绕过同帧门。"""
    text = str(value or "").strip()
    if not text:
        return ""
    return ntpath.normcase(ntpath.normpath(text))


class HandLedger:
    """同局手牌账本（只存最后一帧可信读数，不累积历史）。"""

    def __init__(self, clock: Callable[[], float] | None = None):
        self._clock = clock or time.time
        self._memory: HandMemory | None = None
        self.updates = 0
        self.recalls = 0
        self.rejects: dict[str, int] = {}

    # ------------------------------------------------------------------
    def reset(self) -> None:
        self._memory = None

    @property
    def memory(self) -> HandMemory | None:
        return self._memory

    def _reject(self, code: str) -> None:
        self.rejects[code] = self.rejects.get(code, 0) + 1

    # ------------------------------------------------------------------
    def remember(self, result: dict | None, *, round_seq: int,
                 caishen: str | None, frame: str = "") -> HandMemory | None:
        """把一帧可信读数写进账本；不可信则原样保留旧读数并返回 ``None``。"""
        if not isinstance(result, dict):
            self._reject("no_result")
            return None
        status = result.get("status")
        if status not in TRUSTED_STATUS:
            self._reject(f"status_{status}")
            return None
        tiles = _clean_ids(result.get("hand_ids") or ())
        if not tiles:
            self._reject("no_hand_ids")
            return None
        melds = _clean_groups(result.get("melds") or ())
        if result.get("melds") and not melds:
            self._reject("melds_unreadable")
            return None
        memory = HandMemory(
            round_seq=int(round_seq),
            caishen=caishen,
            caishen_id=result.get("caishen_id"),
            tiles=tiles,
            melds=melds,
            source=str(status),
            ts=self._clock(),
            frame=str(frame),
        )
        self._memory = memory
        self.updates += 1
        return memory

    # ------------------------------------------------------------------
    def forget_tile(self, tile_id: int) -> bool:
        """我方刚打出一张：从账本里精确扣掉这张。

        为什么必须扣：打出后张数 -1，等下一次摸牌张数又回到原值。若不扣，
        ``recall`` 的张数校验会在"打完又摸一张"之后重新成立，于是把**含
        已打出那张**的旧手牌当成当前手牌（同一张牌出现两次），这是能骗过
        张数校验的真实污染路径。扣不准就直接清空账本。
        """
        memory = self._memory
        if memory is None:
            return False
        tiles = list(memory.tiles)
        try:
            tiles.remove(int(tile_id))
        except ValueError:
            self.reset()
            self._reject("discard_not_in_memory")
            return False
        self._memory = replace(
            memory, tiles=tuple(sorted(tiles)),
            source=f"{memory.source}+discard", ts=self._clock())
        return True

    # ------------------------------------------------------------------
    def recall(self, *, round_seq: int, caishen: str | None, count: int,
               whitelist: Sequence[int] = (),
               current_frame: str = "") -> HandMemory | None:
        """按"独立历史帧 + 同局 + 同财神 + 同张数 + 白名单是子集"取回读数。

        ``current_frame`` 是本帧的帧标识，调用方**必须**传（见模块头部的
        P0 说明）：账本读数只有来自**另一帧**才算独立证据。两条帧判据都
        fail-closed——同帧（``same_frame``）与帧标识缺失
        （``unknown_frame``）一律返回 ``None``。
        """
        memory = self._memory
        if memory is None:
            self._reject("empty")
            return None
        memory_frame = _frame_key(memory.frame)
        here = _frame_key(current_frame)
        if not memory_frame or not here:
            # 证明不了"这是另一帧写入的读数"就弃权：宁可不兜底，也不自证。
            self._reject("unknown_frame")
            return None
        if memory_frame == here:
            # 本帧刚写进账本的读数不能反过来证明本帧读对了（循环论证）。
            self._reject("same_frame")
            return None
        if memory.round_seq != int(round_seq):
            self._reject("round_mismatch")
            return None
        if (memory.caishen or None) != (caishen or None):
            self._reject("caishen_mismatch")
            return None
        if int(count) <= 0 or memory.count != int(count):
            self._reject("count_mismatch")
            return None
        allowed = {int(v) for v in whitelist}
        if allowed and not allowed.issubset(set(memory.tiles)):
            self._reject("whitelist_not_subset")
            return None
        self.recalls += 1
        return memory

    # ------------------------------------------------------------------
    def ledger(self) -> dict:
        memory = self._memory
        return {
            "active": memory is not None,
            "updates": self.updates,
            "recalls": self.recalls,
            "rejects": dict(sorted(self.rejects.items())),
            "memory": memory.ledger() if memory is not None else None,
        }
