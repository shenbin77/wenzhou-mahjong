"""auto_review（自动复盘与疑似问题筛查）测试。

夹具来源：FX_* 为 2026-10-25 实机局 248061 的真实台账行
（采集/dev_auto_play/muse_live_20261025_0418 与 _0418b），仅裁掉
筛查不读的嵌套证据（claim 的 L1 分数表、table_state 全量等），
筛查所读字段的值逐字保留；settle_samples / panel_recovery 尚无
真实台账样本（修复与接线均在本局之后），按当前
auto_play_loop._verify_claim_settled / _panel_recovery_attempt 的
落盘结构原样构造。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto_review
from auto_review import review_session

BOT_DIR = Path(__file__).resolve().parent.parent

# --- 真实会话头（0418/5556 actions.jsonl 第 1 行；note 字段与筛查
# 无关且源文件内已是乱码，略） ---
HEADER_DEV = {
    "dev": True, "session": "D:\\温州麻将\\温州茶（传统麻将）\\采集\\dev_auto_play\\muse_live_20261025_0418\\5556",
    "serial": "emulator-5556", "mode": "tea_2p", "enabled": True,
    "dry_run": False, "caishen": "auto",
    "started_at": "2026-10-25T04:16:44.099752+08:00",
    "claim_tap_channel_ready": True,
}
HEADER_CLEAN = dict(HEADER_DEV, dev=False)

# --- 真实 claim_unverified 停手行（0418/5556 第 14 行投影） ---
FX_CLAIM_STOP = {
    "ts": "2026-10-25T04:17:17.386130+08:00", "round_seq": 1,
    "frame": "D:\\温州麻将\\温州茶（传统麻将）\\采集\\dev_auto_play\\muse_live_20261025_0418\\5556\\frames\\20261025_041713_766187_before_63b680.png",
    "event": "stop", "status": "guard", "gate": "claim_unverified",
    "reason": "过后暗手 14→17，预期不变或立即摸牌 +1",
    "caishen": None, "guard": "action_visible",
    "claim": {"action": "pass", "confidence": 0.7},
}

# --- 真实财神未知三连行（0418b/5556 第 422–424 行投影） ---
_CAISHEN_FRAME = ("D:\\温州麻将\\温州茶（传统麻将）\\采集\\dev_auto_play\\"
                  "muse_live_20261025_0418b\\5556\\frames\\")
FX_CAISHEN_1 = {
    "ts": "2026-10-25T04:36:18.808168+08:00", "event": "idle",
    "status": "abstain",
    "reason": "财神连续 1/3 帧未知，等待有界重拍；期间零点击且不沿用旧财神",
    "caishen": None, "unknown_caishen_ticks": 1,
    "unknown_caishen_budget": 3, "tick": 414, "round_seq": 2,
    "frame": _CAISHEN_FRAME + "20261025_043618_641644_before_7ca9ca.png",
    "table_state": {"caishen_known": False, "concealed_count": None,
                    "notes": ["caishen_unknown"]},
}
FX_CAISHEN_2 = dict(
    FX_CAISHEN_1, ts="2026-10-25T04:36:20.635138+08:00",
    unknown_caishen_ticks=2,
    reason="财神连续 2/3 帧未知，等待有界重拍；期间零点击且不沿用旧财神",
    frame=_CAISHEN_FRAME + "20261025_043620_488180_before_c6f549.png")
FX_CAISHEN_3 = dict(
    FX_CAISHEN_1, ts="2026-10-25T04:36:22.481332+08:00",
    event="stop", unknown_caishen_ticks=3, gate="gate_unknown_caishen",
    reason="财神连续 3 帧未知：停止代打（P0-5）",
    frame=_CAISHEN_FRAME + "20261025_043622_313026_before_9e181a.png")

# --- 真实 abstain 行（0418/5558 第 2 行投影） ---
FX_ABSTAIN = {
    "ts": "2026-10-25T04:16:44.058223+08:00", "event": "idle",
    "status": "abstain", "reason": "识牌不完整，停止决策",
    "frame": "D:\\温州麻将\\温州茶（传统麻将）\\采集\\dev_auto_play\\muse_live_20261025_0418\\5558\\frames\\20261025_041643_575294_before_baf75f.png",
    "round_seq": 1,
    "turn_gate": {"state": "wait", "code": "",
                  "reason": "本帧没有可执行建议（status='abstain')"},
}

ADVISE_ROW = {
    "ts": "2026-10-25T04:16:50.000000+08:00", "event": "idle",
    "status": "advise", "reason": "等待对方出牌", "round_seq": 1,
}


def _settle_sample(attempt, concealed, verified=False, reason="过后暗手不符"):
    """样本结构与 _verify_claim_settled 落盘逐字对齐。"""
    return {"attempt": attempt, "concealed": concealed, "meld_groups": 0,
            "total": concealed, "claim_buttons": False, "settled": False,
            "selection_panel": False, "verified": verified,
            "reason": reason, "frame": f"frames/after_claim_{attempt}.png"}


def _claim_row(samples, verified, reason):
    return {
        "ts": "2026-10-25T05:00:00.000000+08:00", "event": "idle",
        "status": "guard", "round_seq": 1,
        "frame": "frames/before_claim.png",
        "claim": {"action": "pass",
                  "auto_click": {"executed": True, "stage": "tapped",
                                 "semantic_verified": verified,
                                 "semantic_reason": reason,
                                 "settle_samples": samples}},
    }


def _write_jsonl(path: Path, rows) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                              for r in rows) + "\n", encoding="utf-8")


def _session(tmp_path: Path, actions=None, follow=None, flags=None,
             name="sess") -> Path:
    session_dir = tmp_path / name
    session_dir.mkdir(parents=True, exist_ok=True)
    if actions is not None:
        _write_jsonl(session_dir / "actions.jsonl", actions)
    if follow is not None:
        _write_jsonl(session_dir / "follow_session.jsonl", follow)
    if flags is not None:
        _write_jsonl(session_dir / "user_flags.jsonl", flags)
    return session_dir


def _by_category(summary, category):
    return [i for i in summary["issues"] if i["category"] == category]


# ---------------------------------------------------------------------------
# 规则 1：stop
# ---------------------------------------------------------------------------

def test_stop_hit_real_fixture(tmp_path):
    session = _session(tmp_path, actions=[HEADER_DEV, FX_CLAIM_STOP])
    summary = review_session(session)
    stops = _by_category(summary, "stop")
    assert len(stops) == 1
    stop = stops[0]
    assert stop["excerpt"]["gate"] == "claim_unverified"
    assert stop["excerpt"]["reason"] == "过后暗手 14→17，预期不变或立即摸牌 +1"
    assert stop["excerpt"]["claim_action"] == "pass"
    assert stop["class"] == "recognition"
    assert stop["line"] == 2
    assert stop["frame"] == "20261025_041713_766187_before_63b680.png"


def test_stop_absent_on_clean_ledger(tmp_path):
    session = _session(tmp_path, actions=[HEADER_CLEAN, ADVISE_ROW])
    summary = review_session(session)
    assert summary["issue_count"] == 0


# ---------------------------------------------------------------------------
# 规则 2：claim settle_samples
# ---------------------------------------------------------------------------

def test_settle_unconverged_flagged(tmp_path):
    samples = [_settle_sample(0, 10), _settle_sample(1, 17),
               _settle_sample(2, 10)]
    row = _claim_row(samples, False, "点后状态 8 帧内未落定，fail-closed：过后暗手 16→10")
    session = _session(tmp_path, actions=[HEADER_DEV, row])
    summary = review_session(session)
    hits = _by_category(summary, "claim_settle_anomaly")
    assert len(hits) == 1
    assert hits[0]["excerpt"]["samples_path"] == "claim.auto_click.settle_samples"
    assert hits[0]["excerpt"]["sample_count"] == 3
    assert "未收敛" in hits[0]["excerpt"]["detail"]
    assert hits[0]["frame"] == "after_claim_2.png"


def test_settle_final_mismatch_flagged(tmp_path):
    samples = [_settle_sample(0, 10), _settle_sample(1, 10)]
    row = _claim_row(samples, False, "过后暗手 16→10，预期不变或立即摸牌 +1")
    session = _session(tmp_path, actions=[HEADER_DEV, row])
    summary = review_session(session)
    hits = _by_category(summary, "claim_settle_anomaly")
    assert len(hits) == 1
    assert "最终不符" in hits[0]["excerpt"]["detail"]


def test_settle_verified_not_flagged(tmp_path):
    samples = [_settle_sample(0, 10), _settle_sample(1, 16, True, "过后暗手不变")]
    row = _claim_row(samples, True, "过后暗手不变")
    session = _session(tmp_path, actions=[HEADER_DEV, row])
    summary = review_session(session)
    assert _by_category(summary, "claim_settle_anomaly") == []


def test_settle_chi_chain_path_flagged(tmp_path):
    samples = [_settle_sample(0, 13), _settle_sample(1, 9)]
    row = {"ts": "2026-10-25T05:01:00.000000+08:00", "event": "idle",
           "status": "guard", "round_seq": 1, "frame": "frames/b.png",
           "claim": {"action": "chi",
                     "chi_chain": {"stage": "unverified",
                                   "settle_samples": samples}}}
    session = _session(tmp_path, actions=[HEADER_DEV, row])
    summary = review_session(session)
    hits = _by_category(summary, "claim_settle_anomaly")
    assert len(hits) == 1
    assert hits[0]["excerpt"]["samples_path"] == "claim.chi_chain.settle_samples"


# ---------------------------------------------------------------------------
# 规则 3：面板恢复
# ---------------------------------------------------------------------------

def test_panel_recovery_attempts_listed(tmp_path):
    abstain_row = {
        "ts": "2026-10-25T05:02:00.000000+08:00", "event": "idle",
        "status": "abstain", "round_seq": 1, "frame": "frames/p1.png",
        "reason": "胡提示面板待二击恢复条件未满足，继续弃权等待",
        "panel_recovery": {"tile_id": 23, "saved_at": "2026-10-25T05:01:58",
                           "decision": "state_stale", "outcome": "abstain",
                           "reason": "state_stale: 状态已过期"},
    }
    recovered_row = {
        "ts": "2026-10-25T05:03:00.000000+08:00", "event": "action",
        "status": "advise", "round_seq": 1, "frame": "frames/p2.png",
        "panel_recovery": {"tile_id": 23, "saved_at": "2026-10-25T05:02:58",
                           "decision": "ok", "outcome": "recovered",
                           "case": "A", "box": [100, 700, 140, 800]},
    }
    session = _session(tmp_path, actions=[HEADER_DEV, abstain_row, recovered_row])
    summary = review_session(session)
    hits = _by_category(summary, "panel_recovery")
    assert len(hits) == 2
    assert hits[0]["excerpt"]["outcome"] == "abstain"
    assert hits[0]["excerpt"]["result"] == "not_recovered"
    assert hits[1]["excerpt"]["outcome"] == "recovered"
    assert hits[1]["excerpt"]["result"] == "success"
    assert all(i["class"] == "recognition" for i in hits)


# ---------------------------------------------------------------------------
# 规则 4：abstain 连续段
# ---------------------------------------------------------------------------

def _abstain_run(n, start_second=0):
    rows = []
    for k in range(n):
        row = dict(FX_ABSTAIN)
        row["ts"] = f"2026-10-25T04:16:{44 + start_second + k:02d}.000000+08:00"
        rows.append(row)
    return rows


def test_abstain_run_hit(tmp_path):
    session = _session(tmp_path, actions=[HEADER_DEV] + _abstain_run(5))
    summary = review_session(session)
    hits = _by_category(summary, "abstain_run")
    assert len(hits) == 1
    assert hits[0]["excerpt"]["run_length"] == 5
    assert hits[0]["excerpt"]["key"] == "wait"
    assert hits[0]["class"] == "decision"
    assert hits[0]["line"] == 2 and hits[0]["line_end"] == 6


def test_abstain_run_below_threshold(tmp_path):
    session = _session(tmp_path, actions=[HEADER_DEV] + _abstain_run(4))
    summary = review_session(session)
    assert _by_category(summary, "abstain_run") == []


def test_abstain_run_broken_by_advise(tmp_path):
    rows = [HEADER_DEV] + _abstain_run(3) + [ADVISE_ROW] + _abstain_run(3, 10)
    session = _session(tmp_path, actions=rows)
    summary = review_session(session)
    assert _by_category(summary, "abstain_run") == []


# ---------------------------------------------------------------------------
# 规则 5：财神未知段
# ---------------------------------------------------------------------------

def test_caishen_segment_hit_real_fixture(tmp_path):
    session = _session(tmp_path,
                       actions=[HEADER_DEV, FX_CAISHEN_1, FX_CAISHEN_2, FX_CAISHEN_3])
    summary = review_session(session)
    hits = _by_category(summary, "caishen_unknown_run")
    assert len(hits) == 1
    assert hits[0]["excerpt"]["max_ticks"] == 3
    assert hits[0]["excerpt"]["segment_length"] == 3
    assert hits[0]["excerpt"]["ends_with_stop"] is True
    stops = _by_category(summary, "stop")
    assert len(stops) == 1
    assert stops[0]["excerpt"]["gate"] == "gate_unknown_caishen"


def test_caishen_single_tick_not_flagged(tmp_path):
    session = _session(tmp_path, actions=[HEADER_DEV, FX_CAISHEN_1, ADVISE_ROW])
    summary = review_session(session)
    assert _by_category(summary, "caishen_unknown_run") == []


def test_caishen_frozen_segment_flagged(tmp_path):
    frozen = []
    for k in range(5):
        row = dict(FX_CAISHEN_1)
        row["unknown_caishen_ticks"] = 0
        row["reason"] = ("财神未知但本帧未读到已发牌手牌（等候室/局间过渡），"
                         "P0-5 计数冻结；期间零点击且不沿用旧财神")
        row["ts"] = f"2026-10-25T04:40:0{k}.000000+08:00"
        frozen.append(row)
    session = _session(tmp_path, actions=[HEADER_DEV] + frozen)
    summary = review_session(session)
    hits = _by_category(summary, "caishen_unknown_run")
    assert len(hits) == 1
    assert hits[0]["excerpt"]["frozen_rows"] == 5
    assert hits[0]["excerpt"]["max_ticks"] == 0


# ---------------------------------------------------------------------------
# 规则 6：user_flags
# ---------------------------------------------------------------------------

def test_user_flags_all_listed(tmp_path):
    flags = [
        {"at": "2026-10-25T04:20:00+08:00", "text": "这把财神好像读错了",
         "frame": "frames/f1.png"},
        {"at": "2026-10-25T04:21:00+08:00", "class": "recognition",
         "text": "有一张牌没认出来"},
    ]
    session = _session(tmp_path, actions=[HEADER_CLEAN, ADVISE_ROW], flags=flags)
    summary = review_session(session)
    hits = _by_category(summary, "user_flag")
    assert len(hits) == 2
    assert hits[0]["class"] == "decision"  # 无 class 字段 → 默认决策类
    assert hits[0]["excerpt"]["text"] == "这把财神好像读错了"
    assert hits[0]["frame"] == "f1.png"
    assert hits[1]["class"] == "recognition"


# ---------------------------------------------------------------------------
# 容错 / 空会话 / 缺失目录
# ---------------------------------------------------------------------------

def test_corrupt_lines_tolerated(tmp_path):
    session_dir = tmp_path / "sess"
    session_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(HEADER_CLEAN, ensure_ascii=False), "{broken json",
             "42", "", json.dumps(FX_CLAIM_STOP, ensure_ascii=False)]
    (session_dir / "actions.jsonl").write_text("\n".join(lines) + "\n",
                                               encoding="utf-8")
    summary = review_session(session_dir)
    assert len(_by_category(summary, "stop")) == 1
    assert summary["sources"]["actions.jsonl"]["rows"] == 2
    assert summary["sources"]["actions.jsonl"]["corrupt_lines"] == 2


def test_empty_session(tmp_path):
    session_dir = tmp_path / "empty"
    session_dir.mkdir(parents=True, exist_ok=True)
    summary = review_session(session_dir)
    assert summary["issue_count"] == 0
    assert Path(summary["report_json"]).exists()
    assert Path(summary["report_md"]).exists()
    assert summary["corpus_eligible"] is False
    assert any("mode" in b for b in summary["corpus_blockers"])


def test_missing_dir_no_crash(tmp_path):
    summary = review_session(tmp_path / "does_not_exist")
    assert summary["issue_count"] == 0
    assert summary["report_md"] is None


# ---------------------------------------------------------------------------
# corpus_eligible（数据隔离口径）
# ---------------------------------------------------------------------------

def test_corpus_dev_blocked(tmp_path):
    session = _session(tmp_path, actions=[HEADER_DEV, ADVISE_ROW])
    summary = review_session(session)
    assert summary["corpus_eligible"] is False
    assert any("dev=true" in b for b in summary["corpus_blockers"])
    assert summary["dev"] is True
    assert summary["mode"] == "tea_2p"
    assert summary["serial"] == "emulator-5556"


def test_corpus_mode_missing_blocked(tmp_path):
    header = {k: v for k, v in HEADER_CLEAN.items() if k != "mode"}
    session = _session(tmp_path, actions=[header, ADVISE_ROW])
    summary = review_session(session)
    assert summary["corpus_eligible"] is False
    assert any("mode" in b for b in summary["corpus_blockers"])


def test_corpus_stop_blocked(tmp_path):
    session = _session(tmp_path, actions=[HEADER_CLEAN, FX_CLAIM_STOP])
    summary = review_session(session)
    assert summary["corpus_eligible"] is False
    assert any("stop" in b for b in summary["corpus_blockers"])


def test_corpus_clean_session_eligible(tmp_path):
    session = _session(tmp_path, actions=[HEADER_CLEAN, ADVISE_ROW])
    summary = review_session(session)
    assert summary["corpus_eligible"] is True
    assert summary["corpus_blockers"] == []


# ---------------------------------------------------------------------------
# 汇总口径 / 报告文件 / follow 兜底
# ---------------------------------------------------------------------------

def test_by_class_aggregation_and_report_files(tmp_path):
    rows = [HEADER_DEV, FX_CLAIM_STOP] + _abstain_run(5)
    flags = [{"at": "2026-10-25T04:22:00+08:00", "text": "标记一把"}]
    session = _session(tmp_path, actions=rows, flags=flags)
    summary = review_session(session)
    assert summary["issue_count"] == 3
    assert summary["by_category"] == {"stop": 1, "abstain_run": 1,
                                      "user_flag": 1}
    assert summary["by_class"] == {"recognition": 1, "decision": 2}
    assert sum(summary["by_class"].values()) == summary["issue_count"]
    for key in ("session_dir", "issue_count", "by_category",
                "report_json", "report_md"):
        assert key in summary
    on_disk = json.loads(Path(summary["report_json"]).read_text(
        encoding="utf-8"))
    assert on_disk["mode"] == "tea_2p" and on_disk["dev"] is True
    assert on_disk["corpus_eligible"] is False
    md = Path(summary["report_md"]).read_text(encoding="utf-8")
    assert "训练语料准入：❌ 不可进语料" in md
    assert "## 候选后续（仅供人工审核）" in md
    assert "claim 验证在压暗帧误杀" not in md  # 只描述模式，不下断言式结论
    assert "硬停手" in md


def test_follow_only_session_stop_fallback(tmp_path):
    follow_header = {
        "schema": "follow_session/v1", "kind": "session_header",
        "at": "2026-10-25T04:16:44.101322+08:00", "session": "x",
        "serial": "emulator-5556", "mode": "tea_2p", "dev": True,
        "claim_tap_channel_ready": True,
    }
    follow_stop = {
        "schema": "follow_session/v1",
        "at": "2026-10-25T04:17:17.386130+08:00", "event": "stop",
        "tick": 12, "frame": "frames/f_stop.png",
        "gates": {"stop_kind": "claim_unverified"},
        "vision": {"status": "guard",
                   "reason": "过后暗手 14→17，预期不变或立即摸牌 +1"},
    }
    session = _session(tmp_path, follow=[follow_header, follow_stop])
    summary = review_session(session)
    stops = _by_category(summary, "stop")
    assert len(stops) == 1
    assert stops[0]["source"] == "follow_session.jsonl"
    assert stops[0]["excerpt"]["gate"] == "claim_unverified"
    assert summary["primary_ledger"] == "follow_session.jsonl"
    assert summary["mode"] == "tea_2p"


def test_find_session_dirs_and_review_root(tmp_path):
    root = tmp_path / "dev_auto_play"
    group = root / "group_a"
    group.mkdir(parents=True)
    _session(group, actions=[HEADER_CLEAN, ADVISE_ROW], name="5556")
    (root / "not_a_session").mkdir()
    dirs = auto_review.find_session_dirs(root)
    assert [d.name for d in dirs] == ["5556"]
    summaries = auto_review.review_root(root)
    assert len(summaries) == 1
    assert summaries[0]["corpus_eligible"] is True


def test_cli_batch_smoke(tmp_path):
    root = tmp_path / "dev_auto_play"
    _session(root, actions=[HEADER_DEV, FX_CLAIM_STOP], name="s1")
    _session(root, actions=[HEADER_CLEAN, ADVISE_ROW], name="s2")
    proc = subprocess.run(
        [sys.executable, str(BOT_DIR / "run_auto_review.py"), str(root)],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=str(BOT_DIR))
    assert proc.returncode == 0
    assert "共 2 个会话" in proc.stdout
    assert (root / "s1" / "auto_review.md").exists()
    assert (root / "s2" / "auto_review.json").exists()
