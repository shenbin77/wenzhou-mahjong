"""The adviser keeps every capture local and never sends game input."""

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from assist_tea import analyze_manual_hand, analyze_snapshot, capture_and_analyze, import_and_analyze  # noqa: E402
from assist_window import TeaAssistWindow, TILE_CHOICES, same_tea_round, tea_round_fingerprint  # noqa: E402


FRAME = BOT_DIR.parent / "采集" / "2026-10-16" / "740195" / "r08" / "frame_02.png"
FOUR_PLAYER_FRAME = BOT_DIR.parent / "采集" / "2026-10-13" / "323028" / "r01" / "frame_02.png"
# 真实 UI 契约：组合框的值是 TILE_CHOICES 的 key（"9万 (w9)" 这种带后缀的形式）。
LEGACY_CAISHEN_PICK = next(key for key, label in TILE_CHOICES.items() if label == "w9")
# 旧版测试替身用的裸牌名：TILE_CHOICES 里不存在，用来验证"解析不出来也必须清空"。
UNPARSABLE_CAISHEN_PICK = "九万"


def test_real_two_player_frame_produces_visible_advice_without_changing_source(tmp_path):
    before = hashlib.sha256(FRAME.read_bytes()).hexdigest()
    marked = tmp_path / "advice.png"
    result = analyze_snapshot(
        FRAME, caishen_label="s9", confirmed_two_player=True,
        output_path=marked, log_dir=tmp_path / "logs",
    )
    assert result["status"] == "advise"
    assert result["dry_run"] is True
    assert len(result["hand"]) == 17
    assert result["suggested_boxes"] == [[6, 768, 95, 892]]
    image = cv2.imdecode(np.fromfile(str(marked), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None and tuple(image[768, 6]) == (0, 0, 255)
    assert hashlib.sha256(FRAME.read_bytes()).hexdigest() == before


def test_selected_caishen_conflicting_with_visible_marker_abstains(tmp_path):
    result = analyze_snapshot(
        FRAME, caishen_label="s7", confirmed_two_player=True,
        log_dir=tmp_path / "logs",
    )
    assert result["status"] == "abstain"
    assert result["caishen_observed"] == "s9"
    assert result["caishen_selected"] == "s7"
    assert "财神" in result["reason"]


def test_four_player_tea_uses_same_vision_with_separate_mode_gate(tmp_path):
    result = analyze_snapshot(
        FOUR_PLAYER_FRAME, caishen_label="", confirmed_four_player=True,
        output_path=tmp_path / "tea4.marked.png", log_dir=tmp_path / "logs",
    )
    assert result["mode"] == "tea_4p" and result["status"] == "advise"
    assert len(result["hand"]) == 17 and result["caishen"] == "s6"
    assert result["suggestion"] == "1万" and result["suggested_boxes"]
    wrong_mode = analyze_snapshot(
        FOUR_PLAYER_FRAME, caishen_label="", confirmed_two_player=True,
        log_dir=tmp_path / "wrong_mode_logs",
    )
    assert wrong_mode["status"] == "abstain" and "4 人局" in wrong_mode["reason"]
    assert wrong_mode["mode"] == "tea_2p"
    assert (tmp_path / "tea4.marked.png").is_file()
    imported = import_and_analyze(
        FOUR_PLAYER_FRAME, session_dir=tmp_path / "session",
        caishen_label="", confirmed_four_player=True,
    )
    assert imported["mode"] == "tea_4p" and imported["status"] == "advise"
    corrected = analyze_manual_hand(
        " ".join(imported["hand"]), caishen_label=imported["caishen"],
        confirmed_four_player=True, image_path=imported["image"],
        session_dir=tmp_path / "session", corrected_from=imported["record"],
    )
    assert corrected["mode"] == "tea_4p" and corrected["suggestion"] == imported["suggestion"]


def test_manual_caishen_substitutes_only_when_marker_is_unreadable(tmp_path):
    """财字小牌读不出时，手选财神是一等证据，可以继续；但必须标明来源为人工。

    放宽的前提是"手选只对当前这一局成立"：换局清空由
    ``test_new_round_drops_the_manual_caishen_pick`` 单独盯住。
    """
    image = cv2.imdecode(np.fromfile(FRAME, np.uint8), cv2.IMREAD_COLOR)
    image[348:423, 622:677] = 0
    obscured = tmp_path / "marker_hidden.png"
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    obscured.write_bytes(encoded.tobytes())
    result = analyze_snapshot(
        obscured, caishen_label="s9", confirmed_two_player=True,
        require_visible_caishen=True,
    )
    # 财神来源必须是人工，且没有画面分数；UI 据此标注"手选"
    assert result["caishen"] == "s9" and result["caishen_source"] == "manual"
    assert result["caishen_score"] is None
    # 不再因为"看不见财字"而弃权
    assert "必须看清" not in result.get("reason", "")
    assert result.get("advice_blocked_by") != "caishen_unconfirmed"
    assert result["status"] == "advise"


def test_unsupported_manual_caishen_has_distinct_reason():
    result = analyze_snapshot(FRAME, caishen_label="fa", confirmed_two_player=True)
    assert result["status"] == "abstain"
    assert result["mode"] == "tea_2p"
    assert "不支持所选财神" in result["reason"]


def test_unconfirmed_mode_and_incomplete_frame_cannot_offer_advice(tmp_path):
    with pytest.raises(ValueError, match="2 人局"):
        analyze_snapshot(
            FRAME, caishen_label="s7", confirmed_two_player=False,
            log_dir=tmp_path / "logs",
        )
    blank = tmp_path / "blank.png"
    ok, encoded = cv2.imencode(".png", np.zeros((900, 1600, 3), np.uint8))
    assert ok
    blank.write_bytes(encoded.tobytes())
    result = analyze_snapshot(
        blank, caishen_label="s7", confirmed_two_player=True,
        output_path=tmp_path / "blank.marked.png", log_dir=tmp_path / "logs",
    )
    assert result["status"] == "abstain"
    assert (tmp_path / "blank.marked.png").is_file()


def test_live_capture_checks_foreground_and_writes_append_only_record(tmp_path, monkeypatch):
    import assist_tea

    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"fake")
    commands = []
    image_bytes = FRAME.read_bytes()

    def fake_run(command, **kwargs):
        commands.append(command)
        if "dumpsys" in command:
            return SimpleNamespace(stdout=b"topResumedActivity=com.xm.zjgamecenter/org.cocos2dx.lua.AppActivity")
        if "screencap" in command:
            return SimpleNamespace(stdout=image_bytes)
        raise AssertionError(command)

    monkeypatch.setattr(assist_tea.subprocess, "run", fake_run)
    result = capture_and_analyze(
        adb_path=str(adb), serial="emulator-5556", session_dir=tmp_path / "session",
        caishen_label="s9", confirmed_two_player=True,
    )
    assert result["status"] == "advise"
    assert result["image_sha256"] == hashlib.sha256(image_bytes).hexdigest()
    assert Path(result["image"]).read_bytes() == image_bytes
    assert json.loads(Path(result["record"]).read_text(encoding="utf-8")) == result
    assert all("tap" not in command and "swipe" not in command for command in commands)


def test_live_capture_rejects_other_foreground_before_screenshot(tmp_path, monkeypatch):
    import assist_tea

    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"fake")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout=b"topResumedActivity=com.example.other/.Main")

    monkeypatch.setattr(assist_tea.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="游戏不在模拟器前台"):
        capture_and_analyze(
            adb_path=str(adb), serial="emulator-5556", session_dir=tmp_path / "session",
        )
    assert len(commands) == 1
    assert not (tmp_path / "session").exists()


def test_imported_frame_and_manual_correction_keep_separate_records(tmp_path):
    before = hashlib.sha256(FRAME.read_bytes()).hexdigest()
    imported = import_and_analyze(
        FRAME, session_dir=tmp_path / "session", caishen_label="",
        confirmed_two_player=True,
    )
    assert imported["status"] == "advise"
    assert imported["caishen"] == "s9" and imported["caishen_source"] == "image"
    assert imported["source"] == "imported_image"
    assert imported["image_sha256"] == before
    assert Path(imported["image"]).read_bytes() == FRAME.read_bytes()
    assert json.loads(Path(imported["record"]).read_text(encoding="utf-8")) == imported

    corrected = analyze_manual_hand(
        " ".join(imported["hand"]), caishen_label=imported["caishen"],
        confirmed_two_player=True, session_dir=tmp_path / "session",
        image_path=imported["image"], corrected_from=imported["record"],
    )
    assert corrected["status"] == "advise"
    assert corrected["source"] == "manual_correction"
    assert corrected["suggested_boxes"] == []
    assert corrected["corrected_from"] == imported["record"]
    assert json.loads(Path(corrected["record"]).read_text(encoding="utf-8")) == corrected
    assert hashlib.sha256(FRAME.read_bytes()).hexdigest() == before


def test_manual_correction_rejects_unconfirmed_or_impossible_hand(tmp_path):
    labels = "w2 w4 w5 w7 s1 s1 s3 s6 s6 s7 s7 t1 t2 t8 t8 t8 t5"
    with pytest.raises(ValueError, match="2 人局"):
        analyze_manual_hand(labels, caishen_label="s7", confirmed_two_player=False)
    with pytest.raises(ValueError, match="16 或 17"):
        analyze_manual_hand("w1 w2", caishen_label="s7", confirmed_two_player=True)
    with pytest.raises(ValueError, match="不支持"):
        analyze_manual_hand(labels.replace("w2", "hongzhong"),
                            caishen_label="s7", confirmed_two_player=True)
    with pytest.raises(ValueError, match="必须关联"):
        analyze_manual_hand(labels, caishen_label="s7", confirmed_two_player=True)
    obscured = (BOT_DIR.parent / "采集" / "2026-10-19" / "984226" /
                "r02" / "candidates" / "candidate_054.png")
    with pytest.raises(ValueError, match="未确认茶 2 人及无中发规则"):
        analyze_manual_hand(labels, caishen_label="s7", confirmed_two_player=True,
                            image_path=obscured)
    with pytest.raises(ValueError, match="所选财神"):
        analyze_manual_hand(labels, caishen_label="s7", confirmed_two_player=True,
                            image_path=FRAME)
    with pytest.raises(ValueError):
        analyze_manual_hand(" ".join(["w1"] * 5 + labels.split()[5:]),
                            caishen_label="s7", confirmed_two_player=True,
                            session_dir=tmp_path)
    assert not list(tmp_path.glob("manual_*.json"))


class _Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def test_continuous_tea_adopts_next_round_when_caishen_is_automatic(tmp_path):
    r01 = BOT_DIR.parent / "采集" / "2026-10-19" / "833667" / "r01"
    r02 = BOT_DIR.parent / "采集" / "2026-10-19" / "833667" / "r02"
    assert tea_round_fingerprint(r01 / "frame_15s.png") == tea_round_fingerprint(r01 / "frame_20s.png")
    assert tea_round_fingerprint(r01 / "frame_20s.png") != tea_round_fingerprint(r02 / "frame_04s_complete.png")
    assert not same_tea_round(tea_round_fingerprint(r01 / "frame_20s.png"),
                              tea_round_fingerprint(r02 / "frame_04s_complete.png"))
    image = cv2.imdecode(np.fromfile(r01 / "frame_20s.png", np.uint8), cv2.IMREAD_COLOR)
    image[60, 20] = (255, 255, 255) if int(image[60, 20].mean()) < 128 else (0, 0, 0)
    noisy = tmp_path / "same_round_one_pixel_noise.png"
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    noisy.write_bytes(encoded.tobytes())
    assert same_tea_round(tea_round_fingerprint(r01 / "frame_20s.png"),
                          tea_round_fingerprint(noisy))

    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window.mode = _Value("温州茶 · 2人")
    window.continuous = True
    window.continuous_round = None
    window.timer = None
    window.status = _Value("")
    window.live_info = _Value("")
    window.hand = _Value("旧手牌")
    window.current = {"status": "advise"}
    window.preview = object()
    shown = []
    window.picture = SimpleNamespace(configure=lambda **kwargs: shown.append(kwargs))

    assert window._accept_continuous_result({"status": "advise", "image": str(r01 / "frame_20s.png")})
    assert window._accept_continuous_result({"status": "advise", "image": str(noisy)})
    assert window.continuous is True
    assert window._accept_continuous_result({"status": "advise", "image": str(r01 / "frame_15s.png")})
    # 同一局 abstain：只撤销建议，保留局基线与界面证据，不闪白。
    assert window._accept_continuous_result({
        "status": "abstain", "reason": "暂时遮挡",
        "image": str(r01 / "frame_20s.png"),
    })
    assert window.continuous is True
    assert window.current == {"status": "advise"}
    assert shown == []
    assert window.continuous_round == tea_round_fingerprint(r01 / "frame_20s.png")
    # abstain + 新局 HUD：必须先做换局检测，不能因弃权提前 return。
    assert not window._accept_continuous_result({
        "status": "abstain", "reason": "新局过渡",
        "image": str(r02 / "frame_04s_complete.png"),
    })
    assert window.continuous is True and window.current is None
    assert window.continuous_round == tea_round_fingerprint(
        r02 / "frame_04s_complete.png")
    assert "继续跟随" in window.status.get()
    assert window.hand.get() == "手牌：—" and shown[-1] == {"image": ""}
    # 非 abstain 路径：wait 无图 fail-closed；advise 同局放行、换局跳过渡帧。
    assert window._accept_continuous_result({"status": "advise", "image": str(r02 / "frame_04s_complete.png")})
    assert not window._accept_continuous_result({"status": "wait", "image": None})
    assert window.continuous is True and window.current is None
    assert window._accept_continuous_result({"status": "advise", "image": str(r02 / "frame_04s_complete.png")})
    assert not window._accept_continuous_result({"status": "advise", "image": str(r01 / "frame_20s.png")})
    assert window.continuous is True and window.current is None
    assert window.continuous_round == tea_round_fingerprint(r01 / "frame_20s.png")


def test_live_avatar_covering_room_hud_does_not_fake_a_new_round():
    frame = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261024_002903"
             / "live" / "tea_2p" / "20261024_003231_828201_4b43a1.png")
    if not frame.is_file():
        pytest.skip("实战头像遮挡样本缺失")
    assert tea_round_fingerprint(frame) is None


def test_live_abstain_keeps_current_frame_for_evidence():
    """临时弃权只撤销建议，不能把实时主界面的当前证据清空。"""
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window.mode = _Value("温州茶 · 2人")
    window.continuous = True
    window.live_info = _Value("")
    window._reset_round_display = lambda reason: pytest.fail(
        f"弃权帧不应重置主界面: {reason}")

    assert window._accept_continuous_result({
        "status": "abstain", "image": "current-live-frame.png",
        "hand": ["w1", "w2"],
    })
    assert "保留当前识牌证据" in window.live_info.get()


def test_ready_requires_binding_and_passed_mode_check():
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window.mode = _Value("温州茶 · 2人")
    window.account = _Value("玩家1")
    window.serial = _Value("emulator-5556")
    window.caishen = _Value("")
    window.status = _Value("")
    window._binding = None
    window._check_state = "unchecked"
    window._check_mode_key = None
    assert not window._ready()
    assert "绑定" in window.status.get()
    window._binding = {
        "account": "玩家1", "serial": "emulator-5556", "mode": "tea_2p"}
    window._check_state = "match"
    window._check_mode_key = "tea_2p"
    window._ensure_recorder = lambda: None
    assert window._ready()
    window.caishen.set(next(iter(TILE_CHOICES)))
    assert window._ready()


def test_mode_check_revoked_drops_inflight_advice():
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window.mode = _Value("温州茶 · 4人")
    window.caishen = _Value("")
    window.serial = _Value("emulator-5556")
    window._check_state = "match"
    window._check_mode_key = "tea_4p"
    window.future_context = window._context()
    window._check_state = "unknown"
    window.future = SimpleNamespace(done=lambda: True, result=lambda: {"status": "advise"})
    window.capture_started = None
    window.continuous = True
    window.continuous_round = "old"
    window.timer = None
    window.live_info = _Value("")
    window.status = _Value("")
    window.current = {"status": "advise"}
    window.hand = _Value("旧手牌")
    window.preview = object()
    shown = []
    window.picture = SimpleNamespace(configure=lambda **kwargs: shown.append(kwargs))
    window._poll()
    assert window.current is None and window.continuous is False
    assert window.hand.get() == "手牌：—" and shown[-1] == {"image": ""}
    assert "设置已变化" in window.status.get()


def test_unreadable_caishen_still_exposes_tile_evidence_but_no_advice(tmp_path):
    """财神不可用时仍展示识牌证据，但必须弃权且不产生建议。"""
    image = cv2.imdecode(np.fromfile(str(FRAME), np.uint8), cv2.IMREAD_COLOR)
    image[348:423, 622:677] = 0
    obscured = tmp_path / "caishen_hidden.png"
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    obscured.write_bytes(encoded.tobytes())
    marked = tmp_path / "evidence.png"
    result = analyze_snapshot(
        obscured, caishen_label="", confirmed_two_player=True,
        output_path=marked, log_dir=tmp_path / "logs",
    )
    assert result["status"] == "abstain"
    assert result["advice_blocked_by"] == "caishen_unconfirmed"
    assert "不给建议" in result["reason"]
    assert "suggestion" not in result and "shanten" not in result
    assert len(result["hand"]) == 17
    assert len(result["vision_diagnostics"]) == 17
    assert result["hand_ids"]
    assert marked.is_file()


def test_unreadable_caishen_still_exposes_tile_evidence_but_no_advice(tmp_path):
    """财神不可用时仍展示识牌证据，但必须弃权且不产生建议。"""
    image = cv2.imdecode(np.fromfile(str(FRAME), np.uint8), cv2.IMREAD_COLOR)
    image[348:423, 622:677] = 0
    obscured = tmp_path / "caishen_hidden.png"
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    obscured.write_bytes(encoded.tobytes())
    marked = tmp_path / "evidence.png"
    result = analyze_snapshot(
        obscured, caishen_label="", confirmed_two_player=True,
        output_path=marked, log_dir=tmp_path / "logs",
    )
    assert result["status"] == "abstain"
    assert result["advice_blocked_by"] == "caishen_unconfirmed"
    assert "不给建议" in result["reason"]
    assert "suggestion" not in result and "shanten" not in result
    assert len(result["hand"]) == 17
    assert len(result["vision_diagnostics"]) == 17
    assert result["hand_ids"]
    assert marked.is_file()


def test_unknown_caishen_reports_none_instead_of_empty_string(tmp_path):
    """财神既无画面来源也无人工选定时，三个字段统一为 None，不混用空串。

    与 live_capture.observed_caishen 的既有写法对齐；钉死口径，避免后续
    有人在 "" 与 None 之间来回改动。
    """
    image = cv2.imdecode(np.fromfile(str(FRAME), np.uint8), cv2.IMREAD_COLOR)
    image[348:423, 622:677] = 0
    obscured = tmp_path / "caishen_hidden.png"
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    obscured.write_bytes(encoded.tobytes())
    result = analyze_snapshot(
        obscured, caishen_label="", confirmed_two_player=True,
        log_dir=tmp_path / "logs",
    )
    assert result["status"] == "abstain"
    assert result["caishen"] is None
    assert result["caishen_source"] is None
    assert result["caishen_score"] is None


def test_lossless_live_frame_reports_caishen_after_meld_recognition():
    """旧副露实战帧现在可以给出建议，同时保持画面财神来源。"""
    frame = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261023_201640" /
             "live" / "tea_2p" / "20261023_205345_749900_23750c.png")
    if not frame.exists():
        pytest.skip("实战截屏样本已不在此机器上")
    result = analyze_snapshot(frame, caishen_label="", confirmed_two_player=True)
    assert result["status"] == "advise"
    assert "caishen_unconfirmed" not in (result.get("advice_blocked_by") or "")
    assert result["caishen"] == "s4"
    assert result["caishen_source"] == "image"
    assert result["caishen_score"] >= .98


def test_live_triplet_and_new_two_circle_caishen_are_read_from_native_frames():
    """用户指出的漏读九筒与二筒财神，均以原生截图回归验证。"""
    live = BOT_DIR.parent / "采集" / "assist_sessions"
    triplet = live / "diagnostics" / "codex_live_20261023_233630.png"
    two_circle = (live / "20261023_225827" / "live" / "tea_2p" /
                  "20261023_234222_268552_4a7b6e.png")
    if not triplet.exists() or not two_circle.exists():
        pytest.skip("本机实战截图样本缺失")
    from tile_vision import TileVision
    image = cv2.imdecode(np.fromfile(str(triplet), np.uint8), cv2.IMREAD_COLOR)
    vision = TileVision(template_dir=BOT_DIR.parent / "采集" / "templates" / "tea_tiles")
    vision.caishen_label = "w2"
    vision.recognize_hand(image)
    assert vision.last_complete
    assert [row[0] for row in vision.last_melds] == [29, 29, 29, 1, 1, 1]
    # 当前真实语义：t9 实战模板入库后（manifest 36→39，2026-10-24 02:26），
    # 本帧九筒首轮就 0.9212–0.9698、margin 0.44+，直接 accepted，**不再进**
    # recover_triplet 兜底分支，所以 exposed 组第一条的 reason 是 "accepted"。
    # 兜底分支本身仍然存在（tile_vision.py 用 f'{region}_triplet_correlated'
    # 拼字符串，整串字面量在源码里 grep 不到），由
    # test_weak_tile_recover_path.py（降分必须恢复 + 强牌不走兜底）和
    # test_tile_meld_seg.py::test_two_weak_tiles_are_not_recovered_by_mere_triplet
    # （同组弱两张不许恢复）分别锁住。
    assert vision.meld_diagnostics[0]["reason"] == "accepted"

    result = analyze_snapshot(two_circle, caishen_label="", confirmed_two_player=True)
    assert result["status"] == "advise"
    assert result["caishen"] == "t2"
    assert result["caishen_source"] == "image"
    assert len(result["hand_ids"]) == 17
    assert result["hand_ids"][0] == 22


def test_two_live_melds_reach_candidate_rows_and_recorder(tmp_path):
    """An actual 11-tile concealed hand still yields auditable advice."""
    frame = (BOT_DIR.parent / "采集" / "assist_sessions" / "diagnostics" /
             "codex_live_20261023_233630.png")
    if not frame.is_file():
        pytest.skip("本机副露实战截图样本缺失")
    from live_capture import analyze_bytes
    from recorder import AssistRecorder

    result = analyze_bytes(frame.read_bytes(), mode="tea_2p", image_path=frame,
                           log_dir=tmp_path / "logs")
    assert result["status"] == "advise"
    assert result["meld_count"] == 2
    assert result["melds"] == [[29, 29, 29], [1, 1, 1]]
    assert len(result["hand_ids"]) == 11
    assert len(result["candidates"]) == 5

    recorder = AssistRecorder(tmp_path / "records.jsonl", account="test")
    assert recorder.observe(result) is None
    assert recorder.observe(result) is None
    assert recorder.state == "suggested"
    assert recorder.active.payload["melds"] == result["melds"]


def test_new_round_drops_the_manual_caishen_pick():
    """换局必须清空手选财神，否则会拿上一局的财神算这一局。

    测试替身必须用**真实 UI 契约**：``assist_window.TILE_CHOICES`` 的 key 是
    ``f"{tile_name(vision_to_engine(tid))} ({label})"``，即组合框里真实显示、
    真实取值都是 ``"9万 (w9)"`` 这种带 ``(w9)`` 后缀的形式，不是裸 ``"九万"``。
    旧替身写裸名，``TILE_CHOICES.get`` 命中不到 → ``_caishen_selection`` 返回
    ``("", False)`` → 手选被误判成"自动财神"→ 换局不清空，测试因此失败。
    这是**测试替身过期**（测试 bug），不是源码 bug；下面的第二个窗口再用裸名
    验证源码侧的兜底：解析不出来的历史值也不许跨局留存（不崩、照样清空）。
    """
    r01 = BOT_DIR.parent / "采集" / "2026-10-19" / "833667" / "r01"
    r02 = BOT_DIR.parent / "采集" / "2026-10-19" / "833667" / "r02"
    window = TeaAssistWindow.__new__(TeaAssistWindow)
    window.mode = _Value("温州茶 · 2人")
    window.continuous = True
    window.continuous_round = tea_round_fingerprint(r01 / "frame_20s.png")
    window.timer = None
    window.status = _Value("")
    window.live_info = _Value("")
    window.caishen = _Value(LEGACY_CAISHEN_PICK)
    window.manual_caishen = _Value(True)
    window.current = {"status": "advise"}
    window.hand = _Value("旧手牌")
    window.preview = object()
    shown = []
    window.picture = SimpleNamespace(configure=lambda **kwargs: shown.append(kwargs))

    assert not window._accept_continuous_result(
        {"status": "advise", "image": str(r02 / "frame_04s_complete.png")})
    assert window.caishen.get() == ""
    assert not window.manual_caishen.get()
    # 手选已作废，后续取财神时不得再读到上一局的标签
    assert window._caishen_selection() == ("", False)
    assert "自动建立新局基线" in window.status.get()
    assert "手选财神已清空" in window.status.get()

    # 源码兜底：历史/旧版残留值（TILE_CHOICES 解析不出来）同样不许跨局留存，
    # 而且不许抛异常——否则一次换局就能让实时跟随整个线程崩掉。
    legacy = TeaAssistWindow.__new__(TeaAssistWindow)
    legacy.mode = _Value("温州茶 · 2人")
    legacy.continuous = True
    legacy.continuous_round = tea_round_fingerprint(r01 / "frame_20s.png")
    legacy.timer = None
    legacy.status = _Value("")
    legacy.live_info = _Value("")
    legacy.caishen = _Value(UNPARSABLE_CAISHEN_PICK)
    legacy.manual_caishen = _Value(True)
    legacy.current = {"status": "advise"}
    legacy.hand = _Value("旧手牌")
    legacy.preview = object()
    legacy.picture = SimpleNamespace(configure=lambda **kwargs: None)

    assert legacy._caishen_selection() == ("", False)      # 解析失败：取不到可用值
    assert not legacy._accept_continuous_result(
        {"status": "advise", "image": str(r02 / "frame_04s_complete.png")})
    assert legacy.caishen.get() == ""                      # 但换局照样清空
    assert not legacy.manual_caishen.get()
    assert "手选财神已清空" in legacy.status.get()
