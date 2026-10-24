"""筒牌点数独立判据（pip_discriminator）与歧义分支的安全回归。

锁死三件事：
1. 判据本身正确：1-9 筒的模板逐个读对点数与排布；四筒 0 红点、五筒 1 红心；
2. 接入面最小：只在"top1/top2 都是筒牌且 margin 不足"这一条歧义分支生效，
   实机复现帧（真牌四筒，score 0.8921 / margin 0.0123）从 15/16 修复为 16/16；
3. 误判不上升：五筒不得被读成四筒；非筒牌与非歧义路径行为不变。
"""

import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import pip_discriminator as pipmod  # noqa: E402
from tile_vision import TILE_IDS, TileVision  # noqa: E402

TEA_ROOT = BOT_DIR.parent
TEMPLATES = TEA_ROOT / "采集" / "templates" / "tea_tiles"
MANIFEST = TEMPLATES / "manifest.json"
GOLD_MANIFEST = BOT_DIR / "gold" / "tea_2p_manifest.json"
TILE_IDS_INVERSE = {value: key for key, value in TILE_IDS.items()}

# 房间 869684 的实机帧：第 9 张牌真值四筒，模板 margin 只有 0.0123。
EVIDENCE_DIR = (TEA_ROOT / "采集" / "dev_auto_play" /
                "20261024_041701_0df7a7" / "frames")
EVIDENCE_FRAME = EVIDENCE_DIR / "20261024_041702_676954_before_e60a4f.png"
# 同一帧里被模板高置信度认成五筒的两张牌：它们是"四筒/五筒"边界上的对照组。
AMBIGUOUS_BOX = [837, 768, 925, 892]
T5_BOXES = [[929, 768, 1017, 892], [1021, 768, 1110, 892]]


def _image(path):
    image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None, f"复现帧缺失: {path}"
    return image


def _crop(image, box):
    x0, y0, x1, y1 = box
    return image[y0:y1, x0:x1]


def _template_for(label):
    """First template file the manifest declares for a vision label."""
    records = json.loads(MANIFEST.read_text(encoding="utf-8"))["templates"]
    for record in records:
        if record["label"] == label:
            return TEMPLATES / record["file"]
    raise AssertionError(f"模板清单里没有 {label}")


@pytest.mark.parametrize("label", [f"t{i}" for i in range(1, 10)])
def test_dot_template_reads_exact_pip_count(label):
    """每个筒牌模板都读出自身点数，并且排布通过标准形状校验。"""
    reading = pipmod.read_dot_pips(_image(_template_for(label)))
    assert reading.count == int(label[1:]), reading.as_dict()
    assert reading.layout_ok is True, reading.as_dict()
    assert reading.reason == "ok"


def test_four_dot_has_no_red_pip_and_five_dot_has_one():
    """四筒全绿、五筒中心红——这条物理差异是四/五歧义的第二重证据。"""
    four = pipmod.read_dot_pips(_image(_template_for("t4")))
    five = pipmod.read_dot_pips(_image(_template_for("t5")))
    assert (four.count, four.red_count) == (4, 0)
    assert (five.count, five.red_count) == (5, 1)


def test_five_dot_centre_pip_is_the_red_one():
    """五筒的红点是正中间那颗，不是任意一颗。"""
    reading = pipmod.read_dot_pips(_image(_template_for("t5")))
    centre = [flag for flag, (cx, cy) in
              zip(reading.red_flags, reading.centers)
              if abs(cx - .5) <= .17 and abs(cy - .54) <= .17]
    assert centre == [True]
    assert reading.red_count == 1


@pytest.mark.parametrize("label", ["w1", "w5", "w9", "s1", "s7", "s9",
                                   "dong", "xi", "bei", "bai", "hongzhong"])
def test_non_dot_templates_never_yield_a_pip_count(label):
    """条/万/字牌不得被判据读成"某点筒牌"，否则歧义分支会凭空定牌。"""
    path = _template_for(label)
    reading = pipmod.read_dot_pips(_image(path))
    assert reading.count is None, (label, reading.as_dict())
    assert reading.layout_ok is False


def test_marker_overlapping_the_face_abstains():
    """弃牌角标压在牌面顶部时点数不可信，必须弃权而不是少数一颗。"""
    marker_frame = TEMPLATES / "t6_live_20261023_201640.png"
    assert marker_frame.exists(), "对照模板缺失"
    reading = pipmod.read_dot_pips(_image(marker_frame))
    assert reading.count is None
    assert reading.reason == "border_touch"


def test_blank_and_tiny_crops_abstain():
    """空/过小的 ROI 不得抛异常，也不得给出点数。"""
    blank = np.full((120, 88, 3), 240, np.uint8)
    assert pipmod.read_dot_pips(blank).count is None
    assert pipmod.read_dot_pips(np.zeros((8, 8, 3), np.uint8)).count is None
    assert pipmod.read_dot_pips(None).reason == "empty"


@pytest.mark.parametrize("column,row", [(1, 2), (2, 2), (2, 3),
                                       (2, 4), (3, 3)])
def test_grid_layouts_are_validated_structurally(column, row):
    """规则网格排布通过；把各行压成一行则必须被拒。"""
    xs = ([.50] if column == 1 else
          [.20 + (column - 1 - i) * (.60 / (column - 1)) for i in range(column)])
    ys = ([.54] if row == 1 else
          [.20 + (row - 1 - j) * (.60 / (row - 1)) for j in range(row)])
    points = [(x, y) for y in ys for x in xs]
    assert pipmod._layout_ok(points, column * row) is True
    collapsed = [(x, .54) for x, _ in points]
    assert pipmod._layout_ok(collapsed, column * row) is False



def test_single_pip_layout_must_sit_near_the_centre():
    """一点筒只有一个圆点，位置必须接近牌面中心。"""
    assert pipmod._layout_ok([(.49, .54)], 1) is True
    assert pipmod._layout_ok([(.10, .10)], 1) is False
    assert pipmod._layout_ok([(.49, .54), (.70, .80)], 1) is False



def test_diagonal_layouts_for_three_and_seven():
    """三筒斜排、七筒"斜三 + 方四"都能通过；方四点不得冒充三筒。"""
    three = [(.29, .29), (.51, .54), (.71, .78)]
    assert pipmod._layout_ok(three, 3) is True
    # 七筒实测排布：上半段斜三被压缩（dy 只有 0.11），下面接 2x2。
    seven = [(.21, .20), (.50, .31), (.78, .42),
             (.32, .63), (.62, .63), (.32, .86), (.62, .86)]
    assert pipmod._layout_ok(seven, 7) is True
    four = [(.27, .29), (.70, .29), (.27, .78), (.70, .78)]
    assert pipmod._layout_ok(four, 3) is False
    # 斜三 + 方四 的 7 点排布不得被当成 3 点（点数不同，直接不成立）。
    assert pipmod._layout_ok(seven, 3) is False



def test_label_helpers():
    """只有 t1..t9 被认作筒牌；其他标签一律不参与点数裁决。"""
    assert pipmod.expected_pip_count("t4") == 4
    assert pipmod.is_dot_label("t9") is True
    for label in ("w4", "s4", "bai", "dong", "hongzhong", "", None, "t0", "t10"):
        assert pipmod.expected_pip_count(label) is None


def test_thresholds_are_untouched():
    """全局识牌门槛不得因本判据而下调。"""
    vision = TileVision(template_dir=TEMPLATES)
    assert vision.min_score == pytest.approx(.82)
    assert vision.min_margin == pytest.approx(.055)


@pytest.fixture(scope="module")
def evidence():
    """实机复现帧 + 改动后的一次完整识牌结果。"""
    image = _image(EVIDENCE_FRAME)
    vision = TileVision(template_dir=TEMPLATES)
    tiles = vision.recognize_hand(image)
    return image, vision, tiles


def test_ambiguous_dot_tile_is_settled_by_pip_count(evidence):
    """复现帧：15/16 → 16/16，且裁决理由是点数而不是抬高模板分。"""
    image, vision, tiles = evidence
    assert vision.detection_diagnostics["concealed_count"] == 16
    assert len(tiles) == 16
    assert vision.last_complete is True
    settled = [d for d in vision.diagnostics if d["reason"] == "pip_count"]
    assert len(settled) == 1
    entry = settled[0]
    assert entry["box"] == AMBIGUOUS_BOX
    assert (entry["label"], entry["tile_id"]) == ("t4", TILE_IDS["t4"])
    assert entry["pip_reading"]["count"] == 4
    assert entry["pip_reading"]["layout_ok"] is True
    assert entry["pip_reading"]["red_count"] == 0
    # 原始分数/差距必须留档，便于追溯"当时是低于 margin 才走到这个分支"。
    assert entry["pip_source_score"] == pytest.approx(.8920547962, abs=1e-9)
    assert entry["pip_source_margin"] == pytest.approx(.0123000741, abs=1e-9)
    # 模板分与阈值一个都没动。
    assert entry["score"] == pytest.approx(entry["pip_source_score"])
    assert entry["margin"] == pytest.approx(entry["pip_source_margin"])


def test_pip_count_does_not_rescue_the_five_dot_tiles(evidence):
    """同一帧两张五筒本来就是高置信度接受，判据不得改动它们的结论。"""
    image, vision, _ = evidence
    for box in T5_BOXES:
        entry = [d for d in vision.diagnostics if d["box"] == box]
        assert len(entry) == 1
        assert entry[0]["label"] == "t5"
        assert entry[0]["accepted"] is True
        assert entry[0]["reason"] == "accepted"
        assert "pip_reading" not in entry[0]


def test_fused_pips_abstain_instead_of_undercounting():
    """圆点糊成一整块时点数必须弃权——否则 8 筒会被"数"成 4 筒。

    实机对照：该皮肤 8 筒的 8 颗点全部相切，填充后是一整块，距离变换只在
    四角出峰（中间两行没有峰），朴素计数正好得到"4 点、标准 2×2"。
    面积自检（比 2.38 > 1.35）必须把它判成不可读。
    """
    frame = EVIDENCE_DIR / "20261024_042100_203891_before_1c6f38.png"
    assert frame.exists(), "对照帧缺失"
    reading = pipmod.read_dot_pips(_crop(_image(frame), [1113, 768, 1202, 892]))
    assert reading.count is None
    assert reading.reason == "fused_blob"


def test_five_dot_never_reads_as_four_across_evidence_frames():
    """误判守门：模板高置信度定牌的筒牌，点数判据要么一致、要么弃权。

    覆盖复现目录前 30 帧（每帧含 2 张四筒、2 张五筒，以及七筒/八筒/九筒）。
    判据给出点数时**必须**与模板标签一致；拿不准（糊块/裁边/排布不符）时
    允许弃权但不能给错。全量 1816 帧的同一口径回归见交付报告。
    """
    frames = sorted(EVIDENCE_DIR.glob("*.png"))[:30]
    assert len(frames) == 30
    checked, unreadable, conflicts = {}, {}, []
    for path in frames:
        image = _image(path)
        vision = TileVision(template_dir=TEMPLATES)
        vision.recognize_hand(image)
        for entry in vision.diagnostics:
            label = entry.get("label")
            expected = pipmod.expected_pip_count(label)
            if not entry.get("accepted") or expected is None:
                continue
            checked[label] = checked.get(label, 0) + 1
            reading = pipmod.read_dot_pips(_crop(image, entry["box"]))
            if reading.count is None:
                unreadable[reading.reason] = unreadable.get(reading.reason, 0) + 1
            elif reading.count != expected:
                conflicts.append((path.name, entry["box"], label,
                                  reading.count, reading.reason))
    assert checked.get("t4", 0) >= 30 and checked.get("t5", 0) >= 30
    assert conflicts == []
    # 可读率不是本判据的验收指标（弃权是安全方向），但要求大部分牌读得出。
    assert sum(checked.values()) - sum(unreadable.values()) >= sum(checked.values()) * .8


def test_non_dot_ambiguity_is_still_abstained(evidence):
    """非筒牌的歧义分支保持原样：不得出现点数裁决，也不得凭空接受。"""
    image, vision, _ = evidence
    for entry in vision.diagnostics:
        if entry["reason"] == "pip_count":
            assert pipmod.is_dot_label(entry["label"])
        if not entry["accepted"]:
            assert entry["tile_id"] is None


def test_gold_frames_keep_zero_wrong_tiles():
    """金标回放：已标注手牌上不得出现任何"认错的牌"。"""
    records = json.loads(GOLD_MANIFEST.read_text(encoding="utf-8"))["frames"]
    assert records, "金标清单为空"
    for record in records:
        # 清单里的 frame_path 相对 D:\温州麻将（含"温州茶（传统麻将）"前缀）。
        path = TEA_ROOT.parent / record["frame_path"]

        assert path.exists(), f"金标帧缺失: {path}"
        image = _image(path)
        vision = TileVision(template_dir=TEMPLATES)
        tiles = vision.recognize_hand(image)
        predicted = [TILE_IDS_INVERSE[tile_id] for tile_id, *_ in tiles]
        gold = record["annotated_hand_multiset"]
        remaining = dict(gold)
        wrong = []
        for label in predicted:
            if remaining.get(label, 0) > 0:
                remaining[label] -= 1
            else:
                wrong.append(label)
        assert wrong == [], (record["frame_path"], wrong)
