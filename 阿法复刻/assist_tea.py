"""Two-player tea screenshot adviser. Reads one saved image; never controls the game."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from tea_bot import TeaBot, engine_to_vision, tile_name, vision_to_engine
from tile_vision import TARGET_IDS, TILE_IDS
from harvest_tea import DEFAULT_ADB, DEFAULT_SERIAL, EXPECTED_PACKAGE


def _load_image(path: Path) -> tuple[bytes, np.ndarray]:
    data = path.read_bytes()
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"无法读取图像: {path}")
    return data, image


def _write_annotation(
    image: np.ndarray,
    diagnostics: list[dict],
    selected_id: int | None,
    output: Path | None,
) -> None:
    if output is None:
        return
    if output.exists():
        raise FileExistsError(f"标注图已存在，不覆盖: {output}")
    marked = image.copy()
    for item in diagnostics:
        x0, y0, x1, y1 = item["box"]
        is_suggestion = selected_id is not None and item["tile_id"] == selected_id
        colour = (0, 0, 255) if is_suggestion else (
            (0, 180, 0) if item["accepted"] else (0, 190, 255)
        )
        cv2.rectangle(marked, (x0, y0), (x1, y1), colour, 4 if is_suggestion else 2)
    ok, encoded = cv2.imencode(".png", marked)
    if not ok:
        raise RuntimeError("标注图编码失败")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(encoded.tobytes())


def analyze_snapshot(
    image_path: str | Path,
    *,
    caishen_label: str,
    confirmed_two_player: bool,
    output_path: str | Path | None = None,
    log_dir: str | Path | None = None,
) -> dict:
    """Advise on one saved 2p frame. Any uncertain or unsupported state abstains."""
    if not confirmed_two_player:
        raise ValueError("须先人工确认这是温州茶 2 人局")
    caishen_id = TILE_IDS.get(caishen_label)
    if caishen_id not in TARGET_IDS:
        raise ValueError(f"无效财神牌: {caishen_label}")
    source = Path(image_path)
    output = Path(output_path) if output_path is not None else None
    if output is not None and output.resolve() == source.resolve():
        raise ValueError("标注图不能覆盖原始截图")
    data, image = _load_image(source)
    logs = Path(log_dir) if log_dir is not None else Path(tempfile.gettempdir()) / "tea_assist_logs"
    bot = TeaBot(dry_run=True, caishen_id=vision_to_engine(caishen_id), log_dir=str(logs))
    try:
        try:
            hand = bot.see(data)
            thought = bot.think(hand)
        except (ValueError, RuntimeError) as exc:
            vision = bot._vision
            diagnostics = vision.diagnostics if vision is not None else []
            _write_annotation(image, diagnostics, None, output)
            return {
                "status": "abstain",
                "reason": str(exc),
                "image": str(source),
                "annotation": str(output) if output else None,
                "recognized": [d["label"] for d in diagnostics if d["accepted"]],
            }

        vision = bot._vision
        diagnostics = vision.diagnostics
        suggested_id = engine_to_vision(thought.discard) if thought.discard is not None else None
        status = "hu" if thought.is_hu else ("advise" if suggested_id is not None else "wait")
        suggested_boxes = [
            d["box"] for d in diagnostics
            if suggested_id is not None and d["tile_id"] == suggested_id
        ]
        if status == "advise" and not suggested_boxes:
            _write_annotation(image, diagnostics, None, output)
            return {
                "status": "abstain",
                "reason": "建议牌未在截图中定位，停止建议",
                "image": str(source),
                "annotation": str(output) if output else None,
            }
        _write_annotation(image, diagnostics, suggested_id, output)
        return {
            "status": status,
            "mode": "tea_2p",
            "image": str(source),
            "annotation": str(output) if output else None,
            "hand": [d["label"] for d in diagnostics],
            "caishen": caishen_label,
            "shanten": thought.shanten,
            "suggestion": tile_name(thought.discard) if thought.discard is not None else None,
            "suggested_boxes": suggested_boxes,
            "reason": thought.reason,
            "dry_run": True,
        }
    finally:
        for handler in list(bot.logger.handlers):
            bot.logger.removeHandler(handler)
            handler.close()


def analyze_manual_hand(
    labels: str,
    *,
    caishen_label: str,
    confirmed_two_player: bool,
    session_dir: str | Path | None = None,
    image_path: str | Path | None = None,
    corrected_from: str | Path | None = None,
) -> dict:
    """Recalculate an explicitly corrected 2p hand; no tile position is inferred."""
    if not confirmed_two_player:
        raise ValueError("须先人工确认这是温州茶 2 人局")
    caishen_id = TILE_IDS.get(caishen_label)
    if caishen_id not in TARGET_IDS:
        raise ValueError(f"无效财神牌: {caishen_label}")
    hand_labels = labels.replace(",", " ").replace("，", " ").split()
    if len(hand_labels) not in (16, 17):
        raise ValueError(f"闭手应为 16 或 17 张，当前 {len(hand_labels)} 张")
    invalid = [label for label in hand_labels if TILE_IDS.get(label) not in TARGET_IDS]
    if invalid:
        raise ValueError(f"不支持的牌名: {', '.join(invalid)}")
    folder = Path(session_dir) if session_dir is not None else Path(tempfile.gettempdir()) / "tea_assist_logs"
    bot = TeaBot(dry_run=True, caishen_id=vision_to_engine(caishen_id), log_dir=str(folder / "logs"))
    try:
        thought = bot.think([vision_to_engine(TILE_IDS[label]) for label in hand_labels])
    finally:
        for handler in list(bot.logger.handlers):
            bot.logger.removeHandler(handler)
            handler.close()
    status = "hu" if thought.is_hu else ("advise" if thought.discard is not None else "wait")
    result = {
        "status": status,
        "source": "manual_correction",
        "mode": "tea_2p",
        "image": str(image_path) if image_path else None,
        "annotation": None,
        "corrected_from": str(corrected_from) if corrected_from else None,
        "hand": hand_labels,
        "caishen": caishen_label,
        "shanten": thought.shanten,
        "suggestion": tile_name(thought.discard) if thought.discard is not None else None,
        "suggested_boxes": [],
        "reason": thought.reason,
        "dry_run": True,
        "captured_at": datetime.now().astimezone().isoformat(),
    }
    if session_dir is not None:
        folder.mkdir(parents=True, exist_ok=True)
        record = folder / f"manual_{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid.uuid4().hex[:6]}.json"
        result["record"] = str(record)
        with record.open("x", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    return result


def import_and_analyze(
    image_path: str | Path,
    *,
    session_dir: str | Path,
    caishen_label: str,
    confirmed_two_player: bool,
) -> dict:
    """Import a screenshot into an append-only local session and analyze it."""
    source = Path(image_path)
    raw, image = _load_image(source)
    suffix = source.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg"}:
        raise ValueError("仅支持 PNG 或 JPEG 截图")
    folder = Path(session_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:6]
    original = folder / f"{stamp}{suffix}"
    annotation = folder / f"{stamp}.marked.png"
    sidecar = folder / f"{stamp}.json"
    with original.open("xb") as fh:
        fh.write(raw)
    if image.shape[:2] != (900, 1600):
        result = {"status": "abstain", "reason": "画面尺寸不是已验证的 1600×900",
                  "image": str(original), "annotation": None}
    else:
        try:
            result = analyze_snapshot(
                original, caishen_label=caishen_label,
                confirmed_two_player=confirmed_two_player,
                output_path=annotation, log_dir=folder / "logs",
            )
        except ValueError as exc:
            result = {"status": "abstain", "reason": str(exc),
                      "image": str(original), "annotation": None}
    result.update({
        "source": "imported_image",
        "source_image": str(source),
        "captured_at": datetime.now().astimezone().isoformat(),
        "image_sha256": hashlib.sha256(raw).hexdigest(),
        "record": str(sidecar),
    })
    with sidecar.open("x", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return result


def capture_and_analyze(
    *,
    serial: str = DEFAULT_SERIAL,
    adb_path: str = DEFAULT_ADB,
    session_dir: str | Path,
    caishen_label: str = "",
    confirmed_two_player: bool = False,
) -> dict:
    """Capture one game frame and save it with an append-only local decision record."""
    adb = Path(adb_path)
    if not adb.is_file():
        raise FileNotFoundError(f"ADB 不存在: {adb}")
    if not serial or any(char.isspace() for char in serial):
        raise ValueError("模拟器序列号无效")
    prefix = [str(adb), "-s", serial]
    focus = subprocess.run(
        prefix + ["shell", "dumpsys", "activity", "activities"],
        capture_output=True, timeout=15, check=True,
    ).stdout.decode(errors="replace")
    resumed = [line for line in focus.splitlines() if "topResumedActivity=" in line]
    if not any(f"{EXPECTED_PACKAGE}/" in line for line in resumed):
        raise RuntimeError("游戏不在模拟器前台，停止采集")
    raw = subprocess.run(
        prefix + ["exec-out", "screencap", "-p"],
        capture_output=True, timeout=20, check=True,
    ).stdout
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError("模拟器截图不是有效 PNG")
    folder = Path(session_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:6]
    original = folder / f"{stamp}.png"
    annotation = folder / f"{stamp}.marked.png"
    sidecar = folder / f"{stamp}.json"
    with original.open("xb") as fh:
        fh.write(raw)
    if image.shape[:2] != (900, 1600):
        result = {"status": "abstain", "reason": "画面尺寸不是已验证的 1600×900",
                  "image": str(original), "annotation": None}
    else:
        try:
            result = analyze_snapshot(
                original,
                caishen_label=caishen_label,
                confirmed_two_player=confirmed_two_player,
                output_path=annotation,
                log_dir=folder / "logs",
            )
        except ValueError as exc:
            result = {"status": "abstain", "reason": str(exc),
                      "image": str(original), "annotation": None}
    result.update({
        "captured_at": datetime.now().astimezone().isoformat(),
        "serial": serial,
        "image_sha256": hashlib.sha256(raw).hexdigest(),
        "record": str(sidecar),
    })
    with sidecar.open("x", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="温州茶 2 人局截图辅助建议（不点击）")
    parser.add_argument("--image", required=True, help="已有截图路径")
    parser.add_argument("--caishen", required=True, help="人工核实的财神牌，如 s7 / t5 / bai")
    parser.add_argument("--confirm-2p", action="store_true", help="人工确认截图来自温州茶 2 人局")
    parser.add_argument("--output", help="新标注图路径；红框为建议牌，黄框为未识别牌")
    args = parser.parse_args()
    try:
        result = analyze_snapshot(
            args.image,
            caishen_label=args.caishen,
            confirmed_two_player=args.confirm_2p,
            output_path=args.output,
        )
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["status"] == "abstain" else 0


if __name__ == "__main__":
    raise SystemExit(main())
