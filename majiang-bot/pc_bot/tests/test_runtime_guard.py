import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_guard import ActionGate, PendingDiscard, PendingPeng, RoundTracker, valid_target_hand


class RuntimeGuardTests(unittest.TestCase):
    def test_sampling_honours_and_seventeen_tiles_cannot_reach_engine(self):
        self.assertFalse(valid_target_hand([1]*4+[2]*4+[3]*4+[61]))
        self.assertFalse(valid_target_hand([1,2,3,4,5,6,7,8,9,21,22,23,24,25,26,27,28]))
        self.assertFalse(valid_target_hand([1]*5+[2,3,4,5,6,7,8,9]))

    def test_short_hand_requires_confirmed_meld_count(self):
        hand = [1,2,3,4,5,6,21,22,23,55,55]
        self.assertFalse(valid_target_hand(hand))
        self.assertTrue(valid_target_hand(hand, 1))

    def test_animation_empty_frame_does_not_finish_round(self):
        tracker = RoundTracker()
        self.assertEqual(tracker.update(14), ('round_start', True))
        tracker.update(0)
        self.assertEqual(tracker.rounds_done, 0)
        self.assertTrue(tracker.active)

    def test_settlement_edge_counts_once_and_stops_at_sixteen(self):
        tracker = RoundTracker()
        for i in range(16):
            tracker.update(0)
            tracker.update(0)
            tracker.update(13)
            self.assertEqual(tracker.update(14, settlement=True)[0], 'round_end')
            tracker.update(14, settlement=True)
            self.assertEqual(tracker.rounds_done, i+1)
        self.assertEqual(tracker.rounds_done, 16)
        self.assertFalse(tracker.active)

    def test_initial_settlement_does_not_invent_completed_round(self):
        tracker = RoundTracker()
        tracker.update(14, settlement=True)
        self.assertEqual(tracker.rounds_done, 0)

    def test_settlement_template_flicker_does_not_duplicate_round(self):
        tracker = RoundTracker()
        tracker.update(14)
        tracker.update(13,settlement=True)
        tracker.update(13)
        tracker.update(13,settlement=True)
        self.assertEqual(tracker.rounds_started,1)
        self.assertEqual(tracker.rounds_done,1)

    def test_peng_confirmation_requires_two_of_same_tile_removed(self):
        pending = PendingPeng((1,1,2,3,4),0)
        self.assertTrue(pending.confirmed([2,3,4]))
        self.assertFalse(pending.confirmed([1,1,4]))
        self.assertFalse(PendingPeng((55,55,2,3,4),0).confirmed([2,3,4]))

    def test_click_requires_two_stable_observations_and_no_replay(self):
        gate = ActionGate()
        key = ('discard', (1,2,3))
        self.assertFalse(gate.ready(key))
        self.assertTrue(gate.ready(key))
        gate.consume(key)
        self.assertFalse(gate.ready(key))
        self.assertFalse(gate.ready(('discard', (1,2,4))))

    def test_acknowledgement_matches_exact_discard_not_arbitrary_change(self):
        pending = PendingDiscard((1,1,2,3),1,(200,800),0)
        self.assertTrue(pending.confirmed([1,2,3]))
        self.assertFalse(pending.confirmed([1,1,2]))


if __name__ == '__main__':
    unittest.main()
