import sys
from pathlib import Path
BOT = Path(r"D:\温州麻将\温州茶（传统麻将）\bot")
sys.path.insert(0, str(BOT))
from live_capture import analyze_bytes, SegmentClock
frame = BOT.parent / "采集" / "2026-10-16" / "740195" / "r08" / "frame_02.png"
raw = frame.read_bytes()
clock = SegmentClock()
res = analyze_bytes(raw, mode="tea_2p", image_path=frame, log_dir=BOT / "test_out" / "_probe_logs", clock=clock)
print("status", res.get("status"))
print("caishen", res.get("caishen"), res.get("caishen_source"))
print("hand len", len(res.get("hand") or []))
print("cands", len(res.get("candidates") or []))
print("suggestion", res.get("suggestion"))
print("reason", res.get("reason"))
t = clock.timing()
print(t.as_dict())
