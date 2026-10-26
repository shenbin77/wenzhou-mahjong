# -*- coding: utf-8 -*-
"""Pre-flight smoke: run the assist vision pipeline once on the live frame."""
import json, sys
sys.path.insert(0, r"D:\温州麻将\温州茶（传统麻将）\bot")
from assist_tea import analyze_snapshot

frame = r"D:\温州麻将\_preflight\live_now.png"
label = sys.argv[1] if len(sys.argv) > 1 else "t5"
try:
    r = analyze_snapshot(frame, caishen_label=label, confirmed_two_player=True)
    keep = {}
    for k, v in r.items():
        if isinstance(v, (str, int, float, bool, type(None))):
            keep[k] = v
        elif isinstance(v, list) and len(v) <= 20:
            keep[k] = v
        else:
            keep[k] = f"<{type(v).__name__} len={len(v) if hasattr(v, '__len__') else '?'}>"
    print(json.dumps(keep, ensure_ascii=False, indent=1)[:3000])
except Exception as e:
    print("ANALYZE-ERROR:", type(e).__name__, e)
