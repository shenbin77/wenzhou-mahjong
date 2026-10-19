#!/usr/bin/env python3
"""
UIAutomator helper for 浙江游戏大厅 harvest.
Replaces template matching with Android UI element queries.
Works via ADB - no image comparison needed.
"""
import subprocess
import xml.etree.ElementTree as ET
import re
import time

ADB = r"D:\leidian\LDPlayer14\adb.exe"
SERIAL = "emulator-5554"

def adb(*args, timeout=30):
    """Run ADB command, return stdout."""
    cmd = [ADB, "-s", SERIAL] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception as e:
        return f"ADB_ERROR: {e}"

def dump_ui():
    """Dump current UI hierarchy via uiautomator."""
    # Dump to device
    adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
    # Pull to local temp
    import tempfile, os
    tmp = tempfile.mktemp(suffix=".xml")
    subprocess.run([ADB, "-s", SERIAL, "pull", "/sdcard/ui.xml", tmp],
                   capture_output=True, timeout=15)
    try:
        with open(tmp, 'r', encoding='utf-8') as f:
            return f.read()
    except:
        return ""
    finally:
        try: os.remove(tmp)
        except: pass

def _bounds_center(bounds):
    """Parse [x1,y1][x2,y2] -> (cx, cy)."""
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if m:
        x1, y1, x2, y2 = map(int, m.groups())
        return ((x1+x2)//2, (y1+y2)//2)
    return None

def find_by_text(xml_str, text, partial=True):
    """Find element by text, return (x, y) center coordinates."""
    try:
        root = ET.fromstring(xml_str)
    except:
        return None
    for node in root.iter("node"):
        t = node.get("text", "")
        if not t:
            continue
        match = (text in t) if partial else (t == text)
        if match:
            pos = _bounds_center(node.get("bounds", ""))
            if pos:
                return pos
    return None

def find_by_desc(xml_str, desc, partial=True):
    """Find element by content-desc."""
    try:
        root = ET.fromstring(xml_str)
    except:
        return None
    for node in root.iter("node"):
        d = node.get("content-desc", "")
        if not d:
            continue
        match = (desc in d) if partial else (d == desc)
        if match:
            pos = _bounds_center(node.get("bounds", ""))
            if pos:
                return pos
    return None

def find_by_resid(xml_str, resid, partial=True):
    """Find element by resource-id. Most stable - survives UI reskin."""
    try:
        root = ET.fromstring(xml_str)
    except:
        return None
    for node in root.iter("node"):
        r = node.get("resource-id", "")
        if not r:
            continue
        match = (resid in r) if partial else (r == resid)
        if match:
            pos = _bounds_center(node.get("bounds", ""))
            if pos:
                return pos
    return None

def find_best(xml_str, text=None, desc=None, resid=None,
              class_name=None, clickable=None, partial=True):
    """
    Combined finder - try multiple attributes.
    Returns (x, y) or None.
    Priority: resid > text > desc
    """
    # Try resource-id first (most stable)
    if resid:
        pos = find_by_resid(xml_str, resid, partial)
        if pos:
            return pos
    # Then text
    if text:
        pos = find_by_text(xml_str, text, partial)
        if pos:
            # Optional: verify class/clickable
            return pos
    # Then desc
    if desc:
        pos = find_by_desc(xml_str, desc, partial)
        if pos:
            return pos
    return None

def find_close_button(xml_str):
    """
    Smart close-button finder for popups.
    Tries: content-desc -> resource-id -> top-right corner fallback.
    """
    # 1. Try content-desc
    for kw in ["关闭", "Close", "close", "X"]:
        pos = find_by_desc(xml_str, kw, partial=True)
        if pos:
            return pos
    # 2. Try resource-id
    for kw in ["close", "cancel", "dismiss"]:
        pos = find_by_resid(xml_str, kw, partial=True)
        if pos:
            return pos
    # 3. Fallback: top-right clickable ImageView (typical X position)
    try:
        root = ET.fromstring(xml_str)
    except:
        return None
    candidates = []
    for node in root.iter("node"):
        if node.get("clickable") != "true":
            continue
        cls = node.get("class", "")
        if "Image" not in cls and "Button" not in cls:
            continue
        pos = _bounds_center(node.get("bounds", ""))
        if not pos:
            continue
        x, y = pos
        # Top-right quadrant, small size (typical for X)
        if x > 1200 and y < 300:
            candidates.append((x, y))
    if candidates:
        # Return the rightmost-topmost
        candidates.sort(key=lambda p: (-p[0], p[1]))
        return candidates[0]
    return None

def tap_text(text, partial=True, timeout_sec=10):
    """Find element by text and tap it. Returns True if tapped."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        xml = dump_ui()
        if not xml:
            time.sleep(1)
            continue
        pos = find_by_text(xml, text, partial)
        if pos:
            x, y = pos
            adb("shell", "input", "tap", str(x), str(y))
            return True
        time.sleep(1)
    return False

def tap_desc(desc, partial=True, timeout_sec=10):
    """Find element by content-desc and tap it."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        xml = dump_ui()
        if not xml:
            time.sleep(1)
            continue
        pos = find_by_desc(xml, desc, partial)
        if pos:
            x, y = pos
            adb("shell", "input", "tap", str(x), str(y))
            return True
        time.sleep(1)
    return False

def wait_text(text, partial=True, timeout_sec=15):
    """Wait for text to appear. Returns coordinates or None."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        xml = dump_ui()
        pos = find_by_text(xml, text, partial) if xml else None
        if pos:
            return pos
        time.sleep(1)
    return None

# --- Harvest-specific helpers ---

def goto_match_arena():
    """Navigate: Lobby �?比赛�?�?菁菁的比赛场 �?进入"""
    if not tap_text("比赛�?, timeout_sec=10):
        return False, "比赛�?not found"
    time.sleep(2)
    # 菁菁的比赛场 - tap 进入 button near it
    # Try tapping the 进入 button
    if not tap_text("进入", timeout_sec=10):
        return False, "进入 not found"
    time.sleep(3)
    return True, "OK"

def goto_match_records():
    """Tap 比赛战绩 (trophy)."""
    # Try by text first
    if tap_text("比赛战绩", timeout_sec=10):
        time.sleep(2)
        return True
    # Fallback: try by desc (trophy icon might not have text)
    return False

def close_popup():
    """Close any popup via 确定 or X."""
    xml = dump_ui()
    # Try 确定 first (bigger target)
    pos = find_by_text(xml, "确定", partial=False)
    if pos:
        adb("shell", "input", "tap", str(pos[0]), str(pos[1]))
        time.sleep(1)
        return True
    return False

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: uiauto_helper.py <command> [args]")
        print("  dump              - print UI XML")
        print("  tap <text>        - tap element by text")
        print("  wait <text>       - wait for text")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "dump":
        print(dump_ui()[:2000])
    elif cmd == "tap" and len(sys.argv) > 2:
        ok = tap_text(sys.argv[2])
        print("TAPPED" if ok else "NOT_FOUND")
    elif cmd == "wait" and len(sys.argv) > 2:
        pos = wait_text(sys.argv[2])
        print(f"FOUND {pos}" if pos else "TIMEOUT")
