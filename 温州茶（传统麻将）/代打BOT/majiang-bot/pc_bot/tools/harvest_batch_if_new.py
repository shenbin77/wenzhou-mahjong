#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run batch_settle_ocr only when replay_harvest has new/changed frames.

State file: pc_bot/logs/settle_ocr_batch/.last_harvest_stamp
  JSON: { "mtime_max": float, "file_count": int, "manifest_sha256": str, ... }

Stdout one-liner:
  NO_CHANGE  — stamp matches current harvest
  RAN ...    — batch ran after detecting changes
  SKIP ...   — harvest missing / error

Not a watcher: invoke from cron/routine; exits immediately.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # pc_bot/
REPO = ROOT.parent
HARVEST = Path("/workspace/majiang-data/replay_harvest")
STAMP_PATH = ROOT / "logs" / "settle_ocr_batch" / ".last_harvest_stamp"
BATCH_SCRIPT = Path(__file__).resolve().parent / "batch_settle_ocr.py"


def _iter_pngs(root: Path):
    if not root.is_dir():
        return
    for p in root.rglob("*.png"):
        if p.is_file():
            yield p


def harvest_fingerprint(root: Path) -> dict:
    """mtime max + count + sha256 of sorted relpath:mtime:size lines."""
    rows: list[str] = []
    mtime_max = 0.0
    n = 0
    for p in sorted(_iter_pngs(root), key=lambda x: str(x).replace("\\", "/")):
        try:
            st = p.stat()
        except OSError:
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        rows.append(f"{rel}:{st.st_mtime_ns}:{st.st_size}")
        mtime_max = max(mtime_max, st.st_mtime)
        n += 1
    blob = "\n".join(rows).encode("utf-8")
    return {
        "harvest": str(root),
        "file_count": n,
        "mtime_max": mtime_max,
        "manifest_sha256": hashlib.sha256(blob).hexdigest(),
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def load_stamp(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def unchanged(prev: dict | None, cur: dict) -> bool:
    if not prev:
        return False
    return (
        prev.get("manifest_sha256") == cur["manifest_sha256"]
        and prev.get("file_count") == cur["file_count"]
        and float(prev.get("mtime_max") or 0) == float(cur["mtime_max"])
    )


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    dry = "--dry-run" in argv
    force = "--force" in argv
    argv = [a for a in argv if a not in ("--dry-run", "--force")]

    if not HARVEST.is_dir():
        print(f"SKIP harvest_missing path={HARVEST}")
        return 0

    cur = harvest_fingerprint(HARVEST)
    prev = load_stamp(STAMP_PATH)

    if not force and unchanged(prev, cur):
        print(
            f"NO_CHANGE files={cur['file_count']} "
            f"sha={cur['manifest_sha256'][:12]} mtime_max={cur['mtime_max']:.0f}"
        )
        return 0

    if dry:
        print(
            f"WOULD_RUN files={cur['file_count']} "
            f"sha={cur['manifest_sha256'][:12]} prev_sha="
            f"{(prev or {}).get('manifest_sha256', 'none')[:12]}"
        )
        return 0

    STAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
    py = REPO / ".venv" / "bin" / "python"
    if not py.is_file():
        py = Path(sys.executable)
    cmd = [str(py), str(BATCH_SCRIPT), *argv]
    proc = subprocess.run(cmd, cwd=str(REPO))
    if proc.returncode != 0:
        print(f"SKIP batch_failed exit={proc.returncode}")
        return proc.returncode

    cur["batch_ran_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    cur["batch_argv"] = argv
    STAMP_PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"RAN files={cur['file_count']} sha={cur['manifest_sha256'][:12]} "
        f"stamp={STAMP_PATH}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
