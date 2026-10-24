import sys, time, json
sys.path.insert(0, r"D:\温州麻将\温州茶（传统麻将）\bot")
from pathlib import Path
from assist_tea import capture_and_analyze
from assist_shu import capture_shu_and_analyze
sd = Path(r"D:\温州麻将\温州茶（传统麻将）\bot\test_out\spec005_probe")
rows=[]
for i in range(3):
    t=time.perf_counter()
    r = capture_and_analyze(serial="emulator-5556", session_dir=sd, caishen_label="", confirmed_four_player=True, write_sidecar=False)
    dt=time.perf_counter()-t
    rows.append((round(dt,3), r.get("status"), r.get("reason"), len(r.get("hand") or []), r.get("caishen")))
print(json.dumps(rows, ensure_ascii=False))
