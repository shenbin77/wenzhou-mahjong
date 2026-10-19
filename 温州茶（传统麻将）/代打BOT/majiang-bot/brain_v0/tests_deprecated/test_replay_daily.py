# -*- coding: utf-8 -*-
"""replay_pipeline 日跑自动化测试：拉取到=今天打了，拉取不到=今天没打。

验证第 3/5 条：源可插拔（LocalFolderSource / AccountSource），
run_today 的 pull-or-skip 语义。沙箱无账号/网络，故 AccountSource 仅 stub。
"""
import os
import sys
import json
import tempfile
import datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from replay_pipeline import daily  # noqa: E402


def _today():
    return datetime.datetime.now().strftime("%Y%m%d")


def _mk(path):
    with open(path, "wb") as f:
        f.write(b"\x00\x00")


def test_local_source_finds_todays_videos():
    d = tempfile.mkdtemp()
    p = os.path.join(d, f"rec_{_today()}_0955.mp4")
    _mk(p)
    src = daily.LocalFolderSource(d)
    assert src.pull(_today()) == [p]


def test_local_source_ignores_other_days():
    d = tempfile.mkdtemp()
    yest = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y%m%d")
    _mk(os.path.join(d, f"rec_{yest}_0955.mp4"))
    src = daily.LocalFolderSource(d)
    # 查今天 → 没有 → 空列表（=今天没打）
    assert src.pull(_today()) == []


def test_account_source_not_implemented_is_open_item():
    """账号源在沙箱未接入：run_today 返回 played_today=None，提示需接入。"""
    src = daily.AccountSource()
    res = daily.run_today(tempfile.mkdtemp(), "170075", src, date="20260821")
    assert res["played_today"] is None
    assert "未接入" in res["note"]


def test_run_today_pull_or_skip():
    today = _today()
    # 有当日录像 → 今天打了
    src_dir = tempfile.mkdtemp()
    _mk(os.path.join(src_dir, f"rec_{today}_1000.mp4"))
    base = tempfile.mkdtemp()
    res = daily.run_today(base, "170075", daily.LocalFolderSource(src_dir), date=today)
    assert res["played_today"] is True
    assert os.path.isdir(os.path.join(base, f"房间170075_{today}"))

    # 没有当日录像 → 今天没打
    empty = tempfile.mkdtemp()
    res2 = daily.run_today(tempfile.mkdtemp(), "170075",
                           daily.LocalFolderSource(empty), date=today)
    assert res2["played_today"] is False
    assert "没打" in res2["note"]


def test_run_today_labeled_video_gets_ingested():
    """带 auto_label 的录像应被规范命名落盘（而非进 _unlabeled）。"""
    today = _today()
    src_dir = tempfile.mkdtemp()
    vid_name = f"rec_{today}_raw.mp4"
    vid = os.path.join(src_dir, vid_name)
    _mk(vid)
    base = tempfile.mkdtemp()
    labels = {vid_name: ("自摸", 7, 10, 20)}
    res = daily.run_today(base, "170075", daily.LocalFolderSource(src_dir),
                          date=today, auto_label=labels)
    assert res["played_today"] is True
    room = os.path.join(base, f"房间170075_{today}")
    assert os.path.isfile(os.path.join(room, "第07局_a_自摸_0010-0020.mp4"))


# ---------------------------------------------------------------------------
# 下载安全（CODE_REVIEW_REPORT_2026-08-27）：协议白名单 + 路径穿越防护
# ---------------------------------------------------------------------------
class _StubSource(daily.AccountSource):
    """不起真实 HTTP：列表直接返回给定条目，记录被请求过的下载地址。"""

    def __init__(self, items, **kw):
        super().__init__(api_base="http://mock.local", **kw)
        self._items = items
        self.requested = []

    def _get(self, url):
        self.requested.append(url)
        if "/replays?" in url:
            payload = json.dumps({"replays": self._items}).encode("utf-8")
            return 200, payload
        return 200, b"BYTES"


def test_pull_rejects_non_http_scheme(monkeypatch):
    """非法协议（file:// 等）下载地址 → 不发请求，直接抛 AccountSourceError。"""
    called = []

    def _no_network(*a, **k):
        called.append(a)
        raise AssertionError("非法协议地址不应发起网络请求")

    monkeypatch.setattr(daily.urllib.request, "urlopen", _no_network)
    src = _StubSource([{"name": "rec.mp4", "url": "file:///etc/passwd"}],
                      download_dir=tempfile.mkdtemp())
    with pytest.raises(daily.AccountSourceError):
        src.pull("20260821")
    # 只请求了列表接口，危险地址在发起前就被拒
    assert all("/replays?" in u for u in src.requested)
    assert not called

    # api_base 本身也必须是 http/https
    with pytest.raises(daily.AccountSourceError):
        daily.AccountSource(api_base="file:///tmp")


def test_pull_download_name_cannot_escape_download_dir():
    """../ 逃逸的文件名必须被夹回 download_dir 内；空名直接拒绝。"""
    dl = tempfile.mkdtemp()
    evil = "../../outside_evil.mp4"
    src = _StubSource([{"name": evil, "url": "/replays/1/download"}],
                      download_dir=dl)
    paths = src.pull("20260821")

    assert len(paths) == 1
    root = os.path.realpath(dl)
    dest = os.path.realpath(paths[0])
    assert os.path.commonpath([root, dest]) == root, dest
    assert os.path.basename(dest) == "outside_evil.mp4"
    assert not os.path.exists(os.path.join(os.path.dirname(root), "outside_evil.mp4"))

    # 辅助函数：绝对路径/带目录的名字被夹回目录内，空名拒绝
    clamped = daily._resolve_download_dest(dl, "/abs/path/x.mp4")
    assert os.path.dirname(os.path.realpath(clamped)) == root
    assert os.path.basename(clamped) == "x.mp4"
    for bad in ("", "   "):
        with pytest.raises(daily.AccountSourceError):
            daily._resolve_download_dest(dl, bad)
