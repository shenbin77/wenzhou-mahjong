# -*- coding: utf-8 -*-
"""tracker 修复回归测试：覆盖问题1(自愈)/问题2(封顶非负)/问题3(并发无锁安全)。

运行: python -m pytest tests/test_tracker.py -q
"""
import os
import sys
import threading

SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from config import TOTAL_PER_TILE
from tracker import TileTracker


def _assert_non_negative(tracker):
    for tid, played in tracker.played_counts.items():
        assert 0 <= played <= TOTAL_PER_TILE, f"{tid} 计数越界: {played}"
        assert tracker.get_remaining(tid) >= 0, f"{tid} 剩余为负"


def test_self_heal_single_frame_false_positive():
    """问题1(a): 单帧误检后连续干净帧，计数自愈回 0。"""
    t = TileTracker()
    t.reset()
    # 单帧误检 5p
    t.update_river([("5p", 0.9)], "top")
    assert t.played_counts["5p"] == 1  # 立即计入(provisional)
    # 干净帧 -> 误检消失，回退
    t.update_river([], "top")
    assert t.played_counts["5p"] == 0  # 自愈
    # 继续若干干净帧，仍保持 0
    for _ in range(3):
        t.update_river([("1m", 0.9)], "left")  # 其它位置不影响
    assert t.played_counts["5p"] == 0
    _assert_non_negative(t)


def test_real_tile_committed_not_lost_on_transient_miss():
    """问题1: 真牌连续出现后转正(committed)，偶发漏检不丢计数。"""
    t = TileTracker()
    t.reset()
    for _ in range(3):
        t.update_river([("3s", 0.9)], "top")
    assert t.played_counts["3s"] == 1
    # 模拟一帧漏检
    t.update_river([], "top")
    assert t.played_counts["3s"] == 1  # committed 不回退
    _assert_non_negative(t)


def test_false_positive_five_does_not_exceed_total():
    """问题2/问题1(b): 单帧误检 5 张，不超 TOTAL_PER_TILE，get_remaining 不为负。"""
    t = TileTracker()
    t.reset()
    t.update_river([("9p", 0.9)] * 5, "bottom")
    assert t.played_counts["9p"] == 4
    assert t.get_remaining("9p") == 0
    # 再多一帧同样误检，仍封顶
    t.update_river([("9p", 0.9)] * 5, "bottom")
    assert t.played_counts["9p"] == 4
    _assert_non_negative(t)


def test_count_cap_under_repeated_spurious():
    """问题2(d): 连续多帧大量误检，计数始终封顶在 TOTAL_PER_TILE。"""
    t = TileTracker()
    t.reset()
    for _ in range(10):
        t.update_river([("2p", 0.9)] * 5, "right")
    assert t.played_counts["2p"] == TOTAL_PER_TILE
    assert t.get_remaining("2p") >= 0
    # 干净帧后，若是未转正的误检应自愈回 0
    t.update_river([], "right")
    assert t.played_counts["2p"] == TOTAL_PER_TILE  # 已转正，保留
    _assert_non_negative(t)


def test_increment_loop_caps_each_step():
    """问题2: 自增循环内逐张封顶（多张一次性累加不突破）。"""
    t = TileTracker()
    t.reset()
    # 一次性喂 10 张同一牌（远超 4），应只计到 4
    t.update_river([("4p", 0.95)] * 10, "left")
    assert t.played_counts["4p"] == TOTAL_PER_TILE
    for tid, played in t.played_counts.items():
        assert played <= TOTAL_PER_TILE
        assert t.get_remaining(tid) >= 0


def test_concurrent_read_write_no_crash_and_consistent():
    """问题3: 多线程并发读写不崩、计数不越界、快照一致。"""
    tiles = ["1m", "1s", "1p", "2m"]
    # 每个写线程使用独立的位置，模拟 main.py 中每个出牌河由单线程逐帧喂入完整河面；
    # 不能多个线程写同一位置，否则会互相覆盖河面触发误回退。
    positions = ["top", "left", "right", "bottom"]
    t = TileTracker()
    t.reset()

    writers = []
    readers = []
    errors = []

    def writer(tile, pos):
        try:
            for k in range(1, 4):  # 逐帧增加该牌可见数，模拟连续出牌
                t.update_river([(tile, 0.9)] * k, pos)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def reader():
        try:
            for _ in range(20):
                snap = t.get_snapshot()
                for tid, played in snap["played_counts"].items():
                    assert 0 <= played <= TOTAL_PER_TILE
                # 状态字典应覆盖所有牌
                assert len(snap["status"]) == len(t.played_counts)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    for tile, pos in zip(tiles, positions):
        writers.append(threading.Thread(target=writer, args=(tile, pos)))
    for _ in range(3):
        readers.append(threading.Thread(target=reader))

    for th in writers + readers:
        th.start()
    for th in writers + readers:
        th.join(timeout=10)

    assert not errors, f"并发读写出现异常: {errors}"
    # 每个被写入的牌最终应计到 3（封顶逻辑下为最后一帧的 3）
    for tile in tiles:
        assert t.played_counts[tile] == 3, f"{tile} 并发计数异常: {t.played_counts[tile]}"
        assert t.get_remaining(tile) >= 0
    # 历史条数与计数一致（单调流无回退）
    assert len(t.play_history) == sum(t.played_counts.values())
    _assert_non_negative(t)


def test_snapshot_isolated_from_live_mutation():
    """问题3: 拿到的快照不被后续写线程修改（值拷贝）。"""
    t = TileTracker()
    t.reset()
    t.update_river([("5m", 0.9)], "top")
    snap = t.get_snapshot()
    captured = dict(snap["played_counts"])
    # 后续大量写入
    for _ in range(5):
        t.update_river([("5m", 0.9)] * 3, "top")
    # 快照应保持原值
    assert captured["5m"] == 1
    assert snap["played_counts"]["5m"] == 1


def test_tracker_exposes_dealer_seat_for_advisor():
    """问题A(集成): tracker 暴露 dealer_seat，供 main.py 透传给 advisor 做庄家加权。"""
    t = TileTracker()
    assert t.dealer_seat is None  # 默认不加权
    t.set_dealer_seat("top")
    assert t.dealer_seat == "top"
    t.set_dealer_seat(1)  # 整数座位也接受
    assert t.dealer_seat == 1


def test_update_hand_tiles_legality_guard():
    """问题5: 只有 13/14 张才更新手牌，异常张数跳过并保留上一份合法手牌。"""
    t = TileTracker()
    t.reset()
    good = [str(n) + "m" for n in range(1, 10)] + ["1s", "2s", "3s"]  # 12 张
    assert len(good) == 12
    assert t.update_hand_tiles(good) is False  # 12 张不合法
    assert t.current_hand == []

    hand13 = good + ["4s"]
    assert t.update_hand_tiles(hand13) is True
    assert t.current_hand == hand13

    hand14 = hand13 + ["5s"]
    assert t.update_hand_tiles(hand14) is True
    assert t.current_hand == hand14

    # 单帧过度检测（>14）→ 不覆盖已合法的 14 张手牌
    assert t.update_hand_tiles(hand14 + ["6s", "7s"]) is False
    assert t.current_hand == hand14


def test_persist_actually_writes_round(tmp_path):
    """关闭落库回归: persist 必须真的写库。

    历史 bug: storage.save_round(*to_storage_record(), db_path=...) 把 outcome 位置参数
    撞进 db_path → 抛「multiple values for argument db_path」→ 被 except 吞掉，实时记牌
    长期静默不落库。
    """
    from engine import storage

    t = TileTracker()
    t.reset()
    t.update_river([("3p", 0.9), ("3p", 0.88)], "top")
    t.set_outcome(is_zimo=False, win_type="lose", winner_seat="top")
    db = str(tmp_path / "mahjong.db")
    t.persist(db_path=db)

    rounds = storage.get_recent_rounds(limit=5, db_path=db)
    assert rounds, "persist 未写入任何牌局（落库静默失败）"
    assert rounds[0].get("win_type") == "lose"


def test_locked_accessors_return_detached_snapshots():
    """问题8: 主流程经带锁快照读共享状态，拿到的副本不受后续写入影响。"""
    t = TileTracker()
    t.reset()
    t.update_river([("1m", 0.9)], "top")
    counts = t.get_played_counts()
    history = t.get_play_history()
    assert counts["1m"] == 1
    assert len(history) == 1
    # 后续写入不得污染已取出的快照
    for _ in range(3):
        t.update_river([("1m", 0.9)] * 3, "top")
    assert counts["1m"] == 1
    assert len(history) == 1
    assert t.get_played_counts()["1m"] == 3
    # update_advice 走带锁写入
    t.update_advice({"options": [1, 2, 3]})
    assert t.get_snapshot()["latest_advice"] == {"options": [1, 2, 3]}
