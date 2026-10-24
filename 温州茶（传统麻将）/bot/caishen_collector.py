"""Turn an operator's manual caishen pick into a candidate live template.

The operator picking a label in the dropdown means a human read the 财-marked
sprite on the table. When the red badge is present but the sprite does not
match any known template, that moment is a free ground-truth annotation: the
sprite should become that label's next live template.

Nothing here writes the production manifest. Candidates go to
``templates/caishen/pending/`` as a PNG plus a sidecar JSON; only
``bot/review_caishen_pending.py`` may promote or reject them. Detection
logic, thresholds, and ROI in :mod:`caishen_vision` stay untouched — this
module only *reads* that module's verdict and ROI.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import cv2
import numpy as np

import caishen_vision

_DIR = caishen_vision._DIR
PENDING_DIR = _DIR / "pending"
# 候选 ROI 与该 label 已有模板的相关度达到该值，说明已覆盖，不必再收。
_COVERED_THRESHOLD = 0.98


def _session_id(frame_path: str | Path | None) -> str:
    """Derive the assist session id from a frame path, when possible."""
    if not frame_path:
        return "unknown"
    parts = Path(frame_path).parts
    for index, part in enumerate(parts):
        if part == "assist_sessions" and index + 1 < len(parts):
            return parts[index + 1]
    return "unknown"


def _roi() -> tuple[int, int, int, int]:
    roi, _ = caishen_vision._templates()
    return tuple(roi)  # type: ignore[return-value]


def _manifest_templates(label: str) -> list[np.ndarray]:
    """Grayscale copies of every production template owned by ``label``."""
    _, templates = caishen_vision._templates()
    return list(templates.get(label, ()))


def _best_match_score(crop_gray: np.ndarray, label: str) -> float:
    """Best TM_CCOEFF_NORMED of the candidate against one label's templates."""
    best = 0.0
    for tile in _manifest_templates(label):
        score = float(cv2.matchTemplate(crop_gray, tile, cv2.TM_CCOEFF_NORMED)[0, 0])
        best = max(best, score)
    return best


def _pending_records(store: Path) -> list[dict]:
    """Sidecar JSON payloads currently waiting in the pending buffer."""
    records: list[dict] = []
    if not store.exists():
        return records
    for path in sorted(store.glob("*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return records


def _known_labels() -> set[str]:
    """Label vocabulary a caishen pick may use (tile_vision.TILE_IDS)."""
    try:
        from tile_vision import TILE_IDS
        return set(TILE_IDS)
    except Exception:  # tile_vision 正被并行修改时退回静态词汇表，采集不该因此崩溃
        return ({f"{suit}{rank}" for suit in "wts" for rank in range(1, 10)}
                | {"dong", "nan", "xi", "bei", "bai", "hongzhong", "fa"})


def collect_pending(image: np.ndarray, label: str, *,
                    frame_path: str | Path | None = None,
                    session_id: str | None = None,
                    pending_dir: Path | None = None,
                    origin: str = "manual_pick") -> dict:
    """Capture the caishen sprite as a pending template candidate.

    Returns a dict with a ``status`` key:
    ``bad_image`` / ``invalid_label`` / ``matched`` / ``no_badge`` /
    ``already_covered`` / ``duplicate`` / ``pending``. Only ``pending``
    writes anything.
    """
    if image is None or image.shape != (900, 1600, 3):
        return {"status": "bad_image", "label": label}
    if label not in _known_labels():
        return {"status": "invalid_label", "label": label}

    observed = caishen_vision.detect_caishen(image)
    if observed.label is not None:
        # 画面自己就能读出来，人工标注没有新增信息。
        return {"status": "matched", "label": label,
                "matched_label": observed.label, "score": observed.score}
    if observed.reason != "财神小牌匹配不够确定":
        # 财字角标根本不在（大厅/结算/遮挡），不是有效样本。
        return {"status": "no_badge", "label": label,
                "reason": observed.reason}

    x0, y0, x1, y1 = _roi()
    crop = np.ascontiguousarray(image[y0:y1, x0:x1])
    crop_gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    covered = _best_match_score(crop_gray, label)
    if covered >= _COVERED_THRESHOLD:
        return {"status": "already_covered", "label": label, "score": covered}

    store = Path(pending_dir) if pending_dir is not None else PENDING_DIR
    for record in _pending_records(store):
        if record.get("label") == label:
            return {"status": "duplicate", "label": label,
                    "existing": record.get("file"),
                    "session": record.get("session_id")}
    if store.exists():
        for path in store.glob(f"{label}_*.png"):
            # 侧车 JSON 缺失的孤儿 PNG 也算重复，避免刷库。
            return {"status": "duplicate", "label": label,
                    "existing": path.name, "session": "orphan"}

    ok, encoded = cv2.imencode(".png", crop)
    if not ok:
        return {"status": "encode_failed", "label": label}
    data = encoded.tobytes()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    digest8 = hashlib.sha256(data).hexdigest()[:8]
    name = f"{label}_{stamp}_{digest8}"
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{name}.png").write_bytes(data)
    record = {
        "label": label,
        "file": f"{name}.png",
        "source": str(frame_path) if frame_path else "unrecorded",
        "source_sha256": (hashlib.sha256(Path(frame_path).read_bytes()).hexdigest()
                          if frame_path and Path(frame_path).exists() else None),
        "asset_sha256": hashlib.sha256(data).hexdigest(),
        "session_id": session_id or _session_id(frame_path),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "visual_check": f"pending: {origin}",
        "match_score_vs_existing": covered,
        "pending": True,
    }
    (store / f"{name}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "pending", "label": label, "file": f"{name}.png",
            "session_id": record["session_id"], "score": covered}


def collect_pending_from_file(frame_path: str | Path, label: str, **kwargs) -> dict:
    """Read a frame from disk (non-ASCII paths included) and collect from it."""
    img = cv2.imdecode(np.fromfile(str(frame_path), dtype=np.uint8), cv2.IMREAD_COLOR)
    return collect_pending(img, label, frame_path=frame_path, **kwargs)


def collect_pending_from_bytes(png_bytes: bytes, label: str, *,
                               frame_path: str | Path | None = None,
                               **kwargs) -> dict:
    """Collect from raw PNG bytes (a live capture frame)."""
    if not png_bytes:
        return {"status": "bad_image", "label": label}
    img = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    return collect_pending(img, label, frame_path=frame_path, **kwargs)


# ---------------------------------------------------------------------------
# 自动候选提名（2026-10-24 WorkBuddy）：识别失败但财字角标在时，把"最像的
# label"自动落成 pending 候选，省去操作员手选。提名分三级，入库一律走
# review --confirm 的人工复核，与 manual_pick 流程同等把关。
#
# 为什么不直接"分数低就丢掉"：2 人局仍有 19 个牌面只有回放帧模板、没有
# 实时帧模板（两套渲染差异见 caishen_vision 模块注释）。这些牌首见时的
# top1 分数只有 0.90 上下，若因不够高被丢弃，模板库永远补不齐。所以
# 分数只决定"这个提名有多可信"，不决定"要不要抓这张牌"。
# ---------------------------------------------------------------------------

_AUTO_HIGH = 0.97   # >= 此值：几乎可以肯定是该牌，只是缺实时变体
_AUTO_LOW = 0.85    # >= 此值：提名该牌，但复核时必须看放大图
_AUTO_FLOOR = 0.70  # < 70% 视为噪声帧（遮挡/动画中间态），不落候选
_AUTO_TOPK = 3


def rank_labels(image: np.ndarray) -> list[tuple[str, float]]:
    """Return every label's best template score against the caishen ROI.

    Mirrors the scoring inside :func:`caishen_vision.detect_caishen` but
    skips the acceptance gate, so the caller can see *how close* each
    label came when the gate says no.
    """
    if image is None or image.shape != (900, 1600, 3):
        return []
    (x0, y0, x1, y1), templates = caishen_vision._templates()
    crop = image[y0:y1, x0:x1]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    scored = [
        (label, max(float(cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)[0, 0])
                    for t in tiles))
        for label, tiles in templates.items()
    ]
    scored.sort(key=lambda kv: (-kv[1], kv[0]))
    return scored


def _grade(top1_score: float) -> str:
    """Map a top1 score onto a nomination confidence grade."""
    if top1_score >= _AUTO_HIGH:
        return "high"
    if top1_score >= _AUTO_LOW:
        return "low"
    return "unknown"


def collect_auto_candidate(image: np.ndarray, *,
                           frame_path: str | Path | None = None,
                           session_id: str | None = None,
                           pending_dir: Path | None = None) -> dict:
    """Nominate the best-looking label as a pending candidate on a miss.

    Fires only when the red badge is present but no label clears the gate.
    Three grades, all of which still require the human review step before
    reaching the production manifest:

    ``high``    top1 >= 0.97 — confidently that tile, just missing a live
                variant; the operator can confirm straight from the crop.
    ``low``     0.85 <= top1 < 0.97 — likely that tile; the operator must
                eyeball the enlarged crop before confirming.
    ``unknown`` 0.70 <= top1 < 0.85 — no label is close; the crop is kept
                with the top-k scores attached so a human can name it.

    Below 0.70 the frame is treated as noise (occluded sprite or mid-animation)
    and nothing is written.
    """
    if image is None or image.shape != (900, 1600, 3):
        return {"status": "bad_image", "label": None}
    observed = caishen_vision.detect_caishen(image)
    if observed.label is not None:
        return {"status": "matched", "label": observed.label,
                "score": observed.score}
    if observed.reason != "财神小牌匹配不够确定":
        return {"status": "no_badge", "label": None, "reason": observed.reason}

    ranked = rank_labels(image)
    if not ranked:
        return {"status": "bad_image", "label": None}
    top1_label, top1_score = ranked[0]
    topk = [{"label": lb, "score": round(sc, 4)} for lb, sc in ranked[:_AUTO_TOPK]]
    if top1_score < _AUTO_FLOOR:
        return {"status": "noise", "label": None,
                "score": top1_score, "topk": topk}

    grade = _grade(top1_score)
    store = Path(pending_dir) if pending_dir is not None else PENDING_DIR
    # P0-4: never pre-fill label from model top1 — nominations stay unlabeled.
    return _write_auto_nomination(
        image, store, top1_score, topk, grade=grade,
        suggested_label=top1_label,
        frame_path=frame_path, session_id=session_id)


def _write_auto_nomination(image: np.ndarray, store: Path, top1_score: float,
                           topk: list[dict], *, grade: str,
                           suggested_label: str | None = None,
                           frame_path: str | Path | None = None,
                           session_id: str | None = None) -> dict:
    """Keep an unlabeled crop; suggested_label/topk are hints, never gold (P0-4).

    Written as ``unknown_<stamp>_<digest>.png``. ``--confirm`` refuses null
    labels — use ``--confirm <name> --label <tile>``.
    """
    (x0, y0, x1, y1) = _roi()
    crop = np.ascontiguousarray(image[y0:y1, x0:x1])
    ok, encoded = cv2.imencode(".png", crop)
    if not ok:
        return {"status": "encode_failed", "label": None}
    data = encoded.tobytes()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    digest8 = hashlib.sha256(data).hexdigest()[:8]
    name = f"unknown_{stamp}_{digest8}"
    store.mkdir(parents=True, exist_ok=True)
    if suggested_label:
        for record in _pending_records(store):
            if (record.get("label") is None
                    and record.get("suggested_label") == suggested_label
                    and str(record.get("visual_check", "")).startswith("pending: auto_")):
                return {"status": "duplicate", "label": None,
                        "suggested_label": suggested_label,
                        "existing": record.get("file"),
                        "session": record.get("session_id")}
    (store / f"{name}.png").write_bytes(data)
    record = {
        "label": None,
        "suggested_label": suggested_label,
        "file": f"{name}.png",
        "source": str(frame_path) if frame_path else "unrecorded",
        "source_sha256": (hashlib.sha256(Path(frame_path).read_bytes()).hexdigest()
                          if frame_path and Path(frame_path).exists() else None),
        "asset_sha256": hashlib.sha256(data).hexdigest(),
        "session_id": session_id or _session_id(frame_path),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "visual_check": f"pending: auto_{grade}",
        "source_kind": "auto_nomination",
        "top1_score": round(top1_score, 4),
        "topk": topk,
        "pending": True,
    }
    (store / f"{name}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    status = "unknown" if grade == "unknown" else "pending"
    return {"status": status, "label": None, "suggested_label": suggested_label,
            "file": f"{name}.png", "score": top1_score, "topk": topk,
            "grade": grade}


def _write_unknown(image: np.ndarray, store: Path, top1_score: float,
                   topk: list[dict], *,
                   frame_path: str | Path | None = None,
                   session_id: str | None = None) -> dict:
    """Backward-compatible alias for unlabeled auto nominations."""
    suggested = topk[0]["label"] if topk else None
    return _write_auto_nomination(
        image, store, top1_score, topk, grade="unknown",
        suggested_label=suggested, frame_path=frame_path, session_id=session_id)

