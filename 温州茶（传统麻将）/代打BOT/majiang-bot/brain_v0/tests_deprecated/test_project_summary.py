from tools.project_summary import (
    afa_probe_status,
    classifier_verification_status,
    competition_recognizer_probe_status,
    live_route_status,
)


def test_live_route_status_distinguishes_capture_from_quality():
    path, data = live_route_status()
    assert path is not None
    assert data is not None
    verification = data["verification"]
    assert verification["competition_unattended_capture_verified"] is True
    assert verification["competition_recording_to_isolated_db_exercised"] is True
    assert verification["competition_quality_gate_passed"] is False


def test_classifier_history_is_not_mistaken_for_a_gold_set():
    path, total, correct, accuracy = classifier_verification_status()
    assert path is not None
    assert total == 51
    assert correct == 1
    assert accuracy is not None
    assert accuracy < 0.95


def test_afa_probe_records_receive_success_but_keeps_blend_blocked():
    path, data = afa_probe_status()
    assert path is not None
    assert data is not None
    assert data["acceptance"]["endpoint_received_corrected_hand"] is True
    assert data["acceptance"]["response_parsed"] is True
    assert data["acceptance"]["safe_to_blend"] is False


def test_competition_recognizer_probe_exposes_template_blocker():
    path, data = competition_recognizer_probe_status()
    assert path is not None
    assert data is not None
    assert data["image_size"] == [1600, 900]
    assert data["recognized_count"] == 0
    assert data["template_probe"]["status"] == "FAIL"
    assert data["remote_opt_in_probe"]["accepted"] is True
