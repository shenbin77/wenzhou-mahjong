"""Template-bank lifetime, invalidation and observation isolation contracts."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
from threading import Barrier, Lock

import cv2
import numpy as np
import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import tile_vision as tv


@pytest.fixture(autouse=True)
def isolated_banks():
    with tv._TEMPLATE_BANK_LOCK:
        tv._TEMPLATE_BANKS.clear()
    yield
    with tv._TEMPLATE_BANK_LOCK:
        tv._TEMPLATE_BANKS.clear()


def tile_image(kind=0):
    image = np.full((90, 70, 3), (215, 220, 225), np.uint8)
    if kind % 2:
        cv2.circle(image, (35, 43), 17, (20, 20, 140), 5)
        cv2.line(image, (20, 65), (52, 65), (25, 25, 25), 4)
    else:
        cv2.line(image, (35, 18), (35, 70), (25, 25, 25), 6)
        cv2.line(image, (18, 43), (53, 43), (25, 25, 25), 5)
    return image


def write_tile(path, kind):
    ok, png = cv2.imencode(".png", tile_image(kind))
    assert ok
    path.write_bytes(png.tobytes())


def make_bank(directory):
    directory.mkdir()
    records = [
        {"label": "w1", "file": "first.png"},
        {"label": "w2", "file": "second.png"},
        {"label": "w1", "file": "variant.png"},
    ]
    for index, record in enumerate(records):
        write_tile(directory / record["file"], index)
    (directory / "manifest.json").write_text(
        json.dumps({"templates": records}), encoding="utf-8")
    return directory


def load(directory):
    vision = tv.TileVision(template_dir=directory)
    vision.load_templates()
    return vision


def changed_stamp(path):
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def spy_reads(monkeypatch):
    original = tv._read
    calls = []
    lock = Lock()

    def read(path):
        with lock:
            calls.append(Path(path))
        return original(path)

    monkeypatch.setattr(tv, "_read", read)
    return calls


def test_new_instances_share_only_read_only_template_data(tmp_path, monkeypatch):
    directory = make_bank(tmp_path / "tiles")
    calls = spy_reads(monkeypatch)
    first = load(directory)
    expected = first.classify_crop(tile_image())
    second = load(directory)

    assert len(calls) == 3
    assert first._matrix is second._matrix
    assert first.templates is not second.templates
    assert first.classify_crop(tile_image()) == expected
    assert second.classify_crop(tile_image()) == expected
    assert first._matrix.flags.writeable is False
    with pytest.raises(ValueError):
        first._matrix[0, 0] = 0
    with pytest.raises(ValueError):
        first._matrix.setflags(write=True)
    with pytest.raises(ValueError):
        first.templates[0][2].setflags(write=True)

    first.templates.pop()
    first.caishen_label = "w9"
    first.diagnostics.append({"accepted": True})
    first.last_melds.append([1, 1, 1])
    first.last_complete = True
    assert len(second.templates) == 3
    assert second.caishen_label is None
    assert second.diagnostics == []
    assert second.last_melds == []
    assert second.last_complete is False


def test_manifest_change_replaces_bank(tmp_path, monkeypatch):
    directory = make_bank(tmp_path / "tiles")
    calls = spy_reads(monkeypatch)
    first = load(directory)
    manifest = directory / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["templates"][0]["label"] = "w3"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    changed_stamp(manifest)

    second = load(directory)
    assert len(calls) == 6
    assert second._matrix is not first._matrix
    assert first.templates[0][0] == "w1"
    assert second.templates[0][0] == "w3"
    assert len(tv._TEMPLATE_BANKS) == 1


def test_png_change_invalidates_even_when_manifest_is_unchanged(tmp_path):
    directory = make_bank(tmp_path / "tiles")
    first = load(directory)
    manifest_bytes = (directory / "manifest.json").read_bytes()
    write_tile(directory / "first.png", 1)
    changed_stamp(directory / "first.png")
    second = load(directory)

    assert (directory / "manifest.json").read_bytes() == manifest_bytes
    assert second._matrix is not first._matrix
    assert not np.array_equal(second._matrix[0], first._matrix[0])


@pytest.mark.parametrize("broken", ["manifest_missing", "png_missing", "png_corrupt"])
def test_invalid_assets_never_return_previously_cached_bank(tmp_path, broken):
    directory = make_bank(tmp_path / "tiles")
    first = load(directory)
    target = directory / ("manifest.json" if broken == "manifest_missing"
                          else "first.png")
    original = target.read_bytes()
    if broken == "png_corrupt":
        target.write_bytes(b"invalid png")
    else:
        target.unlink()

    retry = tv.TileVision(template_dir=directory)
    with pytest.raises((tv.NoTemplatesError, OSError, ValueError, cv2.error)):
        retry.load_templates()
    assert retry._loaded is False
    assert retry.templates == []
    assert retry._matrix is None
    assert not tv._TEMPLATE_BANKS

    target.write_bytes(original)
    retry.load_templates()
    assert retry._loaded is True
    np.testing.assert_array_equal(retry._matrix, first._matrix)


def test_change_during_loading_is_not_published(tmp_path, monkeypatch):
    directory = make_bank(tmp_path / "tiles")
    original_read = tv._read
    changed = False

    def read(path):
        nonlocal changed
        image = original_read(path)
        if not changed:
            changed = True
            manifest = directory / "manifest.json"
            manifest.write_text(manifest.read_text(encoding="utf-8") + " ",
                                encoding="utf-8")
        return image

    monkeypatch.setattr(tv, "_read", read)
    with pytest.raises(tv.NoTemplatesError, match="changed while loading"):
        load(directory)
    assert not tv._TEMPLATE_BANKS
    monkeypatch.setattr(tv, "_read", original_read)
    assert load(directory)._loaded is True


def test_concurrent_instances_publish_one_complete_bank(tmp_path, monkeypatch):
    directory = make_bank(tmp_path / "tiles")
    calls = spy_reads(monkeypatch)
    barrier = Barrier(8)

    def create(_index):
        barrier.wait(timeout=5)
        return load(directory)

    with ThreadPoolExecutor(max_workers=8) as pool:
        visions = list(pool.map(create, range(8)))
    assert len(calls) == 3
    assert len({id(vision._matrix) for vision in visions}) == 1
    assert len({id(vision.diagnostics) for vision in visions}) == 8
    assert all(len(vision.templates) == 3 for vision in visions)


def test_cache_is_bounded_and_evicted_directory_reloads(tmp_path, monkeypatch):
    directories = [make_bank(tmp_path / str(index))
                   for index in range(tv._TEMPLATE_BANK_LIMIT + 1)]
    calls = spy_reads(monkeypatch)
    for directory in directories:
        load(directory)
    assert len(tv._TEMPLATE_BANKS) == tv._TEMPLATE_BANK_LIMIT
    before = len(calls)
    load(directories[0])
    assert len(calls) == before + 3
    assert len(tv._TEMPLATE_BANKS) == tv._TEMPLATE_BANK_LIMIT


def test_template_directories_and_thresholds_remain_independent(tmp_path):
    one = make_bank(tmp_path / "one")
    two = make_bank(tmp_path / "two")
    write_tile(two / "first.png", 1)
    first = load(one)
    second = load(two)
    strict = tv.TileVision(template_dir=one, min_score=1.1)
    strict.load_templates()
    assert first._matrix is not second._matrix
    assert strict._matrix is first._matrix
    assert not np.array_equal(first._matrix[0], second._matrix[0])
    assert first.classify_crop(tile_image())["accepted"] is True
    assert strict.classify_crop(tile_image())["accepted"] is False
