# -*- coding: utf-8 -*-
"""recognizer 修复回归测试（问题4：手牌最多 14 张上限）。

运行: python -m pytest tests/test_recognizer.py -q
"""
import os
import sys

import numpy as np

SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import config
from recognizer import TileRecognizer


def test_recognize_hand_tiles_capped_at_fourteen(monkeypatch):
    """问题4: 单帧过度检测产出 >14 张时，截断到 14 并告警。"""
    rec = TileRecognizer()
    # 用假的 region 占位（不会被真正匹配，因为下面直接替换 recognize_region）
    fake_img = object()

    # 构造 20 个不同 tile / 不同 x 的匹配（x 间距>25 不触发去重合并）
    tiles_cycle = ["1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m",
                   "1s", "2s", "3s", "4s", "5s", "6s", "7s", "8s", "9s",
                   "1p", "2p"]
    fake_matches = [
        (tid, 0.9, (i * 30, 10)) for i, tid in enumerate(tiles_cycle)
    ]

    def fake_recognize_region(self, region_img_cv2, skip_caishen=False):
        return list(fake_matches)

    monkeypatch.setattr(TileRecognizer, "recognize_region", fake_recognize_region)

    result = rec.recognize_hand_tiles(fake_img)
    assert len(result) == 14, f"手牌未被截断到14: {len(result)}"
    # 截断应保留前面的牌（按 x 排序后的前 14 张）
    assert result[:3] == ["1m", "2m", "3m"]


def test_recognize_hand_tiles_no_caps_when_under_fourteen(monkeypatch):
    """问题4 反向：正常 13 张不应被截断。"""
    rec = TileRecognizer()
    fake_img = object()
    tiles_cycle = ["1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m",
                   "1s", "2s", "3s", "4s"]
    fake_matches = [(tid, 0.9, (i * 30, 10)) for i, tid in enumerate(tiles_cycle)]

    def fake_recognize_region(self, region_img_cv2, skip_caishen=False):
        return list(fake_matches)

    monkeypatch.setattr(TileRecognizer, "recognize_region", fake_recognize_region)

    result = rec.recognize_hand_tiles(fake_img)
    assert len(result) == 13


def test_hand_cap_uses_shared_config_constant():
    """问题4/5：上限常量集中定义，recognizer 与 tracker/main 共用同一来源。"""
    from config import MAX_HAND_TILES
    import recognizer
    import tracker

    assert MAX_HAND_TILES == 14
    assert recognizer.MAX_HAND_TILES == MAX_HAND_TILES
    assert tracker.MAX_HAND_TILES == MAX_HAND_TILES


def test_template_dir_is_repo_relative_and_env_overridable(tmp_path, monkeypatch):
    """问题6(config 可移植性)：TEMPLATE_DIR 按仓库根推导，且支持环境变量覆盖。"""
    import importlib

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert os.path.abspath(config.TEMPLATE_DIR) == os.path.join(repo_root, "templates")

    override = str(tmp_path / "tpl")
    original = os.environ.get("TEMPLATE_DIR")
    try:
        monkeypatch.setenv("TEMPLATE_DIR", override)
        importlib.reload(config)
        assert config.TEMPLATE_DIR == override
    finally:
        monkeypatch.undo()
        importlib.reload(config)
    assert os.path.abspath(config.TEMPLATE_DIR) == os.path.join(repo_root, "templates")
    assert original is None or config.TEMPLATE_DIR != override


def test_auto_backend_falls_back_only_for_large_competition_crop(monkeypatch):
    """auto 模式不影响旧模板，但比赛大尺寸手牌区可走远端兜底。"""
    import remote_recognizer

    class FakeRemote:
        def __init__(self, **kwargs):
            self.stats = {"calls": 0}
            self.last_error = None

        def recognize(self, image):
            self.stats["calls"] += 1
            return ["1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m",
                    "1s", "2s", "3s", "4s"]

    monkeypatch.setenv("MVP_RECOGNIZER_BACKEND", "auto")
    monkeypatch.setattr(remote_recognizer, "AgnesHandRecognizer", FakeRemote)
    rec = TileRecognizer()
    monkeypatch.setattr(rec, "recognize_region", lambda image: [])
    result = rec.recognize_hand_tiles(np.zeros((190, 1600, 3), dtype=np.uint8))
    assert len(result) == 13
    assert rec.remote.stats["calls"] == 1
