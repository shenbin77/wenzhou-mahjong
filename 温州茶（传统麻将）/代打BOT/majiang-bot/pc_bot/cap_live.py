# -*- coding: utf-8 -*-
"""从实机抓 100 帧新鲜截图（当前皮肤），存 frames_live/"""
import subprocess, time
from pathlib import Path

ADB = r"D:\leidian\LDPlayer14\adb.exe"
DEV = "127.0.0.1:5557"
OUT = Path(r"D:\HanaAgent\majiang-bot\pc_bot\frames_live")
OUT.mkdir(exist_ok=True)

N = 100
for i in range(N):
    p = OUT / f"live_{i:03d}.png"
    r = subprocess.run([ADB, "-s", DEV, "exec-out", "screencap", "-p"],
                       capture_output=True, timeout=20)
    if r.stdout and len(r.stdout) > 10000:
        p.write_bytes(r.stdout)
    else:
        print(f"frame {i} 失败", flush=True)
    if (i + 1) % 10 == 0:
        print(f"{i+1}/{N}", flush=True)
    time.sleep(3)
print("done", flush=True)
