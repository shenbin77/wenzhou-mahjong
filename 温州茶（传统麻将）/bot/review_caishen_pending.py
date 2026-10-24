"""Human review gate for pending caishen template candidates.

``bot/caishen_collector.py`` never touches the production manifest; a manual
pick can be a slip of the wrist, so a human confirms each candidate here.

Usage::

    python bot/review_caishen_pending.py            # 交互式逐张复核
    python bot/review_caishen_pending.py --list     # 只列出待复核条目
    python bot/review_caishen_pending.py --confirm <name.json>
    python bot/review_caishen_pending.py --confirm <name.json> --label t6
    python bot/review_caishen_pending.py --reject <name.json>

``--confirm`` appends the entry to ``templates/caishen/manifest.json`` with
``visual_check`` rewritten from ``pending: ...`` to a confirmed
note, moves the PNG next to the other templates, and drops the sidecar
JSON. The next frame picks the new template up automatically (the loader
re-reads the manifest when its mtime/size changes).

Candidates written by ``collect_auto_candidate`` carry a confidence grade.
An ``unknown`` grade has ``label: null`` (no template came close enough) and
must be named with ``--label <tile>`` at confirm time; ``--confirm`` refuses
to promote an unnamed entry so a mislabelled crop can never slip into the
production manifest.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import cv2
import numpy as np

BOT_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BOT_DIR / "templates" / "caishen"
PENDING_DIR = TEMPLATE_DIR / "pending"
MANIFEST = TEMPLATE_DIR / "manifest.json"
ZOOM = 8  # 复核放大倍数：ROI 只有 55x75，肉眼必须放大看


def _entries() -> list[dict]:
    if not PENDING_DIR.exists():
        return []
    out = []
    for path in sorted(PENDING_DIR.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        record["_sidecar"] = path.name
        out.append(record)
    return out


def _save_manifest(manifest: dict) -> None:
    tmp = MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(MANIFEST)


def _zoomed_png(png_path: Path) -> Path:
    """Write an 8x enlarged copy beside the candidate for eyeball review."""
    img = cv2.imdecode(np.fromfile(str(png_path), dtype=np.uint8), cv2.IMREAD_COLOR)
    out = PENDING_DIR / f"zoom_{png_path.stem}.png"
    if img is not None:
        cv2.imwrite(str(out), cv2.resize(img, None, fx=ZOOM, fy=ZOOM,
                                         interpolation=cv2.INTER_CUBIC))
    return out


def _known_labels() -> set[str]:
    """Label vocabulary a caishen pick may use (tile_vision.TILE_IDS)."""
    try:
        from tile_vision import TILE_IDS
        return set(TILE_IDS)
    except Exception:  # tile_vision 正被并行修改时退回静态词汇表
        return ({f"{suit}{rank}" for suit in "wts" for rank in range(1, 10)}
                | {"dong", "nan", "xi", "bei", "bai", "hongzhong", "fa"})


def confirm(name: str, label_override: str | None = None) -> int:
    sidecar = PENDING_DIR / name
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    label = label_override or record["label"]
    if not label:
        print("!! 该候选未命名（label=null）。"
              f"请用 --confirm {name} --label <牌面> 指定牌面后再入库")
        return 2
    if label not in _known_labels():
        print(f"!! 未知牌面: {label}")
        return 2
    png = PENDING_DIR / record["file"]
    if not png.exists():
        print(f"!! 候选图片缺失: {png}")
        return 2
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    taken = {item["file"] for item in manifest["templates"]}
    target = TEMPLATE_DIR / record["file"]
    if target.exists() or record["file"] in taken:
        print(f"!! manifest 已有同名模板: {record['file']}")
        return 2
    source_kind = record.get("source_kind") or (
        "auto_nomination" if str(record.get("visual_check", "")).startswith("pending: auto_")
        else "manual_pick")
    record["visual_check"] = (
        f"confirmed: {source_kind} reviewed {time.strftime('%Y-%m-%d %H:%M:%S')}; "
        "operator read the sprite by eye — never by cluster majority vote")
    shutil.move(str(png), str(target))
    manifest["templates"].append({
        "label": label,
        "file": record["file"],
        "source": record.get("source") or "unrecorded",
        "source_sha256": record.get("source_sha256"),
        "asset_sha256": record["asset_sha256"],
        "visual_check": record["visual_check"],
        "session_id": record.get("session_id"),
        "source_kind": source_kind,
        "review_method": "human_eye",
        "reviewer": "operator",
        "source_frame_hash": record.get("source_sha256"),
        "heldout_passed": False,
        "suggested_label": record.get("suggested_label"),
    })
    manifest["note"] = (
        "Provisional visual labels; source frames retained. Entries sourced under "
        "assist_sessions/*/live are lossless ADB screenshots so real-play frames clear "
        "_MIN_SCORE without relaxing it. A label may own several templates; detection "
        "keeps the best score per label. Every label was verified by eye on an 8x crop "
        "(live entries by the operator on the table, then re-checked here) — never by "
        "cluster majority vote.")
    _save_manifest(manifest)
    sidecar.unlink()
    zoom = PENDING_DIR / f"zoom_{Path(record['file']).stem}.png"
    if zoom.exists():
        zoom.unlink()
    print(f"OK 已入库: {label} <- {record['file']}")
    return 0


def reject(name: str) -> int:
    sidecar = PENDING_DIR / name
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    (PENDING_DIR / record["file"]).unlink(missing_ok=True)
    (PENDING_DIR / f"zoom_{Path(record['file']).stem}.png").unlink(missing_ok=True)
    sidecar.unlink()
    print(f"OK 已丢弃: {record['label']} {record['file']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="复核待入库的财神候选模板")
    parser.add_argument("--list", action="store_true", help="只列出待复核条目")
    parser.add_argument("--confirm", metavar="NAME", help="确认入库（侧车 JSON 文件名）")
    parser.add_argument("--label", metavar="TILE",
                        help="给未命名候选（label=null）指定牌面后入库，与 --confirm 同用")
    parser.add_argument("--reject", metavar="NAME", help="丢弃候选（侧车 JSON 文件名）")
    args = parser.parse_args(argv)

    if args.confirm:
        return confirm(args.confirm, args.label)
    if args.reject:
        return reject(args.reject)

    entries = _entries()
    if args.list or not entries:
        if not entries:
            print("pending 目录为空，没有待复核的财神候选。")
            return 0
        for record in entries:
            print(f"- {record['_sidecar']}  label={record['label']}  "
                  f"session={record.get('session_id')}  at={record.get('at')}  "
                  f"source={record.get('source')}")
        return 0

    for record in entries:
        png = PENDING_DIR / record["file"]
        zoom = _zoomed_png(png) if png.exists() else None
        label = record["label"]
        print("=" * 72)
        print(f"label={label or '(未命名)'}  file={record['file']}  "
              f"session={record.get('session_id')}  at={record.get('at')}")
        print(f"source frame: {record.get('source')}")
        if label is None:
            hints = record.get("topk") or []
            rendered = ", ".join(f"{h['label']}@{h['score']}" for h in hints)
            print(f"自动提名未能判定，参考 top-k: {rendered or '(无)'}")
        if zoom is not None:
            target = label or "哪张牌"
            print(f"放大图（请肉眼核对是不是 {target}）: {zoom}")
        answer = input("确认入库? [y=确认 / n=丢弃 / q=退出] ").strip().lower()
        if answer == "y":
            override = None
            if label is None:
                override = input("请输入这张牌的牌面（如 t6 / w5 / bai）: ").strip()
            confirm(record["_sidecar"], override)
        elif answer == "n":
            reject(record["_sidecar"])
        elif answer == "q":
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
