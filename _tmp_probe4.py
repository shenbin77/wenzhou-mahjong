import sys
from pathlib import Path
BOT = Path(r"D:\温州麻将\温州茶（传统麻将）\bot")
sys.path.insert(0, str(BOT))
import cv2, numpy as np
from live_capture import analyze_bytes, SegmentClock
frame = BOT.parent / "采集" / "2026-10-16" / "740195" / "r08" / "frame_02.png"
img = cv2.imdecode(np.frombuffer(frame.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
img[348:370, 622:652] = 0
img[100:220, 100:220] = 255
img[218] = 255
img[219] = 255
img[:] = img[:]
ok, buf = cv2.imencode(".png", img)
raw = buf.tobytes()
out = BOT / "test_out" / "_probe_nobadge.png"
out.write_bytes(raw)
clock = SegmentClock()
res = analyze_bytes(raw, mode="tea_2p", image_path=out, log_dir=BOT / "test_out" / "_probe_logs", clock=clock, caishen_label="s9", manual_caishen=True)
print("badge-forced status/caishen:", res.get("status"), res.get("caishen"), res.get("caishen_source"))
clock = SegmentClock()
res2 = analyze_bytes(raw, mode="tea_2p", image_path=out, log_dir=BOT / "test_out" / "_probe_logs", clock=clock)
print("no-hand status/caishen:", res2.get("status"), res2.get("caishen"), res2.get("reason"))
