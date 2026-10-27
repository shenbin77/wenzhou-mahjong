"""Bounded, evidence-only capture provenance for incoming A shadow.

This observer owns no gameplay state or click authorization. Its current producer
cannot prove reconnect absence, a reliable HUD round identity, or stable physical
hand completeness. Those facts remain None with explicit blockers. In particular,
a complete observation record is not the same as a trusted hand.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
import copy
import hashlib
import math
from numbers import Integral, Real
import time
import uuid


SCHEMA_VERSION = 1
MAX_CAPTURES = 32
MAX_EVENTS = 128
MAX_SEQUENCE = 32
BASELINE_MAX_AGE_S = 15.0


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _json(value, depth=0):
    """Bound evidence without copying pixels or opaque application objects."""
    if depth > 10:
        return {"evidence_truncated": True, "reason": "depth_limit"}
    if value is None or isinstance(value, (str, bool)):
        return value if not isinstance(value, str) else value[:4096]
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, Mapping):
        items = list(value.items())
        result = {str(k)[:128]: _json(v, depth + 1) for k, v in items[:128]}
        if len(items) > 128:
            result["evidence_truncated"] = True
        return result
    if isinstance(value, (list, tuple)):
        out = [_json(v, depth + 1) for v in value[:256]]
        if len(value) > 256:
            out.append({"evidence_truncated": True})
        return out
    return {"unserializable_type": type(value).__name__}


def _valid_int(value):
    return isinstance(value, Integral) and not isinstance(value, bool)


def _box_ok(box):
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return False
    if not all(isinstance(x, Real) and not isinstance(x, bool)
               and math.isfinite(float(x)) for x in box):
        return False
    x0, y0, x1, y1 = box
    return 0 <= x0 < x1 <= 1600 and 0 <= y0 < y1 <= 900


def hand_observation_evidence(analysis, *, frame_path, frame_sha256, recovered=False):
    """Describe the raw analyze result, never a ledger-completed HandState."""
    result = analysis if isinstance(analysis, Mapping) else {}
    ids = result.get("hand_ids")
    melds = result.get("melds")
    diagnostics = result.get("vision_diagnostics")
    blockers = []
    if not result or result.get("status") not in ("wait", "advise", "hu"):
        blockers.append("raw_analysis_not_complete")
    if not isinstance(ids, (list, tuple)) or not ids or not all(_valid_int(t) for t in ids):
        blockers.append("raw_hand_ids_missing_or_invalid")
        ids = []
    if (not isinstance(melds, (list, tuple))
            or any(not isinstance(g, (list, tuple)) or len(g) not in (3, 4)
                   or not all(_valid_int(t) for t in g) for g in melds)):
        blockers.append("raw_melds_missing_or_invalid")
        melds = []
    expected = 16 - 3 * len(melds)
    if len(ids) not in (expected, expected + 1):
        blockers.append("raw_hand_count_incomplete")
    if not isinstance(diagnostics, (list, tuple)) or not diagnostics:
        blockers.append("raw_diagnostics_missing")
        diagnostics = []
    if diagnostics and (any(not isinstance(d, Mapping) for d in diagnostics)
                        or any(d.get("accepted") is not True
                               or not _box_ok(d.get("box")) for d in diagnostics)):
        blockers.append("raw_diagnostics_rejected_or_invalid")
    accepted_ids = [d.get("tile_id") for d in diagnostics
                    if isinstance(d, Mapping) and d.get("accepted") is True]
    if accepted_ids != list(ids) or len(diagnostics) != len(ids):
        blockers.append("raw_diagnostic_coverage_mismatch")
    if result.get("mode") != "tea_2p":
        blockers.append("raw_mode_not_tea2")
    if not result.get("caishen") or result.get("caishen_source") != "image":
        blockers.append("raw_caishen_not_current_image")
    source_image = result.get("image")
    if source_image is None or str(source_image) != str(frame_path):
        blockers.append("raw_analysis_frame_path_mismatch")
    if recovered:
        blockers.append("ledger_hand_is_not_same_frame")
    # Only validate reported shape/codes. This cannot prove missed physical boxes.
    if not blockers:
        try:
            from tea_bot import vision_to_engine
            from brain_v0.engine.tea.shanten import validate_tiles
            from tile_vision import TILE_IDS
            validate_tiles([vision_to_engine(int(t)) for t in ids],
                           caishen_id=vision_to_engine(TILE_IDS[result["caishen"]]),
                           melds=[[vision_to_engine(int(t)) for t in g] for g in melds])
        except Exception as exc:
            blockers.append("reported_hand_engine_rejected:" + type(exc).__name__)
    evidence = {
        "source": "current_raw_analyze_return_before_ledger_recovery",
        "frame_path": str(frame_path), "frame_sha256": frame_sha256,
        "status": result.get("status"), "mode": result.get("mode"),
        "hand_ids": _json(ids), "melds": _json(melds),
        "vision_diagnostics": _json(diagnostics),
        "caishen": result.get("caishen"), "caishen_source": result.get("caishen_source"),
        "caishen_score": _json(result.get("caishen_score")),
        "reported_count": len(ids), "expected_wait_count": expected,
        "reported_accepted_count": len(accepted_ids),
        "ledger_recovery_applied": bool(recovered),
        "complete_observation_data": not blockers,
        "blockers": sorted(set(blockers)),
    }
    trusted = {
        "frame_sha256": frame_sha256,
        "concealed_complete": None,
        "no_rejected_boxes": (
            all(isinstance(d, Mapping) and d.get("accepted") is True for d in diagnostics)
            if diagnostics else None),
        "caishen": None, "melds": None, "mode": None, "settled_frame": None,
        "source": "strict_trust_not_implemented_by_this_producer",
    }
    return evidence, trusted, blockers


class IncomingShadowObserver:
    """One bounded run-local observation chain; never use registry baselines."""
    def __init__(self, *, serial, session_id, run_uuid=None,
                 monotonic_ns=time.monotonic_ns, wall_ns=time.time_ns,
                 max_captures=MAX_CAPTURES, max_events=MAX_EVENTS):
        self.serial = str(serial)
        self.session_id = str(session_id)
        self.run_uuid = str(run_uuid or uuid.uuid4())
        self._mono = monotonic_ns
        self._wall = wall_ns
        self.capture_seq = 0
        self.event_seq = 0
        self.chain_generation = 0
        self.captures = deque(maxlen=max(2, min(int(max_captures), MAX_CAPTURES)))
        self.events = deque(maxlen=max(4, min(int(max_events), MAX_EVENTS)))
        self.observations = deque(maxlen=MAX_SEQUENCE)
        self._baseline = None
        self._cycle = None
        self._last_scope = None
        self._last_end = None
        self.fault = None
        self.last_claim_report = None

    def call(self, method, *args, **kwargs):
        """Every production hook is isolated from gameplay exceptions."""
        if self.fault is not None and method not in ("summary", "end_cycle"):
            return None
        try:
            return getattr(self, method)(*args, **kwargs)
        except Exception as exc:
            self.fault = "observer_error:" + type(exc).__name__
            self._baseline = None
            if self._cycle is not None:
                self._cycle["breaks"].append(self.fault)
            self._event("observer_error", reason=self.fault)
            return None

    def _event(self, kind, **evidence):
        self.event_seq += 1
        row = {"sequence": self.event_seq, "kind": kind,
               "capture_seq": self.capture_seq, "chain_generation": self.chain_generation,
               **_json(evidence)}
        self.events.append(row)
        return row

    def break_chain(self, reason, **evidence):
        self.chain_generation += 1
        self._baseline = None
        self.observations.clear()
        if self._cycle is not None:
            self._cycle["breaks"].append(str(reason))
        self._event("continuity_break", reason=str(reason), **evidence)

    def begin_cycle(self, *, serial=None, session_id=None):
        if self._cycle is not None:
            self.break_chain("previous_cycle_unfinished")
        self._cycle = {"captures": 0, "primary": None, "image": None, "frame_path": None,
                       "scene": None, "analysis": None, "analysis_source": None,
                       "runtime_scope": None, "snapshot": None, "frame_sources": None,
                       "runtime_event": None, "breaks": [], "ledger_recovered": False}
        observed_serial = self.serial if serial is None else str(serial)
        observed_session = self.session_id if session_id is None else str(session_id)
        if (observed_serial, observed_session) != (self.serial, self.session_id):
            self.break_chain("capture_scope_changed",
                             before={"serial": self.serial, "session_id": self.session_id},
                             after={"serial": observed_serial, "session_id": observed_session})
            self.serial, self.session_id = observed_serial, observed_session
            self._last_scope = None
        self._event("cycle_begin")

    def foreground(self, package=None, error=None):
        from harvest_tea import EXPECTED_PACKAGE
        self._event("foreground_observed", package=package, error=error)
        if error is not None or package != EXPECTED_PACKAGE:
            self.break_chain("foreground_error_or_mismatch", package=package, error=error)

    def capture_started(self):
        self.capture_seq += 1
        cycle = self._cycle
        primary = bool(cycle is not None and cycle["captures"] == 0)
        if cycle is not None:
            cycle["captures"] += 1
        if not primary:
            self.break_chain("internal_or_outside_tick_capture")
        row = {
            "observation_id": f"{self.run_uuid}:capture:{self.capture_seq}",
            "event_id": f"{self.run_uuid}:capture:{self.capture_seq}",
            "capture_seq": self.capture_seq, "serial": self.serial,
            "session_id": self.session_id, "run_uuid": self.run_uuid,
            "host_request_started_monotonic_ns": self._mono(),
            "host_request_started_wall_ns": self._wall(),
            "host_provider_returned_monotonic_ns": None,
            "host_provider_returned_wall_ns": None,
            "host_byte_received_monotonic_ns": None,
            "host_byte_received_wall_ns": None,
            "capture_clock": "host_monotonic_ns",
            "provider_boundary": "AutoPlayLoop._shot_enter_and_return",
            "device_capture_time": None,
            "png_sha256": None, "pixel_sha256": None,
            "frame_sha256": None, "frame_hash_domain": "contiguous_BGR_uint8_pixels",
            "capture_role": "tick_primary" if primary else "internal_or_unscoped",
        }
        self.captures.append(row)
        if primary:
            cycle["primary"] = row
        self._event("capture_requested", observation_id=row["observation_id"])
        return row

    def byte_received(self, raw=None):
        """Host callback-observed byte-return anchor for built-in _run_adb only."""
        if not self.captures or self.captures[-1]["host_provider_returned_monotonic_ns"] is not None:
            self.break_chain("byte_receive_without_active_capture")
            return
        row = self.captures[-1]
        # Observe the callback boundary before provenance hashing adds latency.
        row["host_byte_received_monotonic_ns"] = self._mono()
        row["host_byte_received_wall_ns"] = self._wall()
        if isinstance(raw, bytes):
            row["png_sha256"], row["png_size_bytes"] = _sha(raw), len(raw)
        row["byte_receive_boundary"] = "AutoPlayLoop._run_adb_return_before_decode"

    def capture_returned(self, token, raw=None, error=None):
        if token is None:
            return
        token["host_provider_returned_monotonic_ns"] = self._mono()
        token["host_provider_returned_wall_ns"] = self._wall()
        if error is not None:
            token["capture_error"] = str(error)
            self.break_chain("capture_error", error=str(error))
            return
        if not isinstance(raw, bytes):
            self.break_chain("capture_not_raw_bytes")
            return
        token["png_sha256"] = _sha(raw)
        token["png_size_bytes"] = len(raw)
        self._event("capture_returned", observation_id=token["observation_id"],
                    png_sha256=token["png_sha256"])

    def scene(self, image, ledger, frame_path):
        import numpy as np
        cycle = self._cycle
        if cycle is None or cycle["primary"] is None:
            self.break_chain("scene_without_primary_capture")
            return
        if (not isinstance(image, np.ndarray) or image.shape != (900, 1600, 3)
                or image.dtype != np.uint8):
            self.break_chain("invalid_decoded_frame")
            return
        primary = cycle["primary"]
        primary["pixel_sha256"] = _sha(np.ascontiguousarray(image).tobytes())
        primary["frame_sha256"] = primary["pixel_sha256"]
        primary["pixel_shape"] = list(image.shape)
        primary["pixel_dtype"] = str(image.dtype)
        cycle["image"], cycle["frame_path"] = image, str(frame_path)
        cycle["scene"] = _json(ledger)
        self._event("scene_observed", scene=cycle["scene"],
                    observation_id=primary["observation_id"])
        if not isinstance(ledger, Mapping) or ledger.get("kind") != "in_game":
            self.break_chain("non_game_or_unknown_scene", scene=ledger)

    def runtime_scope(self, *, round_seq, hud_key, caishen, discards_trusted):
        scope = {"runtime_round_seq": _json(round_seq),
                 "runtime_hud_fingerprint_sha256": _sha(hud_key) if isinstance(hud_key, bytes) else None,
                 "runtime_caishen": caishen,
                 "runtime_discards_trusted": bool(discards_trusted),
                 "not_a_verified_room_identity": True}
        if self._last_scope is not None and scope != self._last_scope:
            self.break_chain("runtime_scope_changed", before=self._last_scope, after=scope)
        self._last_scope = scope
        if self._cycle is not None:
            self._cycle["runtime_scope"] = scope

    def analysis_returned(self, raw, frame_path, result=None, error=None):
        cycle = self._cycle
        if error is not None:
            self.break_chain("analysis_error", error=str(error))
            return
        if cycle is None or cycle["primary"] is None:
            self.break_chain("analysis_without_capture")
            return
        if (not isinstance(raw, bytes) or _sha(raw) != cycle["primary"].get("png_sha256")
                or str(frame_path) != cycle["frame_path"]):
            self.break_chain("analysis_not_primary_frame")
            return
        if not isinstance(result, Mapping):
            self.break_chain("analysis_not_mapping")
            return
        keys = ("image", "status", "mode", "hand_ids", "melds", "meld_count",
                "stacked_kongs", "vision_diagnostics", "caishen", "caishen_source",
                "caishen_score", "reason")
        cycle["analysis"] = _json({k: result.get(k) for k in keys})
        cycle["analysis_source"] = {
            "png_sha256": cycle["primary"]["png_sha256"],
            "frame_sha256": cycle["primary"].get("pixel_sha256"),
            "frame_path": str(frame_path), "source": "raw_analyze_return",
        }
        cycle["snapshot"] = None

    def transaction(self, method):
        self.break_chain("execution_or_internal_verification_transaction", method=method,
                         coverage="TapExecutor internal captures are not observed by loop._shot")

    def _snapshot(self, phase, recovered=False):
        cycle = self._cycle
        if cycle is None or cycle["primary"] is None:
            return None, ["primary_capture_missing"]
        primary = cycle["primary"]
        blockers = []
        if (cycle["image"] is None or not primary.get("png_sha256")
                or not primary.get("pixel_sha256")):
            return None, ["capture_or_decoded_frame_missing"]
        if cycle["scene"] is None or cycle["scene"].get("kind") != "in_game":
            blockers.append("scene_not_in_game")
        if cycle["analysis"] is None:
            blockers.append("raw_analysis_missing")
        if cycle["runtime_scope"] is None:
            blockers.append("runtime_scope_missing")
        hand, trusted, hand_blockers = hand_observation_evidence(
            cycle["analysis"], frame_path=cycle["frame_path"],
            frame_sha256=primary["pixel_sha256"], recovered=recovered)
        blockers.extend(hand_blockers)
        if cycle["frame_sources"] is None:
            from discard_vision import snapshot_table
            from room_round_vision import read_room_round, _load_templates, DIGIT_SCORE_MIN
            # No previous/registry argument: read these actual pixels once per
            # primary observation, including rejected slots in both rivers.
            table = snapshot_table(cycle["image"]).as_dict()
            room = read_room_round(cycle["image"]).as_dict()
            labels = sorted(_load_templates())
            missing = sorted(set("0123456789") - set(labels))
            hud = {"source": "room_round_vision.read_room_round", "raw": room,
                   "template_digits": labels, "missing_template_digits": missing,
                   "score_min_existing": DIGIT_SCORE_MIN, "candidate_margin_available": False,
                   "trusted_room_identity": None,
                   "blockers": ["hud_round_identity_unverified_top1_only"]
                               + (["hud_digit_templates_incomplete"] if missing else [])}
            cycle["frame_sources"] = (table, hud)
        table, hud = cycle["frame_sources"]
        if not table["opponent"].get("valid") or not table["own"].get("valid"):
            blockers.append("river_geometry_untrusted")
        row = {
            **copy.deepcopy(primary), "sequence": primary["capture_seq"],
            "captured_at": primary["host_provider_returned_monotonic_ns"] / 1e9,
            "captured_at_semantics": "host_provider_return_monotonic_seconds",
            "player_count": table.get("player_count"),
            "round_epoch": None, "observer_chain_generation": self.chain_generation,
            "window_lineage": None, "scene": phase,
            "reset": None, "reconnect": None,
            "reset_evidence": "no independent reset-absence producer",
            "reconnect_evidence": "no independent transport/reconnect producer",
            "opponent": table["opponent"], "own": table["own"],
            "sparkles": table.get("sparkles", []),
            "river_source": "independent_current_frame_snapshot_table",
            "hand_evidence": hand, "trusted_hand": trusted,
            "hud_evidence": hud, "runtime_scope": cycle["runtime_scope"],
            "source": cycle["analysis_source"],
            "observation_complete": not blockers,
            "observation_blockers": sorted(set(blockers)),
        }
        cycle["snapshot"] = row
        return row, blockers

    def prepare_claim(self, *, hand_recovery=None):
        cycle = self._cycle
        recovered = bool(isinstance(hand_recovery, Mapping) and hand_recovery.get("applied") is True)
        if cycle is not None:
            cycle["ledger_recovered"] = recovered
        current, blockers = self._snapshot("claim", recovered=recovered)
        baseline = copy.deepcopy(self._baseline)
        blockers = list(blockers)
        if self.fault:
            blockers.append(self.fault)
        if cycle is not None:
            blockers.extend(cycle["breaks"])
        if current is None:
            report = {"schema_version": SCHEMA_VERSION, "context": None,
                      "producer_blockers": sorted(set(blockers)),
                      "strict_four_gates": False, "execution_eligible": False,
                      "source_coverage": "no primary capture; evidence only"}
            self.last_claim_report = report
            return copy.deepcopy(report)
        if not current["observation_complete"]:
            self.break_chain("claim_observation_incomplete", blockers=blockers)
            baseline = None
        if baseline is None:
            blockers.append("independent_baseline_missing")
        events = []
        if baseline is not None:
            if current["capture_seq"] <= baseline["capture_seq"]:
                blockers.append("capture_sequence_not_forward")
            age = current["captured_at"] - baseline["captured_at"]
            if not 0 < age <= BASELINE_MAX_AGE_S:
                blockers.append("baseline_host_age_invalid_or_expired")
            selected = [o for o in self.observations
                        if o["capture_seq"] >= baseline["capture_seq"]]
            selected = selected + [current]
            seqs = [o["capture_seq"] for o in selected]
            if (not seqs or seqs[0] != baseline["capture_seq"]
                    or len(seqs) != len(set(seqs))
                    or any(b != a + 1 for a, b in zip(seqs, seqs[1:]))):
                blockers.append("independent_capture_gap")
            lineage = "observed-window:" + baseline["observation_id"]
            baseline["window_lineage"] = lineage
            current["window_lineage"] = lineage
            for o in selected:
                events.append({k: o.get(k) for k in (
                    "serial", "session_id", "run_uuid", "event_id", "sequence",
                    "capture_seq", "scene", "reset", "reconnect", "round_epoch",
                    "png_sha256", "pixel_sha256")})
                events[-1]["window_lineage"] = lineage
            if not baseline["opponent"].get("tiles"):
                blockers.append("first_river_tile_baseline_unsupported")
            for snapshot in (baseline, current):
                tiles = snapshot["opponent"].get("tiles", [])
                if any(not _valid_int(t.get("index")) or not 0 <= t["index"] < 16 for t in tiles):
                    blockers.append("row_beyond_zero_unsupported")
        blockers.extend(("reconnect_absence_unproven", "reset_absence_unproven",
                         "hud_round_identity_unverified", "physical_hand_stability_unproven"))
        context = {"schema_version": SCHEMA_VERSION, "producer": "IncomingShadowObserver",
                   "serial": self.serial, "session_id": self.session_id, "run_uuid": self.run_uuid,
                   "baseline": baseline, "current": current, "events": events,
                   "producer_blockers": sorted(set(blockers)),
                   "strict_four_gates": False, "execution_eligible": False}
        report = {"schema_version": SCHEMA_VERSION, "context": context,
                  "producer_blockers": context["producer_blockers"],
                  "strict_four_gates": False, "execution_eligible": False,
                  "source_coverage": "loop._shot primary frames; executor transactions break chain",
                  "raw_hand_recovery": _json(hand_recovery),
                  "recent_boundaries": list(self.events)[-12:]}
        self.last_claim_report = copy.deepcopy(report)
        return report

    def runtime_event(self, event):
        observed = _json({
            k: event.get(k) for k in ("event", "status", "gate", "error", "round_signal",
                                      "guard", "turn_gate", "verified", "reason")})
        claim = event.get("claim") or {}
        observed["claim_summary"] = _json({
            k: claim.get(k) for k in ("status", "action", "reason")
        }) if isinstance(claim, Mapping) else None
        if self._cycle is not None:
            self._cycle["runtime_event"] = observed
        kind = event.get("event")
        signal = event.get("round_signal") or {}
        turn = event.get("turn_gate") or {}
        if (kind in ("stop", "error", "round_reset", "left_table", "round_transition")
                or (isinstance(signal, Mapping) and signal.get("state") not in (None, "same"))
                or event.get("guard") == "uncertain"
                or (isinstance(turn, Mapping)
                    and (turn.get("state") == "blocked" or turn.get("code") == "hint_panel"))
                or (isinstance(claim, Mapping)
                    and (claim.get("status") == "read_failed"
                         or claim.get("action") == "cancel_selection"))):
            self.break_chain("runtime_control_boundary", event=observed)
        self._event("runtime_event", event=observed)
        return self.summary()

    def end_cycle(self, result=None, error=None):
        cycle = self._cycle
        if cycle is None:
            return
        try:
            if error is not None:
                self.break_chain("tick_exception", error=str(error))
            event = result if isinstance(result, Mapping) else {}
            phase = "claim" if isinstance(event.get("claim"), Mapping) else "in_play"
            snapshot, blockers = self._snapshot(phase, recovered=cycle["ledger_recovered"])
            ordinary_wait = (event.get("event") == "idle" and phase == "in_play"
                             and event.get("status") == "wait")
            ordinary_claim = (event.get("event") == "idle" and phase == "claim"
                              and event.get("guard") == "action_visible")
            if (self.fault or snapshot is None or blockers or cycle["breaks"]
                    or not (ordinary_wait or ordinary_claim)):
                self.break_chain("early_return_or_incomplete_observation",
                                 blockers=blockers, returned_event=event.get("event"))
            else:
                self.observations.append(copy.deepcopy(snapshot))
                if phase == "in_play":
                    self._baseline = copy.deepcopy(snapshot)
                self._event("complete_observation", observation_id=snapshot["observation_id"],
                            scene=phase, strict_trust=False)
            self._last_end = {"observation_id": (cycle["primary"] or {}).get("observation_id"),
                              "complete": bool(snapshot and not blockers and not cycle["breaks"]),
                              "breaks": sorted(set(cycle["breaks"])),
                              "observation_blockers": blockers}
        finally:
            # The only image reference is released at every exit.
            self._cycle = None

    def summary(self):
        cycle = self._cycle
        primary = (cycle or {}).get("primary") or {}
        return {
            "schema_version": SCHEMA_VERSION, "serial": self.serial,
            "session_id": self.session_id, "run_uuid": self.run_uuid,
            "capture_seq": self.capture_seq, "observation_id": primary.get("observation_id"),
            "png_sha256": primary.get("png_sha256"), "pixel_sha256": primary.get("pixel_sha256"),
            "capture_span": {k: primary.get(k) for k in (
                "host_request_started_monotonic_ns", "host_provider_returned_monotonic_ns",
                "host_request_started_wall_ns", "host_provider_returned_wall_ns",
                "host_byte_received_monotonic_ns", "host_byte_received_wall_ns")},
            "chain_generation": self.chain_generation, "fault": self.fault,
            "baseline_observation_id": (self._baseline or {}).get("observation_id"),
            "cycle_breaks": sorted(set((cycle or {}).get("breaks", []))),
            "last_completed_cycle": copy.deepcopy(self._last_end),
            "retained_capture_count": len(self.captures), "retained_event_count": len(self.events),
            "strict_four_gates": False, "execution_eligible": False,
        }


class _ObservedTapExecutor:
    """Transparent delegation: record transaction entry, pass every result through."""
    def __init__(self, executor, observer):
        object.__setattr__(self, "_delegate", executor)
        object.__setattr__(self, "_observer", observer)

    def __setattr__(self, name, value):
        setattr(self._delegate, name, value)

    def __getattr__(self, name):
        value = getattr(self._delegate, name)
        if not callable(value) or not (name.startswith("execute") or name == "_tap"):
            return value
        def observed(*args, **kwargs):
            self._observer.call("transaction", name)
            return value(*args, **kwargs)
        return observed


class ShadowObserverBinding:
    """Installed only while the explicit shadow flag is enabled."""
    def __init__(self, loop, observer):
        self.original_shot = loop._shot
        self.original_analyze = loop._analyze
        self.original_foreground = loop._foreground
        self.original_tap = loop._tap

        def shot(*args, **kwargs):
            token = observer.call("capture_started")
            try:
                raw = self.original_shot(*args, **kwargs)
            except Exception as exc:
                observer.call("capture_returned", token, error=type(exc).__name__)
                raise
            observer.call("capture_returned", token, raw)
            return raw

        def analyze(raw, path, *args, **kwargs):
            try:
                result = self.original_analyze(raw, path, *args, **kwargs)
            except Exception as exc:
                observer.call("analysis_returned", raw, path, error=type(exc).__name__)
                raise
            observer.call("analysis_returned", raw, path, result)
            return result

        def foreground(*args, **kwargs):
            try:
                value = self.original_foreground(*args, **kwargs)
            except Exception as exc:
                observer.call("foreground", error=type(exc).__name__)
                raise
            observer.call("foreground", value)
            return value

        self.shot, self.analyze, self.foreground = shot, analyze, foreground
        self.tap = _ObservedTapExecutor(self.original_tap, observer)
        loop._shot, loop._analyze, loop._foreground, loop._tap = shot, analyze, foreground, self.tap

    def detach(self, loop):
        for name, wrapper, original in (
                ("_shot", self.shot, self.original_shot),
                ("_analyze", self.analyze, self.original_analyze),
                ("_foreground", self.foreground, self.original_foreground),
                ("_tap", self.tap, self.original_tap)):
            if getattr(loop, name) is wrapper:
                setattr(loop, name, original)
