"""财神牌面覆盖自检：一键列出已覆盖/缺失的牌面与模板路径。

用法（Python 3.11，非沙箱）::

    python bot/caishen_coverage.py            # 人可读表 + 汇总
    python bot/caishen_coverage.py --json     # 机器可读，供 CI/交接存档
    python bot/caishen_coverage.py --missing  # 只打印缺失清单

2 人茶局不使用红中与发财（判定模板见
``bot/templates/tea_no_zhong_fa.png``），因此有效需求是 32 个牌面，
而不是完整牌谱的 34 个。这里据此统计，避免把用不到的牌算成缺口。

每个牌面需要两张模板才能真正实战可用：
  * ``replay`` —— 回放帧（H.264 有损）裁出来的
  * ``live``   —— 实时 ADB 无损截图裁出来的
两套渲染存在系统性差异，只用回放模板去比实时帧，分数卡在 0.90~0.95
过不了 0.98 门禁（详见 :mod:`caishen_vision`）。所以"缺 live 变体"
也是缺口，只是不会认错，只会首见时弃权一次（fail-closed 安全方向）。
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BOT_DIR))

from tile_vision import TILE_IDS  # noqa: E402
import caishen_vision  # noqa: E402

# 2 人茶局牌墙里不存在的牌面（红中、发财）。
UNUSED_IN_TWO_PLAYER = frozenset({"hongzhong", "fa"})

# live 模板文件名形如 label_YYYYMMDD_HHMMSS_hash.png；回放模板是 label.png
_LIVE_NAME = re.compile(r"^[a-z0-9]+_\d{8}_\d{6}_[0-9a-f]{8}$")


def _known_labels() -> list[str]:
    """Every tile in the vocabulary, in a stable display order."""
    return sorted(TILE_IDS)


def scan() -> dict:
    """Walk the manifest and report per-label template coverage."""
    manifest = json.loads(
        (caishen_vision._DIR / "manifest.json").read_text(encoding="utf-8"))
    replay = collections.Counter()
    live: collections.defaultdict[str, list[str]] = collections.defaultdict(list)
    for item in manifest["templates"]:
        stem = Path(item["file"]).stem
        if _LIVE_NAME.match(stem):
            live[item["label"]].append(item["file"])
        else:
            replay[item["label"]] += 1

    labels = _known_labels()
    rows = []
    missing_live: list[dict] = []
    missing_all: list[dict] = []
    for label in labels:
        n_replay = replay.get(label, 0)
        files = sorted(live.get(label, ()))
        if label in UNUSED_IN_TWO_PLAYER:
            status = "unused"
        elif files:
            status = "ok"
        elif n_replay:
            status = "missing_live"
        else:
            status = "missing"
        rows.append({"label": label, "replay": n_replay, "live": len(files),
                     "live_files": files, "status": status})
        if status in {"missing_live", "missing"}:
            entry = {"label": label,
                     "status": status,
                     "replay_templates": n_replay,
                     "live_template_path": str(caishen_vision._DIR / "live-template")}
            if status == "missing_live":
                missing_live.append(entry)
            else:
                missing_all.append(entry)

    needed = [r for r in rows if r["status"] != "unused"]
    covered = [r for r in needed if r["status"] == "ok"]
    pending_dir = caishen_vision._DIR / "pending"
    queued = sorted(pending_dir.glob("*.json")) if pending_dir.exists() else []
    return {
        "roi": list(manifest["roi"]),
        "template_dir": str(caishen_vision._DIR),
        "total_vocabulary": len(labels),
        "unused_in_two_player": sorted(UNUSED_IN_TWO_PLAYER),
        "required": len(needed),
        "covered": len(covered),
        "missing_live": missing_live,
        "missing_completely": missing_all,
        "pending_candidates": [p.name for p in queued],
        "rows": rows,
    }


def _render(report: dict) -> None:
    print(f"模板目录: {report['template_dir']}")
    print(f"财神 ROI: {report['roi']}")
    print()
    print(f"{'牌面':<10}{'回放':<6}{'live':<6}状态")
    print("-" * 44)
    for row in report["rows"]:
        marks = {"ok": "OK", "missing_live": "缺 live 变体",
                 "missing": "完全缺失", "unused": "2人局不使用"}[row["status"]]
        print(f"{row['label']:<10}{row['replay']:<6}{row['live']:<6}{marks}")
    print()
    print(f"牌谱总数 {report['total_vocabulary']}，"
          f"2 人局不使用 {len(report['unused_in_two_player'])} 张"
          f"（{'/'.join(report['unused_in_two_player'])}）")
    print(f"有效需求 {report['required']} 个牌面，"
          f"已可实战直读 {report['covered']} 个，"
          f"待补 {len(report['missing_live']) + len(report['missing_completely'])} 个")
    if report["missing_completely"]:
        print("\n完全缺失（一张模板都没有）：")
        for entry in report["missing_completely"]:
            print(f"  - {entry['label']}")
    if report["missing_live"]:
        print("\n已有回放模板、缺实时帧模板（首见会弃权一次，之后自动学会）：")
        for entry in report["missing_live"]:
            print(f"  - {entry['label']}")
    if report["pending_candidates"]:
        print("\npending 待人工复核：")
        for name in report["pending_candidates"]:
            print(f"  - {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="财神牌面覆盖自检")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--missing", action="store_true", help="只输出缺失清单")
    args = parser.parse_args()

    report = scan()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.missing:
        for entry in report["missing_completely"] + report["missing_live"]:
            print(f"{entry['label']}\t{entry['status']}")
    else:
        _render(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
