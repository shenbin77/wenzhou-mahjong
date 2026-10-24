"""Inspect the two dimmed regression frames tile by tile."""
import sys

sys.path.insert(0, r'D:\温州麻将\温州茶（传统麻将）\bot')
sys.path.insert(0, r'D:\温州麻将')

from tile_vision import _read, caishen_badge, face_background, TileVision
from _probe_meld_geom import raw_boxes

FRAMES = [
    ('bright r05', r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-15\424821\r05\frame_02.png'),
    ('dimmed r08', r'D:\温州麻将\温州茶（传统麻将）\采集\2026-10-15\424821\r08\frame_02.png'),
]
tv = TileVision(template_dir=r'D:\温州麻将\温州茶（传统麻将）\采集\templates\tea_tiles')
for tag, p in FRAMES:
    img = _read(p)
    boxes, _, _ = raw_boxes(img)
    print(f'=== {tag} boxes={len(boxes)}')
    for i, b in enumerate(boxes):
        crop = img[b[1]:b[3], b[0]:b[2]]
        if crop.size == 0:
            print(f'   [{i:02d}] empty')
            continue
        d = tv.classify_crop(crop, caishen_label=None)
        print(f'   [{i:02d}] bg={face_background(crop):6.1f} badge={caishen_badge(crop)} '
              f'label={d["label"]} score={d["score"]:.3f} acc={d["accepted"]} ({d["reason"]})')
    rec = tv.recognize_hand(img, caishen_label=None)
    print(f'   -> recognized={len(rec)} complete={tv.last_complete} '
          f'concealed={tv.detection_diagnostics.get("concealed_count")}')
