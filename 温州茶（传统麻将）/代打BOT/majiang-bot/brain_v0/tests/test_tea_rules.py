# -*- coding: utf-8 -*-
"""温州茶规则包: 配置、牌墙、软硬双翻、2p/4p 结算钩子、逆时针胡牌优先。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.tea import (
    BASE_PROGRESSION_2468,
    FA,
    USER_ACCOUNT,
    VARIANT_ID,
    ZHONG,
    TeaTableConfig,
    build_deck,
    classify_hu,
    configs_match_confirmed_screenshots,
    deck_size,
    default_config_2p,
    default_config_4p,
    detect_pattern_tags,
    legal_tile_ids,
    multiplier_for,
    pick_discard_winner,
    settle_2p,
    settle_4p,
    settle_by_player_count,
)
from engine.tea.kinds import HuKind

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tea"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class TestTableConfig:
    def test_confirmed_2p_banner_and_flags(self):
        fx = _load("table_2p.json")
        cfg = TeaTableConfig.from_mapping(fx)
        assert cfg.player_count == 2
        assert cfg.rounds == 16
        assert cfg.base_progression == BASE_PROGRESSION_2468
        assert cfg.allow_continue_table and cfg.split_cost
        assert cfg.gang_scores and cfg.gangshanghua_double
        assert cfg.no_zhong_fa and cfg.banqing and cfg.qingyise_unordered
        assert cfg.sanbei_enabled is False
        assert cfg.genfeng_enabled is False
        assert cfg.rule_banner() == fx["expected_banner"]
        for k, v in fx["sample_streak_bases"].items():
            assert cfg.base_for_streak(int(k)) == v

    def test_confirmed_4p_enables_sanbei_genfeng(self):
        fx = _load("table_4p.json")
        cfg = TeaTableConfig.from_mapping(fx)
        assert cfg.sanbei_enabled is True
        assert cfg.genfeng_enabled is True
        assert cfg.rule_banner() == fx["expected_banner"]

    def test_default_hooks_match_screenshots(self):
        cfgs = configs_match_confirmed_screenshots()
        assert cfgs["2p"].rule_banner() == default_config_2p().rule_banner()
        assert "4人" in cfgs["4p"].rule_banner()
        assert USER_ACCOUNT == "晓红"
        assert VARIANT_ID == "wenzhou_tea"

    def test_streak_caps_at_last_progression(self):
        cfg = default_config_2p()
        assert cfg.base_for_streak(4) == 8
        assert cfg.base_for_streak(9) == 8


class TestDeckNoZhongFa:
    def test_deck_excludes_zhong_fa(self):
        deck = build_deck(no_zhong_fa=True)
        assert deck_size(no_zhong_fa=True) == 128
        assert ZHONG not in deck and FA not in deck
        assert ZHONG not in legal_tile_ids(no_zhong_fa=True)

    def test_deck_with_zhong_fa_is_136(self):
        assert deck_size(no_zhong_fa=False) == 136
        assert ZHONG in legal_tile_ids(no_zhong_fa=False)


class TestHuKinds:
    def test_multipliers(self):
        assert multiplier_for(HuKind.RUAN) == 1
        assert multiplier_for("ying") == 2
        assert multiplier_for(HuKind.SHUANGFAN) == 4

    def test_classify_priority(self):
        assert classify_hu(["ruan8dui"]) == HuKind.YING
        assert classify_hu(["ying8dui"]) == HuKind.SHUANGFAN
        assert classify_hu(["gangshanghua"]) == HuKind.SHUANGFAN
        assert classify_hu(["gangshanghua"], gangshanghua_as_shuangfan=False) == HuKind.RUAN
        assert classify_hu(["tianhu", "zimo"]) == HuKind.SHUANGFAN
        assert classify_hu([]) == HuKind.RUAN


class TestPatternsFromFixtures:
    def test_pattern_fixtures(self):
        fx = _load("patterns.json")
        caishen = fx["caishen_id"]
        for key in ("ying8dui_no_joker", "ruan8dui", "banqing", "qingyise_unordered"):
            case = fx[key]
            cid = None if key == "ying8dui_no_joker" else caishen
            if key == "qingyise_unordered":
                cid = None
            if key == "banqing":
                cid = None
            tags = detect_pattern_tags(case["tiles"], caishen_id=cid)
            for expected in case["expect_tags_contain"]:
                assert expected in tags, (key, tags)


class TestPriority:
    def test_ccw_fixture(self):
        fx = _load("priority.json")
        w = pick_discard_winner(
            fx["discarder"], fx["claimants"], player_count=fx["player_count"]
        )
        assert w == fx["expect_winner"]
        w2 = pick_discard_winner(
            fx["discarder_2p"], fx["claimants_2p"], player_count=fx["player_count_2p"]
        )
        assert w2 == fx["expect_winner_2p"]

    def test_no_claimant(self):
        assert pick_discard_winner(0, [], player_count=4) is None


def _parse_deltas(d):
    return {int(k): int(v) for k, v in d.items()}


class TestSettleFixtures:
    @pytest.mark.parametrize("case", _load("settle_cases.json"), ids=lambda c: c["id"])
    def test_settle_case(self, case):
        hook = {"settle_2p": settle_2p, "settle_4p": settle_4p}[case["hook"]]
        result = hook(**case["args"])
        exp = case["expect"]
        if "base" in exp:
            assert result["base"] == exp["base"]
        if "multiplier" in exp:
            assert result["multiplier"] == exp["multiplier"]
        if "unit" in exp:
            assert result["unit"] == exp["unit"]
        if "hu_kind" in exp:
            assert result["hu_kind"] == exp["hu_kind"]
        if "genfeng_enabled" in exp:
            assert result["genfeng_enabled"] is exp["genfeng_enabled"]
        if "sanbei_enabled" in exp:
            assert result["sanbei_enabled"] is exp["sanbei_enabled"]
        if "deltas" in exp:
            assert result["deltas"] == _parse_deltas(exp["deltas"])
        assert sum(result["deltas"].values()) == 0


class TestSettleHooksWiring:
    def test_dispatch_by_player_count(self):
        r2 = settle_by_player_count(
            2,
            win_type="zimo",
            winner_seat=1,
            dealer_seat=0,
            dealer_streak=1,
            hu_kind="ruan",
        )
        assert r2["player_count"] == 2 and r2["genfeng_enabled"] is False
        r4 = settle_by_player_count(
            4,
            win_type="zimo",
            winner_seat=1,
            dealer_seat=0,
            dealer_streak=1,
            hu_kind="ruan",
        )
        assert r4["player_count"] == 4 and r4["sanbei_enabled"] is True
        assert r4["deltas"][1] == 6

    def test_2p_caishen_money_and_gang(self):
        r = settle_2p(
            win_type="fangpao",
            winner_seat=0,
            dealer_seat=1,
            dealer_streak=1,
            loser_seat=1,
            hu_kind="ying",
            gang_records=[{"type": "zhi", "seat": 0, "from_seat": 1}],
            caishen_counts={0: 2},
        )
        # unit=2*2=4, fangpao pay=8; zhi gang +1; caishen 2*1 from opponent
        assert r["deltas"][0] == 8 + 1 + 2
        assert r["deltas"][1] == -(8 + 1 + 2)

    def test_room_740195_streak2_base4(self):
        """回放顶栏: 牢庄:2 底数:4。"""
        cfg = default_config_2p()
        assert cfg.base_for_streak(2) == 4
        r = settle_2p(
            win_type="zimo",
            winner_seat=0,
            dealer_seat=0,
            dealer_streak=2,
            hu_tags=["gangshanghua"],
            config=cfg,
        )
        assert r["base"] == 4 and r["hu_kind"] == "shuangfan" and r["unit"] == 16


class TestVariantDispatcher:
    def test_settle_for_variant_tea_2p(self):
        from engine.variant import settle_for_variant

        r = settle_for_variant(
            "wenzhou_tea",
            player_count=2,
            win_type="zimo",
            winner_seat=0,
            dealer_seat=0,
            dealer_streak=1,
            hu_kind="ruan",
        )
        assert r["deltas"] == {0: 2, 1: -2}
        assert r["player_count"] == 2

    def test_settle_for_variant_rejects_unknown(self):
        from engine.variant import settle_for_variant

        with pytest.raises(ValueError):
            settle_for_variant(
                "unknown_game",
                player_count=2,
                win_type="zimo",
                winner_seat=0,
                dealer_seat=0,
                dealer_streak=1,
            )
