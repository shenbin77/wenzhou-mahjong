from tools.replay_surface_guard import inspect_surface


def test_waiting_table_surface_is_rejected():
    result = inspect_surface("artifacts/post_back_20260913.png")
    assert result["classification"] == "waiting_table"
    assert result["orange_ratio"] > result["threshold"]


def test_replay_list_surface_is_not_waiting_table():
    result = inspect_surface("artifacts/post_overlay_close_20260913.png")
    assert result["classification"] == "not_replay"
    assert result["orange_ratio"] <= result["threshold"]


def test_active_replay_controls_are_positive_signal():
    result = inspect_surface(
        "artifacts/replay_runs/competition_playback_probe_20260913/t0.png"
    )
    assert result["classification"] == "playback_surface"
    assert result["has_replay_controls"] is True
