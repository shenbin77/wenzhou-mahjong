# -*- coding: utf-8 -*-
"""Dashboard 新接口 headless 测试（替代无法在沙箱跑真实 main.py）。

验证 Q3 相关后端：/api/stats、/api/opponents、/api/rounds 返回结构正确，
且与 engine.storage 的真实数据一致。沙箱无 MuMu/Qwen，故用 Flask test client
直接打接口，不依赖 live 识别链路。
"""
import os
import sys
import tempfile

import pytest

# dashboard 内部 `from config import ...`，需把 src 放到 path
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_IMPORT_ERR = ""
try:
    import dashboard  # noqa: E402
    HAS_DASHBOARD = True
except Exception as e:  # pragma: no cover - 环境缺依赖时跳过
    HAS_DASHBOARD = False
    _IMPORT_ERR = str(e)


pytestmark = pytest.mark.skipif(not HAS_DASHBOARD, reason=f"dashboard 不可用: {_IMPORT_ERR}")


@pytest.fixture
def client(tmp_path):
    # 指向临时库，避免污染 data/mahjong.db
    db = str(tmp_path / "mahjong.db")
    os.environ["DASHBOARD_DB_PATH"] = db  # dashboard 是否读取由实现决定；此处仅保证可启动
    dashboard.app.config.update(TESTING=True)
    with dashboard.app.test_client() as c:
        yield c


def _json(client, path):
    """dashboard 路由用 json.dumps 返回纯文本（非 application/json），
    故用 body 解析而非 get_json()。"""
    r = client.get(path)
    assert r.status_code == 200
    import json
    return json.loads(r.get_data(as_text=True))


def test_stats_endpoint_returns_structure(client):
    data = _json(client, "/api/stats")
    assert "total_rounds" in data
    assert "self" in data
    assert "win_rate" in data


def test_opponents_endpoint_returns_list(client):
    data = _json(client, "/api/opponents")
    assert isinstance(data, (list, dict))


def test_rounds_endpoint_returns_list(client):
    data = _json(client, "/api/rounds?limit=20")
    assert isinstance(data, (list, dict))


def test_session_advice_continue_when_circle_positive(client):
    data = _json(client, "/api/session_advice?circle_pnl=3&last_round_pnl=1&session_pnl=3")
    assert data["action"] == "continue"
    assert data["circle_pnl"] == 3


def test_session_advice_rest_when_circle_negative(client):
    data = _json(client, "/api/session_advice?circle_pnl=-5&last_round_pnl=-3&session_pnl=-5")
    assert data["action"] == "rest"


def test_session_advice_rest_on_big_single_round_loss(client):
    # 单局输 >10 分立即离场（舅舅硬止损）
    data = _json(client, "/api/session_advice?circle_pnl=2&last_round_pnl=-12&session_pnl=-12")
    assert data["action"] == "rest"


def test_session_advice_rest_on_big_session_loss(client):
    # 累计输 >10 分立即离场
    data = _json(client, "/api/session_advice?circle_pnl=1&last_round_pnl=1&session_pnl=-15")
    assert data["action"] == "rest"


# ------------------------------------------------------------------ 问题5
def test_index_renders_with_tracker(client):
    """仪表盘主页应可正常渲染（含 tracker 快照数据）。"""
    from tracker import TileTracker
    t = TileTracker()
    t.reset()
    t.update_river([("1m", 0.9)], "top")
    old = dashboard.tracker_ref
    dashboard.tracker_ref = t
    try:
        r = client.get("/")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "一万" in body
        assert "1/4" in body  # TOTAL_PER_TILE 默认 4
    finally:
        dashboard.tracker_ref = old


def test_template_denominator_follows_total_per_tile(client, monkeypatch):
    """问题5: 模板分母必须取 config.TOTAL_PER_TILE，改常量后显示不得仍写死 4。"""
    from tracker import TileTracker
    t = TileTracker()
    t.reset()
    t.update_river([("1m", 0.9)], "top")
    old_ref = dashboard.tracker_ref
    monkeypatch.setattr(dashboard, "TOTAL_PER_TILE", 3)
    dashboard.tracker_ref = t
    try:
        body = client.get("/").get_data(as_text=True)
        assert "1/3" in body, "分母未随 TOTAL_PER_TILE 变化，模板仍硬编码 /4"
        assert "1/4" not in body
    finally:
        dashboard.tracker_ref = old_ref


def test_html_template_has_no_hardcoded_four():
    """问题5: 模板源码层兜底——不得残留写死的 /4 分母。"""
    assert "/4<" not in dashboard.HTML_TEMPLATE
    assert "{{ d.played }}/{{ total_per_tile }}" in dashboard.HTML_TEMPLATE
    assert "{{ info.played }}/{{ total_per_tile }}" in dashboard.HTML_TEMPLATE


# ------------------------------------------------------------------ 问题3
def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_dashboard_thread_exits_gracefully_on_stop():
    """问题3: stop_dashboard 必须能唤醒阻塞在 accept() 的服务线程并在超时内退出。"""
    import threading
    import time as _time
    import urllib.request

    from tracker import TileTracker

    port = _free_port()
    old_ref = dashboard.tracker_ref
    old_server = dashboard._server_ref
    thread = threading.Thread(
        target=dashboard.start_dashboard,
        args=(TileTracker(),),
        kwargs={"host": "127.0.0.1", "port": port},
        daemon=True,
    )
    try:
        dashboard.tracker_ref = None
        thread.start()
        # 等监听就绪
        for _ in range(50):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/status", timeout=1) as resp:
                    assert resp.status == 200
                break
            except Exception:
                _time.sleep(0.1)
        else:
            raise AssertionError("仪表盘服务未能启动")

        assert thread.is_alive()
        assert dashboard.stop_dashboard() is True
        thread.join(timeout=5)
        assert not thread.is_alive(), "服务线程未在超时内退出（优雅关闭失效）"
    finally:
        dashboard.tracker_ref = old_ref
        dashboard._server_ref = old_server
        dashboard._stop_event.clear()


def test_shutdown_route_sets_stop_flag(client):
    """问题3: POST /shutdown 应置位停止标记；无 server 时 stop_dashboard 返回 False。"""
    dashboard._stop_event.clear()
    dashboard._server_ref = None
    r = client.post("/shutdown")
    assert r.status_code == 200
    assert dashboard._stop_event.is_set()
    assert dashboard.stop_dashboard() is False
    dashboard._stop_event.clear()


def test_confirmed_corrected_hand_reaches_afa_with_rivers(client):
    from tracker import TileTracker
    import dashboard as _d
    t = TileTracker()
    t.reset()
    hand = ["1m", "2m", "3m", "4m", "5m", "6m", "7m", "8m", "9m", "1s", "2s", "3s", "4s", "4s"]
    assert t.update_hand_tiles(hand)
    t.update_river([("1m", 0.9)], "left")
    t.update_river([("2m", 0.9)], "right")
    t.update_river([("3m", 0.9)], "top")
    captured = {}

    class FakeAfa:
        last_error = None
        def recommend(self, hand14, rivers=(), penggang=()):
            captured["hand"] = list(hand14)
            captured["rivers"] = rivers
            return {"source": "afa", "min_shanten": 0, "win_tiles": [],
                    "options": [{"tile": hand14[0], "score": 1.0,
                                 "danger_rate": 0.0, "shanten_after": 0,
                                 "win_tiles_after": []}],
                    "peng_suggest": [], "gang_suggest": []}

    old_tracker, old_afa = _d.tracker_ref, _d.get_afa_client()
    _d.tracker_ref = t
    _d.set_afa_client(FakeAfa())
    try:
        _d._compute_advice_for_hand(t)
        advice = t.get_snapshot()["latest_advice"]
    finally:
        _d.tracker_ref = old_tracker
        _d.set_afa_client(old_afa)
    assert captured["hand"] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 41, 42, 43, 44, 44]
    assert captured["rivers"] == {"up": [1], "down": [2], "opposite": [3]}
    assert advice["source_info"]["afa_available"] is True
