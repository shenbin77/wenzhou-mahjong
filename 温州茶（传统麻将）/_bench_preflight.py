import time, sys, hashlib, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "bot"))
from assist_window import PREFLIGHT_FRAMES, PROJECT_ROOT, capture_result

tmpdir = Path(tempfile.mkdtemp(prefix="preflight_bench_"))
for mode in ("tea_2p", "tea_4p", "shu_4p"):
    rel, sha = PREFLIGHT_FRAMES[mode]
    path = PROJECT_ROOT / rel
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == sha, mode
    frame = tmpdir / ("%s.png" % mode)
    frame.write_bytes(raw)
    t0 = time.perf_counter()
    result, timing = capture_result(raw, mode=mode, image_path=frame, log_dir=tmpdir / "logs", folder=str(frame))
    dt = time.perf_counter() - t0
    print("%s: %.2fs status=%s recognize_ms=%s advise_ms=%s" % (
        mode, dt, result.get("status"),
        getattr(timing, "recognize_ms", None),
        getattr(timing, "advise_ms", None)))
print("tmpdir", tmpdir)
