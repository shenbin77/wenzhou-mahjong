# -*- coding: utf-8 -*-
"""Find the '位置不可用' error dialog and BM_CLICK its OK button; report window inventory."""
import ctypes

user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

def text_of(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value

def class_of(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value

targets = []

def cb(hwnd, lparam):
    if user32.IsWindowVisible(hwnd):
        t = text_of(hwnd)
        c = class_of(hwnd)
        if "位置" in t or c == "#32770":
            print(f"top hwnd={hwnd} class={c!r} title={t!r}")
            targets.append(hwnd)
    return True

user32.EnumWindows(EnumWindowsProc(cb), 0)
print("candidates:", targets)

BM_CLICK = 0x00F5
for dlg in targets:
    found = []
    def ccb(child, lparam):
        t = text_of(child)
        if t.strip() in ("确定", "OK", "取消"):
            print(f"  child hwnd={child} class={class_of(child)!r} text={t!r}")
            found.append((child, t))
        return True
    user32.EnumChildWindows(dlg, EnumChildProc(ccb), 0)
    for child, t in found:
        if t.strip() in ("确定", "OK"):
            user32.PostMessageW(child, BM_CLICK, 0, 0)
            print("BM_CLICK sent to", child, t)
