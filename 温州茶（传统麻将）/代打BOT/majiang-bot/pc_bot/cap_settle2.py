"""Fast settlement screenshot capturer (v2).

Two triggers:
  1. Fast poll: adb screencap every ~1.5s + btn_xiayiju template match.
  2. Log trigger: tail latest bot_*.log for "下一局" detection, capture ASAP.

Saves to logs/settle_cap_XX.png ; stops after --want captures or --timeout s.
"""
import subprocess
import sys
import time
import glob
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
        capture_output=True, timeout=15,
    )
    if not p.stdout:
        return None
    return cv2.imdecode(np.frombuffer(p.stdout, np.uint8), cv2.IMREAD_COLOR)


def has_xiayiju(img, tmpl):
    x1, y1, x2, y2 = 800, 400, 1400, 900
    roi = img[y1:y2, x1:x2]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    res = cv2.matchTemplate(gray, tmpl, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    # location gate: real button center must be bottom-right
    th, tw = tmpl.shape
    cx, cy = x1 + max_loc[0] + tw // 2, y1 + max_loc[1] + th // 2
    ok_loc = (1050 <= cx <= 1400) and (700 <= cy <= 900)
    return (max_val >= 0.70 and ok_loc), max_val


def latest_bot_log():
    logs = sorted(glob.glob(str(OUTDIR / "bot_*.log")))
    return logs[-1] if logs else None


def main():
    want = int(sys.argv[sys.argv.index("--want") + 1]) if "--want" in sys.argv else 3
    timeout = int(sys.argv[sys.argv.index("--timeout") + 1]) if "--timeout" in sys.argv else 900
    tmpl = cv2.imread(str(TMPL), cv2.IMREAD_GRAYSCALE)
    if tmpl is None:
        print("TEMPLATE_MISSING", flush=True)
        return 1
    OUTDIR.mkdir(exist_ok=True)

    log_path = latest_bot_log()
    log_pos = Path(log_path).stat().st_size if log_path else 0

    t0 = time.time()
    saved = 0
    last_save = 0.0
    print(f"watching log={log_path}", flush=True)

    def try_save(img, why, conf):
        nonlocal saved, last_save
        if time.time() - last_save < 15:
            return
        fp = OUTDIR / f"settle_cap_{int(time.time())}.png"
        cv2.imwrite(str(fp), img)
        print(f"SAVED {fp.name} why={why} conf={conf:.2f}", flush=True)
        saved += 1
        last_save = time.time()

    while saved < want and time.time() - t0 < timeout:
        # --- log trigger ---
        try:
            lp = latest_bot_log()
            if lp and lp != log_path:
                log_path, log_pos = lp, 0
            if log_path:
                st = Path(log_path).stat().st_size
                if st > log_pos:
                    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                        f.seek(log_pos)
                        chunk = f.read()
                    log_pos = st
                    if "下一局" in chunk:
                        img = screencap()
                        if img is not None:
                            hit, conf = has_xiayiju(img, tmpl)
                            if hit:
                                try_save(img, "log", conf)
                                continue
        except Exception as e:
            print("logtail err", e, flush=True)

        # --- fast poll ---
        try:
            img = screencap()
        except Exception:
            img = None
        if img is not None:
            hit, conf = has_xiayiju(img, tmpl)
            if hit:
                try_save(img, "poll", conf)
        time.sleep(1.2)

    print(f"DONE saved={saved}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
