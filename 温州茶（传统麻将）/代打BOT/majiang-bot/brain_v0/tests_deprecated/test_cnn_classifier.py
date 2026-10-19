# -*- coding: utf-8 -*-
"""engine/vision/cnn_classifier.py 测试：合成数据训练链路 + 置信度兜底 + 双后端。"""
import os
import sys

import numpy as np
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from engine.vision.cnn_classifier import TileClassifier, TORCH_AVAILABLE  # noqa: E402


def test_synthetic_dataset_covers_28_classes():
    X, y = TileClassifier.make_synthetic_dataset(n_per_class=5)
    assert len(set(y)) == 28
    assert len(X) == 28 * 5


def test_untrained_predict_is_low_confidence():
    clf = TileClassifier(backend="cvmlp")
    img = np.random.default_rng(0).random((32, 32)).astype(np.float32)
    res = clf.predict(img)
    # 未训练 → 均匀分布 → 置信度 1/28 < 0.5 → 标为未知
    assert res["tile"] is None
    assert res["name"] == "未知(低置信度)"


def _acc(clf, X, y):
    ok = sum(1 for xi, yi in zip(X, y) if clf.predict(xi)["tile"] == int(yi))
    return ok / len(y)


@pytest.mark.parametrize("backend", ["cvmlp", "torch"] if TORCH_AVAILABLE else ["cvmlp"])
def test_backend_trains_and_predicts(backend):
    """合成图案按类位置可区分，任一后端都应学到明显优于随机(>0.4)。"""
    X, y = TileClassifier.make_synthetic_dataset(n_per_class=15, seed=1)
    clf = TileClassifier(backend=backend)
    clf.fit(X, y, epochs=60)
    acc = _acc(clf, X, y)
    assert acc > 0.4, f"backend={backend} acc={acc:.3f}"


def test_preprocess_handles_0_255_and_0_1():
    """归一化自适应：0-255 截图被 /255，[0,1] 合成数据不被再次压缩。"""
    clf = TileClassifier()
    gray255 = (np.random.default_rng(2).random((32, 32)) * 255).astype(np.uint8)
    flat255 = clf._preprocess(gray255)
    assert flat255.max() <= 1.0 + 1e-6
    gray01 = np.random.default_rng(2).random((32, 32)).astype(np.float32)
    flat01 = clf._preprocess(gray01)
    # [0,1] 数据不应被 /255 压扁（否则信号 <0.004，训练学不动）
    assert flat01.max() > 0.5
