"""Exact wildcard regression, exposed melds, and changed-hand cold latency."""

from collections import Counter
from functools import lru_cache
from itertools import combinations_with_replacement
import json
from pathlib import Path
import random
import subprocess
import sys

import pytest

from engine.advisor import Advisor
from engine.shanten import (
    _qidui_shanten_plain,
    _standard_shanten_plain,
    calc_shanten,
    calc_ting_tiles,
    discard_analysis,
    is_win,
)
from engine.tiles import NUMERIC_TILES


@lru_cache(maxsize=None)
def _enumeration_oracle(hand, use_qidui):
    """The original 27-type replacement enumeration, only for bounded test cases."""
    real = [tile for tile in hand if tile != 55]
    jokers = hand.count(55)
    best = 99
    floor = 0 if len(hand) < 14 else -1
    for replacements in combinations_with_replacement(NUMERIC_TILES, jokers):
        merged = tuple(sorted(real + list(replacements)))
        value = _standard_shanten_plain(merged)
        if use_qidui:
            value = min(value, _qidui_shanten_plain(merged))
        best = min(best, value)
        if best <= floor:
            break
    return best


def _seeded_hands():
    rng = random.Random(20261014)
    numeric_deck = [tile for tile in NUMERIC_TILES for _ in range(4)]
    return [
        tuple(sorted(rng.sample(numeric_deck, length - jokers) + [55] * jokers))
        for jokers in range(3)
        for length in (13, 14)
        for _ in range(8)
    ]


@pytest.mark.parametrize("hand", _seeded_hands())
@pytest.mark.parametrize("use_qidui", [False, True])
def test_matches_original_enumeration_on_seeded_zero_to_two_jokers(hand, use_qidui):
    assert calc_shanten(hand, use_qidui) == _enumeration_oracle(hand, use_qidui)


@pytest.mark.parametrize("hand", [
    [2, 3, 8, 9, 21, 22, 23, 41, 42, 43, 47, 47, 55, 55],
    [1, 1, 4, 4, 7, 7, 21, 21, 24, 24, 27, 41, 55, 55],
    [1, 1, 1, 1, 21, 21, 21, 21, 41, 41, 41, 41, 55, 55],
    [1, 3, 5, 7, 9, 21, 23, 25, 27, 29, 41, 43, 55, 55],
])
@pytest.mark.parametrize("use_qidui", [False, True])
def test_wildcard_boundaries_match_oracle(hand, use_qidui):
    assert calc_shanten(hand, use_qidui) == _enumeration_oracle(tuple(hand), use_qidui)


@pytest.mark.parametrize("jokers", [3, 4])
@pytest.mark.parametrize("use_qidui", [False, True])
def test_sparse_high_joker_shanten_matches_full_enumeration(jokers, use_qidui):
    base = [1, 3, 5, 7, 9, 21, 23, 25, 27, 29, 41, 43, 45, 47]
    hand = tuple(base[:14 - jokers] + [55] * jokers)
    assert calc_shanten(hand, use_qidui) == _enumeration_oracle(hand, use_qidui)


@pytest.mark.parametrize("open_meld_count", range(5))
def test_fixed_meld_waits_and_complete_short_hands(open_meld_count):
    melds = [[1, 2, 3], [4, 5, 6], [7, 8, 9], [21, 22, 23]]
    hand = [tile for group in melds[:4 - open_meld_count] for tile in group] + [25]
    assert len(hand) == 13 - 3 * open_meld_count
    assert calc_shanten(hand, open_meld_count=open_meld_count) == 0
    assert calc_ting_tiles(hand, open_meld_count=open_meld_count) == [25, 55]
    assert is_win(hand + [25], open_meld_count=open_meld_count)
    assert is_win(hand + [55], open_meld_count=open_meld_count)
    advice = Advisor().advise(hand, {}, 5, open_meld_count=open_meld_count)
    assert advice["is_ting"] and advice["ting"] == [25, 55]
    assert not advice["options"]


@pytest.mark.parametrize("open_meld_count", range(5))
def test_short_discard_uses_only_concealed_tiles(open_meld_count):
    melds = [[1, 2, 3], [4, 5, 6], [7, 8, 9], [21, 22, 23]]
    hand = [tile for group in melds[:4 - open_meld_count] for tile in group] + [25, 47]
    advice = Advisor().advise(hand, {47: 3}, 10, open_meld_count=open_meld_count)
    assert advice["options"][0]["tile"] == 47
    assert {option["tile"] for option in advice["options"]} == set(hand)
    options = discard_analysis(hand, open_meld_count=open_meld_count)
    assert options[0]["tile"] in {25, 47}
    assert options[0]["shanten_after"] == 0


def test_exposed_pung_disables_seven_pairs_path():
    short_pairs = [1, 1, 4, 4, 7, 7, 21, 21, 24, 24, 55]
    assert not is_win(short_pairs, open_meld_count=1)
    assert calc_shanten(short_pairs, open_meld_count=1) > 0
    assert calc_shanten(short_pairs, open_meld_count=1) == calc_shanten(
        short_pairs, use_qidui=False, open_meld_count=1
    )


@pytest.mark.parametrize("bad", [-1, 5, 1.0, True, "1"])
def test_rejects_invalid_exposed_meld_count(bad):
    with pytest.raises(ValueError, match="open_meld_count"):
        calc_shanten([1], open_meld_count=bad)


def test_exposed_meld_rejects_oversized_concealed_hand():
    with pytest.raises(ValueError, match="不能超过11张"):
        calc_shanten([1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23], open_meld_count=1)
    with pytest.raises(ValueError, match="仅接受10张"):
        calc_ting_tiles([1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 21], open_meld_count=1)


def test_externally_visible_tiles_are_subtracted_once():
    hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25, 47]
    rows = discard_analysis(hand, {25: 2})
    row = next(row for row in rows if row["tile"] == 47)
    assert row["ukeire"] == {25: 1, 55: 4}


def test_known_complete_groups_remain_winning_with_jokers_and_fixed_melds():
    rng = random.Random(20261015)
    checked = 0
    while checked < 24:
        groups = []
        for _ in range(4):
            suit = rng.choice([0, 20, 40])
            number = rng.randint(1, 7)
            groups.append(
                [suit + number] * 3 if rng.choice([False, True])
                else [suit + number, suit + number + 1, suit + number + 2]
            )
        pair = [rng.choice(NUMERIC_TILES)] * 2
        full = [tile for group in groups for tile in group] + pair
        if max(Counter(full).values()) > 4:
            continue
        for open_count in range(5):
            hidden = [tile for group in groups[open_count:] for tile in group] + pair
            for joker_count in range(min(4, len(hidden)) + 1):
                hand = list(hidden)
                for index in rng.sample(range(len(hand)), joker_count):
                    hand[index] = 55
                assert is_win(hand, open_meld_count=open_count), (hand, open_count)
        checked += 1


def test_replacing_numbered_tiles_with_jokers_never_worsens_shanten():
    rng = random.Random(20261016)
    numeric_deck = [tile for tile in NUMERIC_TILES for _ in range(4)]
    for _ in range(24):
        hand = rng.sample(numeric_deck, 14)
        previous = calc_shanten(hand, use_qidui=False)
        for index in rng.sample(range(14), 4):
            hand[index] = 55
            current = calc_shanten(hand, use_qidui=False)
            assert current <= previous, hand
            previous = current


def test_changed_three_and_four_joker_hands_make_cold_legal_decisions_under_two_seconds():
    engine_root = str(Path(__file__).resolve().parents[1])
    # Fresh interpreter, then six changed hands: no warmed exact-hand cache advantage.
    program = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
from engine.advisor import Advisor
bases = [
    [1,2,3,4,5,6,7,8,9,21,22,23,25,47],
    [1,3,5,7,9,21,23,25,27,29,41,43,45,47],
    [1,1,2,4,5,7,21,22,24,27,41,44,45,47],
]
advisor = Advisor()
results = []
for base in bases:
    for jokers in (3,4):
        hand = base[:14-jokers] + [55]*jokers
        start = time.perf_counter()
        advice = advisor.local_advise(hand, {}, 1)
        elapsed = time.perf_counter()-start
        assert advice['options']
        assert all(option['tile'] in hand and option['tile'] != 55 for option in advice['options'])
        results.append(elapsed)
print(json.dumps(results))
"""
    completed = subprocess.run(
        [sys.executable, "-c", program, engine_root],
        capture_output=True, text=True, timeout=10, check=True,
    )
    durations = json.loads(completed.stdout)
    assert len(durations) == 6
    assert max(durations) < 2, durations
