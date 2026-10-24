"""Tile-face rendering for the v2 assist window.

Faces come from the verified template originals (no generated art).  The
helper only composites images; it never reads screen pixels or sends input.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from tile_vision import TILE_IDS


BOT_DIR = Path(__file__).resolve().parent
TEA_TEMPLATE_DIR = BOT_DIR.parent / "采集" / "templates" / "tea_tiles"
SHU_TEMPLATE_DIR = (BOT_DIR.parent / "代打BOT" / "majiang-bot" / "pc_bot" /
                    "templates_hongzhong_v2")
ROW_TILE_HEIGHT = 40
CANDIDATE_TILE_HEIGHT = 30
HIGHLIGHT_SCALE = 1.6
GOLD = (255, 196, 40, 255)
RECOMMEND_RED = (214, 40, 40, 255)
MUTED_FACE = (255, 255, 255, 255)
BACKGROUND = (246, 246, 248, 255)
STALE_OVERLAY = (210, 210, 210, 255)
TEXT_COLOUR = (32, 32, 32, 255)


def _manifest(directory: Path) -> list[dict]:
    path = directory / "manifest.json"
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("templates", [])


@lru_cache(maxsize=1)
def _face_map() -> dict[str, Path]:
    faces: dict[str, Path] = {}
    for directory in (TEA_TEMPLATE_DIR, SHU_TEMPLATE_DIR):
        for item in _manifest(directory):
            label = item.get("label")
            path = directory / item.get("file", "")
            if label and path.is_file() and label not in faces:
                faces[label] = path
    for label, tile_id in TILE_IDS.items():
        if label in faces:
            continue
        for directory in (TEA_TEMPLATE_DIR, SHU_TEMPLATE_DIR):
            for candidate in sorted(directory.glob(f"{label}.png")):
                faces[label] = candidate
                break
            if label in faces:
                break
    return faces


@lru_cache(maxsize=256)
def _face_image(label: str) -> Image.Image:
    path = _face_map().get(label)
    if path is None:
        return _placeholder()
    with Image.open(path) as source:
        return source.convert("RGBA")


@lru_cache(maxsize=8)
def _placeholder_colour(colour: tuple[int, int, int, int]) -> Image.Image:
    image = Image.new("RGBA", (89, 123), colour)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 88, 122), outline=(180, 180, 180, 255), width=3)
    draw.line((8, 8, 80, 114), fill=(190, 190, 190, 255), width=3)
    draw.line((80, 8, 8, 114), fill=(190, 190, 190, 255), width=3)
    return image


def _placeholder() -> Image.Image:
    return _placeholder_colour(MUTED_FACE)


def _has_face(label: str | None) -> bool:
    return bool(label) and label in _face_map()


def _scaled(image: Image.Image, height: int) -> Image.Image:
    ratio = height / image.height
    return image.resize((max(1, round(image.width * ratio)), height),
                        Image.Resampling.LANCZOS)


def _outlined(image: Image.Image, colour, width: int) -> Image.Image:
    canvas = Image.new("RGBA", (image.width + 2, image.height + 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, canvas.width - 1, canvas.height - 1), outline=colour,
                   width=width)
    canvas.paste(image, (1, 1))
    return canvas


def _font(size: int):
    for name in ("msyh.ttc", "msyh.ttf", "simhei.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_hand(
    labels: Iterable[str],
    *,
    suggestion_label: str | None = None,
    caishen_label: str | None = None,
    muted: bool = False,
    tile_height: int = ROW_TILE_HEIGHT,
    gap: int = 3,
    max_width: int | None = None,
) -> Image.Image:
    """Draw the hand as faces; the advised tile is 1.6x with a red border."""
    labels = [label for label in labels if label]
    boost_height = round(tile_height * HIGHLIGHT_SCALE)
    if not labels:
        canvas = Image.new("RGBA", (max(120, (max_width or 220)), tile_height + 6),
                           BACKGROUND)
        ImageDraw.Draw(canvas).text((6, 8), "未识别到手牌", fill=TEXT_COLOUR,
                                    font=_font(14))
        return canvas
    sized = []
    for label in labels:
        height = boost_height if suggestion_label and label == suggestion_label else tile_height
        sized.append((label, _scaled(_face_image(label), height), height))
    pad = 4
    span = sum(image.width + gap for _, image, _ in sized) + gap
    columns = 1
    if max_width and span > max_width:
        columns = int(np.ceil(span / max(1, max_width)))
        per_line = int(np.ceil(len(sized) / columns))
        rows = [sized[index:index + per_line] for index in range(0, len(sized), per_line)]
    else:
        rows = [sized]
    widths = [sum(image.width + gap for _, image, _ in row) + gap for row in rows]
    heights = [max(height for _, _, height in row) + gap for row in rows]
    canvas = Image.new("RGBA", (max(widths) + pad * 2, sum(heights) + pad),
                       BACKGROUND)
    y = pad
    for row, row_height in zip(rows, heights):
        x = pad
        for label, image, _ in row:
            if muted:
                image = image.convert("L").convert("RGBA")
            if caishen_label and label == caishen_label:
                image = _outlined(image, GOLD, 3)
            elif suggestion_label and label == suggestion_label:
                image = _outlined(image, RECOMMEND_RED, 3)
            canvas.alpha_composite(image, (x + 1, y + (row_height - image.height) // 2))
            x += image.width + gap
        y += row_height
    return canvas


def render_candidates(candidates: Iterable[dict], *,
                      tile_height: int = CANDIDATE_TILE_HEIGHT) -> Image.Image:
    """Top-3 small faces with shanten and ukeire, from the recorder's rows."""
    rows = list(candidates)[:3]
    cell_width = 92
    height = tile_height + 22
    canvas = Image.new("RGBA", (max(cell_width, cell_width * max(1, len(rows))), height),
                       BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    if not rows:
        draw.text((6, 8), "无候选", fill=TEXT_COLOUR, font=_font(13))
        return canvas
    for index, row in enumerate(rows):
        label = _label_for(row.get("tile"))
        x = index * cell_width
        face = _scaled(_face_image(label), tile_height) if label else _scaled(_placeholder(), tile_height)
        if index == 0:
            face = _outlined(face, RECOMMEND_RED, 2)
        canvas.alpha_composite(face, (x + 2, 1))
        caption = f"向听{row.get('shanten_after', '?')}/进{row.get('ukeire_count', '?')}"
        draw.text((x + 2 + face.width + 4, 2), caption, fill=TEXT_COLOUR, font=_font(11))
    return canvas


def render_advice(state, *, width: int = 400) -> Image.Image:
    """Fixed-height advice block: big tile, reason, candidate strip."""
    height = 96
    canvas = Image.new("RGBA", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    if not state.actionable:
        draw.text((8, 8), state.title, fill=(120, 120, 120, 255), font=_font(18))
        draw.multiline_text((8, 38), state.reason or "暂无建议",
                            fill=(120, 120, 120, 255), font=_font(13))
        return canvas
    if state.suggestion_label:
        face = _outlined(_scaled(_face_image(state.suggestion_label), 64), RECOMMEND_RED, 3)
    else:
        face = _outlined(_scaled(_placeholder(), 64), RECOMMEND_RED, 3)
    canvas.alpha_composite(face, (8, 10))
    draw.text((face.width + 22, 6), f"打 {state.suggestion or '—'}", fill=RECOMMEND_RED,
              font=_font(30))
    reason = _reason_line(state)
    draw.multiline_text((face.width + 22, 52), reason, fill=TEXT_COLOUR, font=_font(12))
    strip = render_candidates(state.candidates)
    canvas.alpha_composite(strip, (width - strip.width - 4, height - strip.height - 2))
    return canvas


def _reason_line(state) -> str:
    if state.candidates:
        top = state.candidates[0]
        return (f"向听{top.get('shanten_after', '?')} · "
                f"进张{top.get('ukeire_count', '?')}张 · "
                f"候选{len(state.candidates)}")
    if state.shanten is not None:
        return f"向听{state.shanten}"
    return "依据现有 advisor 输出"


def _label_for(tile) -> str | None:
    if tile is None:
        return None
    try:
        tile_id = int(tile)
    except (TypeError, ValueError):
        return str(tile) if _has_face(str(tile)) else None
    for label, value in TILE_IDS.items():
        if value == tile_id:
            return label
    return None


def missing_face_labels(total: int) -> list[str]:
    faces = _face_map()
    missing = [label for label in TILE_IDS if label not in faces]
    return missing[:total]
