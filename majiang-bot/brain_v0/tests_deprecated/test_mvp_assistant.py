# -*- coding: utf-8 -*-
"""MVP辅助器核心功能测试：手牌管理、校验、确认流程、阿法shadow。"""

import json
import os
import sys
import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


class TestHandValidation:
    """手牌合法性校验测试。"""

    def test_valid_14_tile_hand(self):
        from tracker import TileTracker
        t = TileTracker()
        hand = ["1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m",
                "1p", "2p", "3p", "4p", "5p"]
        result = t.set_pending_hand(hand)
        assert result["ok"] is True
        assert result["error"] is None
        assert len(t.pending_hand) == 14

    def test_valid_13_tile_hand(self):
        from tracker import TileTracker
        t = TileTracker()
        hand = ["1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m",
                "1p", "2p", "3p", "4p"]
        result = t.set_pending_hand(hand)
        assert result["ok"] is True
        assert len(t.pending_hand) == 13

    def test_too_many_tiles(self):
        from tracker import TileTracker
        t = TileTracker()
        hand = ["1m"] * 15
        result = t.set_pending_hand(hand)
        assert result["ok"] is False
        assert "手牌数" in result["error"]

    def test_too_few_tiles(self):
        from tracker import TileTracker
        t = TileTracker()
        hand = []
        result = t.set_pending_hand(hand)
        assert result["ok"] is False
        assert "手牌数" in result["error"]

    def test_invalid_tile_id(self):
        from tracker import TileTracker
        t = TileTracker()
        hand = ["1m", "2m", "99x"]
        result = t.set_pending_hand(hand)
        assert result["ok"] is False
        assert "无效牌ID" in result["error"]

    def test_single_tile_over_limit(self):
        from tracker import TileTracker
        t = TileTracker()
        hand = ["1m", "1m", "1m", "1m", "1m", "2m"]
        result = t.set_pending_hand(hand)
        assert result["ok"] is False
        assert "单牌超过4张" in result["error"]

    def test_exactly_4_same_tile_ok(self):
        from tracker import TileTracker
        t = TileTracker()
        hand = ["1m", "1m", "1m", "1m", "2m", "3m"]
        result = t.set_pending_hand(hand)
        assert result["ok"] is True
        assert len(t.pending_hand) == 6

    def test_all_28_tile_types_accepted(self):
        from tracker import TileTracker
        from config import TILE_TYPES
        t = TileTracker()
        # 每种牌各一张，共28张 → 超出14张上限，应失败
        hand = [tid for tid, _ in TILE_TYPES]
        result = t.set_pending_hand(hand)
        assert result["ok"] is False
        assert "手牌数" in result["error"]

    def test_single_tile_of_each_type_14_total(self):
        from tracker import TileTracker
        from config import TILE_TYPES
        t = TileTracker()
        # 前14种牌各一张
        hand = [tid for tid, _ in TILE_TYPES[:14]]
        result = t.set_pending_hand(hand)
        assert result["ok"] is True
        assert len(t.pending_hand) == 14


class TestPendingConfirmFlow:
    """待确认→确认流程测试。"""

    def test_confirm_sets_current_hand(self):
        from tracker import TileTracker
        t = TileTracker()
        hand = ["1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m",
                "1p", "2p", "3p", "4p", "5p"]
        t.set_pending_hand(hand)
        assert t.pending_confirmed is False

        result = t.confirm_pending_hand()
        assert result["ok"] is True
        assert t.current_hand == hand
        assert t.pending_hand == []
        assert t.pending_confirmed is True

    def test_confirm_empty_pending_fails(self):
        from tracker import TileTracker
        t = TileTracker()
        result = t.confirm_pending_hand()
        assert result["ok"] is False
        assert "无可确认手牌" in result["error"]

    def test_confirm_invalid_hand_fails(self):
        from tracker import TileTracker
        t = TileTracker()
        hand = ["1m"] * 15  # 超限
        t.set_pending_hand(hand)
        # set_pending_hand 已失败，pending_hand应为空
        assert t.pending_hand == []
        result = t.confirm_pending_hand()
        assert result["ok"] is False

    def test_clear_pending(self):
        from tracker import TileTracker
        t = TileTracker()
        hand = ["1m", "2m"]
        t.set_pending_hand(hand)
        t.clear_pending_hand()
        assert t.pending_hand == []
        assert t.pending_confirmed is False

    def test_get_pending_hand(self):
        from tracker import TileTracker
        t = TileTracker()
        hand = ["1m", "2m", "3m"]
        t.set_pending_hand(hand)
        snap = t.get_pending_hand()
        assert snap["hand"] == hand
        assert snap["confirmed"] is False


class TestTrackerSnapshot:
    """快照包含pending状态测试。"""

    def test_snapshot_includes_pending(self):
        from tracker import TileTracker
        t = TileTracker()
        hand = ["1m", "2m"]
        t.set_pending_hand(hand)
        snap = t.get_snapshot()
        assert "pending_hand" in snap
        assert "pending_confirmed" in snap
        assert snap["pending_hand"] == hand
        assert snap["pending_confirmed"] is False


class TestDashboardAPI:
    """Dashboard API端点测试。"""

    @pytest.fixture
    def client(self, tmp_path):
        import dashboard as _dash
        db = str(tmp_path / "mahjong.db")
        os.environ["DASHBOARD_DB_PATH"] = db
        _dash.app.config.update(TESTING=True)
        with _dash.app.test_client() as c:
            yield c

    def test_hand_pending_returns_structure(self, client):
        from tracker import TileTracker
        import dashboard as _dash
        t = TileTracker()
        _dash.tracker_ref = t
        try:
            r = client.get("/api/hand/pending")
            assert r.status_code == 200
            data = json.loads(r.get_data(as_text=True))
            assert "hand" in data
            assert "confirmed" in data
            assert "current_hand" in data
        finally:
            _dash.tracker_ref = None

    def test_hand_set_pending_valid(self, client):
        from tracker import TileTracker
        import dashboard as _dash
        t = TileTracker()
        _dash.tracker_ref = t
        try:
            r = client.post("/api/hand/set_pending",
                           data=json.dumps({"hand": ["1m", "2m", "3m"]}),
                           content_type="application/json")
            assert r.status_code == 200
            data = json.loads(r.get_data(as_text=True))
            assert data["ok"] is True
        finally:
            _dash.tracker_ref = None

    def test_hand_set_pending_invalid_tile(self, client):
        from tracker import TileTracker
        import dashboard as _dash
        t = TileTracker()
        _dash.tracker_ref = t
        try:
            r = client.post("/api/hand/set_pending",
                           data=json.dumps({"hand": ["99x"]}),
                           content_type="application/json")
            assert r.status_code == 400
            data = json.loads(r.get_data(as_text=True))
            assert "无效牌ID" in data["error"]
        finally:
            _dash.tracker_ref = None

    def test_hand_confirm(self, client):
        from tracker import TileTracker
        import dashboard as _dash
        t = TileTracker()
        _dash.tracker_ref = t
        try:
            # 使用合法手牌：9万 + 5筒 = 14张
            hand = ["1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m",
                    "1p", "2p", "3p", "4p", "5p"]
            client.post("/api/hand/set_pending",
                       data=json.dumps({"hand": hand}),
                       content_type="application/json")
            r = client.post("/api/hand/confirm")
            assert r.status_code == 200
            data = json.loads(r.get_data(as_text=True))
            assert data["ok"] is True
            assert data["pending_cleared"] is True
        finally:
            _dash.tracker_ref = None

    def test_hand_clear_pending(self, client):
        from tracker import TileTracker
        import dashboard as _dash
        t = TileTracker()
        _dash.tracker_ref = t
        try:
            client.post("/api/hand/set_pending",
                       data=json.dumps({"hand": ["1m", "2m"]}),
                       content_type="application/json")
            r = client.post("/api/hand/clear_pending")
            assert r.status_code == 200
            data = json.loads(r.get_data(as_text=True))
            assert data["ok"] is True
        finally:
            _dash.tracker_ref = None

    def test_hand_manual_update_add(self, client):
        from tracker import TileTracker
        import dashboard as _dash
        t = TileTracker()
        t.current_hand = ["1m", "2m"]
        _dash.tracker_ref = t
        try:
            r = client.post("/api/hand/manual_update",
                           data=json.dumps({"action": "add", "tile": "3m"}),
                           content_type="application/json")
            assert r.status_code == 200
            data = json.loads(r.get_data(as_text=True))
            assert data["ok"] is True
            assert data["count"] == 3
        finally:
            _dash.tracker_ref = None

    def test_hand_manual_update_remove(self, client):
        from tracker import TileTracker
        import dashboard as _dash
        t = TileTracker()
        t.current_hand = ["1m", "2m", "3m"]
        _dash.tracker_ref = t
        try:
            r = client.post("/api/hand/manual_update",
                           data=json.dumps({"action": "remove", "tile": "2m"}),
                           content_type="application/json")
            assert r.status_code == 200
            data = json.loads(r.get_data(as_text=True))
            assert data["ok"] is True
            assert data["count"] == 2
        finally:
            _dash.tracker_ref = None

    def test_hand_manual_update_replace(self, client):
        from tracker import TileTracker
        import dashboard as _dash
        t = TileTracker()
        t.current_hand = ["1m", "2m", "3m"]
        _dash.tracker_ref = t
        try:
            r = client.post("/api/hand/manual_update",
                           data=json.dumps({"action": "replace", "tile": "4m", "index": 1}),
                           content_type="application/json")
            assert r.status_code == 200
            data = json.loads(r.get_data(as_text=True))
            assert data["ok"] is True
            assert data["hand"] == ["1m", "4m", "3m"]
        finally:
            _dash.tracker_ref = None

    def test_hand_manual_update_set(self, client):
        from tracker import TileTracker
        import dashboard as _dash
        t = TileTracker()
        _dash.tracker_ref = t
        try:
            r = client.post("/api/hand/manual_update",
                           data=json.dumps({"action": "set", "hand": ["1m", "2m", "3m"]}),
                           content_type="application/json")
            assert r.status_code == 200
            data = json.loads(r.get_data(as_text=True))
            assert data["ok"] is True
            assert data["hand"] == ["1m", "2m", "3m"]
        finally:
            _dash.tracker_ref = None

    def test_hand_advice_endpoint(self, client):
        from tracker import TileTracker
        import dashboard as _dash
        t = TileTracker()
        t.current_hand = ["1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m",
                         "1p", "2p", "3p", "4p", "5p"]
        _dash.tracker_ref = t
        try:
            r = client.get("/api/advice")
            assert r.status_code == 200
            data = json.loads(r.get_data(as_text=True))
            assert "hand" in data
            assert "advice" in data
            assert "pending" in data
        finally:
            _dash.tracker_ref = None

    def test_caishen_endpoint(self, client):
        r = client.get("/api/caishen")
        assert r.status_code == 200
        data = json.loads(r.get_data(as_text=True))
        assert data["caishen"] == "red"

    def test_caishen_set_rejects_non_red(self, client):
        r = client.post("/api/caishen",
                       data=json.dumps({"caishen": "green"}),
                       content_type="application/json")
        assert r.status_code == 400
        data = json.loads(r.get_data(as_text=True))
        assert "仅支持红中" in data["error"]


class TestAdvisorShadowDisplay:
    """阿法shadow显示测试。"""

    def test_advisor_shadow_mode_structure(self):
        from engine.advisor import Advisor
        a = Advisor(afa_mode="shadow")
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25, 47]
        res = a.advise(hand, {}, turn_no=3)
        assert res["engine"] == "local"
        assert res["options"]
        assert "warnings" in res

    def test_advisor_with_disabled_afa(self):
        from engine.advisor import Advisor
        class FakeAfa:
            enabled = False
            last_error = "disabled"
            def recommend(self, *args, **kwargs):
                return None

        a = Advisor(afa_client=FakeAfa(), afa_mode="shadow")
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25, 47]
        res = a.advise(hand, {}, turn_no=3)
        assert any("afa_unavailable" in w for w in res["warnings"])

    def test_advisor_local_always_works(self):
        """即使阿法完全不可用，本地建议仍应正常返回。"""
        from engine.advisor import Advisor

        class BrokenAfa:
            enabled = True
            last_error = "connection refused"
            def recommend(self, *args, **kwargs):
                return None  # 返回None而非抛异常，模拟网络超时

        a = Advisor(afa_client=BrokenAfa(), afa_mode="shadow")
        hand = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 25, 47]
        res = a.advise(hand, {}, turn_no=3)
        assert res["engine"] == "local"
        assert len(res["options"]) > 0
        assert res["options"][0]["source"] == "local"


class TestConfigRegions:
    """截图区域配置测试。"""

    def test_legacy_regions_exist(self):
        from src.config import CAPTURE_REGIONS
        assert "hand_tiles" in CAPTURE_REGIONS
        assert CAPTURE_REGIONS["hand_tiles"]["rect"] == (200, 540, 1080, 620)

    def test_1600x900_regions_exist(self):
        from src.config import CAPTURE_REGIONS_1600x900
        assert "hand_tiles" in CAPTURE_REGIONS_1600x900
        rect = CAPTURE_REGIONS_1600x900["hand_tiles"]["rect"]
        # 验证坐标在1600x900范围内
        assert rect[0] >= 0 and rect[2] <= 1600
        assert rect[1] >= 0 and rect[3] <= 900

    def test_capture_regions_mode_default(self):
        from src.config import CAPTURE_REGIONS_MODE
        assert CAPTURE_REGIONS_MODE in ("legacy", "1600x900")

    def test_reference_dimensions(self):
        from src.config import REFERENCE_WIDTH, REFERENCE_HEIGHT
        assert REFERENCE_WIDTH == 1280
        assert REFERENCE_HEIGHT == 626


class TestMVPSmoke:
    """端到端冒烟测试：启动入口可用。"""

    def test_dashboard_importable(self):
        import dashboard
        assert hasattr(dashboard, 'app')
        assert hasattr(dashboard, 'tracker_ref')
        assert hasattr(dashboard, 'set_tracker')
        assert hasattr(dashboard, 'set_afa_client')
        assert hasattr(dashboard, 'get_afa_client')

    def test_tracker_has_pending_methods(self):
        from tracker import TileTracker
        t = TileTracker()
        assert hasattr(t, 'set_pending_hand')
        assert hasattr(t, 'confirm_pending_hand')
        assert hasattr(t, 'get_pending_hand')
        assert hasattr(t, 'clear_pending_hand')

    def test_advisor_accepts_shadow_mode(self):
        from engine.advisor import Advisor
        a = Advisor(afa_mode="shadow")
        assert a.afa_mode == "shadow"

    def test_advisor_rejects_invalid_mode(self):
        from engine.advisor import Advisor
        with pytest.raises(ValueError, match="afa_mode 必须是 shadow 或 blend"):
            Advisor(afa_mode="invalid")
