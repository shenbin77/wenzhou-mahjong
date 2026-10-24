# -*- coding: utf-8 -*-
"""TeaModeLatch 玩法门禁粘滞 + action_ui 的单元测试。

修的那个 bug：吃/碰/杠/胡 的金色圆盘压住"2 人茶"规则文字的取样区
（x=814:874,y=190:228），模板分从 0.999 掉到 0.75~0.89（< 0.92 阈值）→
``tea_bot._default_vision`` 抛 ValueError → **整帧弃权**。按钮出现的那一帧恰恰
是最需要分析的那一帧。

本文件锁死五件事：
1. 首次未确认仍然弃权（fail-closed 不变，粘滞不许凭空产出结论）；
2. 确认后锁存，按钮/遮挡帧**不推翻**已确认的玩法；
3. 换局信号（HUD 指纹 / 财神变化）与 TTL 都能让锁失效并要求重新确认；
4. 出现另一种玩法时立刻翻牌（锁了 2 不代表再也认不出 4）；
5. 锁存状态与每帧 ``action_ui`` 都真的出现在输出里（看门狗要靠它留证）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import mode_vision  # noqa: E402
from mode_vision import (  # noqa: E402
    DEFAULT_LATCH_TTL_S,
    MODE_LATCH,
    PLAYER_MARGIN_MIN,
    PLAYER_SCORE_MIN,
    TeaModeLatch,
    detect_tea_player_count,
    frame_round_identity,
    hud_fingerprint,
    latest_action_ui,
    reset_tea_mode_latch,
    same_hud_fingerprint,
    score_tea_player_count,
    visible_caishen_label,
)
from turn_gate import action_ui_evidence, evaluate_turn  # noqa: E402

TEA_ROOT = BOT_DIR.parent
TWO_P_FRAME = (TEA_ROOT / "采集" / "2026-10-19" / "984226" / "r01" /
               "room984226_round1_3d58eb17_2.png")
FOUR_P_FRAME = TEA_ROOT / "采集" / "2026-10-13" / "323028" / "r01" / "frame_02.png"

#: ``action_ui`` 必须原样带出来、不许偷偷少字段（看门狗按 key 取数）。
REQUIRED_ACTION_UI_KEYS = (
    "action_discs_seen", "action_disc_count", "action_discs",
    "claim_decision", "claim_reason", "action_gold_pixel_ratio",
    "mode_score", "mode_margin", "mode_score_min", "mode_margin_min",
    "mode_raw_players", "mode_players", "mode_source", "mode_latched",
    "mode_latch_resets", "mode_latch_age_frames", "mode_latch_age_s",
)


@pytest.fixture(autouse=True)
def _clean_global_latch():
    """每个用例前后都把进程级锁清空，杜绝跨用例污染。"""
    reset_tea_mode_latch(reason="test_setup")
    yield
    reset_tea_mode_latch(reason="test_teardown")


# ---------------------------------------------------------------------------
# 合成帧工具
# ---------------------------------------------------------------------------

def load_frame(path: Path) -> np.ndarray:
    """中文路径安全读图。"""
    image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None, path
    return image


def synth_frame(source: np.ndarray | None, *, hud_seed: int = 0,
                occlude_rule_text: bool = False) -> np.ndarray:
    """由真实帧拼出一张可控的 1600×900 合成帧。

    只贴规则文字带（y=130:260, x=270:1330，含标题与人数模板取样区），其余留黑，
    再按 ``hud_seed`` 写一段确定性灰阶到 HUD crop（x=15:225, y=55:140）——这段
    区域远离行动圆盘，正是换局判据的依据，也让我们能在测试里精确制造"换局"。

    Args:
        source: 提供规则文字的真实帧；``None`` 得到一张完全没有规则的画面。
        hud_seed: HUD 图案种子；不同种子 => 不同指纹（已实测可分辨）。
        occlude_rule_text: 把圆盘实际会压住的那片区域（x=800:1060, y=192:296）
            涂成中灰，复现"模板分掉到阈值以下"的真实遮挡。
    """
    image = np.zeros((900, 1600, 3), np.uint8)
    if source is not None:
        image[130:260, 270:1330] = source[130:260, 270:1330]
    if occlude_rule_text:
        image[192:296, 800:1060] = 130
    width = 225 - 15
    ramp = ((np.arange(width) + hud_seed * 7) % 256).astype(np.uint8)
    image[55:140, 15:225] = ramp.reshape(1, width, 1)
    return image


@pytest.fixture(scope="module")
def two_p_frame() -> np.ndarray:
    return load_frame(TWO_P_FRAME)


@pytest.fixture(scope="module")
def four_p_frame() -> np.ndarray:
    return load_frame(FOUR_P_FRAME)


@pytest.fixture(scope="module")
def clear_two_p(two_p_frame) -> np.ndarray:
    """规则文字清晰可读的 2 人茶帧。"""
    return synth_frame(two_p_frame)


@pytest.fixture(scope="module")
def covered_two_p(two_p_frame) -> np.ndarray:
    """同一间房，但圆盘区域被压住 —— 逐帧判定必然给 None。"""
    return synth_frame(two_p_frame, occlude_rule_text=True)


# ---------------------------------------------------------------------------
# 1. fail-closed：没确认过就是没确认过
# ---------------------------------------------------------------------------

def test_never_confirmed_frame_does_not_latch_anything():
    """一次都没确认过的画面：players 必须是 None，绝不能被记成 2 人局。"""
    latch = TeaModeLatch()
    reading = latch.observe(synth_frame(None))
    assert reading.players is None
    assert reading.source == mode_vision.LATCH_SOURCE_UNKNOWN
    assert not reading.confirmed
    assert latch.latched_players is None
    assert latch.snapshot()["latched"] is False


def test_obscured_frame_alone_still_abstains(covered_two_p):
    """只喂被遮挡的帧（从不给清晰帧）→ 永远弃权，粘滞不许放宽门槛。"""
    latch = TeaModeLatch()
    for _ in range(3):
        reading = latch.observe(covered_two_p)
        assert reading.players is None, "未确认过的遮挡帧被粘滞放行 = 灾难"
        assert latch.latched_players is None


def test_detect_tea_player_count_contract_unchanged(clear_two_p, covered_two_p):
    """逐帧口径本身没被改动：清晰帧给 2，遮挡帧给 None。"""
    assert detect_tea_player_count(clear_two_p) == 2
    players, score, margin = score_tea_player_count(clear_two_p)
    assert players == 2 and score >= PLAYER_SCORE_MIN
    assert margin >= PLAYER_MARGIN_MIN
    players, score, _margin = score_tea_player_count(covered_two_p)
    assert players is None and score < PLAYER_SCORE_MIN


# ---------------------------------------------------------------------------
# 2. 核心修复：确认后锁存，按钮压住时不推翻
# ---------------------------------------------------------------------------

def test_confirmed_mode_is_latched_and_survives_obscured_frame(
        clear_two_p, covered_two_p):
    """这是修 bug 的那一条：先确认，再遇到被圆盘压住的帧 -> 复用不再弃权。"""
    latch = TeaModeLatch()
    first = latch.observe(clear_two_p)
    assert (first.players, first.source) == (2, mode_vision.LATCH_SOURCE_CONFIRMED)
    assert first.latched is False

    covered = latch.observe(covered_two_p)
    assert covered.players == 2
    assert covered.source == mode_vision.LATCH_SOURCE_LATCHED
    assert covered.latched is True
    assert covered.raw_players is None   # 本帧逐帧口径确实读不出
    # 上次确认的分值要留给人复核
    assert covered.score >= PLAYER_SCORE_MIN
    assert covered.margin >= PLAYER_MARGIN_MIN


def test_latch_survives_long_streak_of_obscured_frames(clear_two_p, covered_two_p):
    """连续 20 帧按钮帧全都要放行（BOSS 实测 20/20 帧全中的那个场景）。"""
    latch = TeaModeLatch()
    latch.observe(clear_two_p)
    for index in range(20):
        reading = latch.observe(covered_two_p)
        assert reading.players == 2, f"第 {index + 1} 帧被推翻"
        assert reading.source == mode_vision.LATCH_SOURCE_LATCHED
    assert latch.snapshot()["age_frames"] == 20


def test_latch_survives_real_action_disc_frame():
    """真局证据：有行动圆盘的采集帧，锁定后仍能读出 2 人茶。"""
    folder = (TEA_ROOT / "采集" / "assist_sessions" / "20261023_201640" /
              "live" / "tea_2p")
    candidates = sorted(folder.glob("*.png"))
    if not candidates:
        pytest.skip("缺采集帧")
    source = load_frame(candidates[0])
    assert detect_tea_player_count(source) == 2

    latch = TeaModeLatch()
    latch.observe(source)
    broken = 0
    recovered = 0
    for path in candidates[1:80]:
        image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
        if detect_tea_player_count(image) is None:
            broken += 1
            if latch.observe(image).players == 2:
                recovered += 1
        else:
            latch.observe(image)
    assert broken > 0, "这一批真局帧里没有逐帧失败样本，证据失效"
    assert recovered == broken, "仍有圆盘帧被判成画面不认识"


# ---------------------------------------------------------------------------
# 3. 重置：换房 / 换玩法 / 换局 / 超时
# ---------------------------------------------------------------------------

def test_hud_fingerprint_change_invalidates_latch(two_p_frame):
    """换了房/换了局（HUD 指纹变了）：遮挡帧不许再吃上一局的锁。"""
    latch = TeaModeLatch()
    latch.observe(synth_frame(two_p_frame, hud_seed=0))
    assert latch.latched_players == 2

    other_room = synth_frame(two_p_frame, hud_seed=1, occlude_rule_text=True)
    reading = latch.observe(other_room)
    assert reading.players is None, "换局后仍按上一局的玩法决策 = 灾难"
    assert reading.source == mode_vision.LATCH_SOURCE_UNKNOWN
    assert latch.resets == 1


def test_same_hud_room_reconfirmation_is_unnecessary(two_p_frame):
    """同一间房里持续遮挡 → 一直复用；一旦再出现清晰帧就重新确认。"""
    latch = TeaModeLatch()
    latch.observe(synth_frame(two_p_frame, hud_seed=3))
    covered = synth_frame(two_p_frame, hud_seed=3, occlude_rule_text=True)
    for _ in range(5):
        assert latch.observe(covered).players == 2
    assert latch.resets == 0
    again = latch.observe(synth_frame(two_p_frame, hud_seed=3))
    assert again.source == mode_vision.LATCH_SOURCE_CONFIRMED


def test_caishen_change_invalidates_latch(two_p_frame, monkeypatch):
    """财神变了（温州茶每局重翻财神）→ 同样必须重新确认。"""
    frame = synth_frame(two_p_frame, hud_seed=0)
    monkeypatch.setattr(mode_vision, "visible_caishen_label", lambda _img: "s4")
    latch = TeaModeLatch()
    assert latch.observe(frame).players == 2

    monkeypatch.setattr(mode_vision, "visible_caishen_label", lambda _img: "t7")
    covered = synth_frame(two_p_frame, hud_seed=0, occlude_rule_text=True)
    reading = latch.observe(covered)
    assert reading.players is None
    assert latch.snapshot()["last_reason"] == "caishen_changed"


def test_ttl_expiry_invalidates_latch(clear_two_p, covered_two_p):
    """长时间没有有效帧（可能循环没看见换房）→ 超时失效。"""
    clock = {"t": 1000.0}
    latch = TeaModeLatch(ttl_s=30.0, clock=lambda: clock["t"])
    latch.observe(clear_two_p)
    clock["t"] += 10.0
    assert latch.observe(covered_two_p).players == 2       # 未过期，复用
    clock["t"] += 25.0                                     # 超过 30s
    # 还没到下一帧就能查到"这把锁已经过期了"（纯读，sweeper 不需要等下一帧）。
    stale = latch.snapshot()
    assert stale["expired"] is True and stale["latched"] is False

    reading = latch.observe(covered_two_p)
    assert reading.players is None
    assert latch.snapshot()["last_reason"] == "latch_ttl_expired"
    assert latch.snapshot()["players"] is None


def test_opposite_mode_flips_the_latch(clear_two_p, four_p_frame):
    """安全关键：锁了 2 之后走进 4 人房，必须立刻翻牌而不是继续按 2 决策。"""
    latch = TeaModeLatch()
    latch.observe(clear_two_p)
    assert latch.latched_players == 2

    four = latch.observe(synth_frame(four_p_frame, hud_seed=0))
    assert four.players == 4
    assert four.source == mode_vision.LATCH_SOURCE_CONFIRMED
    assert latch.latched_players == 4
    # 翻牌之后，同一张 4 人帧的遮挡版也只许按 4 走。
    covered_four = synth_frame(four_p_frame, hud_seed=0, occlude_rule_text=True)
    assert latch.observe(covered_four).players == 4


def test_manual_reset_forces_reconfirmation(clear_two_p, covered_two_p):
    latch = TeaModeLatch()
    latch.observe(clear_two_p)
    latch.reset(reason="room_switch")
    assert latch.observe(covered_two_p).players is None
    assert latch.observe(clear_two_p).players == 2


# ---------------------------------------------------------------------------
# 4. 可观测性：锁存状态 + action_ui
# ---------------------------------------------------------------------------

def test_snapshot_exposes_full_latch_state(clear_two_p, covered_two_p):
    clock = {"t": 500.0}
    latch = TeaModeLatch(ttl_s=DEFAULT_LATCH_TTL_S, clock=lambda: clock["t"])
    empty = latch.snapshot()
    assert empty["latched"] is False and empty["players"] is None

    latch.observe(clear_two_p)
    clock["t"] += 12.5
    latch.observe(covered_two_p)
    snap = latch.snapshot()
    assert snap["latched"] is True
    assert snap["players"] == 2
    assert snap["score"] >= PLAYER_SCORE_MIN
    assert snap["margin"] >= PLAYER_MARGIN_MIN
    assert snap["age_frames"] == 1
    assert snap["age_s"] == pytest.approx(12.5, abs=0.001)
    assert snap["resets"] == 0
    assert snap["ttl_s"] == DEFAULT_LATCH_TTL_S
    assert isinstance(snap["hud_known"], bool)
    assert snap["last_reason"] == "latched_mode_reused"


def test_reading_as_dict_is_json_serialisable(clear_two_p):
    reading = TeaModeLatch().observe(clear_two_p)
    payload = json.loads(json.dumps(reading.as_dict()))
    assert payload["players"] == 2
    assert isinstance(payload["action_ui"], dict)


def test_action_ui_reports_mode_scores_and_latch_flag(clear_two_p, covered_two_p):
    """action_ui 要能回答：凭着多少分确认的、这一帧是不是走了粘滞。"""
    latch = TeaModeLatch()
    ui = latch.observe(clear_two_p).action_ui
    assert ui["mode_source"] == mode_vision.LATCH_SOURCE_CONFIRMED
    assert ui["mode_latched"] is False
    assert ui["mode_raw_players"] == 2
    assert ui["mode_score"] >= PLAYER_SCORE_MIN

    ui = latch.observe(covered_two_p).action_ui
    assert ui["mode_source"] == mode_vision.LATCH_SOURCE_LATCHED
    assert ui["mode_latched"] is True
    assert ui["mode_raw_players"] is None      # 本帧逐帧口径读到的是 None
    assert ui["mode_players"] == 2             # 但结论仍然是已确认的 2
    assert ui["mode_latch_age_frames"] == 1


def test_action_ui_carries_required_keys(clear_two_p):
    """字段一个都不能少：之前有人做过但输出里查不到 action_ui，这里钉住。"""
    ui = TeaModeLatch().observe(clear_two_p).action_ui
    missing = [key for key in REQUIRED_ACTION_UI_KEYS if key not in ui]
    assert not missing, f"action_ui 缺字段: {missing}"
    assert isinstance(ui["action_discs"], list)
    assert isinstance(ui["action_disc_count"], int)
    assert ui["action_disc_count"] == len(ui["action_discs"])


def test_action_ui_records_real_discs():
    """真有圆盘时，数量与位置要进 action_ui（不是只给个 bool）。"""
    from claim_vision import read_claims

    folder = (TEA_ROOT / "采集" / "assist_sessions" / "20261023_201640" /
              "live" / "tea_2p")
    candidates = sorted(folder.glob("*.png"))
    if not candidates:
        pytest.skip("缺采集帧")

    with_discs = None
    for path in candidates:
        image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
        if read_claims(image).known:
            with_discs = image
            break
    if with_discs is None:
        pytest.skip("这一批帧里没有行动圆盘")

    ui = TeaModeLatch().observe(with_discs).action_ui
    assert ui["action_discs_seen"] is True
    assert ui["action_disc_count"] >= 1
    for button in ui["action_discs"]:
        assert len(button["box"]) == 4
        assert len(button["center"]) == 2
        assert all(isinstance(value, int) for value in button["box"])


def test_turn_gate_evidence_carries_action_ui(clear_two_p):
    """每帧输出里必须有 action_ui —— turn_gate 台账是看门狗的事实来源。"""
    result = {"mode": "tea_2p", "status": "advise", "hand_ids": [],
              "caishen": "t2", "suggested_boxes": [[0, 0, 1, 1]]}
    verdict = evaluate_turn(result, clear_two_p)
    assert "action_ui" in verdict.evidence, "输出里查不到 action_ui"
    assert "action_ui" in verdict.ledger()["evidence"]
    ui = verdict.evidence["action_ui"]
    assert set(ui) >= {"action_discs_seen", "action_disc_count", "action_discs",
                       "mode_latched", "mode_source", "mode_score"}
    assert isinstance(ui["mode_latched"], bool)


def test_turn_gate_action_ui_reflects_latched_mode(clear_two_p, covered_two_p):
    """同一管道的 evidence 要能看出"这一帧是吃粘滞过关的"。"""
    MODE_LATCH.observe(clear_two_p)
    ui = action_ui_evidence(covered_two_p)
    assert ui["mode_latched"] is True
    assert ui["mode_players"] == 2
    assert ui["mode_source"] == "latched"
    assert ui["mode_latch_available"] is True


# ---------------------------------------------------------------------------
# 5. 与既有 HUD 判据的一致性（防漂移）
# ---------------------------------------------------------------------------

def test_hud_fingerprint_matches_assist_window_on_real_frames(two_p_frame,
                                                              four_p_frame):
    """本模块按帧重写了 assist_window 的 HUD 指纹；两边必须判成同一局。

    assist_window 只吃文件路径，本层只有 image，所以只能各写一份。这个用例把
    两份实现钉在一起：任一侧改了 crop/量化都会立刻报警。
    """
    from assist_window import same_tea_round, tea_round_fingerprint

    for path, frame in ((TWO_P_FRAME, two_p_frame), (FOUR_P_FRAME, four_p_frame)):
        theirs = tea_round_fingerprint(path)
        mine = hud_fingerprint(frame)
        assert theirs is not None and mine is not None, path
        assert same_tea_round(theirs, mine), f"{path.name}: 两份 HUD 指纹不一致"
        assert same_hud_fingerprint(theirs, mine)


def test_unreadable_signals_are_treated_as_unknown_not_mismatch():
    """读不到的信号不许自己产生重置 —— 与 auto_play_loop.same_round 同口径。"""
    assert same_hud_fingerprint(None, b"anything") is True
    assert same_hud_fingerprint(b"anything", None) is True
    assert same_hud_fingerprint(None, None) is True
    assert visible_caishen_label(np.zeros((900, 1600, 3), np.uint8)) is None
    assert frame_round_identity(None) == (None, None)


# ---------------------------------------------------------------------------
# 6. TeaBot 接线：不再抛"未确认茶 2 人局"
# ---------------------------------------------------------------------------

def _new_bot(tmp_path):
    from tea_bot import TeaBot
    return TeaBot(dry_run=True, log_dir=str(tmp_path))


def _close_bot(bot) -> None:
    for handler in list(bot.logger.handlers):
        bot.logger.removeHandler(handler)
        handler.close()


def _mode_error(bot, png: bytes) -> str | None:
    """跑一次识牌，返回"玩法门禁"那一层的错误；过了门禁则返回 None。"""
    try:
        bot._default_vision(png)
    except ValueError as exc:
        message = str(exc)
        return message if ("未确认茶" in message or "人局" in message) else None
    except Exception:  # noqa: BLE001 - 门禁之后的失败说明门禁已放行
        return None
    return None


def test_tea_bot_still_abstains_on_never_confirmed_frame(tmp_path):
    """改完之后，一次都没确认过的画面照样抛"未确认茶 2 人局"。"""
    bot = _new_bot(tmp_path)
    blank = synth_frame(None)
    ok, buffer = cv2.imencode(".png", blank)
    assert ok
    with pytest.raises(ValueError, match="未确认茶 2 人局"):
        bot._default_vision(buffer.tobytes())
    _close_bot(bot)


def test_tea_bot_recovers_covered_frame_after_confirmation(tmp_path, clear_two_p,
                                                           covered_two_p):
    """BOSS 报的现象在 ``_default_vision`` 这一层的复现与修复。"""
    bot = _new_bot(tmp_path)
    clear_ok, clear_png = cv2.imencode(".png", clear_two_p)
    covered_ok, covered_png = cv2.imencode(".png", covered_two_p)
    assert clear_ok and covered_ok

    before = _mode_error(bot, covered_png.tobytes())
    assert before is not None and "未确认茶" in before, "修改前本该弃权"

    _mode_error(bot, clear_png.tobytes())        # 确认一次（之后识牌会失败）
    after = _mode_error(bot, covered_png.tobytes())
    assert after is None, f"圆盘遮挡帧又被整帧弃权了：{after}"
    _close_bot(bot)


def test_tea_bot_read_player_count_exposes_action_ui(tmp_path, clear_two_p):
    """TeaBot 要把 action_ui 暴露出来供上层逐帧记录。"""
    bot = _new_bot(tmp_path)
    assert bot.last_action_ui == {}
    players, ui = bot._read_player_count(clear_two_p, detect_tea_player_count)
    assert players == 2
    missing = [key for key in REQUIRED_ACTION_UI_KEYS if key not in ui]
    assert not missing, f"TeaBot 读出的 action_ui 缺字段: {missing}"
    _close_bot(bot)


def test_tea_bot_read_player_count_falls_back_without_mode_module(
        tmp_path, clear_two_p, monkeypatch):
    """``mode_vision`` 被 SimpleNamespace 替身顶掉时，行为与修改前完全一致。"""
    import types

    bot = _new_bot(tmp_path)
    monkeypatch.setitem(sys.modules, "mode_vision", types.SimpleNamespace(
        detect_tea_player_count=lambda _img: 2,
        detect_tea_no_zhong_fa=lambda _img: True))
    players, ui = bot._read_player_count(clear_two_p, lambda _img: 2)
    assert players == 2
    assert ui["mode_source"] == "per_frame"
    assert ui["mode_latched"] is False
    _close_bot(bot)


def test_global_latch_helpers_round_trip(clear_two_p, covered_two_p):
    """``confirm_tea_player_count`` / ``latest_action_ui`` 是给上层用的出口。"""
    from mode_vision import confirm_tea_player_count

    assert confirm_tea_player_count(covered_two_p) is None   # 未确认 → 弃权
    assert confirm_tea_player_count(clear_two_p) == 2
    assert confirm_tea_player_count(covered_two_p) == 2      # 锁存生效
    ui = latest_action_ui()
    assert set(REQUIRED_ACTION_UI_KEYS) <= set(ui)
    reset_tea_mode_latch(reason="test")
    assert MODE_LATCH.latched_players is None
    assert confirm_tea_player_count(covered_two_p) is None
