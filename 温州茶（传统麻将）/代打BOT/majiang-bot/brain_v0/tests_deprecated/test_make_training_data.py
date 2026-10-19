# -*- coding: utf-8 -*-
"""tools/make_training_data.py 真实截图管线测试：合成截图 → 切单张牌 → 出 npz。"""
import os
import sys
import json

import numpy as np
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.make_training_data import (  # noqa: E402
    make_from_screenshots, segment_row_tiles, normalize_tile,
)


def _synthetic_hand_screenshot(path, n_tiles=7, img_w=1280, img_h=626):
    """画一张 1280x626 假截图：手牌区(200,540)-(1080,620) 放 n_tiles 张亮色牌。"""
    import cv2
    img = np.full((img_h, img_w, 3), 40, dtype=np.uint8)  # 暗背景
    x1, y1, x2, y2 = 200, 540, 1080, 620
    avail = (x2 - x1) // n_tiles
    for i in range(n_tiles):
        cx = x1 + i * avail
        cv2.rectangle(img, (cx + 4, y1 + 6), (cx + avail - 6, y2 - 6), (235, 235, 235), -1)
    cv2.imwrite(str(path), img)
    return path


def test_segment_row_tiles_splits_tiles():
    import cv2
    tmp = _synthetic_hand_screenshot("_t.png")
    img = cv2.imread(str(tmp))
    tiles = segment_row_tiles(img, (200, 540, 1080, 620))
    assert len(tiles) == 7
    # 每张都是牌面（亮）
    for t in tiles:
        assert t.mean() > 100
    os.remove(str(tmp))
    assert len(tiles) == 7


@pytest.mark.parametrize("labels", [None, {0: [1, 2, 3, 4, 5, 6, 7]}])
def test_make_from_screenshots_npz(tmp_path, labels):
    src = tmp_path / "shots"
    src.mkdir()
    img_name = "frame_001.png"
    _synthetic_hand_screenshot(src / img_name, n_tiles=7)
    label_map = None
    if labels is not None:
        label_map = {img_name: labels[0]}
    out = tmp_path / "ds.npz"
    res = make_from_screenshots(str(src), str(out), labels=label_map)
    assert res["n_images"] == 1
    assert res["n_tiles"] == 7
    assert os.path.exists(out)
    d = np.load(out, allow_pickle=True)
    assert d["X"].shape == (7, 32, 32)
    assert d["X"].dtype == np.float32
    assert float(d["X"].max()) <= 1.0
    if label_map is not None:
        assert res["n_labeled"] == 7
        assert list(d["y"]) == [1, 2, 3, 4, 5, 6, 7]
        assert res["note"] == "已标注"
    else:
        assert res["n_labeled"] == 0
        assert "y" not in d or len(d["y"]) == 0
        assert res["note"].startswith("未标注")


def test_normalize_tile_range():
    import cv2
    img = np.full((40, 40, 3), 200, dtype=np.uint8)
    n = normalize_tile(img)
    assert n.shape == (32, 32)
    assert n.dtype == np.float32
    assert 0.0 <= float(n.max()) <= 1.0