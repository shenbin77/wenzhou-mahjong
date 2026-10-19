"""Two-player tea screenshot adviser. Reads one saved image; never controls the game."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import cv2
import numpy as np

from tea_bot import TeaBot, engine_to_vision, tile_name, vision_to_engine
from tile_vision import TARGET_IDS, TILE_IDS


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
