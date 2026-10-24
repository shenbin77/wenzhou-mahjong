# -*- coding: utf-8 -*-
"""生成「把碎片合并抽成模块级纯函数」的补丁（**不应用**），并验证行为等价。

产物：
  _backups/20261024_run_merge/extract_merge_split_runs.patch
  _backups/20261024_run_merge/tile_vision.py.extracted   （应用后的样子，供复查）

等价性验证：把改后的文件当独立模块加载，对 3 张代表性帧比对
(count, boxes, last_meld_boxes, meld_groups)，必须与当前文件完全一致。
"""
import importlib.util
import pathlib
import subprocess

BOT = pathlib.Path(".")
SRC = BOT / "tile_vision.py"
DST = pathlib.Path("../_backups/20261024_run_merge/tile_vision.py.extracted")
PATCH = pathlib.Path("../_backups/20261024_run_merge/extract_merge_split_runs.patch")

FUNC = '''def _merge_split_runs(runs, scale):
    """Merge runs that are two halves of one tile split by a glyph column.

    A dense glyph column (the 1-9 ring of a dot tile, an ink stroke of a
    character tile) can break the cream-column mask, so one face arrives as two
    runs that are each shorter than the 48*scale minimum tile width and are
    both discarded downstream (20261024_015126: the 7 of a 5-6-7 dots chi was
    lost this way and turned a legal meld into an illegal 2-tile group).

    Merge only when ALL of the following hold, so two real tiles can never be
    glued into one:
      * both sides are narrower than one tile (< 48*scale) - i.e. neither side
        is a tile on its own, both would be dropped anyway;
      * the gap is far below any real separation (< 6*scale): in-group tile
        seams are ~4px, inter-group gaps ~15px, hand seams 4-5px;
      * the result still fits inside one tile (<= 125*scale).
    The last clause is unreachable while the first two hold (47+5+47 = 99), and
    is kept only as a guard against future edits to the constants.
    """
    merged = []
    for a, b in runs:
        if merged:
            pa, pb = merged[-1]
            if (pb - pa) < 48 * scale and (b - a) < 48 * scale \\
                    and (a - pb) < 6 * scale and (b - pa) <= 125 * scale:
                merged[-1] = (pa, b)
                continue
        merged.append((a, b))
    return merged


'''

OLD_INLINE = """        # A glyph column can split one face into two runs; both are then dropped
        # by the width filter below (20261024_015126: the 7 of 5-6-7 dots was
        # lost this way, turning a legal chi into an illegal 2-tile group).
        # Merge only when BOTH sides are narrower than a single tile, the gap is
        # far below any real group gap, and the result still fits one tile.
        merged_runs = []
        for a, b in runs:
            if merged_runs:
                pa, pb = merged_runs[-1]
                if (pb - pa) < 48*scale and (b - a) < 48*scale \\
                        and (a - pb) < 6*scale and (b - pa) <= 125*scale:
                    merged_runs[-1] = (pa, b)
                    continue
            merged_runs.append((a, b))
        runs = merged_runs
"""
NEW_INLINE = """        runs = _merge_split_runs(runs, scale)
"""

CLASS_ANCHOR = "class TileVision:"


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fingerprint(module, frame):
    import cv2
    import numpy as np
    img = cv2.imdecode(np.fromfile(str(frame), np.uint8), cv2.IMREAD_COLOR)
    vision = module.TileVision(
        template_dir=pathlib.Path("../采集/templates/tea_tiles"))
    count, boxes = vision.count_tiles(img)
    return (count,
            tuple(tuple(int(v) for v in b) for b in boxes),
            tuple(tuple(int(v) for v in b) for b in vision.last_meld_boxes),
            tuple(vision.detection_diagnostics.get("meld_groups") or []))


def main():
    text = SRC.read_text(encoding="utf-8")
    assert text.count(CLASS_ANCHOR) == 1
    assert text.count(OLD_INLINE) == 1, text.count(OLD_INLINE)
    patched = text.replace(OLD_INLINE, NEW_INLINE, 1)
    patched = patched.replace(CLASS_ANCHOR, FUNC + CLASS_ANCHOR, 1)
    # 必须显式 newline="\n"：Windows 下 write_text 会把 \n 翻成 \r\n，
    # 那样 diff 会把整份文件都标成改动，评审时看不出真正改了哪几行。
    with DST.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(patched)

    tmp = BOT / ".tile_vision_extracted_check.py"
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(patched)
    try:
        cur = load_module(SRC, "tv_current")
        new = load_module(tmp, "tv_extracted")
        frames = [
            pathlib.Path("../采集/assist_sessions/20261024_015005/live/tea_2p"
                         "/20261024_015126_255781_543a04.png"),
            pathlib.Path("../采集/2026-10-16/740195/r10/frame_01.png"),
            pathlib.Path("../采集/assist_sessions/20261023_223759/live/tea_2p"
                         "/20261023_224750_469278_eec744.png"),
        ]
        ok = True
        for frame in frames:
            a = fingerprint(cur, frame)
            b = fingerprint(new, frame)
            same = a == b
            ok = ok and same
            print(("OK  " if same else "DIFF"), frame.name, a[0], a[3])
        print("行为等价:", ok)
    finally:
        tmp.unlink()

    res = subprocess.run(
        ["diff", "-u", "--label", "a/bot/tile_vision.py",
         "--label", "b/bot/tile_vision.py", str(SRC), str(DST)],
        capture_output=True)
    PATCH.write_bytes(res.stdout)
    print(res.stdout.decode("utf-8", "replace")[:1200])
    print("patch bytes", PATCH.stat().st_size)


if __name__ == "__main__":
    main()
