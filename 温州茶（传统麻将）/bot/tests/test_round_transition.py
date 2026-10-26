"""round_transition 单测。

2026-10-25 已从 17:00 / 17:11 实机局间页反查到原生 1600×900 真帧，
正式模板由 4 张同页按钮区域逐像素中位生成，以消除按钮上的移动手指动画。
合成帧继续覆盖执行器状态机；真帧覆盖模板/场景正例和正常对局负例。
"""
import sys
from pathlib import Path
from threading import Event

import cv2
import numpy as np
import pytest

BOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT))
import round_transition as rt  # noqa: E402
import full_auto_control as fac  # noqa: E402

REAL_INGAME = (BOT.parent / "采集" / "dev_auto_play" / "ui_20261025_165115_emulator5556_玉姐"
               / "frames" / "20261025_165416_996466_before_29c49d.png")
REAL_NEXT_ROUND = (BOT.parent / "采集" / "dev_auto_play"
                   / "ui_20261025_165932_emulator5558_拾则" / "frames"
                   / "20261025_170009_431341_before_5b9e69.png")
REAL_POST_CLICK_WAIT_5556 = (
    BOT.parent / "采集" / "dev_auto_play"
    / "ui_20261026_170909_emulator5556_烘熊瞎" / "frames"
    / "20261026_171328_801775_before_fdd1f3.png")
REAL_POST_CLICK_WAIT_5554 = (
    BOT.parent / "采集" / "dev_auto_play"
    / "ui_20261026_172351_emulator5554_玉姐" / "frames"
    / "20261026_172838_951780_before_a86493.png")
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


def _waiting_peer_frame():
    """本端已点“下一局”，但还在等对手：无旧按钮、无结算遮罩、手牌未发。"""
    return np.full((900, 1600, 3), (40, 85, 55), np.uint8)


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


def test_no_button_is_transient_no_click():
    taps = []
    r = _exe([_settle_frame(with_button=False)], taps).attempt(round_no=2)
    assert r.status == "no_click" and not taps
    assert "等待重拍" in r.reason


def test_button_without_scene_rejected():
    img = _new_round_frame()
    img[BOX[1]:BOX[3], BOX[0]:BOX[2]] = _button()
    taps = []
    r = _exe([img], taps).attempt(round_no=2)
    # 已回到明亮手牌带，default_new_round_probe 可确认牌桌已发牌；无需再点。
    assert r.status == "advanced_without_click" and not taps


def test_recheck_moved_or_gone_no_click():
    taps = []
    r = _exe([_settle_frame(), _settle_frame(with_button=False)], taps).attempt(round_no=2)
    assert r.status == "no_click" and not taps


def test_revocation_during_recheck_sleep_prevents_the_tap():
    taps = []
    cancelled = Event()

    def revoke(_seconds):
        cancelled.set()

    exe = rt.RoundTransitionExecutor(
        capture=Seq([_settle_frame(), _settle_frame()]),
        tap=lambda x, y: taps.append((x, y)), sleep=revoke,
        template=_button(), post_polls=1)

    result = exe.attempt(round_no=2, cancel_event=cancelled)

    assert result.status == "cancelled"
    assert result.clicked is False
    assert "after_recheck_sleep" in result.reason
    assert taps == []


def test_revocation_after_committed_tap_only_verifies_and_never_advances():
    taps, advanced = [], []
    cancelled = Event()

    def tap(x, y):
        taps.append((x, y))
        cancelled.set()

    exe = rt.RoundTransitionExecutor(
        capture=Seq([_settle_frame(), _settle_frame(), _new_round_frame()]),
        tap=tap, sleep=lambda _seconds: None, template=_button(),
        post_polls=1, on_advanced=advanced.append)

    result = exe.attempt(round_no=2, cancel_event=cancelled)

    assert result.status == "cancelled_after_click"
    assert result.clicked is True
    assert len(taps) == 1
    assert advanced == []
    assert result.evidence["post"]


def test_outer_match_then_fresh_score_drop_is_not_terminal(monkeypatch):
    """复现 17:06:39：外层 0.9755 命中，fresh 帧瞬降 0.683。

    这只能等待下一帧重拍，绝不能被解释成比赛结束。
    """
    taps = []
    exe = _exe([_settle_frame()], taps)
    probes = iter([
        (False, {
            "button": {"ok": False, "score": 0.6831,
                       "reason": "template_score_low:0.683"},
            "scene": {"ok": True, "waiting_room": False,
                      "reason": "round_summary_scene"},
        }),
    ])
    monkeypatch.setattr(exe, "_gates", lambda _image: next(probes))
    monkeypatch.setattr(exe, "_probe",
                        lambda _image: (False, "still_settled_or_unknown"))

    result = exe.attempt(round_no=1)

    assert result.status == "no_click"
    assert result.clicked is False
    assert "0.683" in result.reason
    assert not taps


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


def test_button_and_old_summary_gone_means_accepted_waiting_not_halted():
    """复现 17:13 / 17:28：对手未就绪时可长时间留在绿桌过场。

    旧按钮与旧结算页连续多帧消失，已足以确认本端点击被接受；
    但没有发牌正证据，所以不调 on_advanced，也不把执行器锁死。
    """
    taps, advanced = [], []
    wait = _waiting_peer_frame()
    exe = _exe([_settle_frame(), _settle_frame(), wait, wait, wait], taps,
               on_advanced=advanced.append)

    result = exe.attempt(round_no=4)

    assert result.status == "accepted_waiting"
    assert result.clicked is True
    assert exe.halted is False
    assert len(taps) == 1
    assert advanced == []
    assert result.evidence["post"][-1]["departed_streak"] == 3
    # 同一局仍不许因为未发牌而补第二击。
    assert exe.attempt(round_no=4).status == "no_click"
    assert len(taps) == 1


def test_button_gone_but_old_summary_remains_is_still_uncertain():
    """只丢按钮不够：旧结算页仍在时必须 fail-closed。"""
    taps = []
    old_summary = _settle_frame(with_button=False)
    exe = _exe([_settle_frame(), _settle_frame(), old_summary], taps)

    result = exe.attempt(round_no=5)

    assert result.status == "post_verify_uncertain"
    assert exe.halted is True
    assert len(taps) == 1


@pytest.mark.parametrize("frame_path", [
    REAL_POST_CLICK_WAIT_5556,
    REAL_POST_CLICK_WAIT_5554,
])
def test_real_post_click_wait_frames_are_positive_table_transitions(frame_path):
    """17:13 请财神与 17:28 骰子前绿桌帧是本端已就绪的正证据。"""
    if not frame_path.is_file():
        pytest.skip(f"real frame missing: {frame_path}")
    image = cv2.imdecode(
        np.fromfile(str(frame_path), np.uint8), cv2.IMREAD_COLOR)

    evidence = rt.post_click_transition_scene(image)

    assert evidence["ok"] is True
    assert evidence["reason"] == "post_click_table_transition"
    assert evidence["green_ratio"] >= rt.POST_TABLE_GREEN_MIN_RATIO


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
    template = rt.load_template()
    assert template is not None
    ev = rt.find_next_round_button(img, template)
    scene = rt.round_end_scene(img)
    assert not (ev["ok"] and scene["ok"])


@pytest.mark.skipif(not REAL_NEXT_ROUND.is_file(), reason="real next-round frame missing")
def test_real_next_round_frame_hits_tracked_template_and_summary_scene():
    template = rt.load_template()
    assert template is not None
    assert template.shape[:2] == (115, 295)
    img = cv2.imdecode(
        np.fromfile(str(REAL_NEXT_ROUND), np.uint8), cv2.IMREAD_COLOR)
    button = rt.find_next_round_button(img, template)
    scene = rt.round_end_scene(img)
    assert button["ok"] is True
    assert button["score"] >= rt.TEMPLATE_MIN_SCORE
    assert button["box"] == [1170, 780, 1465, 895]
    assert button["orange_ratio"] >= rt.ORANGE_MIN_RATIO
    assert scene["ok"] is True
    assert scene["summary_overlay"] is True
    assert scene["reason"] == "round_summary_scene"


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
