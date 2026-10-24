"""实测对比：改动前 / 改动后 逐帧识牌（只读采集帧，不写采集目录）。"""
import importlib.machinery
import importlib.util
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

BOT = Path(__file__).resolve().parent
TEA = BOT.parent
TEMPLATES = TEA / '采集' / 'templates' / 'tea_tiles'
BACKUP = TEA / '_backups' / '20261024_wb_pip_discriminator' / 'tile_vision.py.orig'

sys.path.insert(0, str(BOT))
import tile_vision as new_mod  # noqa: E402


def load_baseline():
    loader = importlib.machinery.SourceFileLoader('tile_vision_baseline', str(BACKUP))
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader('tile_vision_baseline', loader))
    sys.modules['tile_vision_baseline'] = module
    loader.exec_module(module)
    return module


def read_image(path):
    """Decode a capture PNG; corrupt/empty files are reported as None."""
    try:
        buffer = np.fromfile(str(path), dtype=np.uint8)
        if buffer.size == 0:
            return None
        return cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    except cv2.error:
        return None


def dot_rejections(vision):
    """Dot-tile crops the classifier refused to label, with their evidence."""
    out = []
    for d in vision.diagnostics + vision.meld_diagnostics:
        if d.get('accepted'):
            continue
        label = d.get('label') or ''
        runner = d.get('runner_up') or ''
        if label.startswith('t') or runner.startswith('t'):
            out.append({
                'box': d.get('box'),
                'label': label,
                'runner_up': runner,
                'score': round(float(d.get('score') or 0.), 10),
                'margin': round(float(d.get('margin') or 0.), 10),
                'reason': d.get('reason'),
            })
    return out


def run(module, path):
    image = read_image(path)
    if image is None or image.size == 0:
        return None
    vision = module.TileVision(template_dir=TEMPLATES)
    tiles = vision.recognize_hand(image)
    return {
        'count': vision.detection_diagnostics.get('concealed_count'),
        'accepted': len(tiles),
        'complete': bool(vision.last_complete),
        'rejected': [d for d in vision.diagnostics if not d.get('accepted')],
        'rejections': dot_rejections(vision),
        'diagnostics': vision.diagnostics,
        'tiles': [int(t[0]) for t in tiles],
    }


def frame_paths():
    paths = []
    dev = TEA / '采集' / 'dev_auto_play'
    if dev.exists():
        for folder in sorted(dev.glob('*/frames')):
            paths.extend(sorted(folder.glob('*.png')))
        for folder in sorted(dev.glob('*')):
            if folder.is_dir() and not (folder / 'frames').exists():
                paths.extend(sorted(folder.glob('*.png')))
    for day in sorted((TEA / '采集').glob('2026-10-*')):
        paths.extend(sorted(day.rglob('frame_*.png')))
    seen, unique = set(), []
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    paths = frame_paths()
    if limit:
        paths = paths[:limit]
    baseline = load_baseline()
    rows = []
    started = time.time()
    skipped = []
    for index, path in enumerate(paths, 1):
        before = run(baseline, path)
        after = run(new_mod, path)
        if before is None or after is None:
            skipped.append(str(path.relative_to(TEA)))
            continue
        rows.append({
            'frame': str(path.relative_to(TEA)),
            'before_accepted': before['accepted'],
            'before_complete': before['complete'],
            'before_rejections': before['rejections'],
            'after_accepted': after['accepted'],
            'after_complete': after['complete'],
            'after_rejections': after['rejections'],
            'after_pip': [{'box': d.get('box'), 'label': d.get('label'),
                           'reason': d.get('reason'),
                           'pip': d.get('pip_reading'),
                           'score': d.get('pip_source_score'),
                           'margin': d.get('pip_source_margin')}
                          for d in after['diagnostics']
                          if d.get('reason') == 'pip_count'],
            'before_tiles': before['tiles'],
            'after_tiles': after['tiles'],
        })
        if index % 25 == 0:
            print(f'  ...{index}/{len(paths)}  {time.time()-started:.1f}s', flush=True)
    out = BOT / '_accept_pip_eval.json'
    out.write_text(json.dumps({'frames': rows, 'skipped': skipped},
                              ensure_ascii=False, indent=1),
                   encoding='utf-8')
    print(f'wrote {out} rows={len(rows)} skipped={len(skipped)} '
          f'in {time.time()-started:.1f}s')
    if skipped:
        print('skipped:', skipped[:5])


if __name__ == '__main__':
    main()
