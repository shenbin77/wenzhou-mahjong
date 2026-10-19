# -*- coding: utf-8 -*-
"""replay_pipeline/backfill.py 测试：真实样本房间回填 + 双口径胜率。"""
import os
import sys
import tempfile
import shutil

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from replay_pipeline import backfill  # noqa: E402
from engine import play_style  # noqa: E402

SAMPLE_ROOM = os.path.join(_ROOT, "回放分类", "房间170075_20260821")


def _tmp_db():
    d = tempfile.mkdtemp()
    return os.path.join(d, "mahjong.db")


def test_outcome_mapping():
    oc = backfill.outcome_for("自摸")
    assert oc["win_type"] == "zimo" and oc["is_win"] == 1
    oc = backfill.outcome_for("点炮", "default")
    assert oc["win_type"] == "lose" and oc["is_win"] == 0
    oc = backfill.outcome_for("点炮", "point")
    assert oc["win_type"] == "point" and oc["is_win"] == 1


def test_backfill_room_on_real_sample():
    db = _tmp_db()
    r = backfill.backfill_room(SAMPLE_ROOM, db_path=db)
    assert r["unique_rounds"] == 14
    assert r["inserted"] == 14
    assert r["dedup_outcome"].get("自摸", 0) == 5
    assert r["dedup_outcome"].get("点炮", 0) == 9
    wr = play_style.win_rate(db)
    assert wr["total"] == 14
    assert wr["win_count"] == 5


def test_backfill_all_two_interpretations():
    # 隔离测试：只处理 SAMPLE_ROOM，避免新目录干扰
    tmp_base = tempfile.mkdtemp()
    try:
        # 复制 SAMPLE_ROOM 到临时目录
        shutil.copytree(SAMPLE_ROOM, os.path.join(tmp_base, "房间170075_20260821"))
        db = _tmp_db()
        res = backfill.backfill_all(base_dir=tmp_base, db_path=db, interpret="default")
        # 默认口径：点炮=输 -> 胜率 5/14
        assert res["default_win_rate"]["win_count"] == 5
        assert res["default_win_rate"]["total"] == 14
        assert res["default_win_rate"]["win_rate"] == pytest.approx(round(5 / 14, 3), abs=1e-6)
        assert res["db_win_rate"]["win_count"] == 5
        # 另一口径：点炮=接炮胡 -> 全部为赢 -> 100%
        assert res["alternate_win_rate"]["win_rate"] == 1.0
    finally:
        shutil.rmtree(tmp_base, ignore_errors=True)
