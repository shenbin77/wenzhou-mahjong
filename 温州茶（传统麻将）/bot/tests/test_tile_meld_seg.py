"""Regression tests for the exposed-meld prefix segmentation in count_tiles.

Defect (2026-10-24, dev session 20261024_005400): a 西杠 platform renders its
stacked 4th tile as a middle box raised ~26px, so the old per-box baseline
test rejected the first box and the whole gang was counted into the closed
hand (concealed=16, meld_boxes=[] → "识牌不完整" abstain).

The new segmentation is group-shape based: leading raised run + platform
boundary gap + 3-4 position groups. Label evidence is intentionally not
used to declare a gang (three identical faces may be a peng).
"""

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from tile_vision import TileVision  # noqa: E402

TEA_TEMPLATES = BOT_DIR.parent / "采集" / "templates" / "tea_tiles"

# dev 开发局会话（BOSS 2026-10-24 规则）：数据标记 dev，不进训练语料。
XIGANG_FRAME = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261024_005400"
                / "live" / "tea_2p" / "20261024_005700_684588_0ae39a.png")
TRUE_PENG_FRAME = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261023_201640"
                   / "live" / "tea_2p" / "20261023_205534_475580_f4a34e.png")
FULL_HAND_FRAME = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261023_235121"
                   / "live" / "tea_2p" / "20261023_235126_427878_8ef13c.png")
ANKONG_FRAME = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261023_223759"
                / "live" / "tea_2p" / "20261023_223915_802417_ee3d70.png")
TWO_RAISED_FRAME = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261023_223759"
                    / "live" / "tea_2p" / "20261023_224750_469278_eec744.png")
CHI_CLIPPED_MARKER_FRAMES = [
    (BOT_DIR.parent / "采集" / "dev_auto_play"
     / "ui_20261027_134956_emulator5554_玉姐" / "frames" / name)
    for name in (
        "20261027_135318_100503_after_chi_abd550.png",
        "20261027_135319_559613_after_chi_8642cc.png",
    )
]


def make_vision() -> TileVision:
    return TileVision(template_dir=TEA_TEMPLATES)


def flat_row(x0, bottom, count, width=84, height=114, gap=4):
    return [(x0 + i * (width + gap), bottom - height,
             x0 + i * (width + gap) + width, bottom)
            for i in range(count)]


def hand_row(x0, count, width=88, height=124, gap=4, bottom=892):
    return flat_row(x0, bottom, count, width, height, gap)


# ---------------------------------------------------------------------------
# Pure-geometry unit tests (no images needed)
# ---------------------------------------------------------------------------

def test_gang_style_prefix_with_raised_middle_is_split():
    """西杠形态：三组副露，第一组中间框抬高 26px，其余组齐平。"""
    melds = flat_row(6, 875, 2)
    melds.insert(1, (93, 735, 175, 849))          # stacked 4th gang tile
    melds += flat_row(278, 875, 3)
    melds += flat_row(551, 875, 3)
    boxes = melds + hand_row(837, 7)
    got_melds, rest, groups = TileVision._split_exposed_prefix(boxes, 1.0)
    assert len(got_melds) == 9
    assert len(rest) == 7
    assert got_melds[1] == (93, 735, 175, 849)    # raised box kept, not cut
    assert groups == [3, 3, 3]


def test_flat_single_peng_group_is_split():
    boxes = flat_row(6, 875, 3) + hand_row(283, 14)
    got_melds, rest, groups = TileVision._split_exposed_prefix(boxes, 1.0)
    assert got_melds == flat_row(6, 875, 3)
    assert len(rest) == 14
    assert groups == [3]


def test_flat_hand_never_cuts():
    boxes = hand_row(7, 17)
    got_melds, rest, groups = TileVision._split_exposed_prefix(boxes, 1.0)
    assert got_melds == [] and len(rest) == 17 and groups == []


def test_two_raised_boxes_are_not_a_meld():
    boxes = flat_row(6, 875, 2) + hand_row(561, 10, gap=90)
    got_melds, rest, groups = TileVision._split_exposed_prefix(boxes, 1.0)
    assert got_melds == [] and len(rest) == 12 and groups == []


def test_boundary_gap_too_small_refuses_cut():
    """抬升组与暗手只隔 10px（小于实测边界 21-30px）→ 拒绝整组切割。"""
    boxes = flat_row(6, 875, 3) + hand_row(272, 13, gap=4)  # 262→272 = 10px
    got_melds, rest, groups = TileVision._split_exposed_prefix(boxes, 1.0)
    assert got_melds == [] and len(rest) == 16 and groups == []


def test_odd_group_sizes_still_leave_the_hand():
    """组大小异常只进审计字段，绝不把平台牌拖回暗手（r10 回放帧形态）。"""
    boxes = flat_row(6, 875, 1) + flat_row(179, 875, 4) + hand_row(560, 10)
    got_melds, rest, groups = TileVision._split_exposed_prefix(boxes, 1.0)
    assert len(got_melds) == 5 and len(rest) == 10
    assert groups == [1, 4]


def test_illegal_trailing_platform_group_returns_to_concealed_hand():
    """实机 5558：真八条碰后，抬高的四万/五万/白板仍是暗手。"""
    vision = make_vision()
    true_meld = flat_row(6, 875, 3)
    false_meld = flat_row(278, 875, 3)
    hand = hand_row(560, 10)
    vision.last_meld_boxes = true_meld + false_meld
    vision.detection_diagnostics = {
        "meld_groups": [3, 3],
        "meld_boxes": [list(box) for box in vision.last_meld_boxes],
        "concealed_count": 10,
    }
    vision.meld_diagnostics = [
        *[{"accepted": True, "tile_id": 48, "box": list(box),
           "region": "exposed"} for box in true_meld],
        *[{"accepted": True, "tile_id": tile_id, "box": list(box),
           "region": "exposed"}
          for tile_id, box in zip((4, 5, 67), false_meld)],
    ]
    vision.diagnostics = [
        {"accepted": True, "tile_id": 21, "box": list(box),
         "region": "concealed"} for box in hand]

    corrected = vision._return_invalid_trailing_platform_groups(hand)

    assert corrected[:3] == false_meld
    assert vision.last_meld_boxes == true_meld
    assert vision.detection_diagnostics["meld_groups"] == [3]
    assert vision.detection_diagnostics["concealed_count"] == 13
    assert [d["region"] for d in vision.diagnostics[:3]] == ["concealed"] * 3
    assert vision.detection_diagnostics["platform_reclassified_groups"][0][
        "tile_ids"] == [4, 5, 67]


def test_narrow_blank_tile_retries_with_median_width(monkeypatch):
    """仅白板窄框允许扩到本帧中位宽，其他低分牌不走该恢复路径。"""
    vision = make_vision()
    image = np.zeros((900, 1600, 3), np.uint8)

    def fake_classify(crop, caishen_label=None):
        width = crop.shape[1]
        return {"accepted": width >= 80, "tile_id": 67 if width >= 80 else None,
                "label": "bai", "score": .94 if width >= 80 else .56,
                "margin": .60 if width >= 80 else .08,
                "reason": "accepted" if width >= 80 else "low_score"}

    monkeypatch.setattr(vision, "classify_crop", fake_classify)
    result, fixed = vision._classify_detected_box(
        image, (452, 761, 513, 875), None,
        typical_width=88, next_left=560)

    assert fixed == (452, 761, 540, 875)
    assert result["accepted"] is True
    assert result["reason"] == "accepted_narrow_bai_retry"
    assert result["original_box"] == [452, 761, 513, 875]


# ---------------------------------------------------------------------------
# Real-frame regressions (frames shipped with the repo)
# ---------------------------------------------------------------------------

def need(frame: Path):
    if not frame.is_file():
        pytest.skip(f"fixture frame missing: {frame}")


def test_xigang_dev_frame_separates_three_meld_groups():
    """修复目标帧（dev）：西杠不再被算进暗手。

    before: concealed=16, last_meld_boxes=[]
    after:  concealed=7, last_meld_boxes=9（西杠 + 两个吃/碰组）
    """
    need(XIGANG_FRAME)
    vision = make_vision()
    img = cv2.imdecode(np.fromfile(str(XIGANG_FRAME), np.uint8), cv2.IMREAD_COLOR)
    count, boxes = vision.count_tiles(img)
    assert vision.detection_diagnostics["visible_count"] == 16
    assert count == 7 and len(boxes) == 7
    assert len(vision.last_meld_boxes) == 9
    # 第一组就是西杠：中间框是叠放的第四张，保持原位、不得虚构第四个框。
    assert vision.last_meld_boxes[:3] == [(6, 761, 89, 875),
                                          (93, 735, 175, 849),
                                          (179, 761, 263, 875)]
    # 算术：明牌框只报可见牌位，不虚构暗杠的第四张。
    assert count + len(vision.last_meld_boxes) == 16


def test_xigang_dev_frame_claim_markers_no_longer_force_abstain():
    """几何修复 + claim-marker 去遮挡后，该帧副露高分可读且完整。

    旧断言要求 weak low_score/ambiguous 仍弃权；那些弱面其实是顶边黄三角
    半透明黑圆遮挡导致的掉分，不是阈值过松。strip_claim_marker 恢复牌面后
    分数回到阈值之上（仍用原 .82/.055，不放宽）。
    """
    need(XIGANG_FRAME)
    vision = make_vision()
    img = cv2.imdecode(np.fromfile(str(XIGANG_FRAME), np.uint8), cv2.IMREAD_COLOR)
    vision.recognize_hand(img)
    # 西杠三张全部按西读出（牌面证据，而非几何断言）。
    xi = [d for d in vision.meld_diagnostics if d.get("label") == "xi"]
    assert len(xi) == 3 and all(d["accepted"] for d in xi)
    # 遮挡剥除后不应再出现低分/歧义弃权；整帧应完整。
    weak = [d for d in vision.meld_diagnostics
            if d.get("reason") in ("low_score", "ambiguous")]
    assert weak == []
    assert all(d["accepted"] for d in vision.meld_diagnostics)
    assert any(d.get("claim_marker") for d in vision.meld_diagnostics)
    assert vision.last_complete is True


@pytest.mark.parametrize("frame", CHI_CLIPPED_MARKER_FRAMES)
def test_new_chi_with_clipped_source_marker_reads_complete_run(frame):
    """底边新吃组只露出黄三角时，仍须按原阈值读全 4万/5万/6万。"""
    need(frame)
    vision = TileVision(template_dir=TEA_TEMPLATES, caishen_label="t1")
    img = cv2.imdecode(np.fromfile(str(frame), np.uint8), cv2.IMREAD_COLOR)

    vision.recognize_hand(img, caishen_label="t1")

    assert vision.detection_diagnostics["meld_groups"] == [3]
    assert [d["label"] for d in vision.meld_diagnostics] == ["w4", "w5", "w6"]
    assert all(d["accepted"] for d in vision.meld_diagnostics)
    assert vision.meld_diagnostics[0].get("claim_marker") is True
    assert [tile_id for tile_id, *_ in vision.last_melds] == [4, 5, 6]
    assert vision.last_complete is True


def test_true_peng_frame_result_unchanged():
    """正例：真副露（碰/吃平台）修复前后都是 14+3。"""
    need(TRUE_PENG_FRAME)
    vision = make_vision()
    img = cv2.imdecode(np.fromfile(str(TRUE_PENG_FRAME), np.uint8),
                       cv2.IMREAD_COLOR)
    count, _ = vision.count_tiles(img)
    assert count == 14
    assert vision.last_meld_boxes == [(6, 761, 89, 875), (93, 761, 175, 875),
                                      (179, 761, 262, 875)]


def test_full_hand_frame_is_never_cut():
    need(FULL_HAND_FRAME)
    vision = make_vision()
    img = cv2.imdecode(np.fromfile(str(FULL_HAND_FRAME), np.uint8),
                       cv2.IMREAD_COLOR)
    count, _ = vision.count_tiles(img)
    assert count == 17 and vision.last_meld_boxes == []


def test_concealed_kong_frame_reports_visible_positions_only():
    """暗杠算术：浮在 ROI 外的牌组不算、第四张不虚构。

    223915 帧：暗杠动画在手牌行上方（ROI y≥720 之外），牌行 15 张且有一处
    缺口；vision 只报可见几何：concealed=15, meld_boxes=[]。
    """
    need(ANKONG_FRAME)
    vision = make_vision()
    img = cv2.imdecode(np.fromfile(str(ANKONG_FRAME), np.uint8), cv2.IMREAD_COLOR)
    count, boxes = vision.count_tiles(img)
    assert count == 15 and vision.last_meld_boxes == []
    assert count + len(vision.last_meld_boxes) == 15
    # 牌行第一缺口（暗杠被抬走的槽位）不虚构补齐。
    assert boxes[0][2] < boxes[1][0] - 50


def test_action_animation_frame_recovers_third_tile_and_still_abstains():
    """动画帧：竖纹把第 2 张西切成两片（39+42px）时，必须合并补回整张。

    2026-10-24 run 合并补丁（tile_vision.py 1b7778ea → 80f65413）之前，这张
    牌被当成两段窄碎片丢掉，台面只剩 2 个西 → cut<3 整段不切，count=12、
    meld_boxes=[]。补回第 3 张后平台切出 1 组 3 框、count=10。
    **最终行为不变**：bot 端仍然弃权（暗手 10 张 ≠ 13/14），不会拿动画帧决策。
    """
    need(TWO_RAISED_FRAME)
    vision = make_vision()
    img = cv2.imdecode(np.fromfile(str(TWO_RAISED_FRAME), np.uint8),
                       cv2.IMREAD_COLOR)
    count, _ = vision.count_tiles(img)
    assert count == 10 and len(vision.last_meld_boxes) == 3
    assert vision.detection_diagnostics["meld_groups"] == [3]
    # 补回来的是整张牌，不是两片碎片：最窄框也要有正常牌宽（≥78px）。
    widths = [box[2] - box[0] for box in vision.last_meld_boxes]
    assert min(widths) >= 78, widths
    assert count + len(vision.last_meld_boxes) == \
        vision.detection_diagnostics["visible_count"]

    # 中间数字变了，结论没变：bot 端必须仍然弃权（"不许给出建议"才是本用例真意图）。
    # 实测 reason 是 ``暗手应为13或14张，识别到10张``，不是泛化的"识牌不完整"，
    # 这里锁具体计数原因，比锁一句笼统文案更抗改动。
    from assist_tea import analyze_snapshot
    result = analyze_snapshot(TWO_RAISED_FRAME, caishen_label="",
                              confirmed_two_player=True, log_dir=None)
    assert result["status"] == "abstain"
    assert "暗手应为13或14张" in (result.get("reason") or "")
    assert not result.get("suggested_boxes")
    assert result.get("suggestion") is None


def test_replay_frame_platform_is_two_complete_chows():
    """回归（锁住既有安全契约 test_tea_bot 的同一帧）：

    r10 回放帧平台真值是两组各三张（吃 6-7-8 筒 + 吃 1-2-3 筒，共 6 个可见
    牌位）。2026-10-24 run 合并补丁之前，中间那张 7 筒被竖纹切成两片、双双
    低于最小牌宽而丢弃，于是看起来是"首框与其余四框隔 90px"、只切出 5 框
    （groups [1,1,3]）——那个 90px 就是少掉的 7 筒，1+1+3 是被切掉一张后的
    假象。补回后 groups=[3,3]、6 框，且 count + melds == visible_count 仍成立。
    安全契约不变：平台 6 框全部离开暗手，回放帧不能冒充完整闭手/报胡。
    """
    frame = (BOT_DIR.parent / "采集" / "2026-10-16" / "740195" / "r10"
             / "frame_01.png")
    need(frame)
    vision = make_vision()
    img = cv2.imdecode(np.fromfile(str(frame), np.uint8), cv2.IMREAD_COLOR)
    count, _ = vision.count_tiles(img)
    assert count == 10 and len(vision.last_meld_boxes) == 6
    assert vision.detection_diagnostics["meld_groups"] == [3, 3]
    assert count + len(vision.last_meld_boxes) == \
        vision.detection_diagnostics["visible_count"]
    # 6 个牌位里没有"半张"：每框宽度都在正常牌宽区间，且组内 15px 组间间隙
    # 只有一处（第 3、4 框之间），说明是两组各三张而不是碎片拼出来的。
    boxes = vision.last_meld_boxes
    widths = [box[2] - box[0] for box in boxes]
    assert min(widths) >= 78, widths
    gaps = [boxes[i + 1][0] - boxes[i][2] for i in range(len(boxes) - 1)]
    assert sum(1 for gap in gaps if gap >= 12) == 1, gaps

    # 安全契约：该帧只能得到"等待摸牌"，绝不允许报胡。
    # 注：``analyze_snapshot`` 的返回体没有 is_hu 字段（实测 None 表示未提供），
    # 胡牌与否由 status 承载（assist_tea：``"hu" if thought.is_hu else ...``），
    # 所以"未报胡"在此等于 status == "wait" 且不是 "hu"，并同时没给任何建议。
    from assist_tea import analyze_snapshot
    result = analyze_snapshot(frame, caishen_label="",
                              confirmed_two_player=True, log_dir=None)
    assert result["status"] == "wait"
    assert result["status"] != "hu"
    assert not result.get("suggested_boxes")


# ---------------------------------------------------------------------------
# 弱牌兜底（triplet correlation）的**下界**：三同牌本身不是证据
#   正向（降 1 张必须恢复）由 bot/tests/test_weak_tile_recover_path.py 锁；
#   本文件只锁反向：同组弱 2 张时兜底必须放弃，宁可少一张也不许造。
# ---------------------------------------------------------------------------
TRIPLET_FRAME = (BOT_DIR.parent / "采集" / "assist_sessions" / "diagnostics"
                 / "codex_live_20261023_233630.png")


def demote_nth_label(monkeypatch, label: str, occurrences):
    """Force selected occurrences of one label into the weak-score path.

    2026-10-24 02:26 三张实战模板入库后，本帧九筒首轮直接 accepted（0.92–0.97），
    兜底分支不会再被真实弱牌触发。这里在 ``classify_crop`` 外层把第 N 次出现的
    该 label 人工降成 low_score（label 与其余读数保持真实），以便确定性地
    复现"同组里有两张没认出来"的边界场景。
    """
    original = TileVision.classify_crop
    state = {"seen": 0}

    def wrapper(self, crop, caishen_label=None):
        result = original(self, crop, caishen_label)
        if result.get("label") == label:
            state["seen"] += 1
            if state["seen"] in occurrences:
                result = dict(result)
                result.update(tile_id=None, accepted=False, reason="low_score",
                              score=0.94, margin=0.30)
        return result

    monkeypatch.setattr(TileVision, "classify_crop", wrapper)
    return state


def test_two_weak_tiles_are_not_recovered_by_mere_triplet(monkeypatch):
    """同组两张弱牌：三同牌本身不算证明 → 不许恢复，宁缺勿造。

    兜底的前提是"另两张都已独立通过阈值"（见 tile_vision.recover_triplet：
    good 必须满 2 个）。这里把第 2、3 张九筒都降成低保真，只剩第 1 张有证据，
    于是三张不能成立；副露只剩一张九筒 + 三张一筒，且不得出现兜底专用 reason。
    """
    need(TRIPLET_FRAME)
    demote_nth_label(monkeypatch, "t9", {2, 3})
    vision = make_vision()
    vision.caishen_label = "w2"
    img = cv2.imdecode(np.fromfile(str(TRIPLET_FRAME), np.uint8), cv2.IMREAD_COLOR)
    vision.recognize_hand(img)

    reasons = [d["reason"] for d in vision.meld_diagnostics]
    assert "exposed_triplet_correlated" not in reasons
    assert [row[0] for row in vision.last_melds] == [29, 1, 1, 1]


# ---------------------------------------------------------------------------
# run 合并补丁的"漏检指纹"反向锁：碎片补回后，平台到手间隙必须回到正常区间
# ---------------------------------------------------------------------------
DEV_GAP_FRAME = (BOT_DIR.parent / "采集" / "assist_sessions" / "20261024_015005"
                 / "live" / "tea_2p" / "20261024_015126_255781_543a04.png")


def test_platform_hand_gap_returns_to_normal_after_fragment_recovery():
    """dev 开发局帧（20261024_015005）：补回被切碎的牌后，间隙必须回到 21–30px。

    补丁前该帧平台到暗手间隙 112px ≈ 一个牌距 + 正常缝，这正是"少了一张牌"的
    几何指纹；补回后 26px，落在真实牌桌的正常区间（实测 21–30px）。这里同时锁
    守恒与"每框都是整张牌"，避免日后有人只看框数就以为对了。
    """
    need(DEV_GAP_FRAME)
    vision = make_vision()
    img = cv2.imdecode(np.fromfile(str(DEV_GAP_FRAME), np.uint8), cv2.IMREAD_COLOR)
    count, boxes = vision.count_tiles(img)

    assert count == 11 and len(vision.last_meld_boxes) == 6
    assert vision.detection_diagnostics["meld_groups"] == [3, 3]
    # 守恒：暗手 + 副露 == 台面可见牌位数。
    assert count + len(vision.last_meld_boxes) == \
        vision.detection_diagnostics["visible_count"]
    widths = [box[2] - box[0] for box in vision.last_meld_boxes]
    assert min(widths) >= 78, widths
    # 平台最后一张到暗手第一张的间隙：正常 21–30px，绝不能是 112px 那种漏检指纹。
    gap = boxes[0][0] - vision.last_meld_boxes[-1][2]
    assert 21 <= gap <= 30, gap

    # dev 帧端到端仍要能正常出建议（副露局面：两组副露 + 11 张暗手）。
    from assist_tea import analyze_snapshot
    result = analyze_snapshot(DEV_GAP_FRAME, caishen_label="",
                              confirmed_two_player=True, log_dir=None)
    assert result["status"] == "advise"
    assert len(result["hand_ids"]) == 11
    assert result["suggested_boxes"]


# ---------------------------------------------------------------------------
# 2026-10-27 claim 复核事故：碰白板成第二副露组，新组三框被列游程切分带偏
# （首框 62px、后两框左移 13-18px），牌面分类全灭 -> assist 层整组扣留 ->
# 复核读到「副露组数 1->0，预期 +1」停手。两账号同签名、连拍两帧同读、
# 100 秒后帧同读：确定性坏读，不是落定动画。修复 = 副露框有界重对齐
# （_realign_meld_box）：换裁框仍走原 min_score/min_margin，阈值未动。
# ---------------------------------------------------------------------------
_DEV_AUTO = BOT_DIR.parent / "采集" / "dev_auto_play"
PENG_BAI_CASE1_DIR = (_DEV_AUTO / "ui_20261027_174410_emulator5556_烘熊瞎"
                      / "frames")
PENG_BAI_CASE2_DIR = (_DEV_AUTO / "ui_20261027_174759_emulator5554_玉姐"
                      / "frames")
PENG_BAI_CASE1_BEFORE = (PENG_BAI_CASE1_DIR
                         / "20261027_180503_660854_before_8342dc.png")
PENG_BAI_CASE1_AFTER = [
    PENG_BAI_CASE1_DIR / "20261027_180511_888985_after_claim_3a7f4c.png",
    PENG_BAI_CASE1_DIR / "20261027_180513_041932_after_claim_07fe39.png",
]
PENG_BAI_CASE2_BEFORE = (PENG_BAI_CASE2_DIR
                         / "20261027_182028_244398_before_fc3f19.png")
PENG_BAI_CASE2_AFTER = [
    PENG_BAI_CASE2_DIR / "20261027_182034_196846_after_claim_b844e1.png",
    PENG_BAI_CASE2_DIR / "20261027_182035_477946_after_claim_48f3e2.png",
]


def _board_from_frame(frame: Path, log_dir: Path):
    """与 auto_play_loop._board_from_frame 相同的生产读路。"""
    from live_capture import analyze_bytes
    from action_policy import HandState, BoardState
    from claim_vision import read_claims

    raw = frame.read_bytes()
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    result = analyze_bytes(raw, mode="tea_2p", image_path=frame,
                           log_dir=log_dir)
    claims = read_claims(image)
    hand = HandState.from_analysis(result)
    return BoardState.from_hand_state(
        hand, claim_buttons=len(claims.buttons), note="regression")


def _vision_meld_label_groups(frame: Path):
    vision = make_vision()
    img = cv2.imdecode(np.fromfile(str(frame), np.uint8), cv2.IMREAD_COLOR)
    vision.recognize_hand(img)
    groups, pos = [], 0
    for size in vision.detection_diagnostics["meld_groups"]:
        groups.append([d.get("label") if d.get("accepted") else None
                       for d in vision.meld_diagnostics[pos:pos + size]])
        pos += size
    return vision, groups


@pytest.mark.parametrize("frame", PENG_BAI_CASE1_AFTER)
def test_peng_bai_case1_after_frames_publish_two_groups(frame):
    """例 1（烘熊瞎第 7 局）复核帧：白板组必须读出，不再整组扣留。"""
    need(frame)
    vision, groups = _vision_meld_label_groups(frame)
    assert groups == [["s2", "s3", "s4"], ["bai", "bai", "bai"]]
    reasons = [d.get("reason") for d in vision.meld_diagnostics]
    assert "accepted_meld_realign" in reasons


@pytest.mark.parametrize("frame", PENG_BAI_CASE2_AFTER)
def test_peng_bai_case2_after_frames_publish_two_groups(frame):
    """例 2（玉姐第 12 局）复核帧：跨账号同签名，同样必须读出。"""
    need(frame)
    vision, groups = _vision_meld_label_groups(frame)
    assert groups == [["t5", "t6", "t7"], ["bai", "bai", "bai"]]
    reasons = [d.get("reason") for d in vision.meld_diagnostics]
    assert "accepted_meld_realign" in reasons


@pytest.mark.parametrize("before_frame,after_frames", [
    (PENG_BAI_CASE1_BEFORE, PENG_BAI_CASE1_AFTER),
    (PENG_BAI_CASE2_BEFORE, PENG_BAI_CASE2_AFTER),
])
def test_peng_bai_claim_verify_passes_end_to_end(before_frame, after_frames,
                                                 tmp_path):
    """生产读路端到端：碰白板复核必须通过（暗手 -2、副露 +1 组、圆盘消失）。"""
    from action_policy import verify_after_click

    need(before_frame)
    before = _board_from_frame(before_frame, tmp_path)
    assert before.concealed_count == 13 and before.meld_group_count == 1
    for frame in after_frames:
        need(frame)
        after = _board_from_frame(frame, tmp_path)
        assert after.concealed_count == 11 and after.meld_group_count == 2
        ok, why = verify_after_click(before, after, "peng")
        assert ok, why


# ---------------------------------------------------------------------------
# 重对齐单元测试（假 classify_crop）：锁死「只换裁框、不降阈值」
# ---------------------------------------------------------------------------

def _fake_classify_by_fill(vision, monkeypatch):
    """裁框与亮牌面（x 108..188, w=80）的重合度决定分数：对准才过线。"""

    def fake_classify(crop, caishen_label=None):
        fill = float(crop.mean()) / 200.0 if crop.size else 0.0
        accepted = fill >= 0.95
        return {"accepted": accepted, "tile_id": 67 if accepted else None,
                "label": "bai", "score": round(fill, 3),
                "margin": 0.6 if accepted else 0.05,
                "reason": "accepted" if accepted else "low_score"}

    monkeypatch.setattr(vision, "classify_crop", fake_classify)


def _shifted_blank_image():
    img = np.zeros((200, 400, 3), np.uint8)
    img[50:164, 108:188] = 200          # 真牌面：x 108..188（宽 80）
    return img


def test_meld_realign_recovers_shifted_blank_face(monkeypatch):
    """原框 (92..154, 62 宽) 只覆盖牌面 74% -> 原判低分；重对齐后过原阈值。"""
    vision = make_vision()
    _fake_classify_by_fill(vision, monkeypatch)
    found = vision._realign_meld_box(
        _shifted_blank_image(), (92, 50, 154, 164), None,
        typical_width=89, next_left=300)
    assert found is not None
    result, used = found
    assert result["accepted"] is True
    assert result["reason"] == "accepted_meld_realign"
    assert result["original_box"] == [92, 50, 154, 164]
    # 采用的裁框必须真正对准亮牌面（x 108..188）：与之重叠至少 55px，
    # 且中心仍留在原框中心的护栏内（宽的 1/4）。
    overlap = min(used[2], 188) - max(used[0], 108)
    assert overlap >= 55, used
    assert abs((used[0] + used[2]) / 2 - 123) <= (used[2] - used[0]) / 4


def test_meld_realign_never_accepts_below_threshold(monkeypatch):
    """任何候选裁框都过不了原阈值时，必须保持原判弃权（安全语义不变）。"""
    vision = make_vision()
    monkeypatch.setattr(
        vision, "classify_crop",
        lambda crop, caishen_label=None: {
            "accepted": False, "tile_id": None, "label": "bai",
            "score": 0.5, "margin": 0.3, "reason": "low_score"})
    found = vision._realign_meld_box(
        _shifted_blank_image(), (92, 50, 154, 164), None,
        typical_width=89, next_left=300)
    assert found is None


def test_base_classifier_has_no_realign(monkeypatch):
    """基础分类路径（暗手/副露首轮）本身不带重对齐：同一坏框保持原判。

    重对齐只存在于 recognize_hand 的兜底阶段（相关性恢复之后），
    _classify_detected_box 的行为与修复前一字不变。
    """
    vision = make_vision()
    _fake_classify_by_fill(vision, monkeypatch)
    result, used = vision._classify_detected_box(
        _shifted_blank_image(), (92, 50, 154, 164), None,
        typical_width=89, next_left=300)
    assert result["accepted"] is False
    assert used == (92, 50, 154, 164)


def test_realign_stage_gates(monkeypatch):
    """调度层三道门：只有 low_score 且分数低于阈值的框才被重对齐。

    idx0（low_score 0.5）被救回；idx1（low_score 但分数 0.94，模拟人工
    降分/高分被拒）与 idx2（reason=ambiguous）必须原样保留——前者正是
    test_two_weak_tiles_are_not_recovered_by_mere_triplet 与单张弱牌兜底
    契约依赖的边界：高分被拒的牌只能走相关性兜底，不许被重试绕过。
    """
    vision = make_vision()
    _fake_classify_by_fill(vision, monkeypatch)
    box = (92, 50, 154, 164)
    vision.last_meld_boxes = [list(box), list(box), list(box)]
    vision.detection_diagnostics = {"meld_groups": [3]}
    vision.meld_diagnostics = [
        {"accepted": False, "reason": "low_score", "score": 0.5,
         "label": "bai", "tile_id": None, "box": list(box),
         "region": "exposed"},
        {"accepted": False, "reason": "low_score", "score": 0.94,
         "label": "bai", "tile_id": None, "box": list(box),
         "region": "exposed"},
        {"accepted": False, "reason": "ambiguous", "score": 0.5,
         "label": "bai", "tile_id": None, "box": list(box),
         "region": "exposed"},
    ]
    vision._realign_failed_meld_boxes(
        _shifted_blank_image(), None, typical_width=89,
        next_left={box: 300})
    saved, high, amb = vision.meld_diagnostics
    assert saved["accepted"] is True
    assert saved["reason"] == "accepted_meld_realign"
    assert saved["original_score"] == 0.5
    assert high["accepted"] is False and high["score"] == 0.94
    assert high["reason"] == "low_score"
    assert amb["accepted"] is False and amb["reason"] == "ambiguous"


def test_meld_realign_cannot_read_the_neighbour(monkeypatch):
    """只有把裁框整体挪到邻牌脸上才能「读出」时，必须拒绝（中心护栏）。

    假分类器只接受中心偏离原框中心 >=40px 的裁框（模拟邻牌脸的位置）；
    原框自身的任何微调裁框都不接受。重对齐若无护栏会把邻牌读成自己，
    造出一张不存在的牌——护栏在位时结果必须保持原判弃权。
    """
    vision = make_vision()
    box = (92, 50, 154, 164)
    center_x = (box[0] + box[2]) / 2.0

    def fake_classify(crop, caishen_label=None):
        # classify_crop 只拿到裁图：用裁图宽度+图内标记列反推裁框位置。
        # 这里改用闭包记录不可行，改为按裁图内容判定：图在 x>=191 处
        # 全亮，其余全黑；裁框右界越过 191 越多，均值越高。
        fill = float(crop.mean()) / 200.0 if crop.size else 0.0
        accepted = fill >= 0.95
        return {"accepted": accepted, "tile_id": 67 if accepted else None,
                "label": "bai", "score": round(fill, 3),
                "margin": 0.6 if accepted else 0.05,
                "reason": "accepted" if accepted else "low_score"}

    monkeypatch.setattr(vision, "classify_crop", fake_classify)
    img = np.zeros((200, 400, 3), np.uint8)
    img[50:164, 191:271] = 200          # 只有邻牌脸是亮的（x 191..271）
    found = vision._realign_meld_box(
        img, box, None, typical_width=89, next_left=300)
    assert found is None


def test_realign_delta_gate_rejects_marginal_gain(monkeypatch):
    """增益门：重对齐候选虽过原阈值，但只比原框读数高 0.07 → 不采用。

    多重搜索（多横移×多宽度取最优）会稀释原门槛的统计效力；用
    「救回分数 − 原分数 ≥ 0.10」补这笔代价。假分类器把分数压在
    0.80+0.07×fill：最对准的裁框 fill=1.0 得 0.87，原诊断分 0.80，
    增益 0.07 不足 → 调度层必须保持原弃权。
    """
    vision = make_vision()

    def fake_classify(crop, caishen_label=None):
        fill = float(crop.mean()) / 200.0 if crop.size else 0.0
        accepted = fill >= 0.95
        return {"accepted": accepted, "tile_id": 67 if accepted else None,
                "label": "bai", "score": round(0.80 + 0.07 * fill, 3),
                "margin": 0.6 if accepted else 0.05,
                "reason": "accepted" if accepted else "low_score"}

    monkeypatch.setattr(vision, "classify_crop", fake_classify)
    box = (92, 50, 154, 164)
    vision.last_meld_boxes = [list(box), list(box), list(box)]
    vision.detection_diagnostics = {"meld_groups": [3]}
    vision.meld_diagnostics = [
        {"accepted": False, "reason": "low_score", "score": 0.80,
         "label": "bai", "tile_id": None, "box": list(box),
         "region": "exposed"},
        {"accepted": True, "reason": "accepted", "score": 0.95,
         "label": "bai", "tile_id": 67, "box": list(box),
         "region": "exposed"},
        {"accepted": True, "reason": "accepted", "score": 0.95,
         "label": "bai", "tile_id": 67, "box": list(box),
         "region": "exposed"},
    ]
    vision._realign_failed_meld_boxes(
        _shifted_blank_image(), None, typical_width=89,
        next_left={box: 300})
    first = vision.meld_diagnostics[0]
    assert first["accepted"] is False
    assert first["score"] == 0.80
    assert first["reason"] == "low_score"


def test_meld_group_geometry_ok_bands():
    """组级几何门：整齐间距通过；重叠/间距离谱必须否决。"""
    ok = TileVision._meld_group_geometry_ok
    good = [(0, (100, 0, 189, 10)), (1, (192, 0, 281, 10)),
            (2, (284, 0, 373, 10))]
    assert ok(good, typical_width=89, typical_pitch=92) is True
    overlapped = [(0, (100, 0, 189, 10)), (1, (150, 0, 239, 10)),
                  (2, (284, 0, 373, 10))]
    assert ok(overlapped, typical_width=89, typical_pitch=92) is False
    stretched = [(0, (100, 0, 189, 10)), (1, (192, 0, 281, 10)),
                 (2, (420, 0, 509, 10))]
    assert ok(stretched, typical_width=89, typical_pitch=92) is False
    narrow = [(0, (100, 0, 150, 10)), (1, (192, 0, 281, 10)),
              (2, (284, 0, 373, 10))]
    assert ok(narrow, typical_width=89, typical_pitch=92) is False
