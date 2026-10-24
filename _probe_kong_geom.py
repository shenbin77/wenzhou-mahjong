# -*- coding: utf-8 -*-
"""只读探针：测量暗杠局面的手牌区几何账目。

对指定帧跑 tile_vision.count_tiles + classify_crop（带财神 label），
输出全部框的位置/label/reason 与 meld 判定，用于确定：
暗杠 4 张同牌在屏幕上占几个框、闭手张数账目怎么算。
不修改任何识别逻辑，不写盘（除 stdout）。
"""
import sys
from pathlib import Path

import cv2
import numpy as np

BOT_DIR = Path(__file__).resolve().parent / "温州茶（传统麻将）" / "bot"
sys.path.insert(0, str(BOT_DIR))
from tile_vision import TileVision  # noqa: E402
from caishen_vision import detect_caishen  # noqa: E402

TEMPLATES = Path(__file__).resolve().parent / "温州茶（传统麻将）" / "采集" / "templates" / "tea_tiles"

FRAMES = [
    "温州茶（传统麻将）/采集/assist_sessions/20261024_005400/live/tea_2p/20261024_005700_684588_0ae39a.png",
]


def main():
    tv = TileVision(template_dir=TEMPLATES)
    for rel in FRAMES:
        path = Path(rel)
        if not path.is_file():
            print(f"MISS {path}")
            continue
        img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        caishen = detect_caishen(img).label
        print(f"== {path.name}  caishen={caishen}")
        count, boxes = tv.count_tiles(img)
        dd = tv.detection_diagnostics
        print(f"   visible={dd.get('visible_count')} concealed={count} "
              f"meld_boxes={dd.get('meld_boxes')} unresolved={dd.get('unresolved_regions')}")
        for i, (x0, y0, x1, y1) in enumerate(list(dd.get('meld_boxes', [])) + boxes):
            d = tv.classify_crop(img[y0:y1, x0:x1], caishen_label=caishen)
            region = 'meld' if i < len(dd.get('meld_boxes', [])) else 'hand'
            print(f"   [{region}] box=({x0},{y0},{x1},{y1}) w={x1-x0} h={y1-y0} "
                  f"label={d['label']} score={d['score']:.3f} "
                  f"acc={d['accepted']} reason={d['reason']} marked={d.get('caishen_marked')}")


if __name__ == "__main__":
    main()
