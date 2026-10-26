from __future__ import annotations

from dataclasses import replace

from window_docking import (
    DockFollower,
    DockTarget,
    Rect,
    choose_dock_placement,
    serial_to_ldplayer_index,
)


class Host:
    def __init__(self, *, x=0, y=0, width=500, height=400, state="normal"):
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.window_state = state
        self.geometries = []
        self.iconified = 0
        self.deiconified = 0

    def state(self):
        return self.window_state

    def update_idletasks(self):
        pass

    def winfo_width(self):
        return self.width

    def winfo_height(self):
        return self.height

    def winfo_x(self):
        return self.x

    def winfo_y(self):
        return self.y

    def geometry(self, value):
        self.geometries.append(value)
        dims, x_text, y_text = value.replace("-", "+-").split("+")
        width, height = dims.split("x")
        self.width, self.height = int(width), int(height)
        self.x, self.y = int(x_text), int(y_text)

    def iconify(self):
        self.iconified += 1
        self.window_state = "iconic"

    def deiconify(self):
        self.deiconified += 1
        self.window_state = "normal"


def target(rect=Rect(100, 50, 900, 650), *, minimized=False,
           work=Rect(0, 0, 1920, 1080)):
    return DockTarget(123, rect, work, minimized)


def test_serial_maps_to_ldplayer_multi_instance_index():
    assert serial_to_ldplayer_index("emulator-5554") == 0
    assert serial_to_ldplayer_index("emulator-5556") == 1
    assert serial_to_ldplayer_index("emulator-5560") == 3
    assert serial_to_ldplayer_index("emulator-5555") is None
    assert serial_to_ldplayer_index("127.0.0.1:5555") is None


def test_prefers_right_when_full_window_fits():
    result = choose_dock_placement(
        Rect(100, 80, 900, 650), (600, 500), Rect(0, 0, 1920, 1080))
    assert result == (result.__class__)(908, 80, "right")


def test_falls_back_below_and_clamps_on_negative_monitor():
    result = choose_dock_placement(
        Rect(-1800, -50, -900, 500), (1000, 400),
        Rect(-1920, -120, 0, 960))
    assert result is not None
    assert result.side == "bottom"
    assert result.x == -1800
    assert result.y == 508


def test_no_space_never_overlaps_game_or_leaves_work_area():
    result = choose_dock_placement(
        Rect(200, 100, 1700, 1000), (1600, 1000), Rect(0, 0, 1920, 1080))
    assert result is None


def test_uses_above_when_right_below_and_left_do_not_fit():
    result = choose_dock_placement(
        Rect(2175, 875, 2878, 1287), (960, 460),
        Rect(1920, -193, 3120, 1687))
    assert result is not None
    assert result.side == "top"
    assert result.x == 2160
    assert result.y == 407


def test_follower_initially_docks_and_tracks_emulator_motion():
    current = target()
    host = Host(width=500, height=400)
    follower = DockFollower(host, "emulator-5554", target_reader=lambda _s: current)
    follower.set_enabled(True)
    assert follower.tick() == "docked_right"
    assert (host.x, host.y) == (908, 50)

    current = replace(current, rect=Rect(200, 100, 1000, 700))
    assert follower.tick() == "docked_right"
    assert (host.x, host.y) == (1008, 100)


def test_manual_drag_disables_follow_instead_of_snapping_back():
    detached = []
    host = Host(width=500, height=400)
    follower = DockFollower(
        host, "emulator-5554", target_reader=lambda _s: target(),
        on_manual_detach=lambda: detached.append(True))
    follower.set_enabled(True)
    follower.tick()
    host.x -= 80
    assert follower.tick() == "manual_detach"
    assert follower.enabled is False
    assert detached == [True]
    assert len(host.geometries) == 1


def test_emulator_minimize_and_restore_only_restores_auto_minimize():
    current = target(minimized=True)
    host = Host(width=500, height=400)
    follower = DockFollower(host, "emulator-5554", target_reader=lambda _s: current)
    follower.set_enabled(True)
    assert follower.tick() == "target_minimized"
    assert host.iconified == 1

    current = replace(current, minimized=False)
    assert follower.tick() == "docked_right"
    assert host.deiconified == 1


def test_user_minimized_assistant_is_not_forced_open():
    host = Host(width=500, height=400, state="iconic")
    follower = DockFollower(host, "emulator-5554", target_reader=lambda _s: target())
    follower.set_enabled(True)
    assert follower.tick() == "assistant_minimized"
    assert host.deiconified == 0


def test_withdrawn_main_window_preserves_tray_and_orb_semantics():
    host = Host(width=500, height=400, state="withdrawn")
    follower = DockFollower(host, "emulator-5554", target_reader=lambda _s: target())
    follower.set_enabled(True)
    assert follower.tick() == "assistant_withdrawn"
    assert host.geometries == []

