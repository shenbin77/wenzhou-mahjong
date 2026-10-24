"""财神模板的独立帧验证（held-out）与 leave-one-source-out 测试。

口径铁律（2026-10-24 证据链审计）：

* 模板在**自己的来源帧**上的匹配只算「来源回放通过」——它是模板诞生的地方，
  分数再高（0.99998 也一样）也**不构成泛化证据**，更不能写成"准确率"或
  "实战可用"。
* 泛化证据只有一种形态：模板在另一张 ``source_frame_hash`` **不同**的实战帧
  上取得 >= ``caishen_vision._MIN_SCORE`` 的分数（见证帧 / witness）。
* 证据不足一律标「未确认」（fail-closed），阈值 0.98 / 0.02 不因任何理由下调。

本模块只做判定与取证，不写 manifest、不改模板；入库闸口在
``bot/review_caishen_pending.py``。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cv2
import numpy as np

import caishen_vision

_BOT_DIR = Path(__file__).resolve().parent
# 默认见证帧检索范围：所有 assist 会话的实战截屏。
DEFAULT_SESSIONS_ROOT = _BOT_DIR.parent / "采集" / "assist_sessions"

_FRAME_SIZE = (900, 1600, 3)


def read_frame(path: str | Path) -> np.ndarray | None:
    """中文路径安全地读一帧 BGR 图；读不出返回 None。"""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    return img


def frame_sha256(path: str | Path) -> str:
    """整帧文件内容的 SHA-256——「是否同一来源帧」的判定依据。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def badge_present(crop_bgr: np.ndarray) -> bool:
    """与 :func:`caishen_vision.detect_caishen` 完全一致的财字角标判定。"""
    badge = crop_bgr[:22, :30]
    red = (badge[:, :, 2] > 175) & (badge[:, :, 1] < 160) & (badge[:, :, 0] < 110)
    return int(red.sum()) >= 70


def _roi() -> tuple[int, int, int, int]:
    roi, _ = caishen_vision._templates()
    return tuple(roi)  # type: ignore[return-value]


def score_on_frame(tile_gray: np.ndarray, image: np.ndarray) -> float | None:
    """候选模板（灰度 ROI 裁图）在一帧实战图上的 TM_CCOEFF_NORMED 分数。"""
    if image is None or image.shape != _FRAME_SIZE:
        return None
    x0, y0, x1, y1 = _roi()
    gray = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    return float(cv2.matchTemplate(gray, tile_gray, cv2.TM_CCOEFF_NORMED)[0, 0])


def check_witness(tile_gray: np.ndarray, witness_path: str | Path,
                  source_frame_hash: str | None, *,
                  min_score: float | None = None) -> dict:
    """用一张**人工指定的见证帧**验证候选模板。

    通过条件（全部满足）：
      1. 见证帧存在且为 1600×900 实战帧；
      2. 见证帧整帧哈希与候选来源帧哈希**不同**（同源帧自匹配不算证据）；
      3. 见证帧带财字角标（确为财神帧）；
      4. 候选模板在其 ROI 上得分 >= ``min_score``（默认生产阈值 0.98）。
    """
    threshold = caishen_vision._MIN_SCORE if min_score is None else min_score
    witness_path = Path(witness_path)
    result = {"passed": False, "score": 0.0, "witness_frame": str(witness_path),
              "witness_frame_hash": None, "reason": ""}
    if not witness_path.exists():
        result["reason"] = "见证帧不存在"
        return result
    try:
        raw = witness_path.read_bytes()
    except OSError:
        result["reason"] = "见证帧读取失败"
        return result
    witness_hash = hashlib.sha256(raw).hexdigest()
    result["witness_frame_hash"] = witness_hash
    if source_frame_hash and witness_hash == source_frame_hash:
        result["reason"] = ("同源帧自匹配：见证帧与候选来源帧哈希相同，"
                            "只能算来源回放通过，不算独立证据")
        return result
    img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None or img.shape != _FRAME_SIZE:
        result["reason"] = "见证帧不是已验证的 1600×900 实战帧"
        return result
    x0, y0, x1, y1 = _roi()
    crop = img[y0:y1, x0:x1]
    if not badge_present(crop):
        result["reason"] = "见证帧没有财字角标，不是有效财神帧"
        return result
    score = score_on_frame(tile_gray, img)
    result["score"] = round(score if score is not None else 0.0, 5)
    if score is None or score < threshold:
        result["reason"] = (f"候选模板在见证帧上只有 "
                            f"{result['score']:.5f} < {threshold}，未通过")
        return result
    result["passed"] = True
    result["reason"] = "独立帧验证通过"
    return result


def find_independent_witness(tile_gray: np.ndarray,
                             source_frame_hash: str | None, *,
                             sessions_root: str | Path | None = None,
                             min_score: float | None = None,
                             max_frames: int = 2000) -> dict | None:
    """在 assist 会话语料里自动寻找一张通过验证的独立帧。

    与 :func:`check_witness` 同一套通过条件；为避免全盘扫描过慢，按均匀
    步长抽样至多 ``max_frames`` 帧，找到第一张合格帧即返回。找不到返回
    None——调用方必须 fail-closed（拒绝入库），不得退而求其次。
    """
    threshold = caishen_vision._MIN_SCORE if min_score is None else min_score
    root = Path(sessions_root) if sessions_root else DEFAULT_SESSIONS_ROOT
    if not root.exists():
        return None
    frames = sorted(p for p in root.rglob("*.png") if not p.name.startswith("_"))
    if not frames:
        return None
    stride = max(1, len(frames) // max_frames)
    x0, y0, x1, y1 = _roi()
    for path in frames[::stride]:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None or img.shape != _FRAME_SIZE:
            continue
        crop = img[y0:y1, x0:x1]
        if not badge_present(crop):
            continue
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        score = float(cv2.matchTemplate(gray, tile_gray, cv2.TM_CCOEFF_NORMED)[0, 0])
        if score < threshold:
            continue
        witness_hash = hashlib.sha256(raw).hexdigest()
        if source_frame_hash and witness_hash == source_frame_hash:
            continue  # 同源帧自匹配，跳过继续找
        return {"passed": True, "score": round(score, 5),
                "witness_frame": str(path),
                "witness_frame_hash": witness_hash,
                "reason": "独立帧验证通过（自动发现）"}
    return None


def leave_one_source_out(pairs: list[tuple[str | Path, str]], *,
                         template_dir: str | Path | None = None,
                         min_score: float | None = None,
                         min_margin: float | None = None) -> list[dict]:
    """Leave-one-source-out：留出与验证帧同源的模板，看剩余模板能否读对。

    ``pairs`` 是 ``(验证帧路径, 该帧人工确认的真值标签)`` 列表。对每一帧：

    * 从模板库剔除所有 ``source_frame_hash`` 与该帧整帧哈希相同的模板
      （即"留出该来源帧"），用**剩余**模板跑与生产一致的判定
      （best-score + ``min_score``/``min_margin`` 门禁）；
    * ``cross_source_score`` 记录真值标签在剩余模板上的最高分——它就是
      "排除同源自匹配后的真实分数"，是多少报多少，不粉饰；
    * ``passed`` 仅在剩余模板把帧读成真值标签时为 True。

    返回逐帧结果列表；``note == "no_remaining_template"`` 表示留出后该标签
    已无模板，属于"未确认"，不算通过。
    """
    template_dir = Path(template_dir) if template_dir else caishen_vision._DIR
    threshold = caishen_vision._MIN_SCORE if min_score is None else min_score
    margin = caishen_vision._MIN_MARGIN if min_margin is None else min_margin
    manifest = json.loads(
        (template_dir / "manifest.json").read_text(encoding="utf-8"))
    x0, y0, x1, y1 = (int(v) for v in manifest["roi"])
    entries: list[dict] = []
    for item in manifest["templates"]:
        try:
            data = (template_dir / item["file"]).read_bytes()
        except OSError:
            continue
        tile = cv2.imdecode(np.frombuffer(data, dtype=np.uint8),
                            cv2.IMREAD_GRAYSCALE)
        if tile is None:
            continue
        entries.append({
            "label": item["label"],
            "file": item["file"],
            "tile": tile,
            "source_hash": (item.get("source_frame_hash")
                            or item.get("source_sha256")),
        })

    results: list[dict] = []
    for frame_path, true_label in pairs:
        frame_path = Path(frame_path)
        try:
            fhash = hashlib.sha256(frame_path.read_bytes()).hexdigest()
        except OSError:
            results.append({"frame": frame_path.name, "frame_sha256": None,
                            "true_label": true_label, "held_out": [],
                            "passed": False, "detected_label": None,
                            "detected_score": 0.0, "cross_source_score": 0.0,
                            "note": "frame_unreadable"})
            continue
        held_out = [e["file"] for e in entries if e["source_hash"] == fhash]
        remaining = [e for e in entries if e["source_hash"] != fhash]
        result = {"frame": frame_path.name, "frame_sha256": fhash,
                  "true_label": true_label, "held_out": held_out,
                  "passed": False, "detected_label": None,
                  "detected_score": 0.0, "cross_source_score": 0.0,
                  "note": ""}
        img = read_frame(frame_path)
        if img is None or img.shape != _FRAME_SIZE:
            result["note"] = "frame_unreadable"
            results.append(result)
            continue
        gray = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        per_label: dict[str, float] = {}
        for entry in remaining:
            score = float(cv2.matchTemplate(gray, entry["tile"],
                                            cv2.TM_CCOEFF_NORMED)[0, 0])
            per_label[entry["label"]] = max(per_label.get(entry["label"], 0.0),
                                            score)
        result["cross_source_score"] = round(per_label.get(true_label, 0.0), 5)
        if true_label not in per_label:
            result["note"] = "no_remaining_template"
        if per_label:
            ranked = sorted(per_label.items(), key=lambda kv: (-kv[1], kv[0]))
            best_label, best = ranked[0]
            second = ranked[1][1] if len(ranked) > 1 else 0.0
            if best >= threshold and best - second >= margin:
                result["detected_label"] = best_label
                result["detected_score"] = round(best, 5)
                result["passed"] = best_label == true_label
        results.append(result)
    return results


def source_replay_check(tile_gray: np.ndarray, source_frame_path: str | Path, *,
                        min_score: float | None = None) -> dict:
    """在**来源帧自身**上打分——只能证明「来源回放」，绝不是 heldout。

    即便分数到 0.99998 / 1.0，也必须保持 ``heldout_passed=False``，并在
    ``note`` 里写明「同源自匹配不算泛化证据」。不得据此写成准确率或实战可用。
    """
    threshold = caishen_vision._MIN_SCORE if min_score is None else min_score
    source_frame_path = Path(source_frame_path)
    result = {
        "source_replay_passed": False,
        "heldout_passed": False,  # 铁律：来源回放永不为 heldout
        "score": 0.0,
        "source_frame": str(source_frame_path),
        "source_frame_hash": None,
        "reason": "",
        "note": "同源自匹配只能写来源回放通过，不算泛化 / 准确率 / 实战可用",
    }
    if not source_frame_path.exists():
        result["reason"] = "来源帧不存在"
        return result
    try:
        raw = source_frame_path.read_bytes()
    except OSError:
        result["reason"] = "来源帧读取失败"
        return result
    result["source_frame_hash"] = hashlib.sha256(raw).hexdigest()
    img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None or img.shape != _FRAME_SIZE:
        result["reason"] = "来源帧不是已验证的 1600×900 实战帧"
        return result
    score = score_on_frame(tile_gray, img)
    result["score"] = round(score if score is not None else 0.0, 5)
    if score is None or score < threshold:
        result["reason"] = (
            f"来源回放未通过：{result['score']:.5f} < {threshold}")
        return result
    result["source_replay_passed"] = True
    result["reason"] = "来源回放通过（非 heldout）"
    return result


def evaluate_promotion_heldout(tile_gray: np.ndarray,
                               source_frame_hash: str | None,
                               witness_path: str | Path | None = None, *,
                               min_score: float | None = None) -> dict:
    """入库闸口用的 heldout 判定——缺独立见证帧一律 fail-closed。

    不发明金标、不扫描替代帧冒充通过；``witness_path`` 为空或校验失败时
    ``passed`` 恒为 False，调用方必须把 ``heldout_passed`` 写成 False。
    """
    if not witness_path:
        return {
            "passed": False,
            "score": 0.0,
            "witness_frame": None,
            "witness_frame_hash": None,
            "reason": "未提供独立见证帧：heldout 未确认（fail-closed）",
        }
    return check_witness(tile_gray, witness_path, source_frame_hash,
                         min_score=min_score)


def leave_one_source_out_or_fail_closed(
        pairs: list[tuple[str | Path, str]] | None, *,
        template_dir: str | Path | None = None,
        min_score: float | None = None,
        min_margin: float | None = None) -> dict:
    """Leave-one-source-out 的 fail-closed 包装。

    * ``pairs`` 为空 / None → 直接未确认，**绝不**用合成或猜测标签凑数；
    * 有对则委派 :func:`leave_one_source_out`，聚合 ``passed`` 仅在全部帧
      都读对时为 True；任一帧 ``no_remaining_template`` / 读错 → 整体未通过。
    """
    if not pairs:
        return {
            "passed": False,
            "results": [],
            "n_pairs": 0,
            "n_passed": 0,
            "reason": "无独立金标帧对：leave-one-source-out 未确认（fail-closed）；"
                      "不得发明第二套金标",
        }
    results = leave_one_source_out(
        pairs, template_dir=template_dir, min_score=min_score,
        min_margin=min_margin)
    n_passed = sum(1 for r in results if r.get("passed"))
    unfinished = [r for r in results
                  if r.get("note") in {"no_remaining_template",
                                       "frame_unreadable"}]
    if unfinished and n_passed == 0:
        reason = ("留出同源模板后无剩余模板或帧不可读："
                  "leave-one-source-out 未确认（fail-closed）")
    elif n_passed == len(results) and results:
        reason = "leave-one-source-out 全部通过"
    else:
        reason = (f"leave-one-source-out 未全部通过："
                  f"{n_passed}/{len(results)}")
    return {
        "passed": bool(results) and n_passed == len(results),
        "results": results,
        "n_pairs": len(results),
        "n_passed": n_passed,
        "reason": reason,
    }
