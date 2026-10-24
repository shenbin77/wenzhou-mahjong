# -*- coding: utf-8 -*-
from pathlib import Path
import json
from datetime import datetime
from zoneinfo import ZoneInfo

EV = Path(r"D:\温州麻将\温州茶（传统麻将）\bot\_backups\wire_facts_codexfix2_20261024_224648")
report = json.loads((EV / "REPORT.json").read_text(encoding="utf-8"))
cases = report["fix"]["cases"]
hashes = report["sha256"]
blank = report["river_blank_smoke"]
now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S UTC+8")
report["generated_at_asia_shanghai"] = now
report["pytest"] = {
    "focused": "18 passed (tests/test_follow_enrich.py)",
    "full_tests_dir": "736 passed",
}
(EV / "REPORT.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

lines = [
    "# wire_facts_codexfix2 evidence",
    "",
    f"- generated: {now} (Asia/Shanghai)",
    "- CLAIM_TAP_CHANNEL_READY = `False`",
    "- WALL_CARD_READER_CONNECTED = `False`",
    "- pytest focused: **18 passed** (`tests/test_follow_enrich.py`)",
    "- pytest full (`tests/`): **736 passed**",
    "- did NOT call user to play; no ADB/clicks/commit",
    "",
    "## Fix: stacked_kong requires visible_boxes",
    "",
    "Deleted `_stacked_kong_match` fallback that allowed 杠 when `visible_boxes` missing.",
    "",
    "| case | result |",
    "|------|--------|",
    f"| logical_count==4, missing visible_boxes | `{cases['missing_visible_boxes']}` |",
    f"| logical_count==4, empty visible_boxes | `{cases['empty_visible_boxes']}` |",
    f"| logical_count==4, wrong count (2 boxes) | `{cases['wrong_count_boxes']}` |",
    f"| logical_count==4, invalid format | `{cases['invalid_format_boxes']}` |",
    f"| logical_count==4 + 3 valid visible_boxes | `{cases['three_valid_visible_boxes']}` |",
    f"| three same alone (no stacked_kongs) | `{cases['three_same_alone']}` |",
    "",
    "New test: `test_stacked_kong_without_visible_boxes_stays_unknown`",
    "",
    "## Unchanged contracts (spot check)",
    "| mode | read_river | river UI | river_own/opp |",
    "|------|------------|----------|---------------|",
    f"| tea_2p | {blank['tea_2p']['read_river_flag']} | {blank['tea_2p']['river_display']} | {blank['tea_2p']['river_own']}/{blank['tea_2p']['river_opp']} |",
    f"| tea_4p | {blank['tea_4p']['read_river_flag']} | {blank['tea_4p']['river_display']} | {blank['tea_4p']['river_own']}/{blank['tea_4p']['river_opp']} |",
    f"| shu_4p | {blank['shu_4p']['read_river_flag']} | {blank['shu_4p']['river_display']} | {blank['shu_4p']['river_own']}/{blank['shu_4p']['river_opp']} |",
    "",
    "## SHA256",
    f"- follow_enrich.py: `{hashes['follow_enrich.py']}`",
    f"- tests/test_follow_enrich.py: `{hashes['tests/test_follow_enrich.py']}`",
    "",
    "## Paths",
    f"- evidence: `{EV}`",
    f"- diff: `{EV / 'files.diff'}`",
    f"- focused log: `{EV / 'focused_pytest.txt'}`",
    f"- full log: `{EV / 'full_pytest.txt'}`",
    f"- REPORT: `{EV / 'REPORT.md'}`",
    "",
]
(EV / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
print((EV / "REPORT.md").read_text(encoding="utf-8"))
