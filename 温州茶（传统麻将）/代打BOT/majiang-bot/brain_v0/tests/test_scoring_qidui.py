"""Seven-pairs fan classification follows the existing wildcard shanten contract."""
from collections import Counter

import pytest

from engine.scoring import ScoreCalculator, _qidui_shape, detect_fans
from engine.shanten import is_win


def _qidui_only_win(hand):
    assert is_win(hand)
    assert not is_win(hand, use_qidui=False)
    fans = detect_fans(hand[:-1], hand[-1])
    assert fans == ["qidui"]
    assert ScoreCalculator().hu_multiplier(fans) == 2


def test_four_identical_tiles_count_as_two_pairs_without_any_extra_fan():
    _qidui_only_win([1,1,1,1,21,21,22,22,23,23,24,24,25,25])


def test_two_jokers_complete_two_separate_singletons():
    _qidui_only_win([1,1,4,4,7,7,21,21,24,24,27,41,55,55])


@pytest.mark.parametrize("hand", [
    [1,1,4,4,7,7,21,21,24,24,27,27,41,55],
    [1,1,4,4,7,7,21,21,24,27,41,55,55,55],
    [1,1,4,4,7,7,21,21,24,27,55,55,55,55],
    [1,1,1,1,21,21,21,21,24,24,27,27,41,41],
])
def test_one_to_four_jokers_and_multiple_quads_match_seven_pairs(hand):
    counts = Counter(tile for tile in hand if tile != 55)
    assert _qidui_shape(counts, hand.count(55))
    assert "qidui" in detect_fans(hand[:-1], hand[-1])
    assert is_win(hand)


@pytest.mark.parametrize("hand", [
    [1,2,3,4,5,6,7,8,9,21,22,23,24,24],
    [1,1,4,4,7,7,21,21,24,27,41,43,55,55],
    [1,1,4,4,7,7,21,24,27,29,41,55,55,55],
    [1,1,4,4,7,21,24,27,29,41,55,55,55,55],
])
def test_insufficient_jokers_and_standard_only_wins_are_not_seven_pairs(hand):
    counts = Counter(tile for tile in hand if tile != 55)
    assert not _qidui_shape(counts, hand.count(55))
    assert "qidui" not in detect_fans(hand[:-1], hand[-1])


@pytest.mark.parametrize("concealed_hand", [
    [1,1,4,4,7,7,21,21,24,24,55],   # one fixed meld: eleven concealed tiles
    [1,1,4,4,21,21,55,55],           # two fixed melds: eight concealed tiles
    [1,1,21,55,55],                   # three fixed melds: five concealed tiles
    [55,55],                          # four fixed melds: two concealed tiles
])
def test_exposed_meld_short_concealed_hand_cannot_form_seven_pairs(concealed_hand):
    counts = Counter(tile for tile in concealed_hand if tile != 55)
    assert not _qidui_shape(counts, concealed_hand.count(55))
    assert "qidui" not in detect_fans(concealed_hand[:-1], concealed_hand[-1])


def test_seven_pairs_fan_stays_twofold_without_luxury_pair_inference():
    hand = [1,1,1,1,21,21,22,22,23,23,24,24,25,25]
    fans = detect_fans(hand[:-1], hand[-1])
    assert fans == ["qidui"]
    assert ScoreCalculator(stacking="max").hu_multiplier(fans) == 2
