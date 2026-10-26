# -*- coding: utf-8 -*-
"""Gold-frame verification: hash check + recognition vs frozen annotations."""
import hashlib, json, sys
from collections import Counter
sys.path.insert(0, r"D:\温州麻将\温州茶（传统麻将）\bot")
from assist_tea import analyze_snapshot

ROOT = r"D:\温州麻将"
man = json.load(open(ROOT + r"\温州茶（传统麻将）\bot\gold\tea_2p_manifest.json", encoding="utf-8"))
for fr in man["frames"]:
    rel = fr["frame_path"].replace("/", "\\")
    path = ROOT + "\\" + rel
    h = hashlib.sha256(open(path, "rb").read()).hexdigest()
    hash_ok = (h == fr["sha256"])
    ann = Counter(fr["annotated_hand_multiset"])
    total = sum(ann.values())
    try:
        r = analyze_snapshot(path, caishen_label="t5", confirmed_two_player=True)
        got = Counter(r.get("hand_ids") or [])
        hit = sum((ann & got).values())
        wrong = sum((got - ann).values())
        print(f"{rel.split(chr(92))[-2]}: hash_ok={hash_ok} status={r.get('status')} "
              f"annotated={total} recognized={sum(got.values())} hit={hit} wrong={wrong} "
              f"reason={r.get('reason')}")
    except Exception as e:
        print(f"{rel}: hash_ok={hash_ok} ERROR {type(e).__name__}: {e}")
