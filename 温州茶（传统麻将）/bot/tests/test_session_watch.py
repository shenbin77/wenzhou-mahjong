# -*- coding: utf-8 -*-
"""session_watch 的离线测试：看门狗分类 / 登录与房卡证据门 / 停顿熔断 /
自动战报 / 场次管家骨架。

全部离线：只读 _takeover / 采集 里的存帧与真台账，零 ADB、零进程操作。
真实夹具缺失时 skip（与 test_session_supervisor 的约定一致）。
合成帧/合成台账走 tmp_path，不写任何生产目录。

当前出厂状态的两条「诚实断言」（标定变化后需同步改）：
- templates/login/ 尚不存在 → detect_login_screen 恒 no_templates，
  分类器不许凭颜色猜登录页（正向路径用合成模板验证）。
- templates/room_hud_digits 缺 3/8 → 真大厅帧房卡读数恒 None
  （fail-closed），读数管线用合成全字形集验证。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import session_watch as sw  # noqa: E402
from session_watch import (  # noqa: E402
    CLICK_WHITELIST,
    EXECUTION_ENABLED,
    NOMINAL_TARGETS,
    ManagerState,
    SessionManager,
    StallPolicy,
    WatchResult,
    WatchState,
    analyze_stalls,
    build_match_report,
    build_match_report_root,
    classify_watch,
    detect_waiting_room,
    detect_login_screen,
    extract_stall_occurrences,
    load_ledger_events,
    read_room_cards,
    room_cards_evidence,
    write_match_report,
)

TAKEOVER = Path(r"D:\温州麻将\_takeover")
CORPUS = Path(r"D:\温州麻将\温州茶（传统麻将）\采集")
SETTLEMENT_FRAME = (
    CORPUS / "dev_auto_play" / "20261024_051634_4286f3" / "frames"
    / "20261024_051634_773578_before_87fd97.png"
)
LOBBY_FRAMES = [TAKEOVER / "shot_5556_lobby.png", TAKEOVER / "shot_5558_lobby.png"]
REAL_SESSION_5558 = CORPUS / "dev_auto_play" / "dual_20261025_030310" / "5558"
REAL_ROOT = CORPUS / "dev_auto_play" / "dual_20261025_030310"
WAITING_FRAMES = [
    Path(r"D:\温州麻将\采集\dev_auto_play\waiting_room_evidence_20261025")
    / "room_877158_waiting_5556_native.png",
    Path(r"D:\温州麻将\采集\dev_auto_play\waiting_room_evidence_20261025")
    / "room_877158_waiting_5558_native.png",
]

GREEN_BGR = (60, 150, 60)   # HSV H60/S153/V150：绿毡口径内
WARM_BGR = (30, 110, 230)   # HSV H12/S221/V230：大厅暖色口径内


def _frame(bgr) -> np.ndarray:
    return np.full((900, 1600, 3), bgr, dtype=np.uint8)


def _load(path: Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def _write_png(path: Path, img: np.ndarray) -> None:
    """写 PNG（中文路径安全：imencode + write_bytes，cv2.imwrite 写不了中文路径）。"""
    ok, buf = cv2.imencode(".png", img)
    assert ok
    path.write_bytes(buf.tobytes())


def _tick(ts: str, status: str = "wait", code: str | None = None, **extra) -> dict:
    item = {"ts": ts, "event": "idle", "status": status}
    if code is not None:
        item["turn_gate"] = {"code": code, "state": "", "reason": ""}
    item.update(extra)
    return item


def _stop(ts: str, gate: str, **extra) -> dict:
    item = {"ts": ts, "event": "stop", "gate": gate, "reason": "synthetic"}
    item.update(extra)
    return item


def _header() -> dict:
    return {"serial": "emulator-9999", "session": "synthetic", "mode": "tea_2p",
            "dry_run": False, "enabled": True,
            "started_at": "2026-10-25T04:00:00+08:00"}


# ---------------------------------------------------------------------------
# 看门狗分类器
# ---------------------------------------------------------------------------

def test_classify_no_input_is_unknown() -> None:
    result = classify_watch()
    assert result.state is WatchState.UNKNOWN
    json.dumps(result.to_dict(), ensure_ascii=False)


def test_classify_wrong_size_frame_is_unknown() -> None:
    result = classify_watch(np.full((100, 100, 3), GREEN_BGR, dtype=np.uint8))
    assert result.state is WatchState.UNKNOWN
    assert result.evidence["frame_error"] == "frame_size_unverified"


def test_classify_green_frame_is_in_game(tmp_path: Path) -> None:
    result = classify_watch(_frame(GREEN_BGR),
                            login_templates_dir=tmp_path / "no_login")
    assert result.state is WatchState.IN_GAME
    assert result.room_cards is None
    assert not [a for a in result.alerts if a["type"] == "login_screen"]


def test_waiting_room_requires_tea_2p_rule_and_large_central_button(
        monkeypatch, tmp_path: Path) -> None:
    image = _frame(GREEN_BGR)
    cv2.rectangle(image, (663, 525), (936, 624), (20, 170, 245), -1)
    monkeypatch.setattr(sw, "score_tea_player_count",
                        lambda _image: (2, .99, .22))

    evidence = detect_waiting_room(image)
    result = classify_watch(image, login_templates_dir=tmp_path / "no_login")

    assert evidence["matched"] is True
    assert evidence["start_button_area"] >= sw.WAITING_BUTTON_MIN_AREA
    assert result.state is WatchState.WAITING_ROOM
    assert result.evidence["screen"] == "tea_2p_waiting_room"


@pytest.mark.parametrize("path", WAITING_FRAMES)
def test_classify_real_two_end_waiting_room_frames(path: Path) -> None:
    if not path.exists():
        pytest.skip("waiting-room fixture frame missing")
    result = classify_watch(_load(path))
    assert result.state is WatchState.WAITING_ROOM
    assert result.evidence["waiting_room"]["player_count"] == 2
    assert result.evidence["waiting_room"]["start_button_area"] >= 15000


def test_classify_warm_frame_is_lobby_with_cards_none(tmp_path: Path) -> None:
    result = classify_watch(_frame(WARM_BGR),
                            login_templates_dir=tmp_path / "no_login")
    assert result.state is WatchState.LOBBY
    # 出厂数字模板不全 → 房卡读不出（None），但不影响大厅分类。
    assert result.room_cards is None
    assert result.evidence["room_cards"]["status"] == "digit_templates_incomplete"


def test_classify_real_settlement_frame() -> None:
    if not SETTLEMENT_FRAME.exists():
        pytest.skip("settlement fixture frame missing")
    result = classify_watch(_load(SETTLEMENT_FRAME))
    assert result.state is WatchState.SETTLEMENT


@pytest.mark.parametrize("path", LOBBY_FRAMES)
def test_classify_real_lobby_frames(path: Path) -> None:
    if not path.exists():
        pytest.skip("lobby fixture frame missing")
    result = classify_watch(_load(path))
    assert result.state is WatchState.LOBBY
    assert result.room_cards is None  # 模板缺 3/8，78/8 都不许猜


def test_classify_ledger_only_real_session_is_in_game() -> None:
    if not (REAL_SESSION_5558 / "actions.jsonl").exists():
        pytest.skip("real session ledger missing")
    events = load_ledger_events(REAL_SESSION_5558)
    result = classify_watch(None, events)
    assert result.state is WatchState.IN_GAME
    assert result.evidence["source"] == "ledger"


def test_classify_ledger_terminal_gate_turn_stop_is_settlement() -> None:
    events = [_header(), _tick("2026-10-25T04:00:01+08:00"),
              _stop("2026-10-25T04:01:00+08:00", "gate_turn", status="abstain")]
    result = classify_watch(None, events)
    assert result.state is WatchState.SETTLEMENT


def test_classify_ledger_terminal_other_stop_is_unknown_but_flagged() -> None:
    events = [_header(), _tick("2026-10-25T04:00:01+08:00"),
              _stop("2026-10-25T04:01:00+08:00", "tap_aborted")]
    result = classify_watch(None, events)
    assert result.state is WatchState.UNKNOWN
    assert result.evidence["loop_stopped"] is True
    assert result.evidence["stop_gate"] == "tap_aborted"


def test_classify_card_exhausted_alert_from_ledger() -> None:
    events = [_header(), _stop("2026-10-25T04:01:00+08:00", "card_exhausted")]
    result = classify_watch(None, events)
    assert any(a["type"] == "room_cards_exhausted" for a in result.alerts)


# ---------------------------------------------------------------------------
# 登录页检测（无模板不猜；合成模板验正向路径）
# ---------------------------------------------------------------------------

def _pattern(seed: int, shape=(80, 120)) -> np.ndarray:
    return np.random.RandomState(seed).randint(0, 256, shape).astype(np.uint8)


def _make_login_templates(tmp_path: Path) -> Path:
    tdir = tmp_path / "login_tpl"
    tdir.mkdir()
    _write_png(tdir / "login_main.png", _pattern(7))
    _write_png(tdir / "login_other.png", _pattern(8))
    return tdir


def test_login_no_templates_is_not_matched(tmp_path: Path) -> None:
    evidence = detect_login_screen(_frame(WARM_BGR), tmp_path / "missing_dir")
    assert evidence["matched"] is False
    assert evidence["status"] == "no_templates"


def test_login_synthetic_template_hit_and_critical_alert(tmp_path: Path) -> None:
    tdir = _make_login_templates(tmp_path)
    frame = _frame(WARM_BGR)
    patch = _pattern(7)
    frame[300:380, 400:520] = cv2.cvtColor(patch, cv2.COLOR_GRAY2BGR)
    evidence = detect_login_screen(frame, tdir)
    assert evidence["matched"] is True
    assert evidence["template"] == "login_main.png"

    result = classify_watch(frame, login_templates_dir=tdir)
    assert result.state is WatchState.LOGIN_SCREEN
    alert = next(a for a in result.alerts if a["type"] == "login_screen")
    assert alert["severity"] == "critical"


def test_login_template_absent_in_frame_is_not_matched(tmp_path: Path) -> None:
    tdir = _make_login_templates(tmp_path)
    evidence = detect_login_screen(_frame(WARM_BGR), tdir)
    assert evidence["matched"] is False


# ---------------------------------------------------------------------------
# 房卡读数（出厂模板不全 → None；合成全字形集验管线）
# ---------------------------------------------------------------------------

def test_room_cards_shipped_templates_incomplete() -> None:
    evidence = room_cards_evidence(_frame(WARM_BGR))
    assert evidence["value"] is None
    assert evidence["status"] == "digit_templates_incomplete"
    assert evidence["template_missing"] == ["3", "8"]
    assert read_room_cards(_frame(WARM_BGR)) is None


def _make_digit_templates(tmp_path: Path, duplicate: str | None = None) -> Path:
    tdir = tmp_path / "digits"
    tdir.mkdir()
    index = {}
    for d in range(10):
        seed = 100 + (4 if duplicate == str(d) else d)
        glyph = np.random.RandomState(seed).randint(0, 256, (30, 22)).astype(np.uint8)
        name = f"d{d}_0.png"
        _write_png(tdir / name, glyph)
        index[str(d)] = [name]
    (tdir / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return tdir


def _paste_glyph(frame: np.ndarray, digit: int, x: int, y: int = 80) -> None:
    glyph = np.random.RandomState(100 + digit).randint(0, 256, (30, 22)).astype(np.uint8)
    frame[y:y + 30, x:x + 22] = cv2.cvtColor(glyph, cv2.COLOR_GRAY2BGR)


def test_room_cards_synthetic_two_digits(tmp_path: Path) -> None:
    tdir = _make_digit_templates(tmp_path)
    frame = _frame(WARM_BGR)
    _paste_glyph(frame, 4, 300)
    _paste_glyph(frame, 2, 325)  # 间距 3px
    assert read_room_cards(frame, tdir) == 42


def test_room_cards_synthetic_single_digit(tmp_path: Path) -> None:
    tdir = _make_digit_templates(tmp_path)
    frame = _frame(WARM_BGR)
    _paste_glyph(frame, 8, 300)
    assert read_room_cards(frame, tdir) == 8


def test_room_cards_synthetic_blank_band_is_none(tmp_path: Path) -> None:
    tdir = _make_digit_templates(tmp_path)
    evidence = room_cards_evidence(_frame(WARM_BGR), tdir)
    assert evidence["value"] is None
    assert evidence["status"] == "no_digit_above_gate"


def test_room_cards_synthetic_long_run_rejected(tmp_path: Path) -> None:
    tdir = _make_digit_templates(tmp_path)
    frame = _frame(WARM_BGR)
    for i, digit in enumerate([9, 7, 4, 3, 3, 1]):  # 6 位串（口令状）不许当房卡
        _paste_glyph(frame, digit, 300 + 25 * i)
    evidence = room_cards_evidence(frame, tdir)
    assert evidence["value"] is None
    assert evidence["status"] == "run_too_long"


def test_room_cards_synthetic_ambiguous_digit_rejected(tmp_path: Path) -> None:
    tdir = _make_digit_templates(tmp_path, duplicate="5")  # 5 与 4 字形相同
    frame = _frame(WARM_BGR)
    _paste_glyph(frame, 4, 300)
    evidence = room_cards_evidence(frame, tdir)
    assert evidence["value"] is None
    assert evidence["status"] == "ambiguous_digit"


# ---------------------------------------------------------------------------
# 停顿熔断
# ---------------------------------------------------------------------------

def test_stalls_stop_categories_and_fuse() -> None:
    events = [_header()] + [
        _stop(f"2026-10-25T04:0{i}:00+08:00", "tap_aborted") for i in range(4)
    ]
    report = analyze_stalls(events)
    assert report["category_counts"] == {"stop:tap_aborted": 4}
    assert report["total_stalls"] == 4
    assert report["process_runs"] == 1
    assert report["relaunches"] == 0
    assert [a["category"] for a in report["fuse_alerts"]] == ["stop:tap_aborted"]
    json.dumps(report, ensure_ascii=False)


def test_stalls_at_threshold_no_fuse() -> None:
    events = [_header()] + [
        _stop(f"2026-10-25T04:0{i}:00+08:00", "gate_turn") for i in range(3)
    ]
    report = analyze_stalls(events)  # 默认阈值 3：超过才熔断，等于不熔断
    assert report["fuse_alerts"] == []


def test_stalls_custom_threshold() -> None:
    events = [_header()] + [
        _stop(f"2026-10-25T04:0{i}:00+08:00", "gate_turn") for i in range(2)
    ]
    report = analyze_stalls(events, StallPolicy(threshold=1))
    assert len(report["fuse_alerts"]) == 1


def test_stalls_abstain_runs_deduped() -> None:
    events = [_header()]
    events += [_tick(f"2026-10-25T04:00:0{i}+08:00", "abstain", "caishen_unknown")
               for i in range(5)]
    events += [_tick("2026-10-25T04:00:06+08:00", "wait")]
    events += [_tick(f"2026-10-25T04:00:1{i}+08:00", "abstain", "caishen_unknown")
               for i in range(5)]
    occurrences = extract_stall_occurrences(events)
    assert [o["category"] for o in occurrences] == [
        "abstain:caishen_unknown", "abstain:caishen_unknown"]
    assert all(o["run_length"] == 5 for o in occurrences)


def test_stalls_short_abstain_run_ignored() -> None:
    events = [_header()] + [
        _tick(f"2026-10-25T04:00:0{i}+08:00", "abstain", "caishen_unknown")
        for i in range(4)
    ]
    assert extract_stall_occurrences(events) == []


def test_stalls_relaunch_count_from_headers() -> None:
    events = [_header(), _tick("2026-10-25T04:00:01+08:00"),
              _header(), _tick("2026-10-25T05:00:01+08:00")]
    report = analyze_stalls(events)
    assert report["process_runs"] == 2
    assert report["relaunches"] == 1


def test_stalls_stop_line_with_abstain_status_counts_as_stop() -> None:
    events = [_header(), _stop("2026-10-25T04:01:00+08:00", "gate_turn",
                                status="abstain")]
    occurrences = extract_stall_occurrences(events)
    assert [o["category"] for o in occurrences] == ["stop:gate_turn"]


# ---------------------------------------------------------------------------
# 自动战报
# ---------------------------------------------------------------------------

def _write_synthetic_session(tmp_path: Path) -> Path:
    session = tmp_path / "sess_9999"
    session.mkdir()
    rows = [
        _header(),
        {"ts": "2026-10-25T04:00:01+08:00", "event": "idle", "status": "wait",
         "frame": "F1.png",
         "frame_facts": {"round_no": 1, "round_total": 16,
                         "room_id": "584265", "caishen": "t5"}},
        {"ts": "2026-10-25T04:00:05+08:00", "event": "action", "status": "advise",
         "frame": "F2.png", "after_frame": "F3.png", "verified": True,
         "claim": {"action": ""},
         "frame_facts": {"round_no": 1, "round_total": 16,
                         "room_id": "584265", "caishen": "t5"}},
        {"ts": "2026-10-25T04:00:30+08:00", "event": "idle", "status": "abstain",
         "frame_facts": {"round_no": 2, "round_total": 16,
                         "room_id": "584265", "caishen": "dong"}},
        _stop("2026-10-25T04:01:00+08:00", "tap_aborted"),
    ]
    (session / "actions.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")
    return session


def test_match_report_synthetic_session(tmp_path: Path) -> None:
    session = _write_synthetic_session(tmp_path)
    report = build_match_report(session)
    assert report["serial"] == "emulator-9999"
    assert report["lines_total"] == 5
    assert report["ticks"] == 4
    assert report["events"] == {"idle": 2, "action": 1, "stop": 1}
    assert report["actions"]["count"] == 1
    assert report["actions"]["verified"] == 1
    assert report["rounds"]["round_nos"] == [1, 2]
    assert report["rounds"]["rounds_observed"] == 2
    assert report["rounds"]["room_ids"] == ["584265"]
    assert report["caishen_distribution"] == {"t5": 2, "dong": 1}
    assert report["caishen_unknown_ticks"] == 1  # stop 行无财神
    assert report["duration_s"] == 59.0
    assert report["evidence_frames"]["unique_refs"] == 3
    assert report["evidence_frames"]["samples"] == ["F1.png", "F2.png", "F3.png"]
    assert report["stalls"]["category_counts"] == {"stop:tap_aborted": 1}
    json.dumps(report, ensure_ascii=False)


def test_match_report_write_files(tmp_path: Path) -> None:
    session = _write_synthetic_session(tmp_path)
    out = tmp_path / "out"
    json_path, md_path = write_match_report(session, out)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["sessions"][session.name]["lines_total"] == 5
    md = md_path.read_text(encoding="utf-8")
    assert "战报" in md and "tap_aborted" in md


def test_match_report_real_session_5558() -> None:
    ledger = REAL_SESSION_5558 / "actions.jsonl"
    if not ledger.exists():
        pytest.skip("real session ledger missing")
    report = build_match_report(REAL_SESSION_5558)
    # 与独立解析（insp_ledger）对过的数字：251 行 = 头 1 + idle 247 + action 3。
    assert report["lines_total"] == 251
    assert report["events"].get("idle") == 247
    assert report["events"].get("action") == 3
    assert report["actions"]["count"] == 3
    assert report["stalls"]["process_runs"] == 1
    assert report["stalls"]["relaunches"] == 0
    assert report["serial"]
    # 现行台账 round_no 恒 None，局身份来自 round_seq（全场第 1 局）。
    assert report["rounds"]["round_nos"] == [1]
    assert report["rounds"]["rounds_observed"] == 1
    assert report["rounds"]["source"] == "round_seq"


def test_match_report_root_aggregates_both_serials() -> None:
    if not REAL_ROOT.is_dir():
        pytest.skip("real session root missing")
    report = build_match_report_root(REAL_ROOT)
    assert set(report["sessions"].keys()) == {"5556", "5558"}
    total = sum(s["lines_total"] for s in report["sessions"].values())
    assert report["aggregate"]["lines_total"] == total
    assert report["aggregate"]["sessions"] == 2
    json.dumps(report, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 场次管家骨架（只出 dry-run 计划）
# ---------------------------------------------------------------------------

def _watch(state: WatchState) -> WatchResult:
    return WatchResult(state)


def test_manager_full_cycle_sequence(tmp_path: Path) -> None:
    sink = tmp_path / "plans.jsonl"
    mgr = SessionManager(sink=sink)
    p1 = mgr.step(_watch(WatchState.SETTLEMENT))
    assert p1["action"] == "return_to_lobby" and p1["kind"] == "tap"
    assert mgr.state is ManagerState.SETTLEMENT
    p2 = mgr.step(_watch(WatchState.LOBBY))
    assert p2["action"] == "enter_table"
    assert mgr.state is ManagerState.ENTER_TABLE
    p3 = mgr.step(_watch(WatchState.IN_GAME))
    assert p3["action"] == "monitor"
    assert mgr.state is ManagerState.IN_GAME
    p4 = mgr.step(_watch(WatchState.SETTLEMENT))
    assert p4["action"] == "return_to_lobby"
    for plan in (p1, p2, p3, p4):
        assert plan["execute"] is False and plan["dry_run"] is True
        assert plan["execution_enabled"] is False
    lines = sink.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4
    assert all(json.loads(line)["execute"] is False for line in lines)


def test_manager_enter_table_retries_then_alert() -> None:
    mgr = SessionManager(max_enter_retries=1)
    assert mgr.step(_watch(WatchState.LOBBY))["action"] == "enter_table"
    assert mgr.step(_watch(WatchState.LOBBY))["action"] == "enter_table"  # 重试 1
    assert mgr.step(_watch(WatchState.LOBBY))["action"] == "alert_human"  # 超限


def test_manager_login_holds_and_alerts() -> None:
    mgr = SessionManager()
    plan = mgr.step(_watch(WatchState.LOGIN_SCREEN))
    assert plan["action"] == "alert_human"
    assert mgr.state is ManagerState.HOLD


def test_manager_waiting_room_defers_to_dedicated_session_entry() -> None:
    mgr = SessionManager()
    plan = mgr.step(_watch(WatchState.WAITING_ROOM))
    assert plan["action"] == "wait_for_session_entry"
    assert plan["kind"] == "none"
    assert plan["execute"] is False
    assert mgr.state is ManagerState.WAITING_ROOM


def test_manager_unknown_waits_without_state_change() -> None:
    mgr = SessionManager()
    plan = mgr.step(_watch(WatchState.UNKNOWN))
    assert plan["action"] == "wait"
    assert mgr.state is ManagerState.HOLD


def test_manager_whitelist_and_nominal_targets() -> None:
    assert set(CLICK_WHITELIST) == set(NOMINAL_TARGETS.keys())
    for target in NOMINAL_TARGETS.values():
        assert 0 <= target["x"] < 1600 and 0 <= target["y"] < 900


def test_module_has_no_execution_imports() -> None:
    source = Path(BOT_DIR / "session_watch.py").read_text(encoding="utf-8")
    for forbidden in ("import subprocess", "import adb", "os.system", "Popen(",
                      "ctypes", "pyautogui"):
        assert forbidden not in source
    assert EXECUTION_ENABLED is False
