"""No device access or business database writes during integration tests."""
import importlib.util
import logging
from pathlib import Path
import sys
import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch, Mock

import cv2
import numpy as np

PC = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PC))
with patch.object(logging,'FileHandler',lambda *a,**k: logging.NullHandler()):
    spec=importlib.util.spec_from_file_location('bot_integration_subject',PC/'bot.py')
    botmod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(botmod)


class BotIntegrationTests(unittest.TestCase):
    def make_bot(self,frame):
        with patch.object(botmod,'ADB') as adb:
            adb.return_value.serial='test-explicit-device'
            adb.return_value.capture.return_value=frame
            instance=botmod.Bot(dry_run=True)
        return instance

    def test_winning_hand_never_returns_a_discard(self):
        hand=[1,2,3,4,5,6,7,8,9,21,22,23,24,55]
        with self.assertRaisesRegex(ValueError,'已胡牌'):
            botmod.Brain().decide(hand)

    def test_non_winning_decision_is_legal_and_preserves_joker(self):
        hand=[1,2,4,5,6,8,21,22,24,26,41,43,45,55]
        choice=botmod.Brain().decide(hand)
        self.assertIn(choice,hand)
        self.assertNotEqual(choice,55)

    def test_observe_mode_never_clicks_or_writes_business_database(self):
        instance=self.make_bot(np.zeros((900,1600,3),np.uint8))
        instance.recognizer=Mock()
        instance.recognizer.count_tiles.return_value=(0,[])
        instance.check_xiayiju=Mock(return_value=None)
        with patch.object(botmod,'save_round') as save:
            instance.tick()
            instance.adb.tap.assert_not_called()
            save.assert_not_called()

    def test_non_dry_tap_requires_target_game_foreground(self):
        instance = self.make_bot(np.zeros((900,1600,3),np.uint8))
        instance.dry_run = False
        instance.adb.foreground_package.return_value = 'com.other.app'
        with self.assertRaisesRegex(RuntimeError, '前景应用不是浙江游戏大厅'):
            instance.do_tap(400,800,'test')
        instance.adb.tap.assert_not_called()
        instance.adb.foreground_package.return_value = 'com.xm.zjgamecenter'
        instance.do_tap(400,800,'test')
        instance.adb.tap.assert_called_once_with(400,800)

    def test_pending_discard_never_repeats_stale_coordinate(self):
        instance = self.make_bot(np.zeros((900,1600,3),np.uint8))
        instance.dry_run = False
        instance.state.active = True
        instance.state.rounds_started = 1
        hand = [1,2,3,4,5,6,7,8,9,21,22,23,24,55]
        instance.recognizer = Mock()
        instance.recognizer.count_tiles.return_value = (14,[(0,750,80,890)]*14)
        instance.recognizer.recognize_hand.return_value = [(tile,0,750,80,890) for tile in hand]
        instance.recognizer.last_complete = True
        instance.check_xiayiju = Mock(return_value=None)
        instance._pending_discard = botmod.PendingDiscard(tuple(hand),1,(40,820),time.monotonic()-2)
        instance.tick()
        instance.adb.tap.assert_not_called()

    def test_observer_does_not_compute_live_discard_recommendations(self):
        instance=self.make_bot(np.zeros((900,1600,3),np.uint8))
        hand=[1,2,3,4,5,6,7,8,9,21,22,23,24,26]
        instance.recognizer=Mock()
        instance.recognizer.count_tiles.return_value=(14,[])
        instance.recognizer.recognize_hand.return_value=[(t,0,0,80,140) for t in hand]
        instance.recognizer.last_complete=True
        instance.check_xiayiju=Mock(return_value=None)
        instance.play_turn=Mock()
        for _ in range(3):instance.tick()
        instance.play_turn.assert_not_called()
        instance.adb.tap.assert_not_called()

    def test_sixteenth_settlement_precedes_any_next_round_action(self):
        instance=self.make_bot(np.zeros((900,1600,3),np.uint8))
        instance.state.rounds_started=16
        instance.state.rounds_done=15
        instance.state.active=True
        instance.recognizer=Mock()
        instance.recognizer.count_tiles.return_value=(14,[])
        instance.check_xiayiju=Mock(return_value=(1300,800))
        instance.tick()
        self.assertEqual(instance.state.rounds_done,16)
        self.assertFalse(instance.running)
        instance.adb.tap.assert_not_called()

    def test_sampling_mode_full_hand_is_rejected_as_target_game(self):
        # cv2.imread cannot reliably open Unicode paths on Windows.
        frame=cv2.imdecode(np.fromfile(str(PC/'frames_live'/'live_070.png'), dtype=np.uint8), cv2.IMREAD_COLOR)
        instance=self.make_bot(frame)
        instance.tick()
        self.assertEqual(instance.state.rounds_started,0)
        instance.adb.tap.assert_not_called()

    def test_incomplete_thirteen_boxes_do_not_start_a_round(self):
        instance=self.make_bot(np.zeros((900,1600,3),np.uint8))
        instance.recognizer=Mock()
        instance.recognizer.count_tiles.return_value=(13,[(1,1,2,2)]*13)
        instance.recognizer.recognize_hand.return_value=[]
        instance.recognizer.last_complete=False
        instance.recognizer.diagnostics=[]
        instance.check_xiayiju=Mock(return_value=None)
        instance.tick()
        self.assertEqual(instance.state.rounds_started,0)
        self.assertFalse(instance.state.active)

    def test_connection_failure_closes_transport_and_persists_stopped_status(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(botmod.CONFIG,{'capture_dir':folder}):
            instance=self.make_bot(np.zeros((900,1600,3),np.uint8))
            instance.adb.check_connection.return_value=(False,'offline')
            self.assertEqual(instance.run(),1)
            instance.adb.close.assert_called_once()
            status=json.loads((Path(folder)/'status.json').read_text('utf-8'))
            self.assertFalse(status['running'])
            self.assertEqual(status['stop_reason'],'connection_unavailable')
            self.assertFalse(status['round_counts_verified'])
            self.assertFalse(list(Path(folder).glob('**/*.png')))

    def test_duration_exit_persists_stopped_status_and_never_captures(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(botmod.CONFIG,{'capture_dir':folder,'duration':1}):
            instance=self.make_bot(np.zeros((900,1600,3),np.uint8))
            instance.started_at-=2
            instance.adb.check_connection.return_value=(True,'device')
            instance.recognizer=Mock()
            self.assertEqual(instance.run(),0)
            instance.adb.capture.assert_not_called()
            instance.adb.close.assert_called_once()
            status=json.loads((Path(folder)/'status.json').read_text('utf-8'))
            self.assertFalse(status['running'])
            self.assertEqual(status['stop_reason'],'duration_limit')

    def test_two_observers_sharing_output_keep_independent_statuses(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(botmod.CONFIG,{'capture_dir':folder}):
            first=self.make_bot(np.zeros((900,1600,3),np.uint8))
            second=self.make_bot(np.zeros((900,1600,3),np.uint8))
            def repeatedly_write(instance):
                for _ in range(20):
                    instance._write_status()
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(repeatedly_write,instance) for instance in (first,second)]
                for future in futures:future.result()
            for instance in (first,second):
                status=json.loads((Path(folder)/f'status_{instance.recorder.session_id}.json').read_text('utf-8'))
                self.assertEqual(status['session_id'],instance.recorder.session_id)
            latest=json.loads((Path(folder)/'status.json').read_text('utf-8'))
            self.assertIn(latest['session_id'],{first.recorder.session_id,second.recorder.session_id})
            self.assertFalse(list(Path(folder).glob('*.tmp')))

    def test_once_closes_transport_even_when_status_write_fails(self):
        instance=Mock()
        instance.adb.check_connection.return_value=(True,'device')
        ok,png=cv2.imencode('.png',np.zeros((90,160,3),np.uint8))
        self.assertTrue(ok)
        instance.adb.screencap.return_value=png.tobytes()
        instance.recognizer.count_tiles.return_value=(0,[])
        instance.recognizer.recognize_hand.return_value=[]
        instance._write_status.side_effect=OSError('injected status failure')
        with patch.object(sys,'argv',['bot.py','--once']),patch.object(botmod,'Bot',return_value=instance),patch.dict(botmod.CONFIG,botmod.CONFIG.copy()):
            with self.assertRaisesRegex(OSError,'injected status failure'):
                botmod.main()
        instance.adb.close.assert_called_once()


if __name__=='__main__':unittest.main()
