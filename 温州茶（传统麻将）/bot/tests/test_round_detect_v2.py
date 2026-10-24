# -*- coding: utf-8 -*-
"""任务C：可信换局检测（牌面证据 + Codex 挡板）的回归测试。

证据模型（assist_window._accept_continuous_result）：
1. HUD 指纹（头像未遮挡时，像素级）；
2. 桌面财神小牌 (622,348,677,423)，0.98 阈值，不受头像遮挡。
不变量：旧局手选财神绝不能跨局进入 advise；置信不足时保持挡板
（不出建议、不清空、不续用）。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import assist_window  # noqa: E402
from assist_window import (  # noqa: E402
    TeaAssistWindow, same_tea_round, tea_round_caishen_evidence,
    tea_round_fingerprint,
)

LIVE = BOT_DIR.parent / "采集" / "assist_sessions" / "20261023_235121" / "live" / "tea_2p"
S2903 = BOT_DIR.parent / "采集" / "assist_sessions" / "20261024_002903" / "live" / "tea_2p"
# 以下帧的桌面财神读数已用 round_survey.py 实测核对（2026-10-24）。
DONG_FRAME = LIVE / "20261023_235126_427878_8ef13c.png"      # dong 1.0
T8_FRAME = LIVE / "20261024_000652_066197_af0c18.png"        # t8 1.0
T8_LATER = LIVE / "20261024_000756_773026_3cce00.png"        # t8 1.0
NO_CARD = LIVE / "20261024_001210_555187_e90155.png"         # 牌面不可读(0.2594)
S1_FRAME = S2903 / "20261024_004701_020244_4e3948.png"       # s1 1.0
INCIDENT = S2903 / "20261024_004748_088050_2169ad.png"       # 事故帧 牌面0.9015
AVATAR_FRAME = S2903 / "20261024_003231_828201_4b43a1.png"   # 头像遮挡样本


class _Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _window(pick: str = "", manual: bool = False) -> TeaAssistWindow:
    """按既有测试惯例构造最小窗口（__new__，不起 Tk）。"""
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window.mode = _Value("温州茶 · 2人")
    window.continuous = True
    window.continuous_round = None
    window.continuous_caishen = None
    window.timer = None
    window.status = _Value("")
    window.live_info = _Value("")
    window.caishen = _Value(pick)
    window.manual_caishen = _Value(manual)
    window.current = {"status": "advise"}
    window.hand = _Value("旧手牌")
    window.preview = object()
    window.picture = SimpleNamespace(configure=lambda **kwargs: None)
    return window


@pytest.mark.skipif(not DONG_FRAME.is_file(), reason="235121 实战帧缺失")
def test_first_frame_adopts_card_identity_without_hud():
    """HUD 被头像全程遮挡时，首次确认的牌面也能建立局身份。"""
    assert tea_round_fingerprint(DONG_FRAME) is None          # HUD 确实被遮挡
    label, score = tea_round_caishen_evidence(DONG_FRAME)
    assert (label, round(score, 3)) == ("dong", 1.0)
    window = _window()
    assert window._accept_continuous_result(
        {"status": "advise", "image": str(DONG_FRAME)})
    assert window.continuous_caishen == "dong"
    assert window.continuous is True


@pytest.mark.skipif(not (DONG_FRAME.is_file() and T8_FRAME.is_file()),
                    reason="235121 实战帧缺失")
def test_card_change_proves_new_round_and_forgets_manual_pick():
    """牌面变化 = 换局硬证据：清空旧手选并自动采用新财神。"""
    window = _window(pick="dong", manual=True)
    assert window._accept_continuous_result(
        {"status": "advise", "image": str(DONG_FRAME)})
    assert window.continuous_caishen == "dong"
    assert not window._accept_continuous_result(
        {"status": "advise", "image": str(T8_FRAME)})
    assert window.continuous is True
    assert window.caishen.get() == "" and not window.manual_caishen.get()
    assert window.continuous_caishen == "t8"
    assert "财神牌已变化" in window.status.get()
    assert "自动建立新局基线" in window.status.get()
    assert "手选财神已清空" in window.status.get()


@pytest.mark.skipif(not T8_FRAME.is_file(), reason="235121 实战帧缺失")
def test_manual_pick_contradicting_card_is_replaced_by_visual_evidence():
    """高置信牌面优先：作废错误手选、跳过本帧并继续自动跟随。"""
    window = _window(pick="s1", manual=True)
    assert not window._accept_continuous_result(
        {"status": "advise", "image": str(T8_FRAME)})      # 牌面 t8 ≠ 手选 s1
    assert window.continuous is True
    assert window.caishen.get() == "" and not window.manual_caishen.get()
    assert window.continuous_caishen == "t8"
    assert "自动更正" in window.status.get()


@pytest.mark.skipif(not (T8_FRAME.is_file() and T8_LATER.is_file()),
                    reason="235121 实战帧缺失")
def test_occluded_hud_with_matching_card_still_advises():
    """头像持续遮挡但牌面与已核对局一致：不再长期停摆，允许建议。"""
    window = _window(pick="t8", manual=True)
    assert window._accept_continuous_result(
        {"status": "advise", "image": str(T8_FRAME)})
    assert window._accept_continuous_result(
        {"status": "advise", "image": str(T8_LATER)})
    assert window.continuous is True
    assert window.continuous_caishen == "t8"


@pytest.mark.skipif(not NO_CARD.is_file(), reason="235121 实战帧缺失")
def test_unreadable_card_uses_current_round_manual_fallback():
    """牌面不可读时，本局人工选择是可用兜底证据。"""
    window = _window(pick="t8", manual=True)
    assert window._accept_continuous_result(
        {"status": "advise", "image": str(T8_FRAME)})
    assert window._accept_continuous_result(
        {"status": "advise", "image": str(NO_CARD)})
    assert window.continuous is True                      # 未停跟随
    assert window.caishen.get() == "t8"                   # 未清空手选
    assert window.manual_caishen.get() is True
    assert "使用本局手选值" in window.live_info.get()


@pytest.mark.skipif(not (S1_FRAME.is_file() and INCIDENT.is_file()),
                    reason="002903 事故帧缺失")
def test_incident_frame_cannot_advise_with_stale_s1_pick():
    """实战复现：004545 局(s1) → 004748(t1 局) 跨局帧，旧 s1 手选必须作废。

    事故帧的 HUD 被视频头像遮挡，但桌面财神牌现在能读成 t1（0.98+，t1 的实战
    角标模板补齐后）——牌面证据直接判定换局：清空旧手选并自动采用 t1；变化帧
    本身仍跳过，旧 s1 绝不会进入新一局的 advise。
    """
    window = _window(pick="s1", manual=True)
    assert tea_round_fingerprint(INCIDENT) is None        # HUD 被头像遮挡
    label, score = tea_round_caishen_evidence(INCIDENT)
    assert label == "t1" and score >= 0.98
    assert not window._accept_continuous_result(
        {"status": "advise", "image": str(INCIDENT)})
    assert window.continuous is True
    assert window.caishen.get() == "" and not window.manual_caishen.get()
    assert window.continuous_caishen == "t1"
    # 该窗口没有历史牌面基线，矛盾由"手选 vs 牌面"这条硬证据判定
    assert "画面财神(t1)自动更正" in window.status.get()
    assert "自动建立新局基线" in window.status.get()


@pytest.mark.skipif(not (S1_FRAME.is_file() and INCIDENT.is_file()),
                    reason="002903 事故帧缺失")
def test_stale_pick_blocked_even_if_avatar_gate_misses(monkeypatch):
    """红色头像门漏报时（指纹误匹配旧锁），旧局手选仍不得进入 advise。"""
    fake_fp = b"0" * 16575          # 与真实指纹等长，模拟"误匹配的旧锁"
    monkeypatch.setattr(assist_window, "tea_round_fingerprint", lambda p: fake_fp)
    window = _window(pick="s1", manual=True)
    assert window._accept_continuous_result(
        {"status": "advise", "image": str(S1_FRAME)})     # s1 牌面 1.0，锁+牌面一致
    assert same_tea_round(fake_fp, window.continuous_round)
    assert window.continuous_caishen == "s1"
    # 事故帧：指纹骗过了头像门，但牌面 s1→t1 的矛盾把它拦住并清空手选。
    assert not window._accept_continuous_result(
        {"status": "advise", "image": str(INCIDENT)})
    assert window.continuous is True
    assert window.caishen.get() == "" and not window.manual_caishen.get()
    assert window.continuous_caishen == "t1"
    assert "财神牌已变化" in window.status.get()
    assert "自动建立新局基线" in window.status.get()


@pytest.mark.skipif(not AVATAR_FRAME.is_file(), reason="002903 头像遮挡样本缺失")
def test_codex_avatar_guard_still_holds():
    """保留 Codex 挡板语义：头像遮挡的 HUD 不能伪造新局指纹。"""
    assert tea_round_fingerprint(AVATAR_FRAME) is None
