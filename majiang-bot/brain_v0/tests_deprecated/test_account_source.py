# -*- coding: utf-8 -*-
"""AccountSource HTTP 客户端单测：列表 + 下载 两接口的本地 mock。

用本地 http.server（线程）起 mock 服务，覆盖：
- 当日回放：列表命中 + 下载落盘 → pull 返回本地路径列表
- 无回放：列表空 → pull 返回 []（=今天没打）
- api_base=None：未接入账号源 → pull 抛 AccountSourceError
- HTTP 500：列表 / 下载服务端异常 → pull 抛 AccountSourceError
- run_today 对 AccountSourceError 的兜底（played_today=None）
- token 鉴权头透传
"""
import os
import sys
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from replay_pipeline import daily  # noqa: E402


# ---------------------------------------------------------------------------
# 本地 mock 服务
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    """类属性即 mock 配置，由 _server() 用 type() 生成子类注入。"""
    replays = []            # 列表接口返回的条目
    list_code = 200         # 列表接口状态码
    download_code = 200     # 下载接口状态码
    download_body = b"FAKE-VIDEO-DATA"
    seen = None             # dict：记录收到的请求头/路径（列表与下载）

    def log_message(self, *args):  # 静音访问日志
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        seen = type(self).seen
        if seen is not None:
            seen.setdefault("paths", []).append(self.path)
            seen.setdefault("headers", []).append(dict(self.headers))
        if self.path.startswith("/replays?") or self.path == "/replays":
            if type(self).list_code != 200:
                self._send(type(self).list_code, b"server error")
                return
            payload = json.dumps({"replays": type(self).replays}).encode("utf-8")
            self._send(200, payload)
        elif self.path.startswith("/replays/"):
            if type(self).download_code != 200:
                self._send(type(self).download_code, b"download error")
                return
            self._send(200, type(self).download_body, ctype="application/octet-stream")
        else:
            self._send(404, b"not found")


def _make_handler(**attrs):
    """生成一个带给定类属性（mock 配置）的 handler 子类。"""
    merged = {"replays": [], "list_code": 200, "download_code": 200,
              "download_body": b"FAKE-VIDEO-DATA", "seen": {}}
    merged.update(attrs)
    return type("MockHandler", (_Handler,), merged)


@pytest.fixture
def server():
    """上下文管理器：起一个线程 http.server，yield base_url；退出即关停。"""
    ctx = {}

    class _Server:
        def __init__(self, **attrs):
            self.handler = _make_handler(**attrs)
            self.httpd = HTTPServer(("127.0.0.1", 0), self.handler)
            self.thread = threading.Thread(
                target=self.httpd.serve_forever, daemon=True)
            self.thread.start()

        @property
        def base(self):
            return f"http://127.0.0.1:{self.httpd.server_port}"

        @property
        def seen(self):
            return self.handler.seen

        def close(self):
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join()

    def _start(**attrs):
        s = _Server(**attrs)
        ctx["s"] = s
        return s

    yield _start
    if "s" in ctx:
        ctx["s"].close()


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
def test_pull_downloads_todays_replays(server):
    """当日回放：列表命中一条 → 下载落盘 → pull 返回本地路径，内容正确。"""
    body = b"\x00\x01real-video-bytes"
    s = server(replays=[{"name": "rec_20260821_1000.mp4", "url": "/replays/1/download"}],
               download_body=body)
    src = daily.AccountSource(api_base=s.base, download_dir=tempfile.mkdtemp())

    paths = src.pull("20260821")

    assert len(paths) == 1
    assert os.path.basename(paths[0]) == "rec_20260821_1000.mp4"
    with open(paths[0], "rb") as f:
        assert f.read() == body


def test_pull_empty_list_means_no_replays(server):
    """无回放：列表返回空 → pull 返回 []（=今天没打）。"""
    s = server(replays=[])
    src = daily.AccountSource(api_base=s.base, download_dir=tempfile.mkdtemp())
    assert src.pull("20260821") == []


def test_pull_without_api_base_raises():
    """api_base=None：账号源未接入 → pull 抛 NotImplementedError（维持原契约）。"""
    src = daily.AccountSource()
    with pytest.raises(NotImplementedError):
        src.pull("20260821")


def test_run_today_not_configured_returns_none():
    """api_base=None 的 AccountSource → run_today 返回 played_today=None 且 note 含 未接入。"""
    src = daily.AccountSource()
    res = daily.run_today(tempfile.mkdtemp(), "170075", src, date="20260821")
    assert res["played_today"] is None
    assert "未接入" in res["note"]


def test_pull_list_http_500_raises(server):
    """HTTP 500（列表服务端异常）→ pull 抛 AccountSourceError。"""
    s = server(replays=[{"name": "a.mp4", "url": "/replays/1/download"}], list_code=500)
    src = daily.AccountSource(api_base=s.base, download_dir=tempfile.mkdtemp())
    with pytest.raises(daily.AccountSourceError):
        src.pull("20260821")


def test_pull_download_http_500_raises(server):
    """HTTP 500（下载服务端异常）→ pull 抛 AccountSourceError。"""
    s = server(replays=[{"name": "a.mp4", "url": "/replays/1/download"}],
               download_code=500)
    src = daily.AccountSource(api_base=s.base, download_dir=tempfile.mkdtemp())
    with pytest.raises(daily.AccountSourceError):
        src.pull("20260821")


def test_pull_sends_bearer_token(server):
    """token 非空 → 列表与下载请求都带 Authorization: Bearer <token>。"""
    s = server(replays=[{"name": "a.mp4", "url": "/replays/1/download"}])
    src = daily.AccountSource(api_base=s.base, token="sekret",
                              download_dir=tempfile.mkdtemp())
    src.pull("20260821")

    seen_headers = s.seen["headers"]
    assert len(seen_headers) == 2  # 列表 + 下载
    for h in seen_headers:
        assert h.get("Authorization") == "Bearer sekret"


def test_run_today_account_error_returns_none(server):
    """run_today 捕获 AccountSourceError → played_today=None。"""
    s = server(replays=[{"name": "a.mp4", "url": "/replays/1/download"}], list_code=500)
    src = daily.AccountSource(api_base=s.base, download_dir=tempfile.mkdtemp())
    res = daily.run_today(tempfile.mkdtemp(), "170075", src, date="20260821")
    assert res["played_today"] is None
    assert "error" in res


def test_run_today_archives_downloaded_replay(server):
    """账号源拉到当日回放 → run_today 判定今天打了并归档（进 _unlabeled）。"""
    s = server(replays=[{"name": "rec_20260821_1000.mp4", "url": "/replays/1/download"}],
               download_body=b"vid")
    src = daily.AccountSource(api_base=s.base, download_dir=tempfile.mkdtemp())
    base = tempfile.mkdtemp()
    res = daily.run_today(base, "170075", src, date="20260821")
    assert res["played_today"] is True
    assert res["videos"]
    room = os.path.join(base, "房间170075_20260821")
    assert os.path.isdir(os.path.join(room, "_unlabeled"))
