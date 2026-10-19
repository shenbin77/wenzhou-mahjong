# -*- coding: utf-8 -*-
import json

from engine.cli import main, parse_counts, parse_tiles
from engine.evaluation import afa_release_gate, compare_local_to_random
from engine.shanten import calc_ukeire, discard_analysis


def test_parse_cli_inputs():
    assert parse_tiles("1, 2，55") == [1, 2, 55]
    assert parse_counts("1:2,25:3") == {1: 2, 25: 3}


def test_ukeire_uses_visible_counts():
    hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25]
    base = calc_ukeire(hand)
    blocked = calc_ukeire(hand, {25: 3})
    assert base[25] > blocked.get(25, 0)


def test_ukeire_default_visible_counts():
    """visible_counts=None 与 visible_counts={} 等价。"""
    hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25]
    assert calc_ukeire(hand, None) == calc_ukeire(hand, {})
    assert calc_ukeire(hand) == calc_ukeire(hand, {})


def test_ukeire_caishen_as_waittile():
    """财神(55) 应作为有效进张出现。"""
    hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25]
    waits = calc_ukeire(hand)
    assert 55 in waits  # 听牌时财神是胡牌张
    assert waits[55] == 4  # 未见过财神,剩余 4 张


def test_ukeire_remaining_boundaries():
    """剩余张数边界: 0 张不进张,4 张全进张。"""
    hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25]
    # 25 已见 4 张(手牌 1 + 场 3) → 剩余 0
    blocked = calc_ukeire(hand, {25: 3})
    assert 25 not in blocked
    # 财神 4 张全见 → 不进张
    no_caishen = calc_ukeire(hand, {55: 4})
    assert 55 not in no_caishen


def test_discard_analysis_is_structured():
    hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25, 47]
    rows = discard_analysis(hand)
    assert rows
    assert {"tile", "shanten_after", "ukeire", "ukeire_count", "eff_score"} <= set(rows[0])
    assert rows == sorted(rows, key=lambda row: (row["shanten_after"], -row["ukeire_count"], row["tile"]))


def test_release_gate_stays_shadow_without_evidence():
    result = afa_release_gate(availability=1.0, evaluated_decisions=20,
                              paired_score_ci95=(-0.1, 0.5))
    assert result["promote_afa"] is False
    assert result["mode"] == "shadow"


def test_release_gate_can_promote_with_evidence():
    result = afa_release_gate(availability=0.999, evaluated_decisions=800,
                              paired_score_ci95=(0.05, 0.3))
    assert result["promote_afa"] is True


def test_paired_simulation_report_smoke():
    result = compare_local_to_random(rounds=1, seed=11)
    assert result["rounds"] == 1
    assert result["production_claim"] is False
    assert result["verdict"].startswith("simulation_")


def test_cli_advise_smoke(capsys):
    code = main(["advise", "--hand",
                 "1,2,3,4,5,6,7,8,9,21,22,23,25", "--turn", "3"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["shanten"] == 0
    assert payload["is_ting"] is True
