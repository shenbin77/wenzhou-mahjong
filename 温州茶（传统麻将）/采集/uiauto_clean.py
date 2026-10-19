# -*- coding: utf-8 -*-
"""UIAutomator helper - clean version without Chinese chars in code"""
import subprocess
import xml.etree.ElementTree as ET
import re
import sys
import time

ADB = r"C:\leidian\LDPlayer14\adb.exe"
DEVICE = "emulator-5554"

def run(cmd, timeout=30):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.stderr, r.returncode

def dump_xml():
    """Dump UI hierarchy to XML string"""
    out, err, code = run([ADB, "-s", DEVICE, "shell", "uiautomator", "dump", "/sdcard/ui.xml"])
    if code != 0:
        return None, f"dump failed: {err}"
    out, err, code = run([ADB, "-s", DEVICE, "shell", "cat", "/sdcard/ui.xml"])
    if code != 0:
        return None, f"read failed: {err}"
    return out, None

def find_nodes(xml_str, text=None, resid=None, desc=None):
    """Find nodes matching criteria, return list of (bounds, text, resid, desc)"""
    try:
        root = ET.fromstring(xml_str)
    except Exception as e:
        return [], f"parse error: {e}"
    results = []
    for node in root.iter("node"):
        t = node.get("text", "")
        r = node.get("resource-id", "")
        d = node.get("content-desc", "")
        b = node.get("bounds", "")
        match = True
        if text and text not in t:
            match = False
        if resid and resid not in r:
            match = False
        if desc and desc not in d:
            match = False
        if match and (text or resid or desc):
            results.append({"bounds": b, "text": t, "resid": r, "desc": d,
                          "clickable": node.get("clickable"), "class": node.get("class")})
    return results, None

def bounds_center(bounds):
    """Parse [x1,y1][x2,y2] to center (x,y)"""
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2

def tap(x, y):
    out, err, code = run([ADB, "-s", DEVICE, "shell", "input", "tap", str(x), str(y)])
    return code == 0

def screenshot(path):
    out, err, code = run([ADB, "-s", DEVICE, "shell", "screencap", "-p", "/sdcard/shot.png"])
    if code != 0:
        return False
    out, err, code = run([ADB, "-s", DEVICE, "pull", "/sdcard/shot.png", path])
    return code == 0

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: dump | find <text> | tap <x> <y>")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "dump":
        xml, err = dump_xml()
        if err:
            print(f"ERROR: {err}")
        else:
            print(xml[:5000])
    elif cmd == "find" and len(sys.argv) > 2:
        xml, err = dump_xml()
        if err:
            print(f"ERROR: {err}")
        else:
            results, _ = find_nodes(xml, text=sys.argv[2])
            for r in results[:10]:
                c = bounds_center(r["bounds"])
                print(f"text='{r['text']}' center={c} clickable={r['clickable']}")
    elif cmd == "tap" and len(sys.argv) > 3:
        ok = tap(int(sys.argv[2]), int(sys.argv[3]))
        print("TAP_OK" if ok else "TAP_FAIL")
