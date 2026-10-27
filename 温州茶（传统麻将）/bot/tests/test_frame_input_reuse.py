"""One observation reaches tea advice without rereads or injected-vision bypass."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import cv2
import numpy as np
import pytest

import live_capture
from assist_tea import analyze_snapshot
from frame_input import DecodedFrame
from tea_bot import TeaBot


FIXTURES = Path(__file__).parent / "fixtures" / "tea2_melded_hu"
SELF_DRAW = FIXTURES / "20261027_020712_698703_before_8b941d.png"
WINNING_HAND = [1, 2, 3, 4, 5, 6, 7, 8, 9, 21, 22, 23, 41, 41, 41, 61, 61]


def png(value=0, *, shape=(32, 32, 3)):
    ok, encoded = cv2.imencode(".png", np.full(shape, value, dtype=np.uint8))
    assert ok
    return encoded.tobytes()


def close_bot(bot):
    for handler in list(bot.logger.handlers):
        bot.logger.removeHandler(handler)
        handler.close()


def count_frame_decodes(monkeypatch, raw):
    calls = []
    original = cv2.imdecode

    def counted(encoded, *args, **kwargs):
        if encoded.nbytes == len(raw) and encoded.tobytes() == raw:
            calls.append(1)
        return original(encoded, *args, **kwargs)

    monkeypatch.setattr(cv2, "imdecode", counted)
    return calls


def test_frame_cannot_mix_bytes_and_pixels_or_mutate_observation(monkeypatch):
    raw = png(7)
    calls = count_frame_decodes(monkeypatch, raw)
    first = DecodedFrame.from_png(raw)
    second = DecodedFrame.from_png(raw)
    assert calls == [1, 1]  # Identical bytes never become a global frame cache.
    assert first is not second and first.image is not second.image
    assert np.all(first.image == 7)
    with pytest.raises(TypeError):
        DecodedFrame(raw, np.zeros((32, 32, 3), np.uint8))
    with pytest.raises(FrozenInstanceError):
        first.raw = png(8)
    with pytest.raises(ValueError):
        first.image[0, 0, 0] = 8
    with pytest.raises(ValueError):
        first.image.setflags(write=True)


@pytest.mark.parametrize("raw", [b"", b"not a png"])
def test_invalid_frame_abstains_without_calling_analysis(tmp_path, raw, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid PNG reached a recognizer")

    monkeypatch.setattr(live_capture, "observed_caishen", forbidden)
    result = live_capture.analyze_bytes(
        raw, mode="tea_2p", image_path=tmp_path / "absent.png", log_dir=tmp_path)
    assert result["status"] == "abstain"
    with pytest.raises(ValueError):
        DecodedFrame.from_png(raw)


def test_mismatched_raw_and_frame_is_rejected_before_analysis(tmp_path, monkeypatch):
    frame = DecodedFrame.from_png(png(7))

    def forbidden(*args, **kwargs):
        pytest.fail("mismatched observation reached a recognizer")

    monkeypatch.setattr(live_capture, "observed_caishen", forbidden)
    with pytest.raises(ValueError, match="does not match"):
        live_capture.analyze_bytes(
            png(8), mode="tea_2p", frame=frame,
            image_path=tmp_path / "absent.png", log_dir=tmp_path)


def test_see_frame_keeps_initial_and_later_vision_injection(tmp_path):
    frame = DecodedFrame.from_png(png())
    seen = []

    def first(raw):
        assert raw is frame.raw and isinstance(raw, bytes)
        seen.append("initial")
        return WINNING_HAND

    bot = TeaBot(vision_fn=first, log_dir=str(tmp_path))
    try:
        assert bot.see_frame(frame) == WINNING_HAND

        def replacement(raw):
            assert raw is frame.raw
            seen.append("replacement")
            return WINNING_HAND[:-1]

        bot.vision_fn = replacement
        assert bot.see_frame(frame) == WINNING_HAND[:-1]
        assert seen == ["initial", "replacement"]
    finally:
        close_bot(bot)


@pytest.mark.parametrize("override", ["subclass", "class_patch"])
def test_see_frame_honors_default_method_overrides(tmp_path, monkeypatch, override):
    frame = DecodedFrame.from_png(png())
    seen = []

    def replacement(self, raw):
        assert raw is frame.raw
        seen.append(raw)
        return WINNING_HAND

    if override == "subclass":
        class CustomBot(TeaBot):
            _default_vision = replacement
        bot_class = CustomBot
    else:
        monkeypatch.setattr(TeaBot, "_default_vision", replacement)
        bot_class = TeaBot
    bot = bot_class(log_dir=str(tmp_path))
    try:
        assert bot.see_frame(frame) == WINNING_HAND
        assert seen == [frame.raw]
    finally:
        close_bot(bot)


@pytest.mark.parametrize("entry", ["bytes", "path", "decoded"])
def test_native_melded_hu_decodes_once_and_keeps_recognition_mark(
        tmp_path, monkeypatch, entry):
    raw = SELF_DRAW.read_bytes()
    calls = count_frame_decodes(monkeypatch, raw)
    reads = []
    original_read = Path.read_bytes

    def counted_read(path):
        if path == SELF_DRAW:
            reads.append(path)
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read)
    if entry == "path":
        result = analyze_snapshot(
            SELF_DRAW, caishen_label="", confirmed_two_player=True, log_dir=tmp_path)
        assert reads == [SELF_DRAW]
    else:
        frame = DecodedFrame.from_png(raw) if entry == "decoded" else None
        clock = live_capture.SegmentClock()
        original_think = TeaBot.think

        def checked_think(bot, hand, **kwargs):
            assert "recognize" in clock.marks
            return original_think(bot, hand, **kwargs)

        monkeypatch.setattr(TeaBot, "think", checked_think)
        result = live_capture.analyze_bytes(
            raw, mode="tea_2p", image_path=SELF_DRAW, log_dir=tmp_path,
            clock=clock, frame=frame)
        assert reads == []
        assert clock.marks["recognize"] <= clock.marks["advise"]
    assert len(calls) == 1
    assert result["status"] == "hu"
    assert result["hu_info"]["scoring_validated"] is False
    assert result["melds"] == [[43, 44, 45], [41, 42, 43]]


def test_raw_is_the_observation_even_if_evidence_path_contains_a_winning_hand(tmp_path):
    blank_raw = png(shape=(900, 1600, 3))
    result = live_capture.analyze_bytes(
        blank_raw, mode="tea_2p", image_path=SELF_DRAW, log_dir=tmp_path)
    assert result["status"] == "abstain"
    assert not result.get("hand_ids")
    assert not result.get("hu_info", {}).get("hu")


def test_analyze_bytes_does_not_require_a_saved_copy(tmp_path):
    result = live_capture.analyze_bytes(
        SELF_DRAW.read_bytes(), mode="tea_2p",
        image_path=tmp_path / "not-saved.png", log_dir=tmp_path)
    assert result["status"] == "hu"
    assert not (tmp_path / "not-saved.png").exists()
