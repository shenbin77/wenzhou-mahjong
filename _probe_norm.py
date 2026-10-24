"""Compare raw vs brightness-normalised classification for the highlighted and dimmed tiles."""
import sys
from pathlib import Path

BOT = Path(r'D:\温州麻将\温州茶（传统麻将）\bot')
sys.path.insert(0, str(BOT))

import cv2
import numpy as np

from tile_vision import TileVision, _read, face_region

TPL = BOT.parent / '采集' / 'templates' / 'tea_tiles'
vision = TileVision(template_dir=TPL)
vision.load_templates()

SESSION = BOT.parent / '采集' / 'assist_sessions' / '20261023_201640' / 'live' / 'tea_2p'


def normalise(crop, target=238.0):
    """Scale brightness so the face background matches an ordinary lit tile."""
    face = face_region(crop)
    hsv = cv2.cvtColor(face, cv2.COLOR_BGR2HSV)
    bg = float(np.percentile(hsv[:, :, 2], 70))
    if bg <= 1.0:
        return crop
    gain = target / bg
    out = np.clip(crop.astype(np.float32) * gain, 0, 255).astype(np.uint8)
    return out


CASES = [
    ('recon 财神牌 四条 (badge)', SESSION / '20261023_205345_749900_23750c.png', (561, 768, 643, 891)),
    ('recon 普通牌 七萬', SESSION / '20261023_205345_749900_23750c.png', (837, 768, 925, 892)),
    ('pong 暗牌 二条 (未识别)', SESSION / '20261023_205534_475580_f4a34e.png', (744, 768, 833, 892)),
    ('pong 暗牌 六萬 (已识别)', SESSION / '20261023_205534_475580_f4a34e.png', (283, 768, 372, 892)),
    ('gold 普通牌 二萬', BOT.parent / '采集' / '2026-10-16' / '740195' / 'r08' / 'frame_02.png', (6, 768, 95, 892)),
]
for tag, path, box in CASES:
    img = _read(path)
    x0, y0, x1, y1 = box
    crop = img[y0:y1, x0:x1]
    raw = vision.classify_crop(crop)
    norm = vision.classify_crop(normalise(crop))
    print(f'{tag}')
    print(f'   raw  label={raw["label"]:>6s} score={raw["score"]:.3f} margin={raw["margin"]:.3f} '
          f'accepted={raw["accepted"]} ({raw["reason"]})')
    print(f'   norm label={norm["label"]:>6s} score={norm["score"]:.3f} margin={norm["margin"]:.3f} '
          f'accepted={norm["accepted"]} ({norm["reason"]})')
