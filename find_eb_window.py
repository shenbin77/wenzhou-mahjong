# -*- coding: utf-8 -*-
"""Find all top-level windows of echobird.exe; print hwnd/rect/title/visibility.
If a large window exists, restore + foreground it."""
import ctypes, subprocess, sys

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

out = subprocess.run(["powershell", "-Command",
                      "(Get-Process echobird | Select-Object -First 1).Id"],
                     capture_output=True, text=True)
PID = int(out.stdout.strip())
print("echobird pid:", PID)

class RECT(ctypes.Structure):
    _fields_ = [("L", ctypes.c_long), ("T", ctypes.c_long),
                ("R", ctypes.c_long), ("B", ctypes.c_long)]

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
results = []

def cb(hwnd, lparam):
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == PID:
        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        results.append((hwnd, user32.IsWindowVisible(hwnd),
                        (r.L, r.T, r.R, r.B), buf.value))
    return True

user32.EnumWindows(EnumWindowsProc(cb), 0)
best = None
for hwnd, vis, rect, title in results:
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    print(f"hwnd={hwnd} vis={vis} rect={rect} size={w}x{h} title={title!r}")
    if vis and w > 800 and h > 500:
        best = (hwnd, rect)
if best:
    hwnd, rect = best
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    print("FOREGROUNDED", hwnd, rect)
else:
    print("no large visible window found")
