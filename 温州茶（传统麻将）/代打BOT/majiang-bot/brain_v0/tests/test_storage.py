# -*- coding: utf-8 -*-
"""长期数据层(storage)回归测试：SQLite 落库 / 你的长期习惯 / 本场对手倾向 / 异常兜底。

所有测试使用临时库路径，绝不写入项目 data/mahjong.db。
"""

import os
import tempfile

import pytest

from engine import storage


@pytest.fixture
def db_path():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "test_mahjong.db")
    yield p
    try:
        os.remove(p)
    except OSError:
        pass


def _sample_plays():
    return [
        {"tile_id": 1, "tile_name": "1万", "player": "bottom", "timestamp": 1.0, "round": 1},
        {"tile_id": 1, "tile_name": "1万", "player": "bottom", "timestamp": 2.0, "round": 1},
        {"tile_id": 21, "tile_name": "1筒", "player": "bottom", "timestamp": 3.0, "round": 1},
        {"tile_id": 41, "tile_name": "1条", "player": "top", "timestamp": 4.0, "round": 1},
        {"tile_id": 41, "tile_name": "1条", "player": "top", "timestamp": 5.0, "round": 1},
        {"tile_id": 55, "tile_name": "红中", "player": "left", "timestamp": 6.0, "round": 1},
    ]


class TestStorageBasics:
    def test_init_db_creates_tables(self, db_path):
        conn = storage.init_db(db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {r[0] for r in cur.fetchall()}
        conn.close()
        assert {"rounds", "plays"} <= names

    def test_save_round_and_plays(self, db_path):
        rid = storage.save_round({"rounds_played": 1}, _sample_plays(), db_path=db_path)
        assert isinstance(rid, int) and rid > 0
        rounds = storage.get_recent_rounds(10, db_path=db_path)
        assert len(rounds) == 1
        conn = storage.init_db(db_path)
        n = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        conn.close()
        # 6 条合法牌全部落库
        assert n == 6


class TestLongtermStats:
    def test_self_profile_aggregates(self, db_path):
        storage.save_round({"rounds_played": 1}, _sample_plays(), db_path=db_path)
        stats = storage.get_longterm_stats(db_path=db_path)
        assert stats["total_rounds"] == 1
        assert stats["total_plays"] == 6
        self_p = stats["self"]
        # bottom 出了 1万,1万,21筒 → 3 张
        assert self_p["total_discards"] == 3
        assert self_p["suit_counts"]["man"] == 2
        assert self_p["suit_counts"]["pin"] == 1
        # 安全/激进比例在 [0,1]
        assert 0.0 <= self_p["safety_pref"] <= 1.0
        assert 0.0 <= self_p["aggression"] <= 1.0


class TestOpponentProfiles:
    def test_session_opponent_profiles(self):
        profs = storage.session_opponent_profiles(_sample_plays())
        by_seat = {p["player"]: p for p in profs}
        # top 出了 41,41 → 2 张条
        assert by_seat["top"]["total_discards"] == 2
        assert by_seat["top"]["suit_counts"]["sou"] == 2
        # bottom 3 张（与长期统计口径一致）
        assert by_seat["bottom"]["total_discards"] == 3
        assert 0.0 <= by_seat["top"]["aggression"] <= 1.0


class TestGraceful:
    def test_empty_db_returns_empty(self, db_path):
        stats = storage.get_longterm_stats(db_path=db_path)
        assert stats["total_rounds"] == 0
        assert stats["total_plays"] == 0
        # 空库时 self 为「零值画像」而非 None/异常
        assert stats["self"]["total_discards"] == 0

    def test_invalid_tile_id_skipped(self, db_path):
        plays = [
            {"tile_id": 999, "player": "top", "timestamp": 1.0},  # 非法牌
            {"tile_id": 1, "player": "top", "timestamp": 2.0},
            {"tile_id": 7, "player": "zzz", "timestamp": 3.0},  # 非法座位
        ]
        rid = storage.save_round({}, plays, db_path=db_path)
        assert rid is not None
        conn = storage.init_db(db_path)
        n = conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        conn.close()
        # 只有 1万 合法落库
        assert n == 1
