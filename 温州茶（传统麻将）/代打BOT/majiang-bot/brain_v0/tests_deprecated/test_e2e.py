# -*- coding: utf-8 -*-
"""
端到端测试 - 验证完整流程
"""
import pytest
import sqlite3
import json
import hashlib
from pathlib import Path
from datetime import datetime, timedelta
import os

# 添加项目根目录到path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.import_e2e import init_db, import_gamelog_e2e
from engine.storage import init_db as init_storage_db


class TestE2EDatabase:
    """端到端数据库测试"""
    
    @pytest.fixture
    def e2e_db(self, tmp_path):
        """创建临时端到端数据库"""
        db_path = tmp_path / "test_e2e.db"
        init_db.__wrapped__(str(db_path)) if hasattr(init_db, '__wrapped__') else None
        # 直接使用函数
        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_sha256 TEXT NOT NULL,
                source_clip TEXT NOT NULL,
                round_idx INTEGER NOT NULL,
                take TEXT NOT NULL DEFAULT 'a',
                started_at TEXT,
                ended_at TEXT,
                result TEXT,
                is_win INTEGER,
                note TEXT,
                gold_label TEXT,
                review_status TEXT DEFAULT 'pending',
                evidence_path TEXT,
                classifier_version TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(source_sha256, round_idx, take)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS plays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                tile_id INTEGER NOT NULL,
                tile_name TEXT,
                action_type TEXT NOT NULL,
                player TEXT NOT NULL,
                turn_no INTEGER,
                timestamp_seconds REAL,
                is_caishen INTEGER DEFAULT 0,
                FOREIGN KEY(round_id) REFERENCES rounds(id)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS ocr_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id INTEGER NOT NULL,
                frame_timestamp REAL NOT NULL,
                frame_path TEXT,
                raw_output TEXT,
                parsed_success INTEGER DEFAULT 0,
                recognizer TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(round_id) REFERENCES rounds(id)
            )
        ''')
        conn.commit()
        conn.close()
        return str(db_path)
    
    def test_import_idempotency(self, e2e_db):
        """测试导入幂等性"""
        # 创建测试JSONL
        test_data = [
            {
                "source_clip": "test.mp4",
                "round_idx": 1,
                "take": "a",
                "timestamp_seconds": 10.0,
                "turn": 5,
                "actual_discard": 1,
                "result": {"winner": 0, "win_type": "zimo"}
            }
        ]
        
        jsonl_path = Path(e2e_db).parent / "test.jsonl"
        with open(jsonl_path, 'w') as f:
            for p in test_data:
                f.write(json.dumps(p) + '\n')
        
        sha256 = hashlib.sha256(b'test').hexdigest()
        
        # 第一次导入
        rounds1, plays1 = import_gamelog_e2e(str(jsonl_path), sha256, source_clip="test.mp4", db_path=e2e_db)
        assert rounds1 == 1
        assert plays1 >= 1
        
        # 第二次导入（应跳过）
        rounds2, plays2 = import_gamelog_e2e(str(jsonl_path), sha256, source_clip="test.mp4", db_path=e2e_db)
        assert rounds2 == 0  # 不应创建新round
        assert plays2 == 0   # 也不应新增plays
        
        # 验证数据库数量不变
        conn = sqlite3.connect(e2e_db)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM rounds')
        assert c.fetchone()[0] == 1
        conn.close()
    
    def test_unique_constraint(self, e2e_db):
        """测试唯一性约束"""
        test_data = [
            {
                "source_clip": "test.mp4",
                "round_idx": 1,
                "take": "a",
                "timestamp_seconds": 10.0,
                "turn": 5,
                "actual_discard": 1
            }
        ]
        
        jsonl_path = Path(e2e_db).parent / "test2.jsonl"
        with open(jsonl_path, 'w') as f:
            for p in test_data:
                f.write(json.dumps(p) + '\n')
        
        sha256 = hashlib.sha256(b'test2').hexdigest()
        
        # 导入两次
        import_gamelog_e2e(str(jsonl_path), sha256, source_clip="test.mp4", db_path=e2e_db)
        import_gamelog_e2e(str(jsonl_path), sha256, source_clip="test.mp4", db_path=e2e_db)
        
        # 验证只有一条round
        conn = sqlite3.connect(e2e_db)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM rounds WHERE source_sha256 = ?', (sha256,))
        assert c.fetchone()[0] == 1
        conn.close()
    
    def test_plays_not_snapshot(self, e2e_db):
        """测试plays只存动作不存快照"""
        test_data = [
            {
                "source_clip": "test.mp4",
                "round_idx": 1,
                "take": "a",
                "timestamp_seconds": 10.0,
                "turn": 5,
                "actual_discard": 1,
                "hand": [1, 2, 3, 4, 5]  # 手牌快照
            }
        ]
        
        jsonl_path = Path(e2e_db).parent / "test3.jsonl"
        with open(jsonl_path, 'w') as f:
            for p in test_data:
                f.write(json.dumps(p) + '\n')
        
        sha256 = hashlib.sha256(b'test3').hexdigest()
        rounds, plays = import_gamelog_e2e(str(jsonl_path), sha256, source_clip="test.mp4", db_path=e2e_db)
        
        # plays应该只包含discard动作，不包含手牌快照
        conn = sqlite3.connect(e2e_db)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM plays WHERE action_type = ?', ('discard',))
        discard_count = c.fetchone()[0]
        assert discard_count >= 1
        
        # 不应该有hand类型的plays
        c.execute('SELECT COUNT(*) FROM plays WHERE action_type = ?', ('hand',))
        hand_count = c.fetchone()[0]
        assert hand_count == 0
        conn.close()

    def test_winner_seat_is_not_written_as_tile(self, e2e_db):
        # winner=0 表示座位，不是 tile_id；没有明确 win_tile 时不得写入伪造 win 动作。
        test_data = [{
            "source_clip": "winner.mp4", "round_idx": 1, "take": "a",
            "timestamp_seconds": 1.0, "turn": 1, "result": {"winner": 0}
        }]
        jsonl_path = Path(e2e_db).parent / "winner.jsonl"
        jsonl_path.write_text(''.join(json.dumps(x) + "\n" for x in test_data), encoding='utf-8')
        source = hashlib.sha256(b'winner').hexdigest()
        rounds, plays = import_gamelog_e2e(str(jsonl_path), source, db_path=e2e_db)
        assert rounds == 1 and plays == 0

    def test_play_sequence_restarts_per_round(self, e2e_db):
        test_data = [
            {"source_clip": "r1.mp4", "round_idx": 1, "take": "a", "actual_discard": 1},
            {"source_clip": "r1.mp4", "round_idx": 1, "take": "a", "actual_discard": 2},
            {"source_clip": "r2.mp4", "round_idx": 2, "take": "a", "actual_discard": 3},
        ]
        jsonl_path = Path(e2e_db).parent / "seq.jsonl"
        jsonl_path.write_text(''.join(json.dumps(x) + "\n" for x in test_data), encoding='utf-8')
        source = hashlib.sha256(b'seq').hexdigest()
        rounds, plays = import_gamelog_e2e(str(jsonl_path), source, db_path=e2e_db)
        assert rounds == 2 and plays == 3
        conn = sqlite3.connect(e2e_db)
        rows = conn.execute("SELECT round_id, seq FROM plays ORDER BY round_id, seq").fetchall()
        conn.close()
        assert [seq for _, seq in rows] == [1, 2, 1]

    def test_play_tile_name_uses_canonical_display_name(self, e2e_db):
        test_data = [{
            "source_clip": "names.mp4", "round_idx": 1, "take": "a",
            "actual_discard": 41,
            "result": {"estimated": False, "winner": 0, "win_tile": 55},
        }]
        jsonl_path = Path(e2e_db).parent / "names.jsonl"
        jsonl_path.write_text(''.join(json.dumps(x) + "\n" for x in test_data), encoding='utf-8')
        source = hashlib.sha256(b'names').hexdigest()
        rounds, plays = import_gamelog_e2e(str(jsonl_path), source, db_path=e2e_db)
        assert rounds == 1 and plays == 2
        conn = sqlite3.connect(e2e_db)
        rows = conn.execute("SELECT tile_id, tile_name, action_type FROM plays ORDER BY seq").fetchall()
        conn.close()
        assert rows == [(41, "1条", "discard"), (55, "红中(财神)", "win")]

    def test_incremental_import_resumes_existing_round_without_duplicates(self, e2e_db):
        """分段分析再次导入时补齐新动作，重跑同一段仍保持幂等。"""
        source = hashlib.sha256(b"resume").hexdigest()
        first = Path(e2e_db).parent / "resume_first.jsonl"
        first.write_text(json.dumps({
            "source_clip": "same.mp4", "round_idx": 1, "take": "a",
            "timestamp_seconds": 1.0, "turn": 1, "actual_discard": 1,
        }) + "\n", encoding="utf-8")
        assert import_gamelog_e2e(str(first), source, db_path=e2e_db) == (1, 1)

        second = Path(e2e_db).parent / "resume_second.jsonl"
        second.write_text(json.dumps({
            "source_clip": "same.mp4", "round_idx": 1, "take": "a",
            "timestamp_seconds": 2.0, "turn": 2, "actual_discard": 2,
        }) + "\n", encoding="utf-8")
        assert import_gamelog_e2e(str(second), source, db_path=e2e_db) == (0, 1)
        assert import_gamelog_e2e(str(second), source, db_path=e2e_db) == (0, 0)

        conn = sqlite3.connect(e2e_db)
        rows = conn.execute(
            "SELECT tile_id, seq FROM plays ORDER BY seq"
        ).fetchall()
        conn.close()
        assert rows == [(1, 1), (2, 2)]

    def test_same_clip_can_contain_multiple_rounds(self, e2e_db):
        """单个 JSONL 中的多局必须按 round_idx 分开，而不是合并。"""
        source = hashlib.sha256(b"multi-round").hexdigest()
        path = Path(e2e_db).parent / "multi_round.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in (
            {"source_clip": "batch.mp4", "round_idx": 1, "take": "a",
             "timestamp_seconds": 1, "turn": 1, "actual_discard": 1},
            {"source_clip": "batch.mp4", "round_idx": 2, "take": "a",
             "timestamp_seconds": 2, "turn": 1, "actual_discard": 2},
        )), encoding="utf-8")
        assert import_gamelog_e2e(str(path), source, db_path=e2e_db) == (2, 2)
        conn = sqlite3.connect(e2e_db)
        rounds = conn.execute(
            "SELECT round_idx FROM rounds ORDER BY round_idx"
        ).fetchall()
        conn.close()
        assert rounds == [(1,), (2,)]

    def test_raw_ocr_is_persisted_and_idempotent(self, e2e_db):
        source = hashlib.sha256(b"ocr").hexdigest()
        path = Path(e2e_db).parent / "ocr.jsonl"
        path.write_text(json.dumps({
            "source_clip": "ocr.mp4", "round_idx": 1, "take": "a",
            "timestamp_seconds": 3.14159, "turn": 2,
            "hand": [1, 2, 3], "raw_output": {
                "hand": ["一万", "二万"], "confidence": 0.91,
            }, "recognizer": "test-ocr",
        }) + "\n", encoding="utf-8")
        assert import_gamelog_e2e(str(path), source, db_path=e2e_db) == (1, 0)
        assert import_gamelog_e2e(str(path), source, db_path=e2e_db) == (0, 0)
        conn = sqlite3.connect(e2e_db)
        rows = conn.execute(
            "SELECT frame_timestamp, raw_output, parsed_success, recognizer "
            "FROM ocr_results"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == 3.142
        assert '"confidence": 0.91' in rows[0][1]
        assert rows[0][2:] == (1, "test-ocr")


class TestHunyuanOCRAdapter:
    """HunyuanOCR适配器测试"""
    
    def test_adapt_output_format(self):
        """测试输出格式适配"""
        from tools.hunyuan_ocr_adapter import HunyuanOCRAdapter
        
        adapter = HunyuanOCRAdapter()
        
        # 模拟HunyuanOCR输出
        ocr_output = [
            {"box": [10, 10, 100, 50], "text": "房间号：170075"},
            {"box": [10, 60, 100, 100], "text": "局数：3/16"},
            {"box": [10, 110, 50, 150], "text": "四萬"},
            {"box": [60, 110, 100, 150], "text": "三萬"},
        ]
        
        result = adapter.adapt_output(ocr_output)
        
        assert result is not None
        assert result["room"] == "170075"
        assert result["round_label"] == "3/16"
        assert 4 in result["hand"]  # 四万
        assert 3 in result["hand"]  # 三万
    
    def test_invalid_output_returns_none(self):
        """测试无效输入返回None"""
        from tools.hunyuan_ocr_adapter import HunyuanOCRAdapter
        
        adapter = HunyuanOCRAdapter()
        result = adapter.adapt_output(None)
        assert result is None
        
        result = adapter.adapt_output([])
        assert result is None

    def test_structured_output_preserves_hand_duplicates_and_rivers(self):
        from tools.hunyuan_ocr_adapter import HunyuanOCRAdapter

        adapter = HunyuanOCRAdapter()
        result = adapter.adapt_output({
            "room": "r1",
            "hand": ["一万", "一万", "红中", "5筒"],
            "river_up": ["二條", "二條"],
            "river_opposite": [], "river_down": [], "river_focus": [],
            "last_discard": "红中",
        })
        assert result["hand"] == [1, 1, 55, 25]
        assert result["river_up"] == [42, 42]
        assert result["last_discard"] == 55


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
