"""Reopen the same app: choose an unused serial or focus an existing UI."""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from app_entry import choose_device


def test_first_click_chooses_first_free_emulator():
    assert choose_device(["emulator-5556","emulator-5558"],{}) == ("emulator-5556",None)


def test_second_click_chooses_other_emulator_not_duplicate():
    assert choose_device(["emulator-5556","emulator-5558"],{"emulator-5556":1234}) == ("emulator-5558",None)


def test_when_all_windows_open_focusing_existing_not_error():
    assert choose_device(["emulator-5556","emulator-5558"],
                         {"emulator-5556":1234,"emulator-5558":5678}) == (None,1234)


def test_explicit_device_focuses_existing_window():
    assert choose_device(["emulator-5556","emulator-5558"],
                         {"emulator-5556":1234},"emulator-5556") == ("emulator-5556",1234)


def test_no_connected_device_fails_closed():
    assert choose_device([], {}) == (None,None)
