"""Two- and four-player tea screenshot adviser; never controls the game."""

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
from caishen_vision import CaishenRead, detect_caishen
from discard_vision import DiscardState, read_discards_sequential
from harvest_tea import DEFAULT_ADB, DEFAULT_SERIAL, EXPECTED_PACKAGE



def _caishen_fail_reason(observed) -> str:
    """Fail-closed user text; never treats candidate as an accepted label."""
    base = str(getattr(observed, "reason", "") or "未识别")
    if base != "财神小牌匹配不够确定":
        return f"未识别本局财神：{base}；仅显示识牌证据，不给建议"
    score = float(getattr(observed, "score", 0.0) or 0.0)
    runner = float(getattr(observed, "runner_up", 0.0) or 0.0)
    cand = getattr(observed, "candidate", None)
    margin = score - runner
    detail = f"score={score:.3f} margin={margin:.3f} 需≥0.98/0.02"
    if cand:
        detail = f"候选{cand} {detail}"
    return f"未识别本局财神：{base}（{detail}）；仅显示识牌证据，不给建议"

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


def _recording_vision(vision) -> tuple[list[dict], list[int]]:
    """Expose read-only vision evidence without changing recognition decisions."""
    diagnostics = [dict(item) for item in (vision.diagnostics if vision is not None else [])]
    unresolved = ((vision.detection_diagnostics if vision is not None else {})
                  .get("unresolved_regions", []))
    diagnostics.extend({
        "tile_id": None, "score": 0.0, "margin": 0.0,
        "accepted": False, "reason": "no_box", "box": list(box),
    } for box in unresolved)
    hand_ids = [int(item["tile_id"]) for item in diagnostics
                if item.get("accepted") and item.get("tile_id") is not None]
    return diagnostics, hand_ids


def _visible_stacked_kongs(vision) -> list[dict]:
    """Read-only meld evidence, even when another tile forces abstention."""
    if vision is None:
        return []
    sizes = list((getattr(vision, "detection_diagnostics", {}) or {})
                 .get("meld_groups") or [])
    boxes = list(getattr(vision, "last_meld_boxes", []) or [])
    faces = list(getattr(vision, "meld_diagnostics", []) or [])
    if sum(sizes) != len(boxes) or len(faces) != len(boxes):
        return []
    found = []
    offset = 0
    for size in sizes:
        group_boxes = boxes[offset:offset + size]
        group_faces = faces[offset:offset + size]
        offset += size
        if (size == 3 and all(face.get("accepted") for face in group_faces)
                and len({face.get("tile_id") for face in group_faces}) == 1
                and TeaBot._stacked_kong_center(group_boxes)):
            found.append({"label": group_faces[0]["label"],
                          "tile_id": group_faces[0]["tile_id"],
                          "logical_count": 4,
                          "visible_boxes": [list(box) for box in group_boxes]})
    return found


def _caishen_fields(caishen_label: str, observed: CaishenRead) -> dict:
    """构造财神回传字段，供成功分支与各弃权分支共用同一口径。

    画面识别优先，其次人工选定；两者都没有时三个字段统一为 ``None``，
    与 ``live_capture.observed_caishen`` 的既有写法一致——同一批返回体里
    不混用 ``""`` 和 ``None`` 两种"未知"。

    Args:
        caishen_label: 本局财神标签；人工选定优先，未选定时为画面识别结果。
        observed: 本帧财字小牌的识别结果。

    Returns:
        含 ``caishen`` / ``caishen_source`` / ``caishen_score`` 的字典；
        ``caishen_source`` 为 ``"image"`` 表示来自画面，``"manual"`` 表示人工选定，
        ``None`` 表示两者皆无。
    """
    if observed.label:
        return {
            "caishen": caishen_label or observed.label,
            "caishen_source": "image",
            "caishen_score": observed.score,
        }
    if caishen_label:
        return {"caishen": caishen_label, "caishen_source": "manual", "caishen_score": None}
    return {"caishen": None, "caishen_source": None, "caishen_score": None}


def _abstain_with_vision_evidence(
    data: bytes,
    image: np.ndarray,
    source: Path,
    output: Path | None,
    *,
    mode: str,
    player_count: int,
    logs: Path,
    reason: str,
    on_recognized=None,
    **extra,
) -> dict:
    """记录识牌证据，但保持弃权：财神不可用时不给任何建议。

    只读展示手牌识别框，便于操作者判断识牌链路本身是否在工作；
    不调用 think()，因此不会产生无财神前提下的建议。
    """
    bot = TeaBot(dry_run=True, caishen_id=None, log_dir=str(logs),
                 player_count=player_count)
    try:
        try:
            try:
                bot.see(data)
            finally:
                if on_recognized is not None:
                    on_recognized()
        except (ValueError, RuntimeError):
            pass
        diagnostics, hand_ids = _recording_vision(bot._vision)
        _write_annotation(image, diagnostics, None, output)
        result = {
            "status": "abstain",
            "mode": mode,
            "reason": reason,
            "image": str(source),
            "annotation": str(output) if output else None,
            "recognized": [d["label"] for d in diagnostics
                           if d.get("accepted") and d.get("label")],
            "hand": [d["label"] for d in diagnostics if d.get("label")],
            "vision_diagnostics": diagnostics,
            "hand_ids": hand_ids,
            "meld_count": bot.meld_count,
            "melds": bot.meld_groups,
            "stacked_kongs": _visible_stacked_kongs(bot._vision),
            "advice_blocked_by": "caishen_unconfirmed",
            "dry_run": True,
        }
        result.update(extra)
        return result
    finally:
        for handler in list(bot.logger.handlers):
            bot.logger.removeHandler(handler)
            handler.close()


def _discard_reading(
    image: np.ndarray,
    previous,
    *,
    player_count: int,
    key: str,
) -> DiscardState:
    """读一帧的牌河与来牌；任何异常都退化成"读不出来"（fail-closed）。

    来牌要前后两帧才判得出来。"上一帧"优先用调用方显式传进来的
    ``previous``，没有就用 ``discard_vision`` 的登记处（按 ``key`` 隔离，
    换局可用 ``forget_discards`` 清空）。

    Args:
        image: 1600x900 BGR 帧。
        previous: 调用方给的上一帧（可为 ``None``）。
        player_count: 本局人数；只在 2 人局上标定过，其余一律不读。
        key: 登记处键，用日志目录区分不同会话。

    Returns:
        :class:`discard_vision.DiscardState`；读不出来时
        ``incoming_tile is None`` 且 ``reason`` 写明原因。
    """
    if player_count != 2:
        return DiscardState(reason="弃牌视觉只在温州茶 2 人布局上标定过，本局不读牌河")
    try:
        return read_discards_sequential(image, previous=previous, key=key)
    except Exception as exc:  # noqa: BLE001 - 视觉层出错只等于"这一帧没有来牌"
        return DiscardState(reason=f"弃牌读取失败，按无来牌处理：{exc}")


def _confirmed_tea_player_count(two: bool, four: bool) -> int:
    if two and four:
        raise ValueError("茶玩法人数不能同时确认 2 人和 4 人")
    if four:
        return 4
    if two:
        return 2
    raise ValueError("须先人工确认这是温州茶 2 人局或 4 人局")


def analyze_snapshot(
    image_path: str | Path,
    *,
    caishen_label: str,
    confirmed_two_player: bool = False,
    confirmed_four_player: bool = False,
    require_visible_caishen: bool = False,
    output_path: str | Path | None = None,
    log_dir: str | Path | None = None,
    on_recognized=None,
    previous_discards=None,
) -> dict:
    """Advise on one confirmed tea frame. Unsupported or uncertain states abstain.

    返回体稳定带 ``discards``（``discard_vision.DiscardState.as_dict()``）：
    ``incoming_tile`` 是刚打出的那张牌的**视觉编码**，读不出来恒为 ``None``，
    ``incoming_source`` 为 ``"opponent"`` / ``"self"`` / ``None``。
    ``action_policy.HandState.from_analysis`` 直接吃这一份。

    Args:
        image_path: 1600x900 原生截图。
        caishen_label: 人工核实的财神牌，留空则从画面识别。
        confirmed_two_player: 人工确认这是温州茶 2 人局。
        confirmed_four_player: 人工确认这是温州茶 4 人局。
        require_visible_caishen: 画面读不出财神且未手选时是否弃权。
        output_path: 标注图路径；``None`` 表示不落图标注。
        log_dir: 日志目录；同时用作弃牌"上一帧"登记处的 key。
        on_recognized: 识牌完成后的回调（可选）。
        previous_discards: 可选的上一帧弃牌读数（:class:`DiscardState` 或
            :class:`TableSnapshot`）。不传则用登记处里上一帧的读数 —— 首帧
            因此只有牌河、没有来牌。
    """
    player_count = _confirmed_tea_player_count(confirmed_two_player, confirmed_four_player)
    mode = f"tea_{player_count}p"
    source = Path(image_path)
    output = Path(output_path) if output_path is not None else None
    if output is not None and output.resolve() == source.resolve():
        raise ValueError("标注图不能覆盖原始截图")
    logs = Path(log_dir) if log_dir is not None else Path(tempfile.gettempdir()) / "tea_assist_logs"
    data, image = _load_image(source)
    # 来牌在财神门禁之前读：财神读不出来只是"不给建议"，牌河事实照样要记进台账，
    # 而且 ``discards`` 必须在每一个返回分支里都在（上游按"有没有这个键"取数）。
    discards = _discard_reading(image, previous_discards, player_count=player_count,
                                key=str(logs))
    discard_fields = {"discards": discards.as_dict()}
    if caishen_label and TILE_IDS.get(caishen_label) not in TARGET_IDS:
        return {"status": "abstain", "mode": mode, "reason": f"当前茶牌集不支持所选财神: {caishen_label}",
                "image": str(source), "annotation": None, "recognized": [],
                **discard_fields}
    observed = detect_caishen(image)
    if require_visible_caishen and observed.label is None and not caishen_label:
        # 画面读不出财字小牌，且操作者也没有手选本局财神时才弃权。
        # 手选等于操作者已在牌桌上核对过财神，是一等证据，不能被这条门禁废掉。
        return _abstain_with_vision_evidence(
            data, image, source, output, mode=mode, player_count=player_count,
            logs=logs,
            reason="连续观察必须看清本局财字小牌；仅显示识牌证据，不给建议",
            on_recognized=on_recognized, caishen_selected=caishen_label,
            caishen_reason=observed.reason,
            **_caishen_fields(caishen_label, observed),
            **discard_fields,
        )
    if observed.label and caishen_label and observed.label != caishen_label:
        return {
            "status": "abstain", "mode": mode, "reason": "所选财神与画面财字小牌不一致",
            "image": str(source), "annotation": None,
            "caishen_observed": observed.label, "caishen_selected": caishen_label,
            "recognized": [],
            **discard_fields,
        }
    if not caishen_label:
        caishen_label = observed.label or ""
    caishen_id = TILE_IDS.get(caishen_label)
    if caishen_id not in TARGET_IDS:
        return _abstain_with_vision_evidence(
            data, image, source, output, mode=mode, player_count=player_count,
            logs=logs,
            reason=_caishen_fail_reason(observed),
            on_recognized=on_recognized, caishen_selected=caishen_label,
            caishen_observed=observed.label, caishen_reason=observed.reason,
            **_caishen_fields(caishen_label, observed),
            **discard_fields,
        )
    bot = TeaBot(dry_run=True, caishen_id=vision_to_engine(caishen_id),
                 log_dir=str(logs), player_count=player_count)
    try:
        try:
            try:
                hand = bot.see(data)
            finally:
                if on_recognized is not None:
                    on_recognized()
            thought = bot.think(hand)
        except (ValueError, RuntimeError) as exc:
            vision = bot._vision
            diagnostics, hand_ids = _recording_vision(vision)
            _write_annotation(image, diagnostics, None, output)
            return {
                "status": "abstain",
                "mode": mode,
                "reason": str(exc),
                "image": str(source),
                "annotation": str(output) if output else None,
                "recognized": [d["label"] for d in diagnostics if d["accepted"]],
                "vision_diagnostics": diagnostics,
                "hand_ids": hand_ids,
                "meld_count": bot.meld_count,
                "melds": bot.meld_groups,
                "stacked_kongs": _visible_stacked_kongs(vision),
                **_caishen_fields(caishen_label, observed),
                **discard_fields,
            }

        vision = bot._vision
        diagnostics = vision.diagnostics
        recording_diagnostics, hand_ids = _recording_vision(vision)
        suggested_id = engine_to_vision(thought.discard) if thought.discard is not None else None
        status = "hu" if thought.is_hu else ("advise" if suggested_id is not None else "wait")
        if player_count == 4 and status == "hu":
            _write_annotation(image, diagnostics, None, output)
            return {"status": "abstain", "mode": mode, "reason": "茶 4 人胡牌与计分尚未验收",
                    "image": str(source), "annotation": str(output) if output else None,
                    "hand": [d["label"] for d in diagnostics], "dry_run": True,
                    "vision_diagnostics": recording_diagnostics, "hand_ids": hand_ids,
                    "meld_count": bot.meld_count, "melds": bot.meld_groups,
                    **_caishen_fields(caishen_label, observed),
                    **discard_fields}
        suggested_boxes = [
            d["box"] for d in diagnostics
            if suggested_id is not None and d["tile_id"] == suggested_id
        ]
        if status == "advise" and not suggested_boxes:
            _write_annotation(image, diagnostics, None, output)
            return {
                "status": "abstain",
                "mode": mode,
                "reason": "建议牌未在截图中定位，停止建议",
                "image": str(source),
                "annotation": str(output) if output else None,
                "vision_diagnostics": recording_diagnostics,
                "hand_ids": hand_ids,
                "meld_count": bot.meld_count,
                "melds": bot.meld_groups,
                **_caishen_fields(caishen_label, observed),
                **discard_fields,
            }
        _write_annotation(image, diagnostics, suggested_id, output)
        return {
            "status": status,
            "mode": mode,
            "image": str(source),
            "annotation": str(output) if output else None,
            "hand": [d["label"] for d in diagnostics],
            **_caishen_fields(caishen_label, observed),
            "shanten": thought.shanten,
            "suggestion": tile_name(thought.discard) if thought.discard is not None else None,
            "suggestion_id": suggested_id,
            "suggested_boxes": suggested_boxes,
            "vision_diagnostics": recording_diagnostics,
            "hand_ids": hand_ids,
            "meld_count": bot.meld_count,
            "melds": bot.meld_groups,
            "stacked_kongs": _visible_stacked_kongs(vision),
            "candidates": [list(candidate) for candidate in thought.discard_candidates],
            "reason": thought.reason,
            "dry_run": True,
            **discard_fields,
        }
    finally:
        for handler in list(bot.logger.handlers):
            bot.logger.removeHandler(handler)
            handler.close()


def analyze_manual_hand(
    labels: str,
    *,
    caishen_label: str,
    confirmed_two_player: bool = False,
    confirmed_four_player: bool = False,
    session_dir: str | Path | None = None,
    image_path: str | Path | None = None,
    corrected_from: str | Path | None = None,
    write_sidecar: bool = True,
) -> dict:
    """Recalculate an explicitly corrected hand; no tile position is inferred."""
    player_count = _confirmed_tea_player_count(confirmed_two_player, confirmed_four_player)
    caishen_id = TILE_IDS.get(caishen_label)
    if caishen_id not in TARGET_IDS:
        raise ValueError(f"无效财神牌: {caishen_label}")
    hand_labels = labels.replace(",", " ").replace("，", " ").split()
    if len(hand_labels) not in (16, 17):
        raise ValueError(f"闭手应为 16 或 17 张，当前 {len(hand_labels)} 张")
    invalid = [label for label in hand_labels if TILE_IDS.get(label) not in TARGET_IDS]
    if invalid:
        raise ValueError(f"不支持的牌名: {', '.join(invalid)}")
    if image_path is None:
        raise ValueError("修正手牌必须关联已核对的茶局截图")
    from mode_vision import detect_tea_no_zhong_fa, detect_tea_player_count
    _, source_image = _load_image(Path(image_path))
    if (detect_tea_player_count(source_image) != player_count
            or not detect_tea_no_zhong_fa(source_image)):
        raise ValueError(f"截图未确认茶 {player_count} 人及无中发规则，停止手工重算")
    observed = detect_caishen(source_image)
    if observed.label and observed.label != caishen_label:
        raise ValueError("所选财神与关联截图的财字小牌不一致")
    folder = Path(session_dir) if session_dir is not None else Path(tempfile.gettempdir()) / "tea_assist_logs"
    bot = TeaBot(dry_run=True, caishen_id=vision_to_engine(caishen_id),
                 log_dir=str(folder / "logs"), player_count=player_count)
    try:
        thought = bot.think([vision_to_engine(TILE_IDS[label]) for label in hand_labels])
    finally:
        for handler in list(bot.logger.handlers):
            bot.logger.removeHandler(handler)
            handler.close()
    status = ("abstain" if player_count == 4 and thought.is_hu else
              "hu" if thought.is_hu else
              "advise" if thought.discard is not None else "wait")
    result = {
        "status": status,
        "source": "manual_correction",
        "mode": f"tea_{player_count}p",
        "image": str(image_path) if image_path else None,
        "annotation": None,
        "corrected_from": str(corrected_from) if corrected_from else None,
        "hand": hand_labels,
        "caishen": caishen_label,
        "shanten": thought.shanten,
        "hand_ids": [TILE_IDS[label] for label in hand_labels],
        "candidates": [list(candidate) for candidate in thought.discard_candidates],
        "suggestion": tile_name(thought.discard) if thought.discard is not None else None,
        "suggested_boxes": [],
        "reason": "茶 4 人胡牌与计分尚未验收" if status == "abstain" else thought.reason,
        "dry_run": True,
        "captured_at": datetime.now().astimezone().isoformat(),
    }
    if session_dir is not None and write_sidecar:
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
    confirmed_two_player: bool = False,
    confirmed_four_player: bool = False,
    write_sidecar: bool = True,
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
                confirmed_four_player=confirmed_four_player,
                output_path=annotation, log_dir=folder / "logs",
            )
        except ValueError as exc:
            result = {"status": "abstain", "reason": str(exc),
                      "image": str(original), "annotation": None}
    result.setdefault("mode", "tea_4p" if confirmed_four_player and not confirmed_two_player
                      else "tea_2p" if confirmed_two_player and not confirmed_four_player else None)
    result.update({
        "source": "imported_image",
        "source_image": str(source),
        "captured_at": datetime.now().astimezone().isoformat(),
        "image_sha256": hashlib.sha256(raw).hexdigest(),
    })
    if write_sidecar:
        result["record"] = str(sidecar)
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
    confirmed_four_player: bool = False,
    require_visible_caishen: bool = False,
    write_sidecar: bool = True,
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
                confirmed_four_player=confirmed_four_player,
                require_visible_caishen=require_visible_caishen,
                output_path=annotation,
                log_dir=folder / "logs",
            )
        except ValueError as exc:
            result = {"status": "abstain", "reason": str(exc),
                      "image": str(original), "annotation": None}
    result.setdefault("mode", "tea_4p" if confirmed_four_player and not confirmed_two_player
                      else "tea_2p" if confirmed_two_player and not confirmed_four_player else None)
    result.update({
        "captured_at": datetime.now().astimezone().isoformat(),
        "serial": serial,
        "image_sha256": hashlib.sha256(raw).hexdigest(),
    })
    if write_sidecar:
        result["record"] = str(sidecar)
        with sidecar.open("x", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="温州茶 2/4 人局截图辅助建议（不点击）")
    parser.add_argument("--image", required=True, help="已有截图路径")
    parser.add_argument("--caishen", default="", help="可选：人工核实的财神牌，如 s7 / t5 / bai；留空则从画面识别")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--confirm-2p", action="store_true", help="人工确认截图来自温州茶 2 人局")
    group.add_argument("--confirm-4p", action="store_true", help="人工确认截图来自温州茶 4 人局")
    parser.add_argument("--output", help="新标注图路径；红框为建议牌，黄框为未识别牌")
    args = parser.parse_args()
    try:
        result = analyze_snapshot(
            args.image,
            caishen_label=args.caishen,
            confirmed_two_player=args.confirm_2p,
            confirmed_four_player=args.confirm_4p,
            output_path=args.output,
        )
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["status"] == "abstain" else 0


if __name__ == "__main__":
    raise SystemExit(main())
