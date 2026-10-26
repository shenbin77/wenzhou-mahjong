"""Compare gold-manifest accuracy before vs after the caishen fix."""
import importlib.machinery
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(r'D:\温州麻将')
BOT = ROOT / '温州茶（传统麻将）' / 'bot'
sys.path.insert(0, str(BOT))

from tile_vision import TILE_IDS, TileVision, _read

before_mod = importlib.machinery.SourceFileLoader(
    'tile_vision_before', r'D:\温州麻将\.tmp\meld4\tile_vision_before.py').load_module()

manifest = json.loads((BOT / 'gold' / 'tea_2p_manifest.json').read_text(encoding='utf-8'))
TPL = ROOT / '温州茶（传统麻将）' / '采集' / 'templates' / 'tea_tiles'
ID_TO_LABEL = {v: k for k, v in TILE_IDS.items()}

bv = before_mod.TileVision(template_dir=TPL)
av = TileVision(template_dir=TPL)

for frame in manifest['frames']:
    path = ROOT / Path(frame['frame_path'])
    img = _read(path)
    gold = Counter(frame['annotated_hand_multiset'])
    print(f'{path.parent.parent.name}/{path.parent.name}/{path.name}  gold={sum(gold.values())} 张')
    for tag, vision in (('before', bv), ('after ', av)):
        result = vision.recognize_hand(img)
        predicted = [ID_TO_LABEL[tid] for tid, *_ in result]
        obs = Counter(predicted)
        correct = sum((gold & obs).values())
        caishen = vision._read_table_caishen(img) if hasattr(vision, '_read_table_caishen') else '-'
        print(f'   {tag}: n={len(predicted):2d} correct={correct} wrong={len(predicted)-correct} '
              f'missed={sum(gold.values())-correct} complete={vision.last_complete} '
              f'caishen={caishen}')
        if tag == 'after ':
            print(f'          labels={predicted}')
    print()
