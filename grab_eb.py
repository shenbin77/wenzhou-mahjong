# -*- coding: utf-8 -*-
"""Restore EchoBird main window, grab it with PIL ImageGrab, downscale, chunk base64."""
import ctypes, subprocess, time, os, base64

user32 = ctypes.windll.user32

out = subprocess.run(["powershell", "-Command",
                      "(Get-Process echobird | Select-Object -First 1).Id"],
                     capture_output=True, text=True)
PID = int(out.stdout.strip())

class RECT(ctypes.Structure):
    _fields_ = [("L", ctypes.c_long), ("T", ctypes.c_long),
                ("R", ctypes.c_long), ("B", ctypes.c_long)]

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
titled = []

def cb(hwnd, lparam):
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == PID:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value == "EchoBird":
            titled.append(hwnd)
    return True

user32.EnumWindows(EnumWindowsProc(cb), 0)
assert titled, "no titled window"
hwnd = titled[0]
user32.ShowWindow(hwnd, 9)       # SW_RESTORE
user32.SetForegroundWindow(hwnd)
time.sleep(2)

r = RECT()
user32.GetWindowRect(hwnd, ctypes.byref(r))
print("restored rect:", r.L, r.T, r.R, r.B)

from PIL import ImageGrab, Image
img = ImageGrab.grab(bbox=(r.L, r.T, r.R, r.B), all_screens=True)
print("grabbed:", img.size)
img.save(r"D:\温州麻将\_eb_win2.png")
small = img.resize((1050, int(1050 * img.height / img.width))).convert("RGB")
small.save(r"D:\温州麻将\_eb_view2.jpg", quality=50)
b = base64.b64encode(open(r"D:\温州麻将\_eb_view2.jpg", "rb").read()).decode()
os.makedirs(r"D:\温州麻将\_eb_chunks2", exist_ok=True)
n = (len(b) + 5999) // 6000
for i in range(n):
    open(rf"D:\温州麻将\_eb_chunks2\c{i:02d}.txt", "w").write(b[i * 6000:(i + 1) * 6000])
print("b64 len", len(b), "chunks", n)
