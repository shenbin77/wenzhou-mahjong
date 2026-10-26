"""无头自动复盘 CLI：对单个会话目录或整个 dev_auto_play 根目录
逐会话运行 auto_review.review_session，在每个会话目录写
auto_review.json / auto_review.md，并打印摘要。

用法：
    python run_auto_review.py <会话目录 | 根目录>
"""

from __future__ import annotations

import sys
from pathlib import Path

from auto_review import find_session_dirs, review_session


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if len(argv) != 2:
        print(__doc__.strip())
        return 2
    target = Path(argv[1])
    dirs = find_session_dirs(target)
    if not dirs:
        print(f"未找到含台账的会话目录：{target}")
        return 1
    total = 0
    for session_dir in dirs:
        summary = review_session(session_dir)
        total += summary["issue_count"]
        cats = ", ".join(f"{k}×{v}" for k, v in
                         sorted(summary["by_category"].items())) or "无"
        corpus = ("可进入人工候选复核池" if summary["corpus_eligible"]
                  else "不可进入人工候选复核池")
        print(f"{summary['session_dir']}")
        print(f"  mode={summary['mode']} dev={summary['dev']} "
              f"serial={summary['serial']} · 疑似问题 "
              f"{summary['issue_count']} 条（{cats}） · {corpus}")
        print(f"  报告：{summary['report_md']}")
    print(f"共 {len(dirs)} 个会话，疑似问题合计 {total} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
