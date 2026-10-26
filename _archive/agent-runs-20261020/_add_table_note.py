"""Insert the lead-approved provenance note into the 43-frame table header.

The table uses CRLF throughout, so the insertion is done in binary mode to keep
line endings uniform (a plain text write would mix LF in and make later diffs
noisy). Only header text is touched; data rows are untouched.
"""
from __future__ import annotations

from pathlib import Path

TABLE = Path(r"D:/温州麻将/温州茶（传统麻将）/采集/workbuddy_review_20261024"
             r"/meld_43frames_before_after.txt")
NOTE = ("注：48/manual 为当时实况（s8 实战模板 02:01 才入库）；"
        "模板补齐后自动识别可复现为 image，判定结果不变。")
MARK = "注：48/manual"


def main() -> int:
    raw = TABLE.read_bytes()
    if MARK.encode("utf-8") in raw:
        print("SKIP 注记已存在，不重复插入")
        return 0
    lines = raw.split(b"\r\n")
    if len(lines) < 4:
        print(f"ABORT 行数异常 {len(lines)}")
        return 1
    # 插在第 2 行（「修复前 = ...」）之后，保持空行结构
    out = lines[:2] + [NOTE.encode("utf-8")] + lines[2:]
    before = raw.count(b"\r\n")
    data = b"\r\n".join(out)
    TABLE.write_bytes(data)
    after = data.count(b"\r\n")
    print(f"OK 插入 1 行；CRLF {before} -> {after}；"
          f"LF-only {data.count(bytes([10])) - after}；size {len(data)}")
    for i, line in enumerate(out[:6], 1):
        print(f"  L{i}\t{line.decode('utf-8')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
