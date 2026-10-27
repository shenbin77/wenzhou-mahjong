"""Offline native-frame fixtures. All runtime output stays in the stage."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
# Works both in the isolated stage and after parent migrates these tests into bot/tests.
BOT = ROOT if (ROOT / "tests/fixtures").is_dir() else Path(
    json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["formal_bot"])

from assist_tea import analyze_snapshot
from harvest_tea import EXPECTED_PACKAGE
from lobby_preflight import classify_session_scene

FILES = {
    "before": BOT / "tests/fixtures/tea2_melded_hu/20261027_020223_972223_before_9e5984.png",
    "claim": BOT / "tests/fixtures/tea2_melded_hu/20261027_020226_207971_before_f7e6b4.png",
    "ambiguous": BOT / "tests/fixtures/incoming_shadow/20261027_174850_853803_before_e60abb.png",
    "modal": BOT / "tests/fixtures/session_scenes/renew_table_modal.png",
}


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    import subprocess
    def forbidden(*args, **kwargs):
        raise AssertionError("offline tests must not launch a process or use ADB")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture(scope="session")
def native_sources(tmp_path_factory):
    result = {}
    logs = tmp_path_factory.mktemp("shadow_context_native_analysis")
    for name, path in FILES.items():
        assert path.is_file(), path
        raw = path.read_bytes()
        image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        analysis = (analyze_snapshot(path, caishen_label="", confirmed_two_player=True,
                                    log_dir=logs) if name != "modal" else None)
        result[name] = dict(path=path, raw=raw, image=image, analysis=analysis,
                            scene=classify_session_scene(image).ledger())
    return result


class Clock:
    def __init__(self):
        self.value = 10_000_000_000
    def mono(self):
        self.value += 1_000_000
        return self.value
    def wall(self):
        return 1_800_000_000_000_000_000 + self.value
    def advance(self, seconds):
        self.value += int(seconds * 1e9)


def feed(observer, frame, *, phase="in_play", finish=False, analysis=None,
         recovery=None, scope_round=1, extra=None):
    """The same observed inputs that the production wrappers receive."""
    observer.call("begin_cycle")
    observer.call("foreground", EXPECTED_PACKAGE)
    token = observer.call("capture_started")
    observer.call("capture_returned", token, frame["raw"])
    observer.call("scene", frame["image"], frame["scene"], frame["path"])
    observer.call("runtime_scope", round_seq=scope_round, hud_key=None,
                  caishen=(frame["analysis"] or {}).get("caishen"),
                  discards_trusted=True)
    actual = copy.deepcopy(frame["analysis"]) if analysis is None else analysis
    observer.call("analysis_returned", frame["raw"], frame["path"], actual)
    if extra:
        extra(observer)
    report = observer.call("prepare_claim", hand_recovery=recovery) if phase == "claim" else None
    event = {"event": "idle", "status": "wait", "round_signal": {"state": "same"}}
    if phase == "claim":
        event = {"event": "idle", "status": "guard", "guard": "action_visible",
                 "claim": {"status": "blocked"}}
    if finish:
        observer.call("runtime_event", event)
        observer.call("end_cycle", event)
    return report
