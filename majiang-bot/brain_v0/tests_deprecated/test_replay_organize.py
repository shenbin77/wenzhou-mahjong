# -*- coding: utf-8 -*-
"""replay_pipeline 测试：合成夹具验证整理逻辑 + 真实样本只读检查。"""
import os
import sys
import tempfile
import shutil

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from replay_pipeline import organize, capture  # noqa: E402


def _mk_clip(d, name):
    with open(os.path.join(d, name), "wb") as f:
        f.write(b"\x00\x00")  # 假 mp4 内容


def test_organize_parses_and_detects_missing_dup():
    d = tempfile.mkdtemp()
    # 第1/3局 自摸，第2局 点炮，缺第?，第1局重复
    _mk_clip(d, "第01局_a_自摸_0000-0020.mp4")
    _mk_clip(d, "第02局_a_点炮_0051-0078.mp4")
    _mk_clip(d, "第03局_a_自摸_0079-0160.mp4")
    _mk_clip(d, "第01局_b_自摸_重复回放_0562-0632.mp4")  # 第1局的另一份剪辑

    rep = organize.organize_room(d)
    assert rep["total_clips"] == 4
    assert rep["unique_rounds"] == 3
    assert rep["duplicates"] == [1]
    assert rep["missing"] == []  # 1-3 连续，无缺
    # manifest 文件生成
    assert os.path.isfile(rep["manifest_csv"])
    assert os.path.isfile(rep["manifest_json"])

    # 缺局检测：删掉第2局再扫
    os.remove(os.path.join(d, "第02局_a_点炮_0051-0078.mp4"))
    rep2 = organize.organize_room(d)
    assert rep2["missing"] == [2]


def test_ingest_file_is_idempotent_and_names_correctly():
    src = tempfile.mkdtemp()
    base = tempfile.mkdtemp()
    sfile = os.path.join(src, "raw.mp4")
    with open(sfile, "wb") as f:
        f.write(b"x")
    dest = capture.ingest_file(sfile, base, "170075", "自摸", 5, 10, 20)
    assert os.path.basename(dest) == "第05局_a_自摸_0010-0020.mp4"
    # 幂等：再 ingest 同文件不报错且返回同路径
    dest2 = capture.ingest_file(sfile, base, "170075", "自摸", 5, 10, 20)
    assert dest2 == dest
    # 结局非法抛错
    with pytest.raises(ValueError):
        capture.ingest_file(sfile, base, "170075", "胡了", 6, 0, 0)


def test_real_sample_readonly_check():
    """对真实样本做只读整理，不改动任何文件。"""
    sample = os.path.join(_ROOT, "回放分类", "房间170075_20260821")
    if not os.path.isdir(sample):
        pytest.skip("真实样本不存在")
    rep = organize.organize_room(sample)
    # 样本已知：15 个剪辑、14 唯一局、缺 7/11、第13局重复
    assert rep["total_clips"] == 15
    assert rep["unique_rounds"] == 14
    assert set(rep["missing"]) == {7, 11}
    assert rep["duplicates"] == [13]
    summ = organize.summarize_outcomes(rep)
    # 该样本含自摸与点炮
    assert summ["自摸"] > 0 and summ["点炮"] > 0
