# -*- coding: utf-8 -*-
"""合成对局生成器 + 派牌时序结构检验联动测试（建议#4 / P0-2 替代方案）。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import make_synthetic_rounds as msr
from tools.dealing_structure_check import analyze, load_records


def test_gen_rigged_shape():
    recs = msr.gen_rigged(120, seed=2)
    assert len(recs) == 120
    for r in recs:
        assert r["winner"] in (0, 1, 2, 3)
        assert set(r["deltas"].keys()) == {"bottom", "top", "left", "right"}


def test_gen_fair_runs():
    # 真实模拟器，少量即可验证管线通畅
    recs = msr.gen_fair(10, seed=1)
    assert len(recs) == 10
    for r in recs:
        assert r["winner"] in (0, 1, 2, 3, None)


def test_rigged_shows_nonstationary_sign():
    # rigged: 同边际0.25但强自相关 → analyze 应检出 lag1 自相关显著
    recs = msr.gen_rigged(200, seed=2)
    rep = analyze(recs, min_rounds=50)
    assert rep["verdict"] == "analyzed"
    assert abs(rep["autocorr_lag1"]) > 0.1
    assert any("自相关" in s for s in rep["non_stationary_signs"])


def test_pipeline_jsonl_roundtrip():
    # 写 JSONL → load_records → analyze 端到端
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        recs = msr.gen_rigged(150, seed=3)
        msr.write_jsonl(recs, path)
        loaded = load_records(path)
        assert len(loaded) == 150
        rep = analyze(loaded, min_rounds=50)
        assert rep["verdict"] == "analyzed"
    finally:
        os.remove(path)


def test_insufficient_data_when_small():
    rep = analyze(msr.gen_rigged(10, seed=1), min_rounds=100)
    assert rep["verdict"] == "insufficient_data"
