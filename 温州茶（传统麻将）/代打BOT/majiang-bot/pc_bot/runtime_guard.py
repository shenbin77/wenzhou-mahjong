"""Pure state guards for target rounds and acknowledged actions."""
from collections import Counter
from dataclasses import dataclass
import time

TARGET_IDS = set(range(1, 10)) | set(range(21, 30)) | set(range(41, 50)) | {55}


def valid_target_hand(hand, open_meld_count=0):
    expected = 13 - 3 * open_meld_count
    return (0 <= open_meld_count <= 4 and len(hand) in (expected, expected + 1)
            and set(hand) <= TARGET_IDS and max(Counter(hand).values(), default=0) <= 4)


class RoundTracker:
    def __init__(self):
        self.last_count = 0
        self.rounds_started = self.rounds_done = 0
        self.turn_no = 1
        self.open_meld_count = 0
        self.active = False
        self.round_start_time = 0
        self._settlement_seen = False
        self._clear_streak = 0

    def update(self, count, settlement=False):
        event = None
        if settlement:
            if self.active and not self._settlement_seen:
                self.rounds_done += 1
                event = 'round_end'
            self.active = False
            self._settlement_seen = True
            self._clear_streak = 0
        elif count == 0:
            self._clear_streak += 1
            if self._clear_streak >= 2:
                self._settlement_seen = False
        elif count in (13, 14) and not self.active and not self._settlement_seen:
            self.active = True
            self._settlement_seen = False
            self.open_meld_count = 0
            self.rounds_started += 1
            self.turn_no = 1
            self.round_start_time = time.time()
            event = 'round_start'
            self._clear_streak = 0
        self.last_count = count
        return event, self.active and count == 14 - 3 * self.open_meld_count


@dataclass
class PendingDiscard:
    hand: tuple
    choice: int
    point: tuple
    created_at: float

    def confirmed(self, hand):
        expected = Counter(self.hand)
        expected[self.choice] -= 1
        return +expected == Counter(hand)


@dataclass
class PendingPeng:
    hand: tuple
    created_at: float

    def confirmed(self, hand):
        before, after = Counter(self.hand), Counter(hand)
        removed = before - after
        return (not (after-before) and len(removed)==1
                and next(iter(removed)) != 55 and next(iter(removed.values()))==2)


class ActionGate:
    """Require the same complete observation twice, then deduplicate it."""
    def __init__(self):
        self.last = None
        self.streak = 0
        self.consumed = None

    def ready(self, key):
        if key != self.last:
            self.last, self.streak = key, 1
            if key != self.consumed:
                self.consumed = None
            return False
        self.streak += 1
        return self.streak >= 2 and key != self.consumed

    def consume(self, key):
        self.consumed = key

    def reset(self):
        self.last = self.consumed = None
        self.streak = 0
