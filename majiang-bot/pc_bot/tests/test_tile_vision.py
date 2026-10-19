"""Offline visual QA; these recorded samples never validate target gameplay."""
from pathlib import Path
import json
import sys
import unittest

import cv2
import numpy as np

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from tile_vision import TileVision,TILE_IDS,TARGET_IDS,_read,face_region
from tools.rebuild_templates import CLUSTER_LABELS,pixel_hash


class TileVisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vision=TileVision()
        cls.vision.load_templates()
        cls.manifest=json.loads((BASE/'templates_hongzhong_v2/manifest.json').read_text(encoding='utf-8'))

    def test_complete_visual_vocabulary_and_target_scope(self):
        records=self.manifest['templates']
        self.assertEqual({r['label'] for r in records},set(TILE_IDS))
        self.assertEqual(len(TILE_IDS),34)
        self.assertEqual(len(TARGET_IDS),28)
        self.assertNotIn(TILE_IDS['dong'],TARGET_IDS)
        self.assertIn(TILE_IDS['hongzhong'],TARGET_IDS)
        for label in TILE_IDS:
            with self.subTest(label=label):
                d=self.vision.classify_crop(_read(BASE/'templates_hongzhong_v2'/f'{label}.png'))
                self.assertTrue(d['accepted'],d)
                self.assertEqual(d['tile_id'],TILE_IDS[label])

    def test_exact_pixel_provenance_for_every_template(self):
        for r in self.manifest['templates']:
            with self.subTest(template=r['file']):
                im=_read(BASE/r['screenshot']);x0,y0,x1,y1=r['box']
                crop=im[y0:y1,x0:x1]
                if r.get('rotation')=='90_clockwise':
                    crop=cv2.rotate(crop,cv2.ROTATE_90_CLOCKWISE)
                self.assertEqual(pixel_hash(crop),r['source_pixel_sha256'])
                output=_read(BASE/'templates_hongzhong_v2'/r['file'])
                self.assertEqual(pixel_hash(output),r['template_pixel_sha256'])
                np.testing.assert_array_equal(output,face_region(crop))

    def test_nonduplicate_cluster_holdout_is_correct(self):
        training={r['source_pixel_sha256'] for r in self.manifest['templates']}
        checked=0
        for p in sorted((BASE/'harvest2/verify').glob('*.png')):
            expected=CLUSTER_LABELS.get(int(p.name[1:3]))
            if expected is None:continue
            im=_read(p)
            if pixel_hash(im) in training:continue
            with self.subTest(source=p.name):
                d=self.vision.classify_crop(im)
                self.assertTrue(d['accepted'],d)
                self.assertEqual(d['label'],expected)
                checked+=1
        self.assertGreaterEqual(checked,50)

    def test_full_hand_holdout_and_leftmost_tiles(self):
        scenes={
            'live_070.png':{
                'concealed':['w5','bai','w9','w9','s1','s2','s3','s9','t4','t4','t7','t8','t9','xi','hongzhong','fa','xi'],
                'meld':[],
            },
            # The three raised leftmost 西 are an exposed meld. count_tiles()
            # intentionally returns concealed boxes only and exposes these via
            # last_meld_boxes/last_melds.
            'live_090.png':{
                'concealed':['w5','bai','w9','w9','s1','s2','s3','s9','t4','t4','t7','t8','t9','fa'],
                'meld':['xi','xi','xi'],
            },
        }
        for name,expected in scenes.items():
            with self.subTest(scene=name):
                im=_read(BASE/'frames_live'/name)
                count,boxes=self.vision.count_tiles(im)
                self.assertEqual(count,len(expected['concealed']))
                self.assertEqual(len(self.vision.last_meld_boxes),len(expected['meld']))
                if expected['meld']:
                    self.assertLess(max(b[0] for b in self.vision.last_meld_boxes),boxes[0][0])
                else:
                    self.assertLess(boxes[0][0],150)
                    self.assertLess(boxes[1][0],150)
                hand=self.vision.recognize_hand(im)
                self.assertEqual([t[0] for t in hand],[TILE_IDS[x] for x in expected['concealed']])
                self.assertEqual([t[0] for t in self.vision.last_melds],[TILE_IDS[x] for x in expected['meld']])
                self.assertTrue(self.vision.last_complete)

    def test_occluded_face_is_rejected_not_guessed(self):
        self.vision.recognize_hand(_read(BASE/'frames_live/live_020.png'))
        first=self.vision.diagnostics[0]
        self.assertFalse(first['accepted'])
        self.assertIsNone(first['tile_id'])
        self.assertFalse(self.vision.last_complete)
        self.assertEqual(sum(d['accepted'] for d in self.vision.diagnostics),16)

    def test_same_style_four_and_five_circles_are_distinct(self):
        t4=_read(BASE/'templates_hongzhong_v2/t4.png')
        t5=_read(BASE/'templates_hongzhong_v2/t5.png')
        self.assertNotEqual(pixel_hash(t4),pixel_hash(t5))
        self.assertEqual(self.vision.classify_crop(t4)['tile_id'],24)
        self.assertEqual(self.vision.classify_crop(t5)['tile_id'],25)

    def test_blank_and_impossible_hand_are_rejected(self):
        blank=np.full((123,90,3),210,np.uint8)
        self.assertFalse(self.vision.classify_crop(blank)['accepted'])
        # A counterexample assembled from a real crop checks the max-four
        # invariant. It is never a source template or gameplay evidence.
        tile=_read(BASE/'templates_hongzhong_v2/dong.png')
        scene=np.zeros((900,1600,3),np.uint8);scene[:]=(35,90,35)
        tile=cv2.resize(tile,(90,123),interpolation=cv2.INTER_NEAREST)
        for i in range(5):scene[760:883,5+i*94:95+i*94]=tile
        result=self.vision.recognize_hand(scene)
        self.assertEqual(len(self.vision.diagnostics),5)
        self.assertEqual(result,[])
        self.assertTrue(all(d['reason']=='more_than_four' for d in self.vision.diagnostics))
        self.assertFalse(self.vision.last_complete)


if __name__=='__main__':
    unittest.main()
