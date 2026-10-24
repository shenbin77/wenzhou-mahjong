"""The caishen collector captures manual picks without touching the manifest."""

import hashlib
import json
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import caishen_vision  # noqa: E402
import caishen_collector as collector  # noqa: E402

CAPTURES = BOT_DIR.parent / "采集"
LIVE_SESSION = CAPTURES / "assist_sessions" / "20261023_223759" / "live" / "tea_2p"
# 牌局结束浮层/大厅帧：ROI 里根本没有财字角标
_NO_BADGE_FRAME = (CAPTURES / "assist_sessions" / "20261023_201640" / "live" /
                   "tea_2p" / "20261023_210537_989509_6a4b5c.png")
_PENDING_READ = caishen_vision.CaishenRead(None, 0.971, 0.965, "财神小牌匹配不够确定")


def _read(path: Path) -> np.ndarray:
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)


_W8_FRAME: Path | None = None


def _w8_frame() -> Path:
    """223759 会话里财神为八萬（w8）的第一帧；样本在本局内是静态的。

    结果做模块级 memo：扫描 340 帧较慢，而且必须在沙箱改动 manifest **之前**
    用正式 manifest 找到这一帧。
    """
    global _W8_FRAME
    if _W8_FRAME is not None:
        return _W8_FRAME
    for path in sorted(LIVE_SESSION.glob("*.png")):
        if caishen_vision.detect_caishen(_read(path)).label == "w8":
            _W8_FRAME = path
            return path
    pytest.skip("223759 会话的八萬局样本已不在此机器上")


def _sandbox_dir(tmp_path: Path, drop_live_of: str | None = None) -> Path:
    """复制正式模板目录，可选摘掉某 label 的实战来源条目（保留回放条目）。"""
    sandbox = tmp_path / "caishen"
    sandbox.mkdir()
    for item in caishen_vision._DIR.iterdir():
        if item.is_file():
            shutil.copy(item, sandbox / item.name)
    if drop_live_of is not None:
        manifest = json.loads((sandbox / "manifest.json").read_text(encoding="utf-8"))
        manifest["templates"] = [
            t for t in manifest["templates"]
            if not (t["label"] == drop_live_of and "assist_sessions" in t["source"])]
        (sandbox / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return sandbox


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """指向沙箱模板目录并清空缓存字典；用例结束自动还原。"""
    def _install(drop_live_of: str | None = None) -> Path:
        sandbox = _sandbox_dir(tmp_path, drop_live_of)
        monkeypatch.setattr(caishen_vision, "_DIR", sandbox)
        monkeypatch.setattr(caishen_vision, "_TEMPLATE_CACHE", {})
        return sandbox
    return _install


# --- 缓存按 manifest 变化自动失效 -------------------------------------------

def test_template_cache_reloads_after_manifest_changes(sandbox):
    """改 manifest 不重启进程，下一帧就要读到新模板。"""
    sandbox(drop_live_of="w8")   # 摘掉 w8 的实战来源条目，仅保留回放条目
    _, first = caishen_vision._templates()
    assert len(first["w8"]) == 1, "沙箱应只保留 w8 的回放模板"

    data = (caishen_vision._DIR / "w8.png").read_bytes()
    manifest = json.loads((caishen_vision._DIR / "manifest.json").read_text(encoding="utf-8"))
    manifest["templates"].append({
        "label": "w8", "file": "w8.png", "source": "unit-test",
        "source_sha256": "0" * 64,
        "asset_sha256": hashlib.sha256(data).hexdigest(),
        "visual_check": "unit-test", "cluster_frames": 1,
    })
    (caishen_vision._DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _, second = caishen_vision._templates()
    assert len(second["w8"]) == 2


def test_thresholds_and_roi_untouched():
    assert caishen_vision._MIN_SCORE == .98
    assert caishen_vision._MIN_MARGIN == .02
    assert caishen_vision._templates()[0] == (622, 348, 677, 423)


# --- 捕获 / 去重 ------------------------------------------------------------

def test_pending_write_and_dedupe(sandbox, tmp_path):
    """角标在、牌面认不出、该牌种还没有实战模板 → 写 pending；重复触发跳过。"""
    frame = _w8_frame()          # 必须在沙箱改动 manifest 之前定位样本帧
    sandbox(drop_live_of="w8")
    image = _read(frame)
    read = caishen_vision.detect_caishen(image)
    assert read.label is None and read.reason == "财神小牌匹配不够确定"
    store = tmp_path / "pending"
    outcome = collector.collect_pending(image, "w8", frame_path=frame, pending_dir=store)
    assert outcome["status"] == "pending", outcome
    pngs = list(store.glob("*.png"))
    sidecars = list(store.glob("*.json"))
    assert len(pngs) == 1 and len(sidecars) == 1
    record = json.loads(sidecars[0].read_text(encoding="utf-8"))
    assert record["label"] == "w8"
    assert record["visual_check"] == "pending: manual_pick"
    assert record["asset_sha256"] == hashlib.sha256(pngs[0].read_bytes()).hexdigest()
    assert record["source_sha256"] == hashlib.sha256(frame.read_bytes()).hexdigest()
    assert record["session_id"] == "20261023_223759"
    assert record["at"]
    # 候选 ROI 必须与正式模板同尺寸
    assert cv2.imdecode(np.fromfile(str(pngs[0]), np.uint8), cv2.IMREAD_COLOR).shape \
        == (423 - 348, 677 - 622, 3)
    # 再跑一次：同 label 已在 pending，必须跳过，不得刷库
    again = collector.collect_pending(image, "w8", frame_path=frame, pending_dir=store)
    assert again["status"] == "duplicate"
    assert len(list(store.glob("*.png"))) == 1


def test_manual_pick_on_already_matched_frame_is_a_noop(sandbox, tmp_path):
    """正式 manifest 下画面自己能读出 w8：人工标注没有新增信息，不落盘。"""
    sandbox()
    image = _read(_w8_frame())
    assert caishen_vision.detect_caishen(image).label == "w8"
    store = tmp_path / "pending"
    outcome = collector.collect_pending(image, "w8", pending_dir=store)
    assert outcome["status"] == "matched"
    assert not store.exists() or not list(store.glob("*"))


def test_already_covered_labels_are_never_captured(sandbox, tmp_path, monkeypatch):
    """画面弃权但该 label 的实战模板已覆盖（>=0.98）→ 不得重复采集。"""
    sandbox()
    image = _read(_w8_frame())
    store = tmp_path / "pending"
    monkeypatch.setattr(caishen_vision, "detect_caishen", lambda img: _PENDING_READ)
    outcome = collector.collect_pending(image, "w8", pending_dir=store)
    assert outcome["status"] == "already_covered"
    assert outcome["score"] >= .98
    assert not store.exists() or not list(store.glob("*.png"))


def test_invalid_label_and_no_badge_never_write(sandbox, tmp_path):
    sandbox()
    image = _read(_w8_frame())
    store = tmp_path / "pending"
    assert collector.collect_pending(
        image, "不存在", pending_dir=store)["status"] == "invalid_label"
    assert collector.collect_pending(
        image[:800], "w8", pending_dir=store)["status"] == "bad_image"
    assert not store.exists() or not list(store.glob("*"))


def test_frame_without_badge_is_never_a_sample(sandbox, tmp_path):
    """非牌桌帧（大厅/结算）连财字角标都没有，绝不能当样本收。"""
    sandbox()
    if not _NO_BADGE_FRAME.exists():
        pytest.skip("实战截屏样本已不在此机器上")
    outcome = collector.collect_pending(
        _read(_NO_BADGE_FRAME), "w8", frame_path=_NO_BADGE_FRAME,
        pending_dir=tmp_path / "pending")
    assert outcome["status"] == "no_badge"
    assert not (tmp_path / "pending").exists()
