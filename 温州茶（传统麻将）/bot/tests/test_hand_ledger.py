"""手牌账本的规格测试（2026-10-25 压暗帧卡死 6 分钟的直接回归防线）。

规格一句话：账本只在「独立历史帧 + 同局 + 同财神 + 同张数 + 白名单是手牌
子集」时提供读数；任一条不成立就返回 ``None``，调用方维持原有弃权语义。

2026-10-26 新增第 0 条硬约束（Codex P0）：账本读数必须来自**另一帧**。
同帧写入同帧取回是循环论证，帧标识缺失一律 fail-closed。
"""

from __future__ import annotations

import pytest

from hand_ledger import HandLedger

ROUND = 3
CAISHEN = "w1"
HAND17 = (1, 1, 2, 2, 3, 3, 4, 4, 21, 21, 22, 22, 23, 23, 24, 24, 62)
#: 真实时序：读数写于上一帧，取回发生在当前帧。两个标识都必须非空且不同。
PREV_FRAME = "frames/20261026_001122_000001_before_ab12cd.png"
THIS_FRAME = "frames/20261026_001123_000002_before_34ef56.png"


def advise(hand=HAND17, **extra):
    payload = {
        "status": "advise",
        "hand_ids": list(hand),
        "melds": [],
        "meld_count": 0,
        "caishen": CAISHEN,
        "caishen_id": 1,
        "suggestion_id": hand[0],
    }
    payload.update(extra)
    return payload


def test_remember_rejects_abstain():
    """压暗帧的残读（abstain）绝不能进账本，否则会把读残的手牌当事实。"""
    ledger = HandLedger()
    assert ledger.remember({"status": "abstain", "hand_ids": [1, 2, 3]},
                           round_seq=ROUND, caishen=CAISHEN) is None
    assert ledger.memory is None
    assert ledger.ledger()["rejects"]["status_abstain"] == 1


def test_remember_rejects_empty_hand():
    ledger = HandLedger()
    assert ledger.remember({"status": "advise", "hand_ids": []},
                           round_seq=ROUND, caishen=CAISHEN) is None
    assert ledger.ledger()["rejects"]["no_hand_ids"] == 1


def test_remember_then_recall_ok():
    """上一帧写入、本帧取回：这是账本唯一合法的复用时序。"""
    ledger = HandLedger()
    memory = ledger.remember(advise(), round_seq=ROUND, caishen=CAISHEN,
                             frame=PREV_FRAME)
    assert memory is not None and memory.count == 17
    got = ledger.recall(round_seq=ROUND, caishen=CAISHEN,
                        count=17, whitelist=[24, 62],
                        current_frame=THIS_FRAME)
    assert got is memory
    ledger_state = ledger.ledger()
    assert ledger_state["active"] is True
    assert ledger_state["updates"] == 1
    assert ledger_state["recalls"] == 1


# ----------------------------------------------------------------------
# 帧判据（2026-10-26 Codex P0：禁止同帧自证）
# ----------------------------------------------------------------------
def test_recall_rejects_same_frame():
    """本帧写进账本的读数不能反过来证明本帧读对了（循环论证）。

    实机证据：``_takeover/wb_live/w9`` 台账里 4 次账本辅助动作的
    ``memory_frame`` 与当前帧相同——那不是独立证据，只是本帧残读的复读。
    """
    ledger = HandLedger()
    ledger.remember(advise(), round_seq=ROUND, caishen=CAISHEN,
                    frame=THIS_FRAME)
    assert ledger.recall(round_seq=ROUND, caishen=CAISHEN, count=17,
                         whitelist=[24, 62], current_frame=THIS_FRAME) is None
    rejects = ledger.ledger()["rejects"]
    assert rejects["same_frame"] == 1


def test_recall_rejects_same_windows_frame_with_separator_and_case_changes():
    """Windows 同一路径不能靠 `/`、`\\` 或盘符大小写绕过独立帧门。"""
    ledger = HandLedger()
    ledger.remember(
        advise(), round_seq=ROUND, caishen=CAISHEN,
        frame=r"D:\Capture\frames\before.png")

    recalled = ledger.recall(
        round_seq=ROUND, caishen=CAISHEN, count=17,
        current_frame="d:/capture/frames/before.png")

    assert recalled is None
    assert ledger.ledger()["rejects"]["same_frame"] == 1
    assert ledger.ledger()["recalls"] == 0


def test_recall_rejects_unknown_frame():
    """帧标识缺失 = 证明不了"这是另一帧写入的" → fail-closed 弃权。"""
    ledger = HandLedger()
    ledger.remember(advise(), round_seq=ROUND, caishen=CAISHEN,
                    frame=PREV_FRAME)
    assert ledger.recall(round_seq=ROUND, caishen=CAISHEN, count=17,
                         whitelist=[24, 62]) is None
    assert ledger.ledger()["rejects"]["unknown_frame"] == 1
    assert ledger.ledger()["recalls"] == 0


def test_recall_rejects_memory_without_frame():
    """账本读数自己没记帧（旧调用点漏传）→ 同样按 unknown_frame 弃权。"""
    ledger = HandLedger()
    ledger.remember(advise(), round_seq=ROUND, caishen=CAISHEN)
    assert ledger.memory is not None and ledger.memory.frame == ""
    assert ledger.recall(round_seq=ROUND, caishen=CAISHEN, count=17,
                         current_frame=THIS_FRAME) is None
    assert ledger.ledger()["rejects"]["unknown_frame"] == 1


def test_recall_rejects_round_mismatch():
    """跨局污染是本模块最危险的失败模式：不同局一律拒绝。"""
    ledger = HandLedger()
    ledger.remember(advise(), round_seq=ROUND, caishen=CAISHEN,
                    frame=PREV_FRAME)
    assert ledger.recall(round_seq=ROUND + 1, caishen=CAISHEN, count=17,
                         current_frame=THIS_FRAME) is None
    assert ledger.ledger()["rejects"]["round_mismatch"] == 1


def test_recall_rejects_caishen_mismatch():
    ledger = HandLedger()
    ledger.remember(advise(), round_seq=ROUND, caishen=CAISHEN,
                    frame=PREV_FRAME)
    assert ledger.recall(round_seq=ROUND, caishen="w2", count=17,
                         current_frame=THIS_FRAME) is None
    assert ledger.ledger()["rejects"]["caishen_mismatch"] == 1


def test_recall_rejects_count_mismatch():
    """张数不等说明中间摸牌/吃碰过，账本已过期。"""
    ledger = HandLedger()
    ledger.remember(advise(), round_seq=ROUND, caishen=CAISHEN,
                    frame=PREV_FRAME)
    assert ledger.recall(round_seq=ROUND, caishen=CAISHEN, count=16,
                         current_frame=THIS_FRAME) is None
    assert ledger.ledger()["rejects"]["count_mismatch"] == 1


def test_recall_rejects_whitelist_outside_hand():
    """白名单里有一张不在账本手牌上 → 账本与本帧不是同一手牌。"""
    ledger = HandLedger()
    ledger.remember(advise(), round_seq=ROUND, caishen=CAISHEN,
                    frame=PREV_FRAME)
    assert ledger.recall(round_seq=ROUND, caishen=CAISHEN,
                         count=17, whitelist=[24, 63],
                         current_frame=THIS_FRAME) is None
    assert ledger.ledger()["rejects"]["whitelist_not_subset"] == 1


def test_forget_tile_keeps_the_original_frame():
    """打出一张后账本仍挂着**原来那一帧**的读数：仍是独立历史证据。"""
    ledger = HandLedger()
    ledger.remember(advise(), round_seq=ROUND, caishen=CAISHEN,
                    frame=PREV_FRAME)
    assert ledger.forget_tile(62) is True
    assert ledger.memory.frame == PREV_FRAME
    assert ledger.recall(round_seq=ROUND, caishen=CAISHEN, count=16,
                         current_frame=THIS_FRAME) is ledger.memory


def test_forget_tile_prevents_double_counting():
    """打出后再摸回同样张数，不能让旧手牌（含已打出那张）重新生效。"""
    ledger = HandLedger()
    ledger.remember(advise(), round_seq=ROUND, caishen=CAISHEN,
                    frame=PREV_FRAME)
    assert ledger.forget_tile(62) is True
    assert ledger.memory.count == 16
    # 摸牌后张数回到 17，但账本只有 16 张 → 张数不等，必须拒绝。
    assert ledger.recall(round_seq=ROUND, caishen=CAISHEN, count=17,
                         current_frame=THIS_FRAME) is None


def test_forget_tile_unknown_tile_clears_ledger():
    """扣不准说明账本已经和桌面不一致，直接清空比带着错误读数更安全。"""
    ledger = HandLedger()
    ledger.remember(advise(), round_seq=ROUND, caishen=CAISHEN,
                    frame=PREV_FRAME)
    assert ledger.forget_tile(63) is False
    assert ledger.memory is None
    assert ledger.ledger()["rejects"]["discard_not_in_memory"] == 1


def test_reset_clears_everything():
    ledger = HandLedger()
    ledger.remember(advise(), round_seq=ROUND, caishen=CAISHEN,
                    frame=PREV_FRAME)
    ledger.reset()
    assert ledger.memory is None
    assert ledger.recall(round_seq=ROUND, caishen=CAISHEN, count=17,
                         current_frame=THIS_FRAME) is None


@pytest.mark.parametrize("bad", [None, {}, {"status": "advise"}])
def test_remember_tolerates_bad_payload(bad):
    ledger = HandLedger()
    assert ledger.remember(bad, round_seq=ROUND, caishen=CAISHEN) is None
