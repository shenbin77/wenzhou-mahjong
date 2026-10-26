# -*- coding: utf-8 -*-
"""Cancel the '选择文件夹' dialog via BM_CLICK on its 取消 button; verify closed."""
import ctypes, time

user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
BM_CLICK = 0x00F5

def text_of(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value

dlg = []

def cb(hwnd, lparam):
    if text_of(hwnd) == "选择文件夹":
        dlg.append(hwnd)
    return True

user32.EnumWindows(EnumWindowsProc(cb), 0)
print("dialog found:", dlg)
if dlg:
    d = dlg[0]
    kids = []
    def ccb(child, lparam):
        kids.append((child, text_of(child)))
        return True
    user32.EnumChildWindows(d, EnumChildProc(ccb), 0)
    for h, t in kids:
        if t.strip():
            print("child", h, repr(t))
    for h, t in kids:
        if t.strip() == "取消":
            user32.PostMessageW(h, BM_CLICK, 0, 0)
            print("cancel clicked", h)
    time.sleep(1)
    print("dialog still visible:", bool(user32.IsWindowVisible(d)))
