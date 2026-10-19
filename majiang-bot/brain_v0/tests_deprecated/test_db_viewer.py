# -*- coding: utf-8 -*-
"""tools/db_viewer.py 的冒烟测试：空库与有数据两种情形。"""
import json
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from engine import storage  # noqa: E402
from tools import db_viewer  # noqa: E402


def _tmp_db():
    d = tempfile.mkdtemp()
    return os.path.join(d, "mahjong.db")


def test_viewer_empty_db_returns_zero_profile():
    db = _tmp_db()
    txt = db_viewer.render_text(db, rounds_limit=5)
    assert "总场次" in txt
    assert "（无数据）" in txt or "弃牌 0" in txt
    # JSON 模式也不崩
    js = json.loads(json.dumps(db_viewer.main.__self__ if False else None)) if False else None
    # 直接调用 main 的 JSON 分支
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = db_viewer.main(["--db", db, "--json"])
    assert rc == 0
    obj = json.loads(buf.getvalue())
    assert obj["stats"]["total_rounds"] == 0


def test_viewer_shows_recorded_rounds():
    db = _tmp_db()
    storage.save_round(
        {"result": "win", "is_win": 1, "is_zimo": 1, "is_dealer": 0,
         "self_score": 6, "rounds_played": 3, "note": "t"},
        [{"tile_id": 1, "tile_name": "一万", "player": "bottom", "turn_no": 1,
          "timestamp": "t1"},
         {"tile_id": 21, "tile_name": "一筒", "player": "bottom", "turn_no": 2,
          "timestamp": "t2"}],
        db_path=db,
    )
    txt = db_viewer.render_text(db, rounds_limit=5)
    assert "总场次     : 1" in txt
    assert "一万" not in txt  # 概览不列具体牌，只列统计
    assert "弃牌 2" in txt
