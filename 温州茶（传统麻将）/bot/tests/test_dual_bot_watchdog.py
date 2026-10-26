from pathlib import Path
import json
import sys

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from dual_bot_watchdog import (  # noqa: E402
    NEVER_RESTART_GATES,
    SAFE_RESTART_GATES,
    build_status_payload,
    halt_section,
    restart_verdict,
    write_halted_report,
    write_status_json,
)


def test_watchdog_restarts_crash_without_terminal_ledger():
    assert restart_verdict({}, 1)[0]
    assert restart_verdict({"event": "idle"}, 1)[0]


def test_watchdog_restarts_only_preclick_anomaly_stop():
    ok, _ = restart_verdict({
        "event": "stop", "gate": "",
        "reason": "连续 3 次异常：点击前复核失败",
    }, 0)
    assert ok
    assert not restart_verdict({
        "event": "stop", "gate": "",
        "reason": "未知终止",
    }, 0)[0]


def test_watchdog_never_restarts_ambiguous_post_click_stop():
    for gate in ("tap_aborted", "tap_failed", "claim_unverified",
                 "claim_tap_failed", "selection_cancel_unverified"):
        ok, reason = restart_verdict({
            "event": "stop", "gate": gate, "reason": "unknown result",
        }, 0)
        assert not ok, reason


def test_watchdog_restarts_only_persisted_first_tap_recovery_case():
    ok, reason = restart_verdict({
        "event": "stop", "gate": "tap_aborted",
        "reason": (
            "点击或点后验证未通过：第一次点击后手牌计数在 3.0s 内未落定"
            "（样本 [12, 13, 0, 13]），状态未知，禁止第二击"),
    }, 0)
    assert ok, reason
    assert not restart_verdict({
        "event": "stop", "gate": "tap_aborted",
        "reason": "点击后张数未知",
    }, 0)[0]


# Ledger excerpt from w5_5556: the action window vanished between the decision
# and the tap, so no input event was ever emitted.
CLAIM_DISC_GONE_REASON = (
    "行动 pass 点击未通过：本帧 read_claims 没有实测到任何行动圆盘"
    "（未检出任何行动圆盘），拒绝点击"
)
# Ledger excerpt from w5_5558: the action window appeared before the tap.
ACTION_PANEL_PRE_REASON = (
    "点击或点后验证未通过：点击前检测到吃碰杠胡/过的行动界面，暂停自动出牌；"
    "框=[(1383, 552, 1483, 652)]"
)


def test_watchdog_restarts_pre_click_action_window_races():
    ok, reason = restart_verdict({
        "event": "stop", "gate": "claim_tap_aborted",
        "reason": CLAIM_DISC_GONE_REASON,
    }, 0)
    assert ok, reason
    assert "未发出点击" in reason

    ok, reason = restart_verdict({
        "event": "stop", "gate": "tap_aborted",
        "reason": ACTION_PANEL_PRE_REASON,
    }, 0)
    assert ok, reason
    assert "未发出点击" in reason


def test_watchdog_never_restarts_after_a_tap_was_emitted():
    post_click_cases = (
        ("tap_aborted", "点击或点后验证未通过：单亮摸切点击后无法验证，状态未知"),
        ("claim_tap_aborted", "claim 圆盘点击后仍存在，状态未知"),
        ("tap_aborted", "点击或点后验证未通过：胡提示面板恢复第二击后无法验证"),
        ("tap_aborted", "点击前复核通过但点击后无法验证"),
        ("claim_tap_aborted", "claim 圆盘点击后无法验证"),
    )
    for gate, reason_text in post_click_cases:
        ok, reason = restart_verdict({
            "event": "stop", "gate": gate, "reason": reason_text,
        }, 0)
        assert not ok, reason


def test_watchdog_still_restarts_persisted_second_tap_case():
    ok, reason = restart_verdict({
        "event": "stop", "gate": "tap_aborted",
        "reason": (
            "点击或点后验证未通过：第一次点击后手牌计数在 3.0s 内未落定"
            "（样本 [12, 13, 0, 13]），状态未知，禁止第二击"),
    }, 0)
    assert ok, reason
    assert "未发出点击" not in reason


# ---------------------------------------------------------------- 停机报告


def test_restart_policy_stays_narrow():
    """加法改造不得顺手扩大自动重启范围。"""
    assert SAFE_RESTART_GATES == {"", "unhandled_exception", "duration", "max_actions"}
    assert "tap_failed" in NEVER_RESTART_GATES
    assert "claim_unverified" in NEVER_RESTART_GATES


def test_halt_section_keeps_tail_gate_and_last_ledger_line(tmp_path):
    session = tmp_path / "emulator-5556_20261025_000000_g1"
    session.mkdir()
    (session / "stdout.log").write_text(
        "\n".join(f"line-{index}" for index in range(1, 31)), encoding="utf-8")
    (session / "stderr.log").write_text("boom\n", encoding="utf-8")
    ledger_line = json.dumps(
        {"ts": "2026-10-25T20:00:00+08:00", "event": "stop",
         "gate": "claim_unverified", "reason": "多亮白名单未确认"}, ensure_ascii=False)
    (session / "actions.jsonl").write_text(ledger_line + "\n", encoding="utf-8")

    text = halt_section("emulator-5556", session, 3, "claim_unverified",
                        "安全停机 claim_unverified", 1)

    assert "claim_unverified" in text
    assert "returncode: 3" in text
    assert "已重启次数: 1" in text
    assert "line-30" in text and "line-11" in text
    assert "line-10" not in text  # 只留最后 20 行
    assert "多亮白名单未确认" in text


def test_halt_section_tolerates_missing_files(tmp_path):
    session = tmp_path / "s"
    session.mkdir()
    text = halt_section("emulator-5558", session, None, "", "未列入白名单", 0)
    assert "actions.jsonl 为空或不存在" in text
    assert "stdout.log 为空或不存在" in text


def test_write_halted_report_merges_both_ends_atomically(tmp_path):
    path = write_halted_report(tmp_path, {
        "emulator-5556": halt_section("emulator-5556", tmp_path, 0, "tap_failed",
                                      "安全停机 tap_failed", 0),
        "emulator-5558": halt_section("emulator-5558", tmp_path, 1, "claim_unverified",
                                      "安全停机 claim_unverified", 2),
    })
    assert path == tmp_path / "halted_report.md"
    text = path.read_text(encoding="utf-8")
    assert "emulator-5556" in text and "emulator-5558" in text
    assert "双端看门狗停机报告" in text
    assert list(tmp_path.glob("halted_report.md.tmp*")) == []


# ---------------------------------------------------------------- 状态落盘


class _FakeChild:
    def __init__(self, payload):
        self._payload = payload

    def status(self):
        return self._payload


def test_status_payload_reports_halted_child_and_policy(tmp_path):
    child = _FakeChild({"serial": "emulator-5558", "alive": False,
                        "halted": True, "halt_gate": "claim_unverified"})
    payload = build_status_payload(tmp_path, [child], 0.0, 100.0, "halted",
                                   {"serial": "emulator-5558",
                                    "reason": "安全停机 claim_unverified"})
    assert payload["schema"] == 1
    assert payload["event"] == "halted"
    assert payload["children"][0]["halted"] is True
    assert payload["last_halt"]["serial"] == "emulator-5558"
    # 状态文件只是视图，不许变成第二套重启策略
    assert set(payload["restart_policy"]["safe_gates"]) == SAFE_RESTART_GATES


def test_write_status_json_is_valid_and_atomic(tmp_path):
    target = tmp_path / "nested" / "watchdog_status.json"
    write_status_json(target, {"schema": 1, "event": "running", "中文": "可读"})
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["event"] == "running"
    assert data["中文"] == "可读"
    assert list(target.parent.glob("*.tmp*")) == []
