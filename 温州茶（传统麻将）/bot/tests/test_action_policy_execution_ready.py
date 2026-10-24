# -*- coding: utf-8 -*-
"""action_policy 执行就绪补测：L3 证据口径收紧 / 吃面板选组 / runner 推荐配置。

与 ``test_action_policy.py`` 同口径：用真实 ``brain_v0`` 引擎，引擎不可用
时整套 skip。夹具的向听数字均由真实引擎实测（2026-10-25），关键断言尽量
写成「规则性质」而非钉死引擎实现细节。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parent.parent
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import action_policy as ap
from claim_vision import read_claims
from action_policy import (
    ACTION_CHI,
    ACTION_HU,
    ACTION_KONG,
    ACTION_PASS,
    ACTION_PENG,
    ActionPolicyConfig,
    BoardState,
    HandState,
    verify_after_click,
)

pytestmark = pytest.mark.skipif(not ap.engine_available(),
                                reason="算牌引擎不可用")


# ---------------------------------------------------------------------------
# L3：圆盘读数缺失必须 fail closed；吃的候选面板残留否决
# ---------------------------------------------------------------------------

def _before() -> BoardState:
    return BoardState(concealed_count=16, melds=(), claim_buttons=2,
                      settled=False, turn_owner="opponent")


def test_l3_pass_unmeasured_discs_fails_closed() -> None:
    """过：圆盘数量读不到（None）→ 证据不足判未证实（原实现会零证据放行）。"""
    after = BoardState(concealed_count=16, melds=(), claim_buttons=None,
                       settled=False, turn_owner="me")
    ok, why = verify_after_click(_before(), after, ACTION_PASS)
    assert not ok and "不可测" in why


@pytest.mark.parametrize("action,concealed,melds", [
    (ACTION_PENG, 14, ((1, 1, 1),)),
    (ACTION_CHI, 14, ((1, 2, 3),)),
    (ACTION_KONG, 13, ((1, 1, 1, 1),)),
])
def test_l3_claim_unmeasured_discs_fails_closed(action, concealed, melds) -> None:
    """碰/吃/杠：其余迁移都对、唯独圆盘数量不可测 → 仍判未证实。"""
    after = BoardState(concealed_count=concealed, melds=melds,
                       claim_buttons=None, settled=False, turn_owner="me")
    ok, why = verify_after_click(_before(), after, action)
    assert not ok and "不可测" in why


def test_l3_chi_selection_panel_veto() -> None:
    base = dict(concealed_count=14, melds=((1, 2, 3),), claim_buttons=0,
                settled=False, turn_owner="me")
    ok, why = verify_after_click(
        _before(), BoardState(selection_panel=True, **base), ACTION_CHI)
    assert not ok and "面板" in why
    ok, why = verify_after_click(
        _before(), BoardState(selection_panel=False, **base), ACTION_CHI)
    assert ok, why
    # 面板读数不可测时不单独否决：圆盘/副露/暗手三项正证据齐全仍判成立。
    ok, why = verify_after_click(
        _before(), BoardState(selection_panel=None, **base), ACTION_CHI)
    assert ok, why
    # 面板否决只作用于吃：同样读数下碰不受影响。
    peng_after = BoardState(concealed_count=14, melds=((1, 1, 1),),
                            claim_buttons=0, settled=False, turn_owner="me",
                            selection_panel=True)
    ok, why = verify_after_click(_before(), peng_after, ACTION_PENG)
    assert ok, why


def test_l3_from_hand_state_passes_selection_panel() -> None:
    hand = HandState(concealed=(1, 2, 3, 4, 5, 6, 7, 8, 9,
                                21, 22, 23, 24, 41, 42, 43))
    board = BoardState.from_hand_state(hand, claim_buttons=0,
                                       selection_panel=True)
    assert board.selection_panel is True
    assert BoardState.from_hand_state(hand).selection_panel is None


# ---------------------------------------------------------------------------
# 吃的二次面板选组
# ---------------------------------------------------------------------------

CHI_CFG = ActionPolicyConfig(require_template_match=False, chi_enabled=True)

H1 = HandState(concealed=(1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24, 41, 42, 43),
               incoming_tile=2, incoming_source="opponent", player_count=4)
H2 = HandState(concealed=(3, 4, 6, 7, 21, 22, 23, 25, 26,
                          41, 42, 43, 45, 46, 8, 9),
               incoming_tile=5, incoming_source="opponent", player_count=4)


def test_chi_panel_picks_only_non_worsening_group() -> None:
    # 实测：H1 向听 0；(1,2,3)→0 不劣化，(2,3,4)→1 劣化。只能选前者。
    choice = ap.choose_chi_panel_option(H1, [(1, 2, 3), (2, 3, 4)], CHI_CFG)
    assert choice.chosen and choice.group == (1, 2, 3)
    assert choice.candidate_index == 0
    assert choice.hand_tiles == (1, 3)
    assert choice.shanten_after is not None and choice.shanten_before is not None
    assert choice.shanten_after <= choice.shanten_before
    json.dumps(choice.ledger(), ensure_ascii=False)


def test_chi_panel_prefers_better_shanten() -> None:
    # 实测：H2 向听 2；(3,4,5)→1、(5,6,7)→2。取向听更优者。
    choice = ap.choose_chi_panel_option(H2, [(3, 4, 5), (5, 6, 7)], CHI_CFG)
    assert choice.chosen and choice.group == (3, 4, 5)
    assert choice.shanten_after is not None and choice.shanten_before is not None
    assert choice.shanten_after < choice.shanten_before


def test_chi_panel_abstains_when_every_option_worsens() -> None:
    # H1 只给劣化组 (2,3,4)（向听 0→1）：必须弃权，而不是凑合选。
    choice = ap.choose_chi_panel_option(H1, [(2, 3, 4)], CHI_CFG)
    assert not choice.chosen and "不劣化" in choice.reason
    assert choice.evaluated and choice.evaluated[0]["valid"]


def test_chi_panel_selection_rule_property() -> None:
    """选中者 = 合法不劣化候选里 (向听, -保留搭子, 下标) 的最优者。"""
    for hand, candidates in ((H1, [(1, 2, 3), (2, 3, 4)]),
                             (H2, [(3, 4, 5), (5, 6, 7)])):
        choice = ap.choose_chi_panel_option(hand, candidates, CHI_CFG)
        assert choice.chosen
        qualifying = [r for r in choice.evaluated
                      if r["valid"] and r["shanten_after"] is not None
                      and choice.shanten_before is not None
                      and r["shanten_after"] <= choice.shanten_before]
        assert qualifying
        best = min(qualifying,
                   key=lambda r: (r["shanten_after"], -r["kept_shapes"],
                                  r["index"]))
        assert choice.candidate_index == best["index"]
        assert choice.kept_shapes == best["kept_shapes"]


def test_chi_panel_full_tie_takes_first_candidate() -> None:
    # 同一组给两次：评估完全同分，必须按下标取靠前者（确定性）。
    choice = ap.choose_chi_panel_option(H1, [(1, 2, 3), (1, 2, 3)], CHI_CFG)
    assert choice.chosen and choice.candidate_index == 0


def test_chi_panel_rejects_group_without_incoming() -> None:
    choice = ap.choose_chi_panel_option(H1, [(7, 8, 9)], CHI_CFG)
    assert not choice.chosen
    assert choice.evaluated and not choice.evaluated[0]["valid"]
    assert "来牌" in choice.evaluated[0]["why"]


def test_chi_panel_rejects_non_sequence_and_missing_tiles() -> None:
    # (2,4,6) 不是顺子；(2,3,29) 跨花色——都非法，且手里也凑不齐。
    choice = ap.choose_chi_panel_option(H1, [(2, 4, 6), (2, 3, 29)], CHI_CFG)
    assert not choice.chosen
    assert len(choice.evaluated) == 2
    assert all(not r["valid"] for r in choice.evaluated)


def test_chi_panel_abstains_without_candidates() -> None:
    choice = ap.choose_chi_panel_option(H1, [], CHI_CFG)
    assert not choice.chosen and "候选" in choice.reason
    assert not ap.choose_chi_panel_option(H1, None, CHI_CFG).chosen


def test_chi_panel_abstains_when_chi_disabled() -> None:
    choice = ap.choose_chi_panel_option(
        H1, [(1, 2, 3)], ActionPolicyConfig(require_template_match=False))
    assert not choice.chosen and "chi_enabled" in choice.reason
    # config=None → 默认配置，chi_enabled=False → 同样弃权。
    assert not ap.choose_chi_panel_option(H1, [(1, 2, 3)]).chosen


def test_chi_panel_abstains_for_two_players_and_unknown_count() -> None:
    two = HandState(concealed=H1.concealed, incoming_tile=2,
                    incoming_source="opponent", player_count=2)
    assert not ap.choose_chi_panel_option(two, [(1, 2, 3)], CHI_CFG).chosen
    unknown = HandState(concealed=H1.concealed, incoming_tile=2,
                        incoming_source="opponent", player_count=None)
    assert not ap.choose_chi_panel_option(unknown, [(1, 2, 3)], CHI_CFG).chosen


def test_chi_panel_abstains_on_self_discard_and_unknown_tile() -> None:
    own = HandState(concealed=H1.concealed, incoming_tile=2,
                    incoming_source="self", player_count=4)
    assert not ap.choose_chi_panel_option(own, [(1, 2, 3)], CHI_CFG).chosen
    no_tile = HandState(concealed=H1.concealed, incoming_tile=None,
                        incoming_source="opponent", player_count=4)
    assert not ap.choose_chi_panel_option(no_tile, [(1, 2, 3)], CHI_CFG).chosen
    # 来牌可由参数覆盖补上，补上后正常选组。
    choice = ap.choose_chi_panel_option(no_tile, [(1, 2, 3)], CHI_CFG,
                                        incoming_tile=2)
    assert choice.chosen and choice.group == (1, 2, 3)


def test_chi_panel_abstains_on_broken_hand() -> None:
    broken = HandState(concealed=(1, 2, 3), incoming_tile=2,
                       incoming_source="opponent", player_count=4)
    assert not broken.is_valid
    assert not ap.choose_chi_panel_option(broken, [(1, 2, 3)], CHI_CFG).chosen


def test_kept_shape_tiles_metric() -> None:
    fill = (21, 21, 21, 41, 42, 43, 25, 26, 47, 48, 49)
    assert ap._kept_shape_tiles((6, 7, 8) + fill, None) == 14
    assert ap._kept_shape_tiles((3, 4, 8) + fill, None) == 13  # 8 万是孤张
    assert ap._kept_shape_tiles((9, 9, 29), None) == 2         # 对子有伴、孤张无伴
    assert ap._kept_shape_tiles((1, 5, 9, 25, 45), None) == 0
    assert ap._kept_shape_tiles((73,), 73) == 1                # 财神本身算有伴


# ---------------------------------------------------------------------------
# runner 推荐配置 + 默认值语义钉死
# ---------------------------------------------------------------------------

def test_recommended_runner_config_v1_excludes_chi() -> None:
    cfg = ap.recommended_runner_config()
    assert cfg.auto_click_enabled is False            # 样例不替调用方开总闸
    assert cfg.confirmed_actions == ap.RECOMMENDED_CONFIRMED_ACTIONS
    assert set(cfg.confirmed_actions) == {
        ACTION_HU, ACTION_PENG, ACTION_KONG, ACTION_PASS}
    assert cfg.chi_enabled is False
    assert cfg.require_template_match is True
    assert ap.validate_runner_config(cfg) == ()


def test_recommended_runner_config_with_chi() -> None:
    cfg = ap.recommended_runner_config(include_chi=True)
    assert ACTION_CHI in cfg.confirmed_actions and cfg.chi_enabled is True
    assert ap.validate_runner_config(cfg) == ()
    on = ap.recommended_runner_config(auto_click_enabled=True, include_chi=True)
    assert not ap.human_confirm_required(ACTION_HU, on)
    assert not ap.human_confirm_required(ACTION_CHI, on)
    # 总闸没开时，即便是推荐配置，每个动作仍要逐个人工确认。
    off = ap.recommended_runner_config(include_chi=True)
    assert ap.human_confirm_required(ACTION_HU, off)


def test_validate_runner_config_flags_inconsistencies() -> None:
    chi_only = ActionPolicyConfig(confirmed_actions=frozenset({ACTION_CHI}))
    assert any("chi" in p for p in ap.validate_runner_config(chi_only))
    unknown = ActionPolicyConfig(confirmed_actions=frozenset({"洗牌"}))
    assert ap.validate_runner_config(unknown)
    weak = ActionPolicyConfig(auto_click_enabled=True,
                              require_template_match=False,
                              confirmed_actions=frozenset({ACTION_HU}))
    assert any("require_template_match" in p
               for p in ap.validate_runner_config(weak))
    empty = ActionPolicyConfig(auto_click_enabled=True)
    assert ap.validate_runner_config(empty)
    assert ap.validate_runner_config(ActionPolicyConfig()) == ()


def test_defaults_unchanged() -> None:
    """默认值语义钉死：总闸关、确认集空、模板强制开、吃关、点击通道未接线。"""
    cfg = ActionPolicyConfig()
    assert cfg.auto_click_enabled is False
    assert cfg.confirmed_actions == frozenset()
    assert cfg.require_template_match is True
    assert cfg.chi_enabled is False
    assert ap.CLAIM_TAP_CHANNEL_READY is True


def test_verified_claim_templates_match_native_action_frames() -> None:
    """既有原生帧回归：五种圆盘中央字形都过 top1+margin 门禁。"""
    frame_a = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261023_235121"
               / "live" / "tea_2p" / "20261024_001334_883101_e40ece.png")
    frame_b = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261023_201640"
               / "live" / "tea_2p" / "20261023_205442_192224_8bef75.png")
    if not frame_a.is_file() or not frame_b.is_file():
        pytest.skip("原生行动圆盘回归帧缺失")
    templates = ap.verified_claim_templates()
    assert set(templates) == {"chi", "peng", "kong", "hu", "pass"}
    expected_a = {"hu", "kong", "peng", "pass"}
    image_a = cv2.imdecode(np.fromfile(str(frame_a), np.uint8), cv2.IMREAD_COLOR)
    matches_a = {
        ap.match_button_glyph(image_a, button, templates)["best"]
        for button in read_claims(image_a).buttons
    }
    assert matches_a == expected_a
    image_b = cv2.imdecode(np.fromfile(str(frame_b), np.uint8), cv2.IMREAD_COLOR)
    matches_b = {
        ap.match_button_glyph(image_b, button, templates)["best"]
        for button in read_claims(image_b).buttons
    }
    assert "chi" in matches_b


def test_live_auto_unknown_claim_uses_verified_pass_instead_of_stalling() -> None:
    """第一版连续代打：来牌未知不猜吃，原生“过”模板过闸后自动过。"""
    frame = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261023_201640"
             / "live" / "tea_2p" / "20261023_205442_192224_8bef75.png")
    if not frame.is_file():
        pytest.skip("原生吃/过行动帧缺失")
    image = cv2.imdecode(np.fromfile(str(frame), np.uint8), cv2.IMREAD_COLOR)
    hand = HandState(
        concealed=(1, 1, 2, 4, 5, 6, 7, 8, 9,
                   21, 22, 23, 24, 41, 42, 43),
        incoming_tile=None, incoming_source=None, player_count=2,
    )

    decision = ap.decide_action(
        read_claims(image), hand,
        ap.recommended_runner_config(auto_click_enabled=True), image=image)

    assert decision is not None
    assert decision.action == ACTION_PASS
    assert decision.executable
    assert decision.needs_human_confirm is False
    assert "来牌未知" in decision.l2_reason
