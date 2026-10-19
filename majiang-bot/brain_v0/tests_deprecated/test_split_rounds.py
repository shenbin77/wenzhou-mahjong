import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from split_rounds import parse_signed, seat_of


def test_parse_signed_merges_split_digits():
    words = [("一", 66, 436), ("3", 109, 418), ("0", 148, 418), ("0", 190, 418)]
    assert parse_signed(words) == [(-300, 66, 436)]


def test_parse_signed_plus_and_separate():
    words = [("+", 828, 175), ("1500", 865, 168), ("+0", 900, 640)]
    vals = parse_signed(words)
    assert (1500, 828, 175) in vals
    assert (0, 900, 640) in vals


def test_parse_signed_ignores_unsigned():
    assert parse_signed([("500", 794, 785)]) == []


def test_seat_of_geometry():
    assert seat_of(865, 168) == "top"
    assert seat_of(66, 436) == "left"
    assert seat_of(1200, 430) == "right"
    assert seat_of(860, 640) == "bottom"
