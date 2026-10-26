import sys
from pathlib import Path
BOT = Path(r"D:\温州麻将\温州茶（传统麻将）\bot")
sys.path.insert(0, str(BOT))
import cv2, numpy as np
from live_capture import analyze_bytes, SegmentClock
frame = BOT.parent / "采集" / "2026-10-16" / "740195" / "r08" / "frame_02.png"
img = cv2.imdecode(np.frombuffer(frame.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
img[348:370, 622:652] = 0
# erase hand row band: find hand boxes from a prior run? just blank lower 25%
img[600:900, :] = 0
ok, buf = cv2.imencode(".png", img)
raw = buf.tobytes()
out = BOT / "test_out" / "_probe_nobadge.png"
out.write_bytes(raw)
res = analyze_bytes(raw, mode="tea_2p", image_path=out, log_dir=BOT / "test_out" / "_probe_logs", clock=SegmentClock())
print("RESULT", res.get("status"), res.get("caishen"), res.get("reason"))
