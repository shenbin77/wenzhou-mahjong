# -*- coding: utf-8 -*-
"""生成「两条写死修复前数字的断言」的补丁（不应用，留给 test 作者复核）。

1) test_action_animation_two_raised_boxes_not_cut
   修复前 count=12 / meld_boxes=[]  →  修复后 count=10 / 3 框 / groups=[3]
2) test_replay_frame_with_split_platform_groups
   修复前 5 框 / groups=[1,1,3]  →  修复后 6 框 / groups=[3,3]
"""
import pathlib
import subprocess

SRC = pathlib.Path("tests/test_tile_meld_seg.py")
OUT = pathlib.Path(
    "../_backups/20261024_run_merge/test_tile_meld_seg_stale_assertions.patch")

OLD1 = """    count, _ = vision.count_tiles(img)
    assert count == 12 and vision.last_meld_boxes == []
"""
NEW1 = """    count, _ = vision.count_tiles(img)
    # 该帧的第 2 个西也被牌面竖纹切成 39px+42px 两片而整张丢失；碎片合并后
    # 三个西都可见 -> 台面 3 框。bot 端这一帧仍然「识牌不完整」弃权，
    # 动画帧不会被当成可决策局面。
    assert count == 10 and len(vision.last_meld_boxes) == 3
    assert vision.detection_diagnostics["meld_groups"] == [3]
"""

OLD2 = """    assert count == 10 and len(vision.last_meld_boxes) == 5
    # 平台内 90px / 15px 两处间隙各切一刀：1 + 1 + 3 个可见牌位。
    assert vision.detection_diagnostics["meld_groups"] == [1, 1, 3]
"""
NEW2 = """    assert count == 10 and len(vision.last_meld_boxes) == 6
    # 平台真值是吃 6-7-8 筒 + 吃 1-2-3 条；原来的 90px 间隙就是被切掉的 7 筒。
    assert vision.detection_diagnostics["meld_groups"] == [3, 3]
"""


def main():
    text = SRC.read_text(encoding="utf-8")
    for old, new in ((OLD1, NEW1), (OLD2, NEW2)):
        assert text.count(old) == 1, (text.count(old), old[:40])
        text = text.replace(old, new, 1)
    tmp = pathlib.Path(".test_tile_meld_seg_patched.py")
    tmp.write_text(text, encoding="utf-8")
    try:
        res = subprocess.run(
            ["diff", "-u",
             "--label", "a/bot/tests/test_tile_meld_seg.py",
             "--label", "b/bot/tests/test_tile_meld_seg.py",
             str(SRC), str(tmp)],
            capture_output=True)
    finally:
        tmp.unlink()
    OUT.write_bytes(res.stdout)
    print(res.stdout.decode("utf-8", "replace"))
    print("patch bytes", OUT.stat().st_size)


if __name__ == "__main__":
    main()
