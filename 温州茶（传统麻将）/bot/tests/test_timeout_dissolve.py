# -*- coding: utf-8 -*-
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from tap_executor import TapExecutor, TapRejected
from timeout_dissolve import read_timeout_dissolve


ROOT = Path(__file__).resolve().parents[3]
SAMPLES = ROOT / "_takeover" / "timeout_dialog_20261026_2150"


def _read(path: Path) -> np.ndarray:
    if not path.is_file():
        pytest.skip(f"缺少本机原始取证帧：{path}")
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None, path
    return image


@pytest.mark.parametrize(
    "name",
    ["emulator-5554_before.png", "emulator-5556_before.png"],
)
def test_exact_timeout_dialog_returns_only_teal_confirm_box(name: str):
    state = read_timeout_dissolve(_read(SAMPLES / name))

    assert state.present is True
    assert state.complete is True
    assert state.panel_box[:3] == (476, 222, 1151)
    assert state.panel_box[3] in {654, 655}
    assert state.confirm_box == (692, 541, 908, 630)
    assert state.confirm_center == (800, 585)
    assert all(state.evidence["checks"].values())
    assert state.evidence["button"]["candidate_count"] == 1
    assert state.evidence["button"]["large_white_glyph_count"] == 2
    assert state.evidence["message"]["fingerprint_hamming"] == 0
    json.dumps(state.ledger(), ensure_ascii=False)


@pytest.mark.parametrize(
    "path",
    [
        SAMPLES / "emulator-5554_after.png",
        SAMPLES / "emulator-5556_after.png",
        ROOT / "_takeover" / "match_end_probe.png",
        ROOT / "_takeover" / "live_now_emulator-5556.png",
    ],
)
def test_non_dialog_real_frames_never_publish_target(path: Path):
    state = read_timeout_dissolve(_read(path))

    assert state.complete is False
    assert state.confirm_box is None
    assert state.confirm_center is None


def test_exact_message_with_a_second_teal_button_fails_unique_button_gate():
    image = _read(SAMPLES / "emulator-5554_before.png")
    # 复制原按钮到另一处；面板、原消息和遮罩均保持有效。
    image[541:630, 360:576] = image[541:630, 692:908]

    state = read_timeout_dissolve(image)

    assert state.present is True
    assert state.complete is False
    assert state.confirm_box is None
    assert state.evidence["button"]["candidate_count"] == 2


def test_exact_panel_and_button_with_changed_message_fails_fingerprint():
    image = _read(SAMPLES / "emulator-5554_before.png")
    # 擦除一个字符；不能把同皮肤的其它“确定”弹窗当作本专用弹窗。
    image[392:434, 620:672] = image[392:434, 590:642]

    state = read_timeout_dissolve(image)

    assert state.present is False
    assert state.complete is False
    assert state.confirm_box is None
    assert state.evidence["checks"]["message"] is False


def test_missing_confirm_label_never_publishes_box():
    image = _read(SAMPLES / "emulator-5554_before.png")
    # 保留青色矩形和精确文案，只抹去“确定”双字。
    image[560:612, 750:850] = image[560:612, 700:800]

    state = read_timeout_dissolve(image)

    assert state.present is True
    assert state.complete is False
    assert state.confirm_box is None
    assert state.evidence["button"]["label_ok"] is False


@pytest.mark.parametrize(
    "image",
    [None, np.zeros((450, 800, 3), dtype=np.uint8), np.zeros((900, 1600), dtype=np.uint8)],
)
def test_invalid_or_non_native_frame_fails_closed(image):
    state = read_timeout_dissolve(image)

    assert state.present is False
    assert state.complete is False
    assert state.confirm_box is None
    assert state.confirm_center is None


def test_dry_run_executor_only_plans_exact_timeout_confirm(tmp_path):
    executor = TapExecutor(enabled=False)
    image = _read(SAMPLES / "emulator-5554_before.png")

    outcome = executor.execute_timeout_dissolve_confirm(
        image=image, session_dir=tmp_path)

    assert outcome["executed"] is False
    assert outcome["dry_run"] is True
    assert outcome["action"] == "confirm_timeout_dissolve"
    assert outcome["tap"] == [800, 585]
    assert outcome["box"] == [692, 541, 908, 630]


def test_executor_rejects_non_timeout_confirm_frame(tmp_path):
    executor = TapExecutor(enabled=False)
    image = _read(SAMPLES / "emulator-5554_after.png")

    with pytest.raises(TapRejected, match="不是证据完整的超时解散弹窗"):
        executor.execute_timeout_dissolve_confirm(
            image=image, session_dir=tmp_path)
