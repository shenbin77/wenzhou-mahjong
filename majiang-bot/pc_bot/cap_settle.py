"""Capture settlement screenshots from the live emulator.

Polls `adb exec-out screencap`, runs the existing btn_xiayiju.png template
match (same as bot.check_xiayiju), and saves the frame whenever the
settlement screen ('next round' button) is visible.

Usage: python cap_settle.py [--want 3] [--timeout 600]
Saves to: D:\\HanaAgent\\majiang-bot\\pc_bot\\logs\\settle_cap_XX.png
"""
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ADB = r"D:\leidian\LDPlayer14\adb.exe"
SERIAL = "127.0.0.1:5557"
BASE = Path(r"D:\HanaAgent\majiang-bot\pc_bot")
TMPL = BASE / "templates_new" / "btn_xiayiju.png"
OUTDIR = BASE / "logs"


def screencap():
    p = subprocess.run(
        [ADB, "-s", SERIAL, "exec-out", "screencap", "-p"],
        capture_output=True, timeout=20,
    )
    if not p.stdout:
        return None
    return cv2.imdecode(np.frombuffer(p.stdout, np.uint8), cv2.IMREAD_COLOR)


def has_xiayiju(img, tmpl):
    x1, y1, x2, y2 = 800, 400, 1400, 900
    roi = img[y1:y2, x1:x2]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    res = cv2.matchTemplate(gray, tmpl, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(res)
    return max_val >= 0.7, max_val


def main():
    want = int(sys.argv[sys.argv.index("--want") + 1]) if "--want" in sys.argv else 3
    timeout = int(sys.argv[sys.argv.index("--timeout") + 1]) if "--timeout" in sys.argv else 600
    tmpl = cv2.imread(str(TMPL), cv2.IMREAD_GRAYSCALE)
    if tmpl is None:
        print("TEMPLATE_MISSING", TMPL)
        return 1
    OUTDIR.mkdir(exist_ok=True)
    t0 = time.time()
    saved = 0
    last_save = 0.0
    while saved < want and time.time() - t0 < timeout:
        try:
            img = screencap()
        except Exception as e:
            print("cap err", e, flush=True)
            time.sleep(3)
            continue
        if img is None:
            time.sleep(3)
            continue
        hit, conf = has_xiayiju(img, tmpl)
        # debounce: only save once per settlement appearance
        if hit and time.time() - last_save > 20:
            fp = OUTDIR / f"settle_cap_{int(time.time())}.png"
            cv2.imwrite(str(fp), img)
            print(f"SAVED {fp.name} conf={conf:.2f}", flush=True)
            saved += 1
            last_save = time.time()
        time.sleep(4)
    print(f"DONE saved={saved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
