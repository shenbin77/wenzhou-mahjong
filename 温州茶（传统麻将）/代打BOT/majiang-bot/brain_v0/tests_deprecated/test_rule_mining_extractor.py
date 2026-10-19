# -*- coding: utf-8 -*-
"""rule_mining/extractor.py 测试：合成画面 + 确定性假分类器验证本地视觉管线。

覆盖：
- 单帧：区域裁剪→切牌→识别→组合成 checker 可用的原语，数量正确；
- 低置信度：牌被标记为未知并剔除（不伪造结论），unknown 计数进 pu.extraction；
- 视频：合成 AVI → default_extractor 完整管线 → 去重/换手牌开新局；
- mine.mine_from_video：默认 extractor 接线（不再抛 NotImplementedError），
  自定义 extractor 仍可用（参数保持可选）。
"""
import hashlib
import os
import sys

import numpy as np
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import cv2  # noqa: E402

from rule_mining import extractor as X  # noqa: E402
from rule_mining import rules_spec as S  # noqa: E402


# ---------------------------------------------------------------------------
# 假分类器：确定性返回，None=低置信度
# ---------------------------------------------------------------------------
class StubClassifier:
    """按调用顺序循环返回预设 tile 列表；None 表示低置信度（真实模型输出 tile=None）。

    仅用于「单帧」测试（同一帧内调用顺序确定，精确断言牌序列）。
    """

    def __init__(self, tiles=None, conf=0.95):
        self.tiles = tiles or []
        self.conf = conf
        self.calls = []

    def predict(self, img, low_conf_threshold=0.5):
        tile = self.tiles[len(self.calls) % len(self.tiles)] if self.tiles else None
        self.calls.append(tile)
        conf = self.conf if tile is not None else 0.1
        return {"tile": tile, "name": str(tile), "confidence": conf}


class ContentStub:
    """内容确定性假分类器：是「图像」的纯函数（同图→同牌ID）。

    真实分类器对同一张牌面给出同一结果，与全局调用次数无关。跨帧去重/换手牌测试
    必须用此类：同一视觉手牌在多帧里总能得到相同签名，不同手牌得到不同签名。

    实现：先把裁片降采样到 16x16 并量化为 8 级再哈希——贴近真实 CNN 的平移/噪声
    不变性，能吸收 MJPG 编码在边缘引入的 ±20 像素噪声，避免同一手牌因编解码
    噪声被误判成「换手牌开新局」。牌ID由哈希确定性映射到 [1..28] 合法牌ID。
    """

    def __init__(self, force_all_low=False):
        self.force_all_low = force_all_low
        self.calls = 0

    def predict(self, img, low_conf_threshold=0.5):
        self.calls += 1
        if self.force_all_low:
            return {"tile": None, "name": "未知(低置信度)", "confidence": 0.1}
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img.astype(np.float32)
        small = cv2.resize(g, (16, 16), interpolation=cv2.INTER_AREA)
        q = np.clip((small / 32).astype(np.int32), 0, 7)  # 量化到 0..7，抗编码噪声
        h = int(hashlib.md5(q.tobytes()).hexdigest(), 16)
        tile = S.TILE_IDS[h % len(S.TILE_IDS)]  # 映射进合法牌ID（1-9/21-29/41-49/55）
        return {"tile": tile, "name": str(tile), "confidence": 0.9}


def _valid_pu():
    hands = {s: [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 24] for s in S.PLAYERS}
    return {
        "round_no": 1,
        "hands": hands,
        "discards": {s: [] for s in S.PLAYERS},
        "win": {"seat": "bottom", "type": "zimo", "fans": []},
        "scores": {"bottom": 6, "top": -2, "left": -2, "right": -2},
    }


# ---------------------------------------------------------------------------
# 合成画面工具
# ---------------------------------------------------------------------------
def _blank_frame(w=1280, h=626):
    return np.full((h, w, 3), 40, dtype=np.uint8)  # 暗背景（与 test_make_training_data 一致）


def _draw_hand(img, n_tiles=7):
    """在手牌区 (200,540)-(1080,620) 画 n_tiles 张亮色牌，每张带较暗内纹。

    内纹模拟真实牌面图案：
    - 让每张切片的灰度 std > extractor.MIN_TILE_STD，这样空白过滤只剔除真正的
      背景块（如空白出牌河），不误伤牌面；
    - 内纹位置随列号变化，使同一手牌内各张图案互不相同（贴近真实牌面各不相同），
      也让 ContentStub 能区分不同牌；
    - 内纹画在牌身内部，不增加连通分量，故不影响切牌数量。
    """
    x1, y1, x2, y2 = 200, 540, 1080, 620
    avail = (x2 - x1) // n_tiles
    for i in range(n_tiles):
        cx = x1 + i * avail
        cv2.rectangle(img, (cx + 4, y1 + 6), (cx + avail - 6, y2 - 6), (235, 235, 235), -1)
        px = cx + avail // 2 + ((i % 3) - 1) * (avail // 5)
        py = (y1 + 6 + y2 - 6) // 2 + ((i // 3) % 2) * (avail // 6)
        iw = max(4, (avail - 10) // 3)
        ih = max(4, (y2 - y1 - 12) // 3)
        cv2.rectangle(img, (px - iw // 2, py - ih // 2), (px + iw // 2, py + ih // 2), (80, 80, 80), -1)
    return img


def _write_video(path, segments):
    """把 [(n_frames, n_hand_tiles), ...] 段写成 AVI，返回 fps。"""
    fps = 10.0
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"),
                             fps, (1280, 626))
    if not writer.isOpened():  # pragma: no cover
        pytest.skip("OpenCV 无可用视频编码器，跳过真实视频管线测试")
    for n_frames, n_tiles in segments:
        for _ in range(n_frames):
            writer.write(_draw_hand(_blank_frame(), n_tiles))
    writer.release()
    return fps


# ---------------------------------------------------------------------------
# 单帧提取
# ---------------------------------------------------------------------------
def test_extract_tiles_from_frame_splits_and_classifies():
    frame = _draw_hand(_blank_frame(), n_tiles=7)
    clf = StubClassifier(tiles=[1, 2, 3, 4, 5, 6, 7])
    obs = X.extract_tiles_from_frame(frame, clf)
    assert obs["hands"]["bottom"] == [1, 2, 3, 4, 5, 6, 7]  # 数量与顺序正确
    assert obs["unknown"] == 0
    assert len(clf.calls) == 7  # 一张牌一次预测
    for seat in S.PLAYERS:
        assert seat in obs["discards"]  # 结构完整，checker 可直接消费


def test_extract_tiles_from_frame_dark_frame_no_crash():
    obs = X.extract_tiles_from_frame(_blank_frame(), StubClassifier(tiles=[1]))
    assert obs["hands"]["bottom"] == []  # 无牌 → 空，不崩
    assert all(obs["discards"][s] == [] for s in S.PLAYERS)
    assert obs["unknown"] == 0


def test_low_confidence_tiles_marked_unknown_and_dropped():
    frame = _draw_hand(_blank_frame(), n_tiles=3)
    clf = StubClassifier(tiles=[1, None, 3])  # 中间一张低置信度
    pus = X.extract_pus_from_frames([frame], clf)
    assert len(pus) == 1
    assert pus[0]["hands"]["bottom"] == [1, 3]      # 低置信度牌被剔除，不硬猜
    assert pus[0]["extraction"]["low_conf_unknown"] == 1  # 审计：标了 1 张未知


# ---------------------------------------------------------------------------
# extract_pus_from_frames：从帧列表组合牌谱（复用内部逻辑，无需视频编解码）
# ---------------------------------------------------------------------------
def test_extract_pus_from_frames_dedups_same_hand():
    frame = _draw_hand(_blank_frame(), n_tiles=5)
    clf = ContentStub()
    pus = X.extract_pus_from_frames([frame, frame.copy(), frame.copy()], clf)
    assert len(pus) == 1  # 相同手牌状态只记一局
    assert pus[0]["round_no"] == 1
    hand = pus[0]["hands"]["bottom"]
    assert len(hand) == 5  # 5 张牌
    assert all(t in S.TILE_IDS for t in hand)  # 都是合法牌ID
    assert set(pus[0]["discards"]) == set(S.PLAYERS)


def test_extract_pus_from_frames_new_round_on_hand_change():
    frame_a = _draw_hand(_blank_frame(), n_tiles=5)
    frame_b = _draw_hand(_blank_frame(), n_tiles=6)
    clf = ContentStub()
    pus = X.extract_pus_from_frames([frame_a, frame_a.copy(), frame_b], clf)
    assert len(pus) == 2  # 换手牌 → 新一局
    assert [p["round_no"] for p in pus] == [1, 2]
    assert len(pus[0]["hands"]["bottom"]) == 5  # 第 1 局 5 张
    assert len(pus[1]["hands"]["bottom"]) == 6  # 第 2 局 6 张
    assert pus[0]["hands"]["bottom"] != pus[1]["hands"]["bottom"]  # 两局手牌不同


def test_extract_pus_from_frames_all_low_confidence_yields_nothing():
    frame = _draw_hand(_blank_frame(), n_tiles=4)
    clf = ContentStub(force_all_low=True)  # 全部低置信度
    pus = X.extract_pus_from_frames([frame], clf)
    assert pus == []  # 无可信手牌 → 不伪造任何牌谱


# ---------------------------------------------------------------------------
# 完整视频管线（真实 cv2.VideoCapture 抽帧）
# ---------------------------------------------------------------------------
def test_extract_frames_and_default_extractor_on_real_video(tmp_path):
    video = tmp_path / "demo.avi"
    _write_video(video, [(25, 5), (25, 6)])  # 手牌 5 张 → 6 张，两段
    clf = ContentStub()

    frames = X.extract_frames(str(video), sample_interval=1.0)
    assert len(frames) >= 2  # fps=10 时采样到多帧

    pus = X.default_extractor(str(video), clf, sample_interval=1.0)
    assert len(pus) == 2  # 两种手牌状态（同状态多帧去重成 1 局）
    assert len(pus[0]["hands"]["bottom"]) == 5  # 第 1 局 5 张
    assert len(pus[1]["hands"]["bottom"]) == 6  # 第 2 局 6 张
    assert all(t in S.TILE_IDS for p in pus for t in p["hands"]["bottom"])
    assert all("extraction" in p for p in pus)


def test_extract_frames_unopenable_video_raises():
    with pytest.raises(ValueError, match="无法打开视频"):
        X.extract_frames("no_such_file_xyz.mp4")


# ---------------------------------------------------------------------------
# mine.mine_from_video 接线
# ---------------------------------------------------------------------------
def test_mine_from_video_default_extractor_no_not_implemented(monkeypatch, tmp_path):
    """默认路径不再抛 NotImplementedError：接线到本地提取器并跑 checker。"""
    video = tmp_path / "ok.avi"
    _write_video(video, [(10, 5)])

    with monkeypatch.context() as m:
        # 用确定性假提取器替代（避免依赖未训练的真实分类器），验证接线成立
        m.setattr(X, "default_extractor",
                  lambda p: [dict(_valid_pu(), round_no=1)])
        from rule_mining import mine
        rep = mine.mine_from_video(str(video))  # extractor=None → 默认
        assert rep["ok"] is True
        assert rep["total"] == 1


def test_mine_from_video_custom_extractor_still_used():
    """extractor 参数保持可选：传入的提取器被直接使用。"""
    from rule_mining import mine
    calls = []

    def custom(p):
        calls.append(p)
        return [dict(_valid_pu(), round_no=7)]

    rep = mine.mine_from_video("whatever.mp4", extractor=custom)
    assert calls == ["whatever.mp4"]  # 自定义提取器被调用
    assert rep["total"] == 1
    assert rep["ok"] is True