"""停手原因统计脚本的自检：筛选规则、归一化、归因与三问输出。

测试只用临时目录合成会话，不依赖仓库里真实存在的 actions.jsonl，
因此结果与 BOT 是否正在直播无关。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "tools" / "stop_reason_report.py"

_spec = importlib.util.spec_from_file_location("stop_reason_report_under_test",
                                               MODULE_PATH)
assert _spec and _spec.loader
report = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("stop_reason_report_under_test", report)
_spec.loader.exec_module(report)

TZ = timezone(timedelta(hours=8))
BASE = datetime(2026, 10, 25, 21, 0, 0, tzinfo=TZ)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def write_session(root: Path, relative: str, rows: list[dict],
                  *, serial: str = "emulator-5556",
                  dry_run: bool = False) -> Path:
    """在 root/relative/actions.jsonl 写一个会话（首行是会话头）。"""
    directory = root / relative
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "actions.jsonl"
    header = {
        "dev": True,
        "session": str(directory),
        "serial": serial,
        "mode": "tea_2p",
        "enabled": True,
        "dry_run": dry_run,
        "started_at": "2026-10-25T21:00:00+08:00",
    }
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(header, ensure_ascii=False) + "\n")
        for index, row in enumerate(rows):
            payload = {"ts": (BASE + timedelta(seconds=index)).isoformat()}
            payload.update(row)
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path


def idle(reason: str, status: str = "abstain", **extra) -> dict:
    row = {"event": "idle", "status": status, "reason": reason}
    row.update(extra)
    return row


def stop(gate: str, reason: str, **extra) -> dict:
    row = {"event": "stop", "gate": gate, "reason": reason}
    row.update(extra)
    return row


def action() -> dict:
    return {"event": "action", "status": "advise",
            "reason": "向听1，等待摸牌"}


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def test_normalize_reason_strips_coordinates_counts_and_durations():
    assert report.normalize_reason(
        "过后暗手 14→17，预期不变或立即摸牌 +1") == (
        "过后暗手 <N>→<N>，预期不变或立即摸牌 +<N>")
    assert report.normalize_reason("框=[(1383, 552, 1483, 652)]") == "框=<LIST>"
    assert report.normalize_reason("耗时 12.3ms 后超时") == "耗时 <MS> 后超时"
    assert report.normalize_reason("句柄 0x1f 无效") == "句柄 <HEX> 无效"
    assert report.normalize_reason("亮度 23.28") == "亮度 <F>"
    assert report.normalize_reason(None) == "(未记录 reason)"
    assert report.normalize_reason("   ") == "(未记录 reason)"


def test_normalize_reason_merges_same_cause_with_different_numbers():
    a = "明牌第1组仅1张（x≈6..89），疑似漏检，停止决策"
    b = "明牌第3组仅2张（x≈120..300），疑似漏检，停止决策"
    assert report.normalize_reason(a) == report.normalize_reason(b)


# ---------------------------------------------------------------------------
# 路径筛选
# ---------------------------------------------------------------------------


#: 这些用例只校验规则本身，用固定的假路径而不落盘；pytest 的 tmp_path
#: 自身就位于 .pytest_tmp 下，会干扰黑名单判定。
FAKE_ROOT = Path("D:/repo")


@pytest.mark.parametrize("relative", [
    "_takeover/wb_live/a2_5556",
    "_takeover/play7_5558",
    "_takeover/live_5556",
    "温州茶（传统麻将）/采集/dev_auto_play/20261024_041701_0df7a7",
])
def test_whitelisted_live_sessions_pass(relative):
    assert report.blocked_reason(FAKE_ROOT / relative / "actions.jsonl") is None


@pytest.mark.parametrize("relative", [
    "_takeover/wb_audit_bt",
    "_takeover/wb_lead_new",
    "_takeover/wb_eng_moqie",
    "_takeover/bt_full1",
    ".pytest_tmp/closeout_bot/test_x",
    ".tmp/pytest_base/test_action_ui_blocks_before_a0",
    ".tmp_bind/x",
    "_acc_bt3/test_card_zero_stops_the_loop_0",
    "温州茶（传统麻将）/bot/.codex_tmp_wire_full",
])
def test_fixture_directories_are_rejected(relative):
    assert report.blocked_reason(FAKE_ROOT / relative / "actions.jsonl") is not None


def test_dry_run_directory_not_whitelisted():
    path = FAKE_ROOT / "_takeover" / "dry_5556" / "actions.jsonl"
    assert report.blocked_reason(path) == "不在白名单"


def test_discover_sessions_dedupes_hardlinked_copies():
    # 用自带临时目录：硬链接留在 pytest 的 basetemp 里会让它的清理报错。
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        first = write_session(root, "_takeover/wb_live/a1_5556",
                              [idle("识牌不完整，停止决策")])
        linked_dir = root / "_takeover" / "wb_live" / "mirror_5556"
        linked_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.link(first, linked_dir / "actions.jsonl")
        except OSError:
            pytest.skip("当前文件系统不支持硬链接")

        discovery = report.discover_sessions(root)
        assert len(discovery.sessions) == 1
        assert discovery.duplicates >= 1


# ---------------------------------------------------------------------------
# 行模型
# ---------------------------------------------------------------------------


def test_stoppish_excludes_normal_waiting():
    waiting = report.parse_row({"event": "idle", "status": "wait",
                                "reason": "向听2，等待摸牌"})
    abstain = report.parse_row({"event": "idle", "status": "abstain",
                                "reason": "识牌不完整，停止决策"})
    guard = report.parse_row({"event": "idle", "status": "guard",
                              "reason": "检测到吃碰杠胡/过的行动界面，暂停自动出牌"})
    stopped = report.parse_row({"event": "stop", "gate": "duration",
                                "reason": "达到运行时长 5s"})
    assert waiting is not None and not waiting.stoppish
    assert abstain is not None and abstain.stoppish
    assert guard is not None and guard.stoppish
    assert stopped is not None and stopped.stoppish


def test_gate_key_is_explicit_gate_then_implicit_status():
    assert report.parse_row({"event": "stop", "gate": "claim_unverified",
                             "reason": "x"}).gate_key == "claim_unverified"
    assert report.parse_row({"event": "idle", "status": "guard",
                             "guard": "action_visible",
                             "reason": "x"}).gate_key == "guard:action_visible"
    assert report.parse_row({"event": "idle", "status": "abstain",
                             "reason": "x"}).gate_key == "abstain:<implicit>"


# ---------------------------------------------------------------------------
# 归因
# ---------------------------------------------------------------------------


def test_bright_subset_reason_is_not_swallowed_by_incomplete_read():
    """多亮白名单的 reason 里含「识牌不完整」，必须归到更具体的桶。"""
    row = report.parse_row(idle(
        "多亮白名单未确认（bright_subset_unconfirmed）：本帧没有可核对的"
        "算牌建议（status='abstain'，reason='识牌不完整，停止决策'）；"
        "白名单不自行选牌", gate="bright_subset_unconfirmed"))
    assert report.classify_row(row) == ("core", "多亮白名单未确认")


def test_blocked_claim_with_tile_count_mismatch_is_vision_core():
    row = report.parse_row(idle(
        "检测到吃碰杠胡/过的行动界面，暂停自动出牌", status="guard",
        claim={"status": "blocked",
               "claim_reason": "检出暗红盘（胡），胡优先",
               "blocked_reason": "L2 否决：画面出现胡盘，但手牌张数不守恒"
                                 "（闭手14，副露1组/逻辑3张，等待态应为 13），L2 弃权"}))
    assert report.classify_row(row) == ("core", "压暗少读/暗手张数不守恒")


def test_blocked_claim_without_legal_claim_is_decision_fault():
    row = report.parse_row(idle(
        "检测到吃碰杠胡/过的行动界面，暂停自动出牌", status="guard",
        claim={"status": "blocked",
               "claim_reason": "检出金色盘且同行有过按钮，按过处理",
               "blocked_reason": "L2 否决：画面出现金盘，但引擎判定碰/吃/杠皆不可行，弃权"}))
    assert report.classify_row(row) == ("other", "行动盘已识别但 L2 否决")


def test_caishen_unknown_in_lobby_is_normal_waiting_not_root_cause():
    row = report.parse_row(idle(
        "财神未知但本帧未读到已发牌手牌（等候室/局间过渡），P0-5 计数冻结；"
        "期间零点击且不沿用旧财神"))
    bucket, label = report.classify_row(row)
    assert bucket == "normal"
    assert label == "局间过渡/等候室"


def test_caishen_unknown_mid_hand_is_vision_ext():
    row = report.parse_row(idle("财神连续 2/3 帧未知，等待有界重拍"))
    assert report.classify_row(row) == ("ext", "财神未知（多帧）")


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


def test_build_stats_on_synthetic_session(tmp_path):
    rows = [
        action(),
        idle("识牌不完整，停止决策"),
        idle("识牌不完整，停止决策"),
        idle("向听1，等待摸牌", status="wait"),
        stop("claim_unverified", "过后暗手 14→17，预期不变或立即摸牌 +1"),
        {"event": "round_transition", "reason": "换局"},
    ]
    path = write_session(tmp_path, "_takeover/wb_live/a1_5556", rows)
    session = report.load_session(path)
    stats = report.build_stats([session])

    totals = stats["totals"]
    assert totals["sessions"] == 1
    assert totals["actions"] == 1
    assert totals["stops"] == 1
    assert totals["stoppish"] == 3
    assert stats["reasons"][0]["reason"] == "识牌不完整，停止决策"
    assert stats["reasons"][0]["count"] == 2
    assert stats["vision"]["core_total"] == 3
    assert stats["attribution"]["unclassified"] == 0


def test_zero_action_session_is_counted(tmp_path):
    write_session(tmp_path, "_takeover/wb_live/z1_5558",
                  [idle("识牌不完整，停止决策")])
    sessions, _discovery, _rejected = report.collect(tmp_path)
    stats = report.build_stats(sessions)
    assert stats["totals"]["zero_action_sessions"] == 1
    assert stats["totals"]["zero_action_pct"] == 100.0


def test_idle_streak_bucket_counts_consecutive_idles(tmp_path):
    rows = [
        idle("向听1，等待摸牌", status="wait"),
        idle("向听2，等待摸牌", status="wait"),
        idle("向听3，等待摸牌", status="wait"),
        stop("duration", "达到运行时长 5s"),
    ]
    session = report.load_session(
        write_session(tmp_path, "_takeover/wb_live/s1_5556", rows))
    streaks = report.idle_streaks(session)
    assert [value for value, _row in streaks] == [3]
    stats = report.build_stats([session])
    buckets = {item["bucket"]: item for item in stats["idle_streak_buckets"]}
    assert buckets["3-5 帧"]["stop_points"] == 1
    assert buckets["3-5 帧"]["all_stop_points"] == 1
    # 该会话最后一行是 stop，尾部 idle 为 0
    assert buckets["0 帧"]["session_tails"] == 1


def test_load_session_rejects_test_stub_serial(tmp_path):
    path = write_session(tmp_path, "_takeover/wb_live/t1_5556",
                         [action()], serial="emulator-test")
    assert report.load_session(path) is None


def test_load_session_rejects_dry_run(tmp_path):
    path = write_session(tmp_path, "_takeover/wb_live/d1_5556",
                         [action()], dry_run=True)
    assert report.load_session(path) is None


def test_as_of_cuts_rows_by_timestamp(tmp_path):
    rows = [action(), action(), action()]
    path = write_session(tmp_path, "_takeover/wb_live/a1_5556", rows)
    cutoff = BASE + timedelta(seconds=1)
    session = report.load_session(path, as_of=cutoff)
    assert len(session.rows) == 2


def test_collect_reports_header_rejections(tmp_path):
    write_session(tmp_path, "_takeover/wb_live/a1_5556", [action()])
    write_session(tmp_path, "_takeover/wb_live/d1_5556", [action()],
                  dry_run=True)
    sessions, discovery, rejected = report.collect(tmp_path)
    assert len(discovery.sessions) == 2
    assert len(sessions) == 1
    assert rejected["serial/dry_run 不合格"] == 1


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def test_markdown_answers_the_three_questions(tmp_path):
    rows = [
        idle("识牌不完整，停止决策"),
        idle("多亮白名单未确认（bright_subset_unconfirmed）：本帧没有可核对的"
             "算牌建议（status='abstain'，reason='识牌不完整，停止决策'）",
             gate="bright_subset_unconfirmed"),
        stop("claim_unverified", "过后暗手 14→17，预期不变或立即摸牌 +1"),
        idle("检测到吃碰杠胡/过的行动界面，暂停自动出牌", status="guard",
             claim={"status": "blocked",
                    "claim_reason": "检出金色盘且同行有过按钮，按过处理",
                    "blocked_reason": "L2 否决：画面出现金盘，但引擎判定"
                                      "碰/吃/杠皆不可行，弃权"}),
    ]
    write_session(tmp_path, "温州茶（传统麻将）/采集/dev_auto_play/s1", rows)
    sessions, discovery, _rejected = report.collect(tmp_path)
    stats = report.build_stats(sessions)
    samples = report.pick_failure_samples(sessions)
    markdown = report.render_markdown(stats, discovery, samples, tmp_path)

    assert "## 问题一：停手原因 Top5" in markdown
    assert "## 问题二：同一病根" in markdown
    assert "## 问题三：本帧识别正确" in markdown
    assert "典型失败会话" in markdown
    assert stats["totals"]["stoppish"] == 4
    # 识牌不完整 + 多亮白名单 + 「过后暗手 14→17」（张数不守恒）
    assert stats["vision"]["core_total"] == 3
    assert stats["decision_blocked_after_detection"]["count"] == 1
    assert len(samples) == 1
    assert str(samples[0]["path"]).endswith("actions.jsonl")


def test_console_summary_mentions_key_numbers(tmp_path):
    write_session(tmp_path, "_takeover/wb_live/a1_5556",
                  [idle("识牌不完整，停止决策")])
    sessions, discovery, _rejected = report.collect(tmp_path)
    stats = report.build_stats(sessions)
    text = report.render_console(stats, discovery)
    assert "Top5 停手原因" in text
    assert "单帧识牌不稳" in text
    assert "识别对了但决策没接上" in text
