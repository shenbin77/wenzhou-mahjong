"""round_transition 单测。

真实「下一局」局间帧尚未采到（2026-10-25 全量扫描 dev_auto_play 4.7k 帧无命中），
正例使用合成帧 + 合成模板，仅验证门控/单击/点后复核逻辑；真实帧负例验证不误触。
"""
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT))
import round_transition as rt  # noqa: E402
import full_auto_control as fac  # noqa: E402

REAL_INGAME = (BOT.parent / "采集" / "dev_auto_play" / "ui_20261025_165115_emulator5556_玉姐"
               / "frames" / "20261025_165416_996466_before_29c49d.png")
BOX = (1250, 760, 1490, 850)


def _button():
    btn = np.zeros((BOX[3] - BOX[1], BOX[2] - BOX[0], 3), np.uint8)
    btn[:] = (40, 140, 245)  # BGR 橙
    cv2.putText(btn, "NEXT", (40, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (255, 255, 255), 4)
    cv2.rectangle(btn, (3, 3), (btn.shape[1] - 4, btn.shape[0] - 4), (20, 90, 200), 3)
    return btn


def _settle_frame(with_button=True):
    img = np.full((900, 1600, 3), 20, np.uint8)          # 结算遮罩：底部暗
    img[250:600, 450:1150] = (235, 240, 245)              # 中央奶油面板
    if with_button:
        img[BOX[1]:BOX[3], BOX[0]:BOX[2]] = _button()
    return img


def _new_round_frame():
    img = np.full((900, 1600, 3), 60, np.uint8)
    img[760:900, :] = (225, 230, 235)                     # 亮手牌带
    return img


class Seq:
    def __init__(self, frames):
        self.frames = list(frames)

    def __call__(self):
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]


def _exe(frames, taps, **kw):
    return rt.RoundTransitionExecutor(capture=Seq(frames), tap=lambda x, y: taps.append((x, y)),
                                      sleep=lambda s: None, template=_button(),
                                      post_polls=3, **kw)


def test_positive_single_click_and_advance():
    taps, adv = [], []
    exe = _exe([_settle_frame(), _settle_frame(), _new_round_frame()], taps,
               on_advanced=adv.append)
    r = exe.attempt(round_no=3)
    assert r.status == "advanced" and r.clicked
    assert taps == [((BOX[0] + BOX[2]) // 2, (BOX[1] + BOX[3]) // 2)]
    assert adv and adv[0]["round_no"] == 3
    # 同一局再来一次绝不重复点击
    assert exe.attempt(round_no=3).status == "no_click"
    assert len(taps) == 1


def test_template_missing_never_clicks(tmp_path):
    taps = []
    exe = rt.RoundTransitionExecutor(capture=_settle_frame, tap=lambda x, y: taps.append(1),
                                     sleep=lambda s: None, template_path=tmp_path / "none.png")
    assert exe.attempt(round_no=1).status == "no_click" and not taps


def test_no_button_is_terminal_no_click():
    taps = []
    r = _exe([_settle_frame(with_button=False)], taps).attempt(round_no=2)
    assert r.status == "terminal" and not taps


def test_button_without_scene_rejected():
    img = _new_round_frame()
    img[BOX[1]:BOX[3], BOX[0]:BOX[2]] = _button()
    taps = []
    r = _exe([img], taps).attempt(round_no=2)
    assert r.status == "terminal" and not taps


def test_recheck_moved_or_gone_no_click():
    taps = []
    r = _exe([_settle_frame(), _settle_frame(with_button=False)], taps).attempt(round_no=2)
    assert r.status == "no_click" and not taps


def test_round_16_terminal():
    taps = []
    r = _exe([_settle_frame()], taps).attempt(round_no=16)
    assert r.status == "terminal" and not taps


def test_post_verify_uncertain_halts_forever():
    taps = []
    exe = _exe([_settle_frame()], taps)   # 点后按钮一直在
    r = exe.attempt(round_no=4)
    assert r.status == "post_verify_uncertain" and exe.halted and len(taps) == 1
    assert exe.attempt(round_no=5).status == "halted" and len(taps) == 1


def test_low_score_and_size_gates():
    img = _settle_frame(with_button=False)
    img[BOX[1]:BOX[3], BOX[0]:BOX[2]] = (40, 140, 245)   # 纯色块，无文字
    assert not rt.find_next_round_button(img, _button())["ok"]
    tiny = _button()[:30, :60]
    assert rt.find_next_round_button(_settle_frame(), tiny)["reason"].startswith(
        ("button_size", "template_score"))


@pytest.mark.skipif(not REAL_INGAME.is_file(), reason="real frame missing")
def test_real_ingame_frame_rejected():
    img = cv2.imdecode(np.fromfile(str(REAL_INGAME), np.uint8), cv2.IMREAD_COLOR)
    ev = rt.find_next_round_button(img, _button())
    scene = rt.round_end_scene(img)
    assert not (ev["ok"] and scene["ok"])


def test_room_card_policy():
    assert rt.room_card_policy(5)["allow_new_game"] is True
    assert rt.room_card_policy(0)["allow_new_game"] is False
    assert rt.room_card_policy(2)["level"] == "warn"
    unk = rt.room_card_policy(None)
    assert unk["allow_new_game"] is True and unk["log"] == "room_cards_unknown"


def test_full_auto_control_roundtrip(tmp_path):
    fac.write_command("emulator-5556", "resume", root=tmp_path)
    assert fac.consume_command("emulator-5556", root=tmp_path) == "resume"
    assert fac.consume_command("emulator-5556", root=tmp_path) is None
    with pytest.raises(ValueError):
        fac.write_command("emulator-5556", "click", root=tmp_path)


def test_assist_resume_after_round(monkeypatch):
    import assist_window as aw

    class W:
        continuous = True
        full_auto_running = False
        continuous_caishen = None
    w = W()
    calls = []
    w.next_round = lambda: calls.append("next")
    w.start = lambda: calls.append("start")
    w._start_full_auto = lambda: calls.append("auto")
    w.full_auto_status = type("V", (), {"set": lambda self, v: None})()
    w.serial = type("S", (), {"get": lambda self: "emulator-x"})()
    res = rt.TransitionResult("advanced", "ok", clicked=True)
    aw.TeaAssistWindow._on_round_transition_result(w, res)
    assert calls == ["next"] and w._auto_resume_pending
    monkeypatch.setattr(fac, "consume_command", lambda s, root=None: None)
    aw.TeaAssistWindow._poll_full_auto_control(w)
    assert "auto" not in calls          # 财神未重读前不恢复
    w.continuous_caishen = "w8"
    aw.TeaAssistWindow._poll_full_auto_control(w)
    assert calls[-1] == "auto" and not w._auto_resume_pending
