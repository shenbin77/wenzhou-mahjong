"""误判守门统计：把"高置信度已定牌"的筒牌逐一过点数判据。

基线（改动前）已经 accepted 的筒牌，其标签有 score>=0.82 且 margin>=0.055 的
支撑，可以当作参照真值。若点数判据在这批牌上读出不同的点数，说明判据本身
不可靠——尤其是"五筒被读成四筒"，这条必须为 0。
"""
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

BOT = Path(__file__).resolve().parent
TEA = BOT.parent
TEMPLATES = TEA / '采集' / 'templates' / 'tea_tiles'
sys.path.insert(0, str(BOT))
import tile_vision as new_mod  # noqa: E402
from pip_discriminator import expected_pip_count, read_dot_pips  # noqa: E402
from _accept_pip_eval import frame_paths, read_image  # noqa: E402


def main():
    paths = frame_paths()
    per_label = {}
    conflicts = []
    unreadable = {}
    started = time.time()
    for index, path in enumerate(paths, 1):
        image = read_image(path)
        if image is None or image.size == 0:
            continue
        vision = new_mod.TileVision(template_dir=TEMPLATES)
        vision.recognize_hand(image)
        for entry in vision.diagnostics + vision.meld_diagnostics:
            label = entry.get('label')
            expected = expected_pip_count(label)
            if not entry.get('accepted') or expected is None:
                continue
            x0, y0, x1, y1 = entry['box']
            reading = read_dot_pips(image[y0:y1, x0:x1])
            bucket = per_label.setdefault(label, {'tiles': 0, 'agree': 0,
                                                  'unreadable': 0})
            bucket['tiles'] += 1
            if reading.count is None:
                bucket['unreadable'] += 1
                unreadable[reading.reason] = unreadable.get(reading.reason, 0) + 1
                continue
            if reading.count == expected:
                bucket['agree'] += 1
            else:
                conflicts.append({
                    'frame': str(path.relative_to(TEA)),
                    'box': entry['box'],
                    'label': label,
                    'score': round(float(entry['score']), 6),
                    'margin': round(float(entry['margin']), 6),
                    'pip_count': reading.count,
                    'pip_reason': reading.reason,
                })
        if index % 200 == 0:
            print(f'  ...{index}/{len(paths)} {time.time()-started:.0f}s', flush=True)
    out = BOT / '_accept_pip_dotcheck.json'
    out.write_text(json.dumps({'labels': per_label, 'conflicts': conflicts,
                               'unreadable_reasons': unreadable,
                               'frames': len(paths)},
                              ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(per_label, ensure_ascii=False, indent=1))
    print('conflicts', len(conflicts))
    print('five_read_as_four', sum(1 for c in conflicts
                                   if c['label'] == 't5' and c['pip_count'] == 4))
    print('unreadable_reasons', unreadable)
    print(f'wrote {out} in {time.time()-started:.0f}s')


if __name__ == '__main__':
    main()
