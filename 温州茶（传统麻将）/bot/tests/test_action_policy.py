# -*- coding: utf-8 -*-
"""Tests for bot/action_policy.py — the 吃/碰/杠/胡/过 decision gate.

The point of this suite is the fail-closed contract: every path that cannot be
established from pixels **plus** the card engine must abstain, and only a
post-click state transition that really happened may be reported as verified.
Frames here are synthetic (numpy/cv2) like in test_claim_vision.py; the card
engine is the real ``brain_v0`` engine, so "engine says no" is a real verdict.
"""

import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import action_policy as ap  # noqa: E402
from action_policy import (  # noqa: E402
    ACTION_CHI,
    ACTION_HU,
    ACTION_KONG,
    ACTION_PASS,
    ACTION_PENG,
    ActionDecision,
    ActionPolicyConfig,
    BoardState,
    ClaimButton,
    ClaimState,
    HandState,
    blacklist_hit,
    confirm_action,
    decide_action,
    engine_available,
    human_confirm_required,
    match_button_glyph,
    never_tap,
    verify_after_click,
)

pytestmark = pytest.mark.skipif(
    not engine_available(), reason="茶规则引擎不可用，L2 相关的断言无法成立")

# L2 unit tests intentionally skip L1 glyph match (no button templates in CI).
ADVISORY = ActionPolicyConfig(require_template_match=False)



# ---------------------------------------------------------------------------
# 合成画面与圆盘
# ---------------------------------------------------------------------------

TABLE_BGR = (20, 50, 30)
GOLD_BGR = cv2.cvtColor(np.uint8([[[25, 200, 220]]]), cv2.COLOR_HSV2BGR)[0][0]

# 已验证的动作行口径（claim_vision 常数）：中心 x 754..1501、y 578..645，
# 边长按 80..200。下面三个框都落在这个范围内。
GOLD_CENTER = (1033, 600)
GOLD_CENTER_B = (1242, 600)
PASS_CENTER = (1431, 600)
CANCEL_CENTER = (1431, 300)          # 动作行带之外 → 取消


def _box(center: tuple[int, int], half_w: int, half_h: int) -> tuple[int, int, int, int]:
    return (center[0] - half_w, center[1] - half_h,
            center[0] + half_w, center[1] + half_h)


GOLD_BOX = _box(GOLD_CENTER, 63, 66)
GOLD_BOX_B = _box(GOLD_CENTER_B, 63, 66)
PASS_BOX = _box(PASS_CENTER, 50, 50)
CANCEL_BOX = _box(CANCEL_CENTER, 50, 50)


def gold_button() -> ClaimButton:
    return ClaimButton(kind="gold", box=GOLD_BOX, center=GOLD_CENTER)


def pass_button() -> ClaimButton:
    return ClaimButton(kind="pass", box=PASS_BOX, center=PASS_CENTER)


def hu_button() -> ClaimButton:
    return ClaimButton(kind="hu", box=GOLD_BOX, center=GOLD_CENTER)


def cancel_button() -> ClaimButton:
    return ClaimButton(kind="cancel", box=CANCEL_BOX, center=CANCEL_CENTER)


def state(*buttons: ClaimButton, decision: str | None = None,
          reason: str = "test") -> ClaimState:
    known = bool(buttons)
    return ClaimState(buttons=tuple(buttons), decision=decision, reason=reason,
                      evidence={}, known=known)


# ---------------------------------------------------------------------------
# 手牌（引擎编码，闭手 16 张等待态）
# ---------------------------------------------------------------------------

#: 来 1 万可碰、不可杠、不成胡（2 人局不评估吃）。
PENG_HAND = HandState(concealed=(1, 1, 2, 4, 5, 6, 7, 8, 9,
                                 21, 22, 23, 24, 41, 42, 43),
                      incoming_tile=1, incoming_source="opponent", player_count=2, source="test")
#: 同一手牌，来牌是手里没有的南 → 碰/杠/吃/胡全不可行。
NO_CLAIM_HAND = HandState(concealed=(1, 1, 2, 4, 5, 6, 7, 8, 9,
                                     21, 22, 23, 24, 41, 42, 43),
                          incoming_tile=62, incoming_source="opponent", player_count=2, source="test")
#: 闭手 + 来牌正好成胡。
HU_HAND = HandState(concealed=(1, 1, 2, 3, 4, 5, 6, 7, 8, 9,
                               21, 22, 23, 41, 42, 43),
                    incoming_tile=1, incoming_source="opponent", player_count=2, source="test")
#: 手里三张 → 碰与杠同时可行（没有字形模板时不可分辨）。
KONG_HAND = HandState(concealed=(1, 1, 1, 2, 4, 5, 6, 7, 8, 9,
                                 21, 22, 23, 41, 42, 43),
                      incoming_tile=1, incoming_source="opponent", player_count=2, source="test")
#: 来 25 筒可碰，但碰完向听变差（2 → 3）：应当改按过。
WORSEN_HAND = HandState(concealed=(1, 6, 7, 8, 9, 23, 24, 25, 25,
                                   26, 27, 27, 29, 45, 47, 48),
                        incoming_tile=25, incoming_source="opponent", player_count=2, source="test")


# ---------------------------------------------------------------------------
# L1：读不出按钮 → None
# ---------------------------------------------------------------------------

def test_no_buttons_returns_none() -> None:
    """画面上没有任何圆盘：连过都不点，直接 None。"""
    assert decide_action(state(), PENG_HAND, ADVISORY) is None


def test_unknown_claim_state_returns_none() -> None:
    """``known=False`` 不等于"没有行动要做"，所以必须 None。"""
    assert decide_action(state(decision=None, reason=""), PENG_HAND, ADVISORY) is None


def test_non_claim_state_returns_none() -> None:
    assert decide_action(None, PENG_HAND, ADVISORY) is None
    assert decide_action("not a state", PENG_HAND, ADVISORY) is None  # type: ignore[arg-type]


def test_empty_frame_read_returns_none() -> None:
    """真实空帧走 ``read_and_decide`` 也必须 None。"""
    frame = np.zeros((900, 1600, 3), np.uint8)
    frame[:, :] = TABLE_BGR
    assert ap.read_and_decide(frame, PENG_HAND) is None


# ---------------------------------------------------------------------------
# cancel → 弃权
# ---------------------------------------------------------------------------

def test_cancel_present_abstains() -> None:
    """出现取消盘：点它会不可逆地中断进行中的选择，一律 None。"""
    assert decide_action(state(gold_button(), cancel_button()), PENG_HAND, ADVISORY) is None


def test_cancel_alone_abstains() -> None:
    assert decide_action(state(cancel_button()), PENG_HAND, ADVISORY) is None


def test_cancel_with_hu_still_abstains() -> None:
    """哪怕同时有胡盘，取消盘也压倒一切。"""
    assert decide_action(state(hu_button(), cancel_button()), HU_HAND, ADVISORY) is None


# ---------------------------------------------------------------------------
# L2：引擎否决
# ---------------------------------------------------------------------------

def test_engine_says_cannot_peng_then_abstain() -> None:
    """画面出现金盘，但引擎判定碰/杠/吃都不可行 → 弃权，绝不点。"""
    decision = decide_action(state(gold_button(), pass_button()), NO_CLAIM_HAND, ADVISORY)
    assert decision is not None, "有按钮就必须留下证据，不能静默 None"
    assert decision.blocked_reason, "应当被 L2 拦下"
    assert decision.tap_point is None
    assert not decision.executable
    assert decision.abstained
    assert "引擎" in decision.blocked_reason or "L2" in decision.blocked_reason


def test_engine_says_cannot_peng_does_not_fall_back_to_pass() -> None:
    """引擎与画面矛盾时不能"顺手点过"——那是拿不可信状态做决定。"""
    decision = decide_action(state(gold_button(), pass_button()), NO_CLAIM_HAND,
                             ActionPolicyConfig(auto_click_enabled=True,
                                                confirmed_actions=frozenset({"pass"})))
    assert decision is not None
    assert decision.action != ACTION_PASS
    assert not decision.executable


def test_unknown_incoming_tile_blocks_claims() -> None:
    """来牌未知 → 碰/吃/杠/胡全不可验证，金盘在也只能弃权。"""
    hand = HandState(concealed=PENG_HAND.concealed, incoming_tile=None,
                     player_count=2)
    decision = decide_action(state(gold_button(), pass_button()), hand, ADVISORY)
    assert decision is not None
    assert not decision.executable
    assert "来牌未知" in decision.l2_reason


def test_incoming_source_matches_discard_vision_constants() -> None:
    """两个模块的归属字面量必须一致，否则闸门会静默失效。"""
    import discard_vision

    assert ap.INCOMING_SOURCE_OPPONENT == discard_vision.SOURCE_OPPONENT
    assert ap.INCOMING_SOURCE_SELF == discard_vision.SOURCE_SELF
    assert ap.INCOMING_SOURCE_OPPONENT in ap.CLAIMABLE_INCOMING_SOURCES
    assert ap.INCOMING_SOURCE_SELF not in ap.CLAIMABLE_INCOMING_SOURCES


def test_self_discard_is_never_claimable() -> None:
    """自己刚打出的牌不能拿来碰/吃/杠/胡——行动行常在自家出牌后刷出来。"""
    hand = HandState(concealed=PENG_HAND.concealed, incoming_tile=1,
                     incoming_source="self", player_count=2)
    report = ap.evaluate_claim_candidates(hand, ActionPolicyConfig(require_template_match=False))
    for action in ap.CLAIM_ACTIONS:
        assert not report[action]["feasible"], action
        assert "self" in report[action]["why"], action

    decision = decide_action(state(gold_button(), pass_button()), hand, ADVISORY)
    assert decision is not None
    assert not decision.executable
    assert decision.tap_point is None


def test_self_discard_blocks_even_the_hu_button() -> None:
    """胡盘也一样：自家弃牌不能成胡。"""
    hand = HandState(concealed=HU_HAND.concealed, incoming_tile=1,
                     incoming_source=ap.INCOMING_SOURCE_SELF, player_count=2)
    decision = decide_action(state(hu_button(), pass_button()), hand, ADVISORY)
    assert decision is not None
    assert not decision.executable
    assert "self" in decision.blocked_reason or "self" in decision.l2_reason


def test_opponent_discard_is_claimable() -> None:
    """别家打出的牌照常可以碰（对照组，防止闸门写反）。"""
    hand = HandState(concealed=PENG_HAND.concealed, incoming_tile=1,
                     incoming_source="opponent", player_count=2)
    decision = decide_action(state(gold_button(), pass_button()), hand, ADVISORY)
    assert decision is not None
    assert decision.action == ACTION_PENG


def test_unrecognised_incoming_source_is_blocked() -> None:
    """不认识的归属口径一律不信任（fail closed）。"""
    hand = HandState(concealed=PENG_HAND.concealed, incoming_tile=1,
                     incoming_source="上家", player_count=2)
    decision = decide_action(state(gold_button(), pass_button()), hand, ADVISORY)
    assert decision is not None
    assert not decision.executable


def test_unlabelled_incoming_source_abstains() -> None:
    """来牌归属未知（None）一律弃权：证据不足不得把可执行碰当真。"""
    hand = HandState(concealed=PENG_HAND.concealed, incoming_tile=1,
                     incoming_source=None, player_count=2)
    decision = decide_action(state(gold_button(), pass_button()), hand, ADVISORY)
    assert decision is not None
    assert not decision.executable
    assert decision.action == ""
    assert "归属未知" in (decision.l2_reason or decision.blocked_reason or "")


@pytest.mark.parametrize("payload,expected", [
    ({"incoming_source": "self"}, "self"),
    ({"incoming_source": "opponent"}, "opponent"),
    ({"discards": {"incoming_tile": 1, "incoming_source": "self"}}, "self"),
    ({"incoming_source": ""}, None),
    ({"incoming_source": 7}, None),
    ({}, None),
])
def test_incoming_source_of_parses_every_shape(payload: dict,
                                               expected: str | None) -> None:
    """``from_analysis`` 能同时吃下 DiscardState 本体 / as_dict / 顶层键。"""
    assert ap._incoming_source_of(payload) == expected


def test_incoming_source_of_accepts_discard_state_object() -> None:
    import discard_vision

    state = discard_vision.DiscardState(incoming_tile=1, incoming_source="self")
    assert ap._incoming_source_of({"discards": state}) == "self"


def test_hand_count_not_conserved_blocks() -> None:
    """张数不守恒的手牌不能被用来做任何判定。"""
    hand = HandState(concealed=(1, 1, 2, 4), incoming_tile=1,
                     incoming_source="opponent", player_count=2)
    decision = decide_action(state(gold_button(), pass_button()), hand, ADVISORY)
    assert decision is not None
    assert not decision.executable
    assert "张数不守恒" in decision.l2_reason


def test_ambiguous_peng_and_kong_abstains() -> None:
    """手里三张时碰与杠都合法，L1 分不出是哪一个 → 弃权，不猜。"""
    decision = decide_action(state(gold_button(), pass_button()), KONG_HAND, ADVISORY)
    assert decision is not None
    assert decision.blocked_reason
    assert "弃权" in decision.blocked_reason
    assert decision.tap_point is None


# ---------------------------------------------------------------------------
# L2：引擎放行
# ---------------------------------------------------------------------------

def test_engine_confirms_peng() -> None:
    decision = decide_action(state(gold_button(), pass_button()), PENG_HAND, ADVISORY)
    assert decision is not None
    assert decision.action == ACTION_PENG
    assert decision.target_tile == 1
    assert decision.button is not None
    assert decision.tap_point == GOLD_CENTER
    assert not decision.blocked_reason
    assert decision.needs_human_confirm, "默认未经人工确认，不许自动点"
    assert not decision.executable


def test_hu_requires_engine_confirmation() -> None:
    decision = decide_action(state(hu_button(), pass_button()), HU_HAND, ADVISORY)
    assert decision is not None
    assert decision.action == ACTION_HU
    assert decision.confidence > 0.0


def test_hu_button_without_engine_confirmation_is_blocked() -> None:
    """画面出现胡盘，但引擎说不成胡 → 拦下并落证。"""
    decision = decide_action(state(hu_button(), pass_button()), NO_CLAIM_HAND, ADVISORY)
    assert decision is not None
    assert not decision.executable
    assert decision.action == ""


def test_hu_with_melds_is_not_verifiable() -> None:
    """副露局面的胡牌判定未验收 → 即使有胡盘也不认。"""
    hand = HandState(concealed=(1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41),
                     melds=((41, 42, 43),), incoming_tile=42,
                     incoming_source="opponent", player_count=2)
    assert hand.is_valid
    decision = decide_action(state(hu_button()), hand, ADVISORY)
    assert decision is not None
    assert not decision.executable
    assert "副露" in decision.blocked_reason


def test_pass_alone_is_blocked_by_default() -> None:
    """过从不单独出现（已验证口径），单独出现即视为状态不可信。"""
    decision = decide_action(state(pass_button()), NO_CLAIM_HAND, ADVISORY)
    assert decision is not None
    assert not decision.executable
    assert decision.action == ""


def test_pass_alone_allowed_when_opted_in() -> None:
    config = ActionPolicyConfig(require_template_match=False, allow_pass_without_claim=True)
    decision = decide_action(state(pass_button()), NO_CLAIM_HAND, config)
    assert decision is not None
    assert decision.action == ACTION_PASS
    assert decision.tap_point == PASS_CENTER
    assert not decision.blocked_reason


def test_non_improving_claim_falls_back_to_pass() -> None:
    """引擎判定碰会劣化向听 → 改按过，而不是硬碰。"""
    decision = decide_action(state(gold_button(), pass_button()), WORSEN_HAND, ADVISORY)
    assert decision is not None
    assert decision.action == ACTION_PASS
    assert decision.tap_point == PASS_CENTER
    assert "劣化" in decision.l2_reason


def test_non_improving_claim_without_pass_is_blocked() -> None:
    """会劣化向听又没有可点的过 → 弃权。"""
    decision = decide_action(state(gold_button()), WORSEN_HAND, ADVISORY)
    assert decision is not None
    assert not decision.executable
    assert "劣化" in decision.blocked_reason
    assert decision.tap_point is None


def test_improvement_gate_can_be_switched_off() -> None:
    """``require_improvement=False`` 时不再因劣化而改判（仍须人工确认）。"""
    config = ActionPolicyConfig(require_template_match=False, require_improvement=False)
    decision = decide_action(state(gold_button(), pass_button()), WORSEN_HAND, config)
    assert decision is not None
    assert decision.action == ACTION_PENG


def test_end_to_end_read_and_decide_on_synthetic_frame() -> None:
    """真实走 ``read_claims`` → ``decide_action``：金盘 + 过 → 碰。"""
    frame = np.zeros((900, 1600, 3), np.uint8)
    frame[:, :] = TABLE_BGR
    blue_bgr = cv2.cvtColor(np.uint8([[[95, 200, 160]]]), cv2.COLOR_HSV2BGR)[0][0]
    cv2.circle(frame, GOLD_CENTER, 66, tuple(int(c) for c in GOLD_BGR), thickness=-1)
    cv2.circle(frame, PASS_CENTER, 50, tuple(int(c) for c in blue_bgr), thickness=-1)

    read = ap.read_claims(frame)
    assert read.known, read.reason
    kinds = sorted(button.kind for button in read.buttons)
    assert kinds == ["gold", "pass"], kinds

    decision = ap.read_and_decide(frame, PENG_HAND, ADVISORY)
    assert decision is not None
    assert decision.action == ACTION_PENG
    assert decision.button is not None
    assert decision.button.kind == "gold"


def test_chi_disabled_by_default() -> None:
    report = ap.evaluate_claim_candidates(
        HandState(concealed=(1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 41, 42, 43),
                  incoming_tile=2, incoming_source="opponent", player_count=4),
        ActionPolicyConfig(require_template_match=False))
    assert not report[ACTION_CHI]["feasible"]
    assert "未接线" in report[ACTION_CHI]["why"]


def test_chi_needs_four_players() -> None:
    """2 人局没有上家可吃，即便打开开关也不评估。"""
    config = ActionPolicyConfig(require_template_match=False, chi_enabled=True)
    report = ap.evaluate_claim_candidates(
        HandState(concealed=(1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 41, 42, 43),
                  incoming_tile=2, incoming_source="opponent", player_count=2),
        config)
    assert not report[ACTION_CHI]["feasible"]
    assert "2 人局" in report[ACTION_CHI]["why"]


def test_chi_feasible_in_four_player_game() -> None:
    config = ActionPolicyConfig(require_template_match=False, chi_enabled=True)
    report = ap.evaluate_claim_candidates(
        HandState(concealed=(1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 41, 42, 43),
                  incoming_tile=2, incoming_source="opponent", player_count=4),
        config)
    assert report[ACTION_CHI]["feasible"]
    assert report[ACTION_CHI]["options"]


# ---------------------------------------------------------------------------
# L1 模板匹配（口径照 mode_vision.py）
# ---------------------------------------------------------------------------

def _frame_with_two_discs() -> np.ndarray:
    """两张不同字形的金盘：A 中心暗方块，B 中心亮方块（反相，区分度足够）。"""
    frame = np.zeros((900, 1600, 3), np.uint8)
    frame[:, :] = TABLE_BGR
    for center, glyph_bgr in ((GOLD_CENTER, (0, 0, 0)), (GOLD_CENTER_B, (255, 255, 255))):
        cv2.circle(frame, center, 66, tuple(int(c) for c in GOLD_BGR), thickness=-1)
        cv2.rectangle(frame, (center[0] - 26, center[1] - 26),
                      (center[0] + 26, center[1] + 26),
                      tuple(int(c) for c in glyph_bgr), thickness=-1)
    return frame


def test_template_gate_requires_top1_and_margin() -> None:
    frame = _frame_with_two_discs()
    crop_a = frame[GOLD_BOX[1]:GOLD_BOX[3], GOLD_BOX[0]:GOLD_BOX[2]]
    # 次名做成与原图几乎一样 → top1 达标但次名差过小 → ambiguous。
    near = crop_a.copy().astype(np.int16)
    near[::7, ::7] = np.clip(near[::7, ::7] + 6, 0, 255)
    matched = match_button_glyph(frame, gold_button(),
                                 {"kong": crop_a, "peng": near.astype(np.uint8)})
    assert matched["top1"] >= ap.TEMPLATE_TOP1_MIN
    assert matched["status"] == ap.TEMPLATE_STATUS_AMBIGUOUS
    assert matched["margin"] < ap.TEMPLATE_MARGIN_MIN


def test_template_gate_rejects_weak_top1() -> None:
    frame = _frame_with_two_discs()
    noise = np.random.default_rng(7).integers(0, 256, (60, 60), dtype=np.uint8)
    matched = match_button_glyph(frame, gold_button(), {"peng": noise})
    assert matched["status"] == ap.TEMPLATE_STATUS_UNMATCHED
    assert matched["top1"] < ap.TEMPLATE_TOP1_MIN


def test_template_matches_when_distinct() -> None:
    frame = _frame_with_two_discs()
    crop_a = frame[GOLD_BOX[1]:GOLD_BOX[3], GOLD_BOX[0]:GOLD_BOX[2]]
    crop_b = frame[GOLD_BOX_B[1]:GOLD_BOX_B[3], GOLD_BOX_B[0]:GOLD_BOX_B[2]]
    matched = match_button_glyph(frame, gold_button(),
                                 {"kong": crop_a, "peng": crop_b})
    assert matched["status"] == ap.TEMPLATE_STATUS_MATCHED
    assert matched["best"] == "kong"
    assert matched["margin"] >= ap.TEMPLATE_MARGIN_MIN


def test_no_templates_reported_as_such() -> None:
    frame = _frame_with_two_discs()
    matched = match_button_glyph(frame, gold_button(), {})
    assert matched["status"] == ap.TEMPLATE_STATUS_NO_TEMPLATES


def test_template_label_disambiguates_kong() -> None:
    """有字形模板时，L1 认出"杠"且引擎认可 → 才能产出杠。

    没有模板时同一个局面被判为"碰/杠不可分辨"而弃权（见
    :func:`test_ambiguous_peng_and_kong_abstains`）。
    """
    frame = _frame_with_two_discs()
    crop_a = frame[GOLD_BOX[1]:GOLD_BOX[3], GOLD_BOX[0]:GOLD_BOX[2]]
    crop_b = frame[GOLD_BOX_B[1]:GOLD_BOX_B[3], GOLD_BOX_B[0]:GOLD_BOX_B[2]]
    provider = lambda: {"kong": crop_a, "peng": crop_b}  # noqa: E731
    config = ActionPolicyConfig(template_provider=provider)
    decision = decide_action(state(gold_button(), pass_button()), KONG_HAND,
                             config, image=frame)
    assert decision is not None
    assert decision.action == ACTION_KONG
    assert decision.tap_point == GOLD_CENTER
    assert not decision.blocked_reason


def test_require_template_match_blocks_without_templates() -> None:
    """``require_template_match=True`` 时没有模板就绝不放行。"""
    config = ActionPolicyConfig(require_template_match=True)
    decision = decide_action(state(gold_button(), pass_button()), PENG_HAND, config)
    assert decision is not None
    assert not decision.executable
    assert "模板未过闸" in decision.blocked_reason


def test_geometry_outside_verified_band_is_blocked() -> None:
    """圆盘几何不在已验证口径内 → L1 不放行。"""
    stray = ClaimButton(kind="gold", box=(100, 100, 226, 232), center=(163, 166))
    decision = decide_action(state(stray, pass_button()), PENG_HAND, ADVISORY)
    assert decision is not None
    assert not decision.executable
    assert "几何" in decision.blocked_reason


# ---------------------------------------------------------------------------
# 黑名单
# ---------------------------------------------------------------------------

def test_cost_buttons_are_hardcoded() -> None:
    assert ap.COST_BUTTONS["洗牌"] == "房卡×2"
    assert ap.COST_BUTTONS["切牌"] == "房卡×4"
    assert never_tap("洗牌") and never_tap("切牌")
    assert never_tap(None), "未知标签按不可点处理"


def test_blacklist_box_hit() -> None:
    config = ActionPolicyConfig(require_template_match=False, forbidden_boxes=(GOLD_BOX,))
    decision = decide_action(state(gold_button(), pass_button()), PENG_HAND, config)
    assert decision is not None
    assert not decision.executable
    assert "黑名单" in decision.blocked_reason
    assert decision.tap_point is None


def test_blacklist_never_yields_an_action_even_when_engine_agrees() -> None:
    """黑名单命中时，哪怕引擎说能碰、开关全开，也不许产出任何动作。"""
    config = ActionPolicyConfig(require_template_match=False, 
        forbidden_boxes=(GOLD_BOX,),
        auto_click_enabled=True,
        confirmed_actions=frozenset(ap.ALL_ACTIONS),
    )
    decision = decide_action(state(gold_button(), pass_button()), PENG_HAND, config)
    assert decision is not None
    assert decision.action == ""
    assert not decision.executable
    assert decision.tap_point is None


def test_blacklist_template_label_blocks() -> None:
    """模板认出的是花钱按钮（如 洗牌）→ 永不产出动作。"""
    frame = _frame_with_two_discs()
    crop_a = frame[GOLD_BOX[1]:GOLD_BOX[3], GOLD_BOX[0]:GOLD_BOX[2]]
    config = ActionPolicyConfig(template_provider=lambda: {"洗牌": crop_a})
    decision = decide_action(state(gold_button(), pass_button()), PENG_HAND,
                             config, image=frame)
    assert decision is not None
    assert "洗牌" in decision.blocked_reason
    assert "房卡×2" in decision.blocked_reason
    assert decision.tap_point is None


def test_blacklist_hit_direct() -> None:
    assert blacklist_hit([gold_button()], ActionPolicyConfig(require_template_match=False),
                         matched_label="切牌") is not None
    assert blacklist_hit([gold_button()], ActionPolicyConfig(require_template_match=False),
                         matched_label="碰") is None
    assert blacklist_hit([gold_button()], ActionPolicyConfig(require_template_match=False)) is None


# ---------------------------------------------------------------------------
# 人工确认闸门
# ---------------------------------------------------------------------------

def test_human_confirm_gate_defaults_to_blocked() -> None:
    config = ActionPolicyConfig(require_template_match=False)
    for action in ap.ALL_ACTIONS:
        assert human_confirm_required(action, config), f"{action} 默认必须人工确认"


def test_human_confirm_gate_per_action() -> None:
    config = ActionPolicyConfig(auto_click_enabled=True)
    assert human_confirm_required(ACTION_PENG, config)
    assert not human_confirm_required(ACTION_PENG, confirm_action(config, ACTION_PENG))
    assert human_confirm_required(ACTION_HU, confirm_action(config, ACTION_PENG))


def test_auto_click_switch_enables_executable_decision() -> None:
    """With glyph templates + auto + confirmed, decision is executable."""
    frame = np.zeros((900, 1600, 3), np.uint8)
    frame[:, :] = TABLE_BGR
    cv2.circle(frame, GOLD_CENTER, 66, tuple(int(c) for c in GOLD_BGR), thickness=-1)
    crop = frame[GOLD_BOX[1]:GOLD_BOX[3], GOLD_BOX[0]:GOLD_BOX[2]]
    provider = lambda: {"peng": crop}  # noqa: E731
    config = confirm_action(
        ActionPolicyConfig(auto_click_enabled=True, template_provider=provider),
        ACTION_PENG,
    )
    decision = decide_action(state(gold_button(), pass_button()), PENG_HAND,
                             config, image=frame)
    assert decision is not None
    assert decision.action == ACTION_PENG
    assert decision.executable
    assert decision.needs_human_confirm is False



def test_confirm_action_rejects_unknown_action() -> None:
    with pytest.raises(ValueError):
        confirm_action(ActionPolicyConfig(require_template_match=False), "洗牌")


def test_claim_tap_channel_not_wired_yet() -> None:
    """点击通道还没打通，测试把这件事钉住：有人接线后必须回来改这个断言。"""
    assert ap.CLAIM_TAP_CHANNEL_READY is False


# ---------------------------------------------------------------------------
# L3：点击后状态迁移
# ---------------------------------------------------------------------------

def _before(buttons: int = 2) -> BoardState:
    return BoardState(concealed_count=16, melds=(), claim_buttons=buttons,
                      settled=False, turn_owner="opponent")


def test_l3_peng_positive() -> None:
    after = BoardState(concealed_count=14, melds=((1, 1, 1),), claim_buttons=0,
                       settled=False, turn_owner="me")
    ok, why = verify_after_click(_before(), after, ACTION_PENG)
    assert ok, why


@pytest.mark.parametrize("concealed,melds,buttons,marker", [
    (15, ((1, 1, 1),), 0, "暗手只少了 1 张"),
    (14, (), 0, "没有多出副露"),
    (14, ((1, 1, 1),), 2, "圆盘没消失"),
])
def test_l3_peng_negative(concealed: int, melds: tuple, buttons: int,
                          marker: str) -> None:
    after = BoardState(concealed_count=concealed, melds=melds,
                       claim_buttons=buttons, settled=False, turn_owner="me")
    ok, _why = verify_after_click(_before(), after, ACTION_PENG)
    assert not ok, marker


def test_l3_chi_positive_and_negative() -> None:
    after_ok = BoardState(concealed_count=14, melds=((1, 2, 3),), claim_buttons=0,
                          settled=False, turn_owner="me")
    assert verify_after_click(_before(), after_ok, ACTION_CHI)[0]
    # 吃的副露组是 3 张；若是 4 张（张数不对）即判失败。
    after_bad = BoardState(concealed_count=14, melds=((1, 2, 3, 4),),
                           claim_buttons=0, settled=False, turn_owner="me")
    ok, why = verify_after_click(_before(), after_bad, ACTION_CHI)
    assert not ok, why


def test_l3_kong_from_discard_positive() -> None:
    after = BoardState(concealed_count=13, melds=((1, 1, 1, 1),), claim_buttons=0,
                       settled=False, turn_owner="me")
    ok, why = verify_after_click(_before(), after, ACTION_KONG)
    assert ok, why


def test_l3_kong_upgrade_positive() -> None:
    before = BoardState(concealed_count=13, melds=((1, 1, 1),), claim_buttons=2,
                        settled=False, turn_owner="opponent")
    after = BoardState(concealed_count=12, melds=((1, 1, 1, 1),), claim_buttons=0,
                       settled=False, turn_owner="me")
    ok, why = verify_after_click(before, after, ACTION_KONG)
    assert ok, why


@pytest.mark.parametrize("concealed,melds,marker", [
    (14, ((1, 1, 1, 1),), "暗手少了 2 张，既不是明杠也不是加杠"),
    (13, ((1, 1, 1),), "副露张数没长到 4"),
])
def test_l3_kong_negative(concealed: int, melds: tuple, marker: str) -> None:
    after = BoardState(concealed_count=concealed, melds=melds, claim_buttons=0,
                       settled=False, turn_owner="me")
    ok, _why = verify_after_click(_before(), after, ACTION_KONG)
    assert not ok, marker


def test_l3_hu_positive_by_settlement() -> None:
    after = BoardState(concealed_count=17, melds=(), claim_buttons=0,
                       settled=True, turn_owner="me")
    ok, why = verify_after_click(_before(), after, ACTION_HU)
    assert ok, why


def test_l3_hu_positive_by_buttons_gone() -> None:
    after = BoardState(concealed_count=17, melds=(), claim_buttons=0,
                       settled=None, turn_owner="me")
    ok, why = verify_after_click(_before(), after, ACTION_HU)
    assert ok, why


@pytest.mark.parametrize("settled,buttons,marker", [
    (False, 2, "没进结算且圆盘还在"),
    (None, None, "结算与圆盘都不可读 → fail closed"),
])
def test_l3_hu_negative(settled, buttons, marker: str) -> None:
    after = BoardState(concealed_count=17, melds=(), claim_buttons=buttons,
                       settled=settled, turn_owner="me")
    ok, _why = verify_after_click(_before(), after, ACTION_HU)
    assert not ok, marker


def test_l3_pass_positive() -> None:
    after = BoardState(concealed_count=16, melds=(), claim_buttons=0,
                       settled=False, turn_owner="me")
    ok, why = verify_after_click(_before(), after, ACTION_PASS)
    assert ok, why


@pytest.mark.parametrize("concealed,buttons,turn,marker", [
    (15, 0, "me", "暗手变少了"),
    (16, 1, "me", "圆盘没消失"),
    (16, 0, "opponent", "倒计时没转到我方"),
])
def test_l3_pass_negative(concealed: int, buttons: int, turn: str,
                          marker: str) -> None:
    after = BoardState(concealed_count=concealed, melds=(), claim_buttons=buttons,
                       settled=False, turn_owner=turn)
    ok, _why = verify_after_click(_before(), after, ACTION_PASS)
    assert not ok, marker


def test_l3_settled_is_a_failure_for_non_hu() -> None:
    after = BoardState(concealed_count=14, melds=((1, 1, 1),), claim_buttons=0,
                       settled=True, turn_owner="me")
    ok, why = verify_after_click(_before(), after, ACTION_PENG)
    assert not ok
    assert "结算" in why


def test_l3_missing_evidence_fails_closed() -> None:
    ok, why = verify_after_click(None, _before(), ACTION_PENG)
    assert not ok and "点击前" in why
    ok, why = verify_after_click(_before(), None, ACTION_PENG)
    assert not ok and "点击后" in why


def test_l3_unknown_action_fails() -> None:
    ok, why = verify_after_click(_before(), _before(), "洗牌")
    assert not ok and "未知动作" in why


def test_l3_board_state_from_hand_state() -> None:
    board = BoardState.from_hand_state(PENG_HAND, claim_buttons=2,
                                       settled=False, turn_owner="opponent")
    assert board.concealed_count == 16
    assert board.meld_tile_total == 0
    assert board.total == 16


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------

def test_ledger_is_json_serialisable() -> None:
    decision = decide_action(state(gold_button(), pass_button()), PENG_HAND, ADVISORY)
    assert decision is not None
    payload = json.dumps(decision.ledger(), ensure_ascii=False)
    assert ActionDecision is not None
    assert json.loads(payload)["action"] == ACTION_PENG


def test_hand_state_from_analysis_converts_encoding(tmp_path: Path) -> None:
    """视觉编码（白=67）必须转成引擎编码（白=73）才交给引擎。"""
    hand = HandState.from_analysis({
        "hand_ids": [1, 2, 3, 67],
        "melds": [[1, 1, 1]],
        "caishen": "bai",
        "mode": "tea_2p",
    })
    assert hand.caishen == 73
    assert 67 not in hand.concealed
    assert hand.player_count == 2
    assert hand.meld_total == 3


def test_hand_state_from_analysis_empty_is_invalid() -> None:
    hand = HandState.from_analysis(None)
    assert not hand.is_valid
    assert hand.source == "empty"


# ---------------------------------------------------------------------------
# 张数账契约（杠只占一个逻辑面子，kong-math 通告 e82bf00）
# ---------------------------------------------------------------------------

def _shanten_contract():
    """导入引擎侧契约函数（expected_concealed_count 等）。"""
    engine = ap._engine()
    assert engine is not None
    root = engine["root"]
    if root not in sys.path:
        sys.path.insert(0, root)
    from brain_v0.engine.tea import shanten
    return shanten


@pytest.mark.parametrize("melds,expected", [
    ((), 16),
    (((1, 1, 1),), 13),               # 1 组碰：13
    (((1, 1, 1, 1),), 13),            # 1 组杠：仍是 13（不是旧口径的 12）
    (((1, 1, 1), (2, 2, 2)), 10),     # 2 组碰：10
    (((1, 1, 1, 1), (2, 2, 2, 2)), 10),  # 2 组杠：仍是 10
])
def test_concealed_expected_matches_engine_contract(melds: tuple,
                                                    expected: int) -> None:
    """暗手应有张数必须等于引擎契约函数；杠与吃/碰同价（3 张/组）。"""
    shanten = _shanten_contract()
    hand = HandState(concealed=(1,) * expected, melds=tuple(tuple(g) for g in melds))
    assert hand.concealed_expected == expected
    assert hand.concealed_expected == shanten.expected_concealed_count(
        melds=[list(g) for g in melds])
    assert hand.logical_meld_slots == shanten.logical_slot_count(
        melds=[list(g) for g in melds])
    assert hand.meld_total == shanten.visible_physical_count(
        melds=[list(g) for g in melds])
    assert hand.is_valid


def test_kong_meld_does_not_dock_concealed_count() -> None:
    """"13 张暗手 + 1 组杠"是合法等待态（旧口径会误判为 12 张而拒掉）。"""
    hand = HandState(concealed=tuple(range(1, 14)), melds=((21, 21, 21, 21),),
                     incoming_tile=1, player_count=2)
    assert hand.meld_total == 4          # 物理账：杠 4 张
    assert hand.logical_meld_slots == 3  # 逻辑账：只占一个面子
    assert hand.is_valid

    report = ap.evaluate_claim_candidates(hand, ActionPolicyConfig(require_template_match=False))
    # 张数账不再把它误拒；至于引擎怎么判动作由引擎说了算。
    assert all("张数不守恒" not in report[a]["why"] for a in ap.CLAIM_ACTIONS)


def test_logical_count_still_blocks_broken_hands() -> None:
    """逻辑账不等于放松：17 张等待态照样拒。"""
    hand = HandState(concealed=(1,) * 17, melds=(), incoming_tile=1,
                     player_count=2)
    assert not hand.is_valid
    report = ap.evaluate_claim_candidates(hand, ActionPolicyConfig(require_template_match=False))
    assert all(not report[a]["feasible"] for a in ap.CLAIM_ACTIONS)


# ---------------------------------------------------------------------------
# 闪光标归属仲裁（"哪一边后打的"）
#
# 仲裁只在"两帧差分没给出归属"时介入：闪光标只回答"哪边后打"，**不回答
# "有没有新弃牌"**，所以它永远排在两帧差分之后，不能单独拿来产出动作。
# ---------------------------------------------------------------------------

SPARKLE_X = 915          # 与 discard_vision 实测的落点同量级（939/1164/1220）
SPARKLE_HALF_W = 10      # 宽 20 ≤ SPARKLE_MAX_WIDTH(40)
SPARKLE_HALF_H = 6       # 面积 240 ≥ SPARKLE_MIN_AREA(80)


def _sparkle_frame(*sides: str) -> np.ndarray:
    """在指定一侧（"opponent" / "self"）的光带里画一枚金色闪光标。"""
    frame = np.zeros((900, 1600, 3), np.uint8)
    frame[:, :] = TABLE_BGR
    for side in sides:
        x0, y0, x1, y1 = (ap.SPARKLE_BAND_OPPONENT if side == "opponent"
                          else ap.SPARKLE_BAND_SELF)
        cy = (y0 + y1) // 2
        cv2.rectangle(frame,
                      (SPARKLE_X - SPARKLE_HALF_W, cy - SPARKLE_HALF_H),
                      (SPARKLE_X + SPARKLE_HALF_W, cy + SPARKLE_HALF_H),
                      tuple(int(c) for c in GOLD_BGR), thickness=-1)
    return frame


def test_sparkle_constants_match_discard_vision() -> None:
    """闪光标口径必须与 discard_vision 一致，否则仲裁会静默失效。"""
    import discard_vision

    assert ap.SPARKLE_BAND_OPPONENT == discard_vision._OPPONENT_SPARKLE_BAND
    assert ap.SPARKLE_HUE == discard_vision._SPARKLE_HUE
    assert ap.SPARKLE_MIN_SAT == discard_vision._SPARKLE_MIN_SAT
    assert ap.SPARKLE_MIN_VAL == discard_vision._SPARKLE_MIN_VAL
    assert ap.SPARKLE_MIN_AREA == discard_vision._SPARKLE_MIN_AREA
    assert ap.SPARKLE_MAX_WIDTH == discard_vision._SPARKLE_MAX_WIDTH

    # 自家光带没有上游常量可对（discard_vision 只用了对手那条），所以内部
    # 钉死"同一条带整体下移 SPARKLE_BAND_SELF_OFFSET"，改一侧就得改两个。
    ox0, oy0, ox1, oy1 = ap.SPARKLE_BAND_OPPONENT
    sx0, sy0, sx1, sy1 = ap.SPARKLE_BAND_SELF
    assert (sx0, sx1) == (ox0, ox1)
    assert (sy0 - oy0) == ap.SPARKLE_BAND_SELF_OFFSET
    assert (sy1 - oy1) == ap.SPARKLE_BAND_SELF_OFFSET


def test_sparkle_detector_agrees_with_discard_vision() -> None:
    """同一个对手光带，两边必须数出一模一样的中心 x。"""
    import discard_vision

    frame = _sparkle_frame("opponent")
    mine = ap._sparkle_centers(frame, ap.SPARKLE_BAND_OPPONENT)
    theirs = discard_vision.find_discard_sparkles(frame)
    assert mine == theirs == (SPARKLE_X,)


@pytest.mark.parametrize("sides,status", [
    (("opponent",), "opponent"),
    (("self",), "self"),
])
def test_read_sparkle_sides_one_side(sides: tuple, status: str) -> None:
    sides_read = ap.read_sparkle_sides(_sparkle_frame(*sides))
    assert sides_read["status"] == status
    assert sides_read["usable"] is True
    assert sides_read[status] == (SPARKLE_X,)


def test_read_sparkle_sides_both_is_not_usable() -> None:
    """两侧都有标 = 无法仲裁（游戏开局/结算常两边同亮）。"""
    sides = ap.read_sparkle_sides(_sparkle_frame("opponent", "self"))
    assert sides["status"] == "both"
    assert sides["usable"] is False


def test_read_sparkle_sides_none_is_not_usable() -> None:
    sides = ap.read_sparkle_sides(_sparkle_frame())
    assert sides["status"] == "none"
    assert sides["usable"] is False


@pytest.mark.parametrize("image", [None, "not-a-frame", np.zeros((900, 1600), np.uint8),
                                   np.zeros((720, 1280, 3), np.uint8)])
def test_read_sparkle_sides_without_frame(image: object) -> None:
    sides = ap.read_sparkle_sides(image)
    assert sides["status"] == "no_frame"
    assert sides["usable"] is False


def test_arbitration_self_side_keeps_abstaining() -> None:
    """自家后打 → 归属判 self → 所有动作被 L2 闸门拦下（行为与"无法确认"一致）。"""
    hand = HandState(concealed=PENG_HAND.concealed, incoming_tile=1,
                     incoming_source=None, player_count=2)
    source, sides = ap.arbitrate_incoming_source(_sparkle_frame("self"), hand,
                                                 ActionPolicyConfig(require_template_match=False))
    assert source == ap.INCOMING_SOURCE_SELF
    assert sides["status"] == "self"

    decision = decide_action(state(gold_button(), pass_button()), hand,
                             ActionPolicyConfig(require_template_match=False), image=_sparkle_frame("self"))
    assert decision is not None
    assert not decision.executable
    assert decision.tap_point is None


def test_arbitration_opponent_side_recovers_the_claim() -> None:
    """别家后打 → 归属判 opponent，并从对手牌河最新一张补回来牌 → 可碰。

    这正是原先 4 次"两边都长了、归属确认不了"里丢掉的机会。
    """
    hand = HandState(concealed=PENG_HAND.concealed, incoming_tile=None,
                     incoming_source=None, opponent_river=(1, 9, 5),
                     player_count=2)
    updated, evidence = ap.resolve_incoming(_sparkle_frame("opponent"), hand,
                                            ActionPolicyConfig(require_template_match=False))
    assert evidence["arbitrated_source"] == ap.INCOMING_SOURCE_OPPONENT
    assert evidence["recovered_tile"] == 1
    assert updated.incoming_source == ap.INCOMING_SOURCE_OPPONENT
    assert updated.incoming_tile == 1
    # 原对象不被改动（frozen dataclass + replace）。
    assert hand.incoming_tile is None and hand.incoming_source is None

    decision = decide_action(state(gold_button(), pass_button()), hand,
                             ActionPolicyConfig(require_template_match=False), image=_sparkle_frame("opponent"))
    assert decision is not None
    assert decision.action == ACTION_PENG
    assert decision.l1_evidence["incoming"]["arbitrated_source"] == ap.INCOMING_SOURCE_OPPONENT


def test_arbitration_opponent_side_without_river_still_abstains() -> None:
    """判出 opponent 但补不出牌 → 仍然弃权（不许拿闪光标凭空造一张牌）。"""
    hand = HandState(concealed=PENG_HAND.concealed, incoming_tile=None,
                     incoming_source=None, opponent_river=(), player_count=2)
    updated, evidence = ap.resolve_incoming(_sparkle_frame("opponent"), hand,
                                            ActionPolicyConfig(require_template_match=False))
    assert updated.incoming_source == ap.INCOMING_SOURCE_OPPONENT
    assert evidence["recovered_tile"] is None
    assert updated.incoming_tile is None

    decision = decide_action(state(gold_button(), pass_button()), hand,
                             ActionPolicyConfig(require_template_match=False), image=_sparkle_frame("opponent"))
    assert decision is not None
    assert not decision.executable
    assert "来牌未知" in decision.l2_reason


@pytest.mark.parametrize("sides", [(), ("opponent", "self")])
def test_arbitration_unusable_maintains_abstain(sides: tuple) -> None:
    """两侧同亮 / 没标 → 仲裁不了 → 维持弃权，绝不猜。"""
    hand = HandState(concealed=PENG_HAND.concealed, incoming_tile=1,
                     incoming_source=None, player_count=2)
    source, _ = ap.arbitrate_incoming_source(_sparkle_frame(*sides), hand,
                                             ActionPolicyConfig(require_template_match=False))
    assert source is None

    decision = decide_action(state(gold_button(), pass_button()), hand,
                             ActionPolicyConfig(require_template_match=False), image=_sparkle_frame(*sides))
    assert decision is not None
    # 仲裁仍解不出归属 → 证据不足，维持弃权（不得猜成碰）。
    assert not decision.executable
    assert decision.action == ""
    assert "归属未知" in (decision.l2_reason or decision.blocked_reason or "")


def test_arbitration_without_image_maintains_abstain() -> None:
    hand = HandState(concealed=PENG_HAND.concealed, incoming_tile=None,
                     incoming_source=None, player_count=2)
    source, sides = ap.arbitrate_incoming_source(None, hand, ActionPolicyConfig(require_template_match=False))
    assert source is None
    assert sides["status"] == "no_frame"
    assert not sides["usable"]


def test_arbitration_can_be_switched_off() -> None:
    config = ActionPolicyConfig(require_template_match=False, use_sparkle_arbitration=False)
    hand = HandState(concealed=PENG_HAND.concealed, incoming_tile=None,
                     incoming_source=None, player_count=2)
    source, sides = ap.arbitrate_incoming_source(_sparkle_frame("opponent"), hand,
                                                 config)
    assert source is None
    assert sides["status"] == "disabled"


def test_arbitration_never_overrides_a_known_source() -> None:
    """差分已经给出归属时，闪光标只当交叉验证，绝不覆盖（差分优先）。"""
    hand = HandState(concealed=PENG_HAND.concealed, incoming_tile=1,
                     incoming_source=ap.INCOMING_SOURCE_SELF, player_count=2)
    source, sides = ap.arbitrate_incoming_source(_sparkle_frame("opponent"), hand,
                                                 ActionPolicyConfig(require_template_match=False))
    assert source is None
    assert sides["status"] == "already_attributed"
    assert sides["incoming_source"] == ap.INCOMING_SOURCE_SELF

    decision = decide_action(state(gold_button(), pass_button()), hand,
                             ActionPolicyConfig(require_template_match=False), image=_sparkle_frame("opponent"))
    assert decision is not None
    assert not decision.executable
    assert decision.tap_point is None


def test_sparkle_alone_never_invents_a_discard() -> None:
    """闪光标只回答"哪边后打"：没有两帧差分结论时它不产生任何动作。

    这一条把"必须先有两帧差分"钉死：牌河为空、来牌为空，光带再亮也不动。
    """
    hand = HandState(concealed=NO_CLAIM_HAND.concealed, incoming_tile=None,
                     incoming_source=None, opponent_river=(62,), player_count=2)
    updated, evidence = ap.resolve_incoming(_sparkle_frame("opponent"), hand,
                                            ActionPolicyConfig(require_template_match=False))
    assert updated.incoming_tile == 62        # 补牌来自牌河，不是来自闪光标
    decision = decide_action(state(gold_button(), pass_button()), hand,
                             ActionPolicyConfig(require_template_match=False), image=_sparkle_frame("opponent"))
    assert decision is not None
    assert not decision.executable            # 62（南）碰/吃/杠/胡皆不可行

    blank = HandState(concealed=NO_CLAIM_HAND.concealed, incoming_tile=None,
                      incoming_source=None, opponent_river=(), player_count=2)
    untouched, blank_evidence = ap.resolve_incoming(_sparkle_frame("opponent"),
                                                    blank, ActionPolicyConfig(require_template_match=False))
    assert untouched.incoming_tile is None
    assert blank_evidence["recovered_tile"] is None


def test_newest_opponent_tile_uses_the_river_head() -> None:
    """对手牌河右端锚定、向左延伸 → 最新一张是 opponent_river[0]。"""
    hand = HandState(concealed=(), opponent_river=(1, 9, 5))
    assert ap._newest_opponent_tile(hand) == 1
    assert ap._newest_opponent_tile(HandState(concealed=(), opponent_river=())) is None
    assert ap._newest_opponent_tile(HandState(concealed=(), opponent_river=(9999,))) is None


def test_opponent_river_of_accepts_every_shape() -> None:
    assert ap._opponent_river_of({"discards": {"opponent_river": (1, 2)}}) == (1, 2)
    assert ap._opponent_river_of({"opponent_river": [3]}) == (3,)
    assert ap._opponent_river_of({"opponent_river": None}) == ()
    assert ap._opponent_river_of({}) == ()
    assert ap._opponent_river_of(None) == ()


def test_opponent_river_is_carried_from_analysis() -> None:
    hand = HandState.from_analysis({"hand_ids": [1, 2, 3],
                                    "discards": {"opponent_river": (7, 8)}})
    assert hand.opponent_river == (7, 8)


def test_require_template_match_default_blocks_without_templates() -> None:
    """P0-2: default require_template_match=True blocks when no glyph templates."""
    decision = decide_action(state(gold_button(), pass_button()), PENG_HAND,
                             ActionPolicyConfig())
    assert decision is not None
    assert not decision.executable
    assert "模板未过闸" in (decision.blocked_reason or "")


def test_stale_claim_snapshot_is_blocked() -> None:
    """P0-2: ClaimState.captured_at older than max_claim_age_s => L1 abstain."""
    import time
    stale = ClaimState(
        buttons=(gold_button(), pass_button()), decision=None, reason="test",
        evidence={}, known=True, captured_at=time.time() - 30.0,
    )
    decision = decide_action(
        stale, PENG_HAND,
        ActionPolicyConfig(require_template_match=False, max_claim_age_s=5.0),
    )
    assert decision is not None
    assert not decision.executable
    assert "过期" in (decision.blocked_reason or "")


def test_auto_click_forces_template_match() -> None:
    """P0-2: auto_click_enabled forces require_template_match even if caller unset it."""
    cfg = ActionPolicyConfig(auto_click_enabled=True, require_template_match=False,
                             confirmed_actions=frozenset({ACTION_PENG}))
    decision = decide_action(state(gold_button(), pass_button()), PENG_HAND, cfg)
    assert decision is not None
    assert not decision.executable
    assert "模板未过闸" in (decision.blocked_reason or "")
