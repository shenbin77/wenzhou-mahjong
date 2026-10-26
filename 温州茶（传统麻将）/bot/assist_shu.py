"""Local, read-only adviser for the four-player red-Zhong game.

Reuses the existing Shu vision templates and rules engine. A screenshot only
produces advice when every visible concealed tile passes recognition and the
hand size agrees with the number of visible melds.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from assist_tea import _load_image, _recording_vision, _write_annotation
from harvest_tea import DEFAULT_ADB, DEFAULT_SERIAL, EXPECTED_PACKAGE
from mode_vision import detect_shu_rule_text
from pc_bot.tile_vision import TILE_IDS, TARGET_IDS
from tile_vision import TileVision
from brain_v0.engine.advisor import Advisor
from brain_v0.engine.wenzhou_rules import hand_is_win, hand_shanten, ting_tiles


_SHU_TEMPLATE_DIR = (Path(__file__).resolve().parent.parent / "代打BOT" /
                     "majiang-bot" / "pc_bot" / "templates_hongzhong_v2")
# 熟局红中放炮的财神按规则固定是红中，与 live_capture.observed_caishen("shu_*")
# 返回的 caishen_source="rule" 一致。告诉识别器后，"财"角标牌与整张纯金牌才能
# 按红中计入闭手，否则会被当成未识别牌导致整帧弃权。
_SHU_CAISHEN_LABEL = 'hongzhong'


def _validate_hand(labels: list[str], meld_count: int = 0) -> list[int]:
    expected = {13 - 3 * meld_count, 14 - 3 * meld_count}
    if meld_count not in range(5) or len(labels) not in expected:
        raise ValueError(f"暗手应为 {min(expected)} 或 {max(expected)} 张，当前 {len(labels)} 张")
    if any(TILE_IDS.get(label) not in TARGET_IDS for label in labels):
        raise ValueError("手牌含温州熟规则外的牌")
    hand = [TILE_IDS[label] for label in labels]
    if any(n > 4 for n in Counter(hand).values()):
        raise ValueError("同一种牌超过四张")
    return hand


def _calculate(labels: list[str], meld_count: int) -> dict:
    hand = _validate_hand(labels, meld_count)
    shanten = hand_shanten(hand, open_meld_count=meld_count)
    wait_count = 13 - 3 * meld_count
    if len(hand) == wait_count:
        return {
            "status": "wait", "shanten": shanten, "suggestion": None,
            "ting": ting_tiles(hand, open_meld_count=meld_count),
            "reason": "等待摸牌", "options": [],
        }
    if hand_is_win(hand, open_meld_count=meld_count):
        return {
            "status": "hu", "shanten": shanten, "suggestion": None,
            "ting": [], "reason": "疑似可胡，请核对游戏规则与按钮", "options": [],
        }
    # Without opponents' discard history, a risk score would be fabricated.
    # Rank by the existing hand-efficiency engine only.
    advised = Advisor(afa_client=None, danger_lambda=0.0).local_advise(
        hand, played_counts={}, turn_no=1, open_meld_count=meld_count,
    )
    options = advised["options"]
    if not options:
        raise ValueError("规则引擎未给出可弃牌候选")
    return {
        "status": "advise", "shanten": advised["shanten"],
        "suggestion": options[0]["tile"], "ting": [],
        "reason": "只依据当前手牌；未识别四家河牌与放炮风险",
        "options": [
            {key: option[key] for key in
             ("tile", "name", "shanten_after", "ukeire_count", "final_score")}
            for option in options[:5]
        ],
    }


def analyze_shu_snapshot(
    image_path: str | Path, *, confirmed_four_player: bool,
    output_path: str | Path | None = None,
    on_recognized=None,
) -> dict:
    """Inspect one saved frame. Never send game input or network requests."""
    if not confirmed_four_player:
        raise ValueError("须先确认这是温州熟 4 人红中放炮局")
    source = Path(image_path)
    output = Path(output_path) if output_path is not None else None
    if output is not None and output.resolve() == source.resolve():
        raise ValueError("标注图不能覆盖原始截图")
    _, image = _load_image(source)
    if image.shape[1] != 1600 or image.shape[0] not in (900, 948):
        early_reason = "画面尺寸未验证；目前仅支持 1600×900 或录像的 1600×948"
    elif not detect_shu_rule_text(image):
        early_reason = "画面未确认温州熟 4 人红中放炮玩法"
    else:
        early_reason = None
    if early_reason:
        _write_annotation(image, [], None, output)
        return {"mode": "shu_4p", "image": str(source),
                "annotation": str(output) if output else None,
                "hand": [], "meld_count": 0, "suggested_boxes": [],
                "dry_run": True, "status": "abstain", "reason": early_reason}
    vision = TileVision(template_dir=_SHU_TEMPLATE_DIR)
    vision.caishen_label = _SHU_CAISHEN_LABEL
    try:
        vision.recognize_hand(image)
    finally:
        if on_recognized is not None:
            on_recognized()
    diagnostics = vision.diagnostics
    recording_diagnostics, hand_ids = _recording_vision(vision)
    result = {
        "mode": "shu_4p", "image": str(source),
        "annotation": str(output) if output else None,
        "hand": [item["label"] for item in diagnostics],
        "meld_count": 0, "suggested_boxes": [], "dry_run": True,
        "vision_diagnostics": recording_diagnostics, "hand_ids": hand_ids,
    }
    def abstain(reason: str) -> dict:
        _write_annotation(image, diagnostics, None, output)
        return {**result, "status": "abstain", "reason": reason}

    if not vision.last_complete:
        return abstain("存在未识别、遮挡或低置信度手牌")
    meld_boxes = vision.last_meld_boxes
    if len(meld_boxes) not in (0, 3, 4):
        return abstain("副露数量无法确认")
    meld_count = 1 if meld_boxes else 0
    result["meld_count"] = meld_count
    if len(vision.last_melds) != len(meld_boxes):
        return abstain("副露有未识别的牌")
    try:
        calculation = _calculate(result["hand"], meld_count)
    except ValueError as exc:
        return abstain(str(exc))
    suggestion = calculation.pop("suggestion")
    result.update(calculation)
    result["suggestion"] = next(
        (name for name, tile_id in TILE_IDS.items() if tile_id == suggestion), None
    ) if suggestion is not None else None
    if suggestion is not None:
        boxes = [d["box"] for d in diagnostics if d["tile_id"] == suggestion]
        if not boxes:
            return abstain("建议牌未在截图中定位")
        result["suggested_boxes"] = boxes
    _write_annotation(image, diagnostics, suggestion, output)
    return result


def analyze_shu_manual_hand(
    labels: str, *, confirmed_four_player: bool, meld_count: int = 0,
    session_dir: str | Path | None = None,
    image_path: str | Path | None = None,
    corrected_from: str | Path | None = None,
    write_sidecar: bool = True,
) -> dict:
    if not confirmed_four_player:
        raise ValueError("须先确认这是温州熟 4 人红中放炮局")
    if image_path is None:
        raise ValueError("修正手牌必须关联已核对的熟局截图")
    _, source_image = _load_image(Path(image_path))
    if not detect_shu_rule_text(source_image):
        raise ValueError("截图未确认温州熟 4 人红中放炮玩法，停止手工重算")
    parsed = labels.replace("，", " ").replace(",", " ").split()
    calculation = _calculate(parsed, meld_count)
    suggestion = calculation.pop("suggestion")
    result = {
        **calculation, "mode": "shu_4p", "source": "manual_correction",
        "hand": parsed, "meld_count": meld_count,
        "hand_ids": [TILE_IDS[label] for label in parsed],
        "image": str(image_path) if image_path else None,
        "suggestion": next((name for name, tid in TILE_IDS.items() if tid == suggestion), None)
        if suggestion is not None else None,
        "suggested_boxes": [], "annotation": None, "dry_run": True,
        "corrected_from": str(corrected_from) if corrected_from else None,
        "captured_at": datetime.now().astimezone().isoformat(),
    }
    if session_dir is not None and write_sidecar:
        folder = Path(session_dir)
        folder.mkdir(parents=True, exist_ok=True)
        record = folder / f"shu_manual_{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid.uuid4().hex[:6]}.json"
        result["record"] = str(record)
        with record.open("x", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    return result


def import_shu_and_analyze(
    image_path: str | Path, *, session_dir: str | Path,
    confirmed_four_player: bool, write_sidecar: bool = True,
) -> dict:
    source = Path(image_path)
    raw, _ = _load_image(source)
    if source.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        raise ValueError("仅支持 PNG 或 JPEG 截图")
    folder = Path(session_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stem = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:6]
    original = folder / f"{stem}{source.suffix.lower()}"
    marked = folder / f"{stem}.marked.png"
    sidecar = folder / f"{stem}.json"
    with original.open("xb") as fh:
        fh.write(raw)
    try:
        result = analyze_shu_snapshot(
            original, confirmed_four_player=confirmed_four_player,
            output_path=marked,
        )
    except ValueError as exc:
        result = {"mode": "shu_4p", "status": "abstain", "reason": str(exc),
                  "image": str(original), "annotation": None, "dry_run": True}
    result.update({
        "source": "imported_image", "source_image": str(source),
        "captured_at": datetime.now().astimezone().isoformat(),
        "image_sha256": hashlib.sha256(raw).hexdigest(),
    })
    if write_sidecar:
        result["record"] = str(sidecar)
        with sidecar.open("x", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    return result


def capture_shu_and_analyze(
    *, serial: str = DEFAULT_SERIAL, adb_path: str = DEFAULT_ADB,
    session_dir: str | Path, confirmed_four_player: bool,
    write_sidecar: bool = True,
) -> dict:
    """Take one read-only ADB screenshot after verifying the foreground app."""
    adb = Path(adb_path)
    if not adb.is_file():
        raise FileNotFoundError(f"ADB 不存在: {adb}")
    if not serial or any(c.isspace() for c in serial):
        raise ValueError("模拟器序列号无效")
    prefix = [str(adb), "-s", serial]
    focus = subprocess.run(
        prefix + ["shell", "dumpsys", "activity", "activities"],
        capture_output=True, timeout=15, check=True,
    ).stdout.decode(errors="replace")
    if not any(f"{EXPECTED_PACKAGE}/" in line for line in focus.splitlines()
               if "topResumedActivity=" in line):
        raise RuntimeError("游戏不在模拟器前台，停止采集")
    raw = subprocess.run(prefix + ["exec-out", "screencap", "-p"],
                         capture_output=True, timeout=20, check=True).stdout
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError("模拟器截图不是有效 PNG")
    folder = Path(session_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stem = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:6]
    original = folder / f"{stem}.png"
    marked = folder / f"{stem}.marked.png"
    sidecar = folder / f"{stem}.json"
    with original.open("xb") as fh:
        fh.write(raw)
    try:
        result = analyze_shu_snapshot(original, confirmed_four_player=confirmed_four_player,
                                      output_path=marked)
    except ValueError as exc:
        result = {"mode": "shu_4p", "status": "abstain", "reason": str(exc),
                  "image": str(original), "annotation": None, "dry_run": True}
    result.update({"captured_at": datetime.now().astimezone().isoformat(),
                   "serial": serial, "image_sha256": hashlib.sha256(raw).hexdigest(),
                   })
    if write_sidecar:
        result["record"] = str(sidecar)
        with sidecar.open("x", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    return result
