# -*- coding: utf-8 -*-
"""
harvest_tea.py — 温州茶回放采集（统一版）

整合了 Grok 的 PS1 脚本经验 + 模板匹配的鲁棒性。

为什么不用 uiautomator：
    游戏是 Cocos 引擎，UI 是画在 SurfaceView 上的像素，
    uiautomator 看不到游戏内按钮。必须用图片模板匹配。

链路：
    主大厅 --[比赛]--> 比赛场列表 --[进入]--> 场次页 --[战绩]-->
    比赛战绩页 --[选日期]--> 逐行[查看回放] --> 截图3张 --> BACK

红线（不可绕过）：
    1. 弹窗只关 X，绝不点领取/购买/充值
    2. 回放页只截图，不点"下一局"/"再来一局"
    3. 不进雷电游戏中心
    4. 房卡为0立即停

用法：
    python harvest_tea.py --date 2026-10-16          # 默认只看不点
    python harvest_tea.py --date 2026-10-16 --execute # 授权后才启用点击
    python harvest_tea.py --from-zhanji               # 已在战绩页，直接开采
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime

try:
    import cv2
    import numpy as np
except ImportError:
    print("FATAL: pip install opencv-python numpy")
    sys.exit(2)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
DEFAULT_ADB = r"C:\leidian\LDPlayer14\adb.exe"
DEFAULT_SERIAL = "emulator-5556"
# 模板目录：脚本同目录下的 templates\（整合后位置）
DEFAULT_TPL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

TEMPLATES = {
    "btn_bisai":          ("btn_bisai.png",          0.90, "比赛按钮"),
    "btn_enter":          ("btn_enter.png",          0.90, "进入按钮"),
    "btn_zhanji":         ("btn_zhanji.png",         0.90, "战绩按钮"),
    "btn_chakan_huifang": ("btn_chakan_huifang.png", 0.90, "查看回放"),
    "btn_close_x":        ("btn_close_x.png",        0.85, "弹窗X"),
    "btn_date":           ("btn_date.png",           0.85, "日期下拉框"),
    "btn_refresh":        ("btn_refresh.png",        0.85, "刷新按钮"),
    "btn_date_1016_item": ("btn_date_1016_item.png", 0.80, "下拉列表中的2026-10-16"),
}
# Some templates deliberately include neighboring text for a stable match.
# The replay-entry template's geometric center is OUTSIDE the turquoise button.
# These anchors are relative to the matched image, never absolute screen coords.
TAP_ANCHORS = {"btn_chakan_huifang": (.80, .75)}

WAIT_AFTER_TAP = 2.0
SHOTS_PER_REPLAY = 3
SHOT_INTERVAL = 2.0
MAX_SCROLL_PAGES = 5  # 最多翻页数，防止无限翻

ADB_PATH = DEFAULT_ADB
SERIAL = DEFAULT_SERIAL
TPL_DIR = DEFAULT_TPL_DIR
DRY_RUN = True
LOG_FH = None
_tpl_cache = {}

# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    if LOG_FH:
        LOG_FH.write(line + "\n")
        LOG_FH.flush()

def adb(*args, timeout=30):
    cmd = [ADB_PATH, "-s", SERIAL] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, b"", b"timeout"
    except FileNotFoundError:
        log("FATAL: ADB 不存在: %s" % ADB_PATH)
        return -1, b"", b"no adb"

def grab_gray():
    rc, out, _ = adb("exec-out", "screencap", "-p", timeout=30)
    if rc != 0 or not out:
        return None
    img = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

def save_screenshot(path):
    rc, out, _ = adb("exec-out", "screencap", "-p", timeout=30)
    if rc != 0 or not out:
        return False
    img = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return False
    ok, encoded = cv2.imencode(".png", img)
    if not ok:
        return False
    # Python's open handles Unicode paths and xb preserves earlier captures.
    with open(path, "xb") as fh:
        fh.write(encoded.tobytes())
    return True

def tap(x, y, frame_shape):
    """Click only a point from an ADB screenshot with matching native bounds."""
    h, w = frame_shape[:2]
    x, y = int(x), int(y)
    if not (0 <= x < w and 0 <= y < h):
        raise ValueError("点击坐标超出截图范围: (%d,%d) / %dx%d" % (x, y, w, h))
    if DRY_RUN:
        log("[DRY-RUN] tap(%d,%d)" % (x, y))
        return
    rc, _, err = adb("shell", "input", "tap", str(x), str(y), timeout=10)
    if rc != 0:
        raise RuntimeError("ADB 点击失败: %s" % err.decode(errors="replace"))
    log("tap(%d,%d)" % (x, y))

def press_back():
    if DRY_RUN:
        log("[DRY-RUN] BACK")
        return
    rc, _, err = adb("shell", "input", "keyevent", "4", timeout=10)
    if rc != 0:
        raise RuntimeError("ADB BACK 失败: %s" % err.decode(errors="replace"))

def swipe_up(frame_shape):
    """上滑翻页（看更多回放行）"""
    h, w = frame_shape[:2]
    x = w // 2
    start_y, end_y = round(h * .8), round(h * .35)
    if DRY_RUN:
        log("[DRY-RUN] swipe (%d,%d)->(%d,%d)" % (x, start_y, x, end_y))
        return
    rc, _, err = adb("shell", "input", "swipe", str(x), str(start_y),
                     str(x), str(end_y), "500", timeout=10)
    if rc != 0:
        raise RuntimeError("ADB 翻页失败: %s" % err.decode(errors="replace"))
    time.sleep(1.5)

# ---------------------------------------------------------------------------
# 模板匹配
# ---------------------------------------------------------------------------
def load_template(name):
    if name in _tpl_cache:
        return _tpl_cache[name]
    path, thresh, desc = TEMPLATES[name]
    full = os.path.join(TPL_DIR, path)
    if not os.path.exists(full):
        log("模板缺失: %s (%s)" % (full, desc))
        _tpl_cache[name] = None
        return None
    # OpenCV's Windows filename API is not Unicode-safe; decode the file bytes.
    try:
        img = cv2.imdecode(np.fromfile(full, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    except OSError:
        img = None
    if img is None:
        log("模板读取失败: %s" % full)
        _tpl_cache[name] = None
        return None
    _tpl_cache[name] = (img, thresh, desc)
    log("模板加载: %s %dx%d 阈值=%.2f (%s)" % (path, img.shape[1], img.shape[0], thresh, desc))
    return _tpl_cache[name]

def template_variants(gray, tpl):
    """Scale templates, never screenshot coordinates, for other ADB geometries."""
    screen_scale = min(gray.shape[1] / 1600, gray.shape[0] / 900)
    factors = [1.0, 0.8, 1.2]
    if abs(screen_scale - 1.0) > .05:
        factors.extend((screen_scale * .9, screen_scale, screen_scale * 1.1))
    seen = set()
    for factor in factors:
        size = (round(tpl.shape[1] * factor), round(tpl.shape[0] * factor))
        if (size in seen or min(size) < 8 or size[0] > gray.shape[1]
                or size[1] > gray.shape[0]):
            continue
        seen.add(size)
        scaled = (tpl if size == (tpl.shape[1], tpl.shape[0]) else
                  cv2.resize(tpl, size, interpolation=cv2.INTER_AREA if factor < 1 else cv2.INTER_CUBIC))
        yield scaled

def match_point(name, location, variant):
    ax, ay = TAP_ANCHORS.get(name, (.5, .5))
    return (location[0] + round(variant.shape[1] * ax),
            location[1] + round(variant.shape[0] * ay))

def match_template(gray, name):
    t = load_template(name)
    if t is None:
        return False, 0, 0, 0
    tpl, thresh, desc = t
    mx, center = 0., (0, 0)
    for variant in template_variants(gray, tpl):
        res = cv2.matchTemplate(gray, variant, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if score > mx:
            mx = score
            center = match_point(name, loc, variant)
    if mx >= thresh:
        return True, mx, *center
    return False, mx, 0, 0

def match_all(gray, name):
    """找所有匹配点（用于多行查看回放），带NMS去重"""
    t = load_template(name)
    if t is None:
        return []
    tpl, thresh, desc = t
    pts = []
    for variant in template_variants(gray, tpl):
        h, w = variant.shape
        res = cv2.matchTemplate(gray, variant, cv2.TM_CCOEFF_NORMED)
        # Keep one peak per button, avoiding a quadratic scan over every pixel.
        peaks = cv2.dilate(res, np.ones((max(3, h // 3), max(3, w // 3)), np.uint8))
        ys, xs = np.where((res >= thresh) & (res == peaks))
        pts.extend((*match_point(name, (int(x), int(y)), variant),
                    float(res[y, x]), w, h)
                   for y, x in zip(ys, xs))
    kept = []
    for cx, cy, conf, w, h in sorted(pts, key=lambda p: p[2], reverse=True):
        if not any(abs(cx - px) < max(w, pw) // 2 and
                   abs(cy - py) < max(h, ph) // 2 for px, py, _, pw, ph in kept):
            kept.append((cx, cy, conf, w, h))
    pts = [(cx, cy, conf) for cx, cy, conf, _, _ in kept]
    pts.sort(key=lambda p: p[1])  # 按y排序，从上到下
    return pts


def confirm_match(name, prior_gray, cx, cy, multiple=False):
    """Require the same target in a new frame before sending a click."""
    fresh = grab_gray()
    if fresh is None or fresh.shape != prior_gray.shape:
        return None
    loaded = load_template(name)
    if loaded is None:
        return None
    h, w = loaded[0].shape[:2]
    if multiple:
        candidates = match_all(fresh, name)
    else:
        found, conf, x, y = match_template(fresh, name)
        candidates = [(x, y, conf)] if found else []
    candidates.sort(key=lambda p: abs(p[0] - cx) + abs(p[1] - cy))
    tolerance = max(8, min(w, h) // 5)
    if (not candidates or abs(candidates[0][0] - cx) > tolerance
            or abs(candidates[0][1] - cy) > tolerance):
        return None
    return candidates[0][0], candidates[0][1], fresh

# ---------------------------------------------------------------------------
# 弹窗处理（红线：只关X）
# ---------------------------------------------------------------------------
def handle_popup(gray, max_tries=3):
    """关闭弹窗。返回 True=处理过，False=没发现。"""
    handled = False
    for _ in range(max_tries):
        found, conf, cx, cy = match_template(gray, "btn_close_x")
        if found:
            log("发现弹窗X conf=%.3f @(%d,%d)，关闭" % (conf, cx, cy))
            confirmed = confirm_match("btn_close_x", gray, cx, cy)
            if confirmed is None:
                raise RuntimeError("弹窗X未通过双帧确认，停止点击")
            x, y, fresh = confirmed
            tap(x, y, fresh.shape)
            if DRY_RUN:
                raise RuntimeError("[DRY-RUN] 弹窗未关闭，停止导航")
            handled = True
            time.sleep(1.0)
            gray = grab_gray()
            if gray is None:
                raise RuntimeError("关闭弹窗后截图失败")
            continue
        return handled
    if gray is not None and not match_template(gray, "btn_close_x")[0]:
        return handled
    raise RuntimeError("弹窗 %d 次仍未关闭，停止导航" % max_tries)

# ---------------------------------------------------------------------------
# 导航步骤
# ---------------------------------------------------------------------------
def step_find_and_tap(tpl_name, desc, verify_tpl=None, verify_desc="", retries=2):
    """截图→关弹窗→找按钮→点→验证。可重试。"""
    for attempt in range(retries + 1):
        gray = grab_gray()
        if gray is None:
            log("截图失败")
            continue
        # 每步先关弹窗（核心修复）
        handle_popup(gray)
        gray = grab_gray()
        if gray is None:
            continue
        found, conf, cx, cy = match_template(gray, tpl_name)
        log("找[%s]: found=%s conf=%.3f%s (尝试 %d/%d)"
            % (desc, found, conf, " @(%d,%d)" % (cx, cy) if found else "",
               attempt + 1, retries + 1))
        if not found:
            if attempt < retries:
                time.sleep(1.5)
                continue
            return False, gray
        confirmed = confirm_match(tpl_name, gray, cx, cy)
        if confirmed is None:
            log("目标未通过双帧确认，停止本次点击")
            continue
        x, y, fresh = confirmed
        tap(x, y, fresh.shape)
        if DRY_RUN:
            return False, fresh
        time.sleep(WAIT_AFTER_TAP)
        gray = grab_gray()
        if gray is None:
            return False, None
        # 关可能新弹出的弹窗
        handle_popup(gray)
        gray = grab_gray()
        if gray is None:
            return False, None
        if verify_tpl:
            vfound, vconf, _, _ = match_template(gray, verify_tpl)
            log("验证[%s]: found=%s conf=%.3f" % (verify_desc, vfound, vconf))
            if not vfound:
                log("点击后页面未验证，停止；不重复点击")
                return False, gray
        return True, gray
    return False, gray

def goto_bisai():
    log("=== 步骤1：主大厅 → 比赛场 ===")
    return step_find_and_tap("btn_bisai", "比赛按钮",
                             verify_tpl="btn_enter", verify_desc="进入按钮")

def goto_changci():
    log("=== 步骤2：比赛场 → 进入场次 ===")
    return step_find_and_tap("btn_enter", "进入按钮",
                             verify_tpl="btn_zhanji", verify_desc="战绩按钮")

def goto_zhanji():
    log("=== 步骤3：场次页 → 战绩 ===")
    return step_find_and_tap("btn_zhanji", "战绩按钮",
                             verify_tpl="btn_chakan_huifang",
                             verify_desc="查看回放按钮")

def select_date(date_str):
    """
    选择日期。date_str 格式 "2026-10-16"。
    使用 Codex 的字形识别方案（date_picker_cv.py）：
      - 无需OCR引擎，用字形库+余弦相似度
      - 70%权重给日期后半段，区分只差末位数字的日期
      - 双帧确认 + 点击后验证，不盲点
    """
    log("=== 选择日期: %s (字形识别) ===" % date_str)
    if DRY_RUN:
        log("[DRY-RUN] 不打开日期列表，不调用日期点击器")
        return False
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "date_picker_cv",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "date_picker_cv.py")
    )
    if spec is None or spec.loader is None:
        log("错误：找不到 date_picker_cv.py")
        return False
    dpcv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dpcv)

    ref_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "date_reference.png")
    if not os.path.exists(ref_path):
        log("错误：找不到 date_reference.png")
        return False

    # 构建字形库（5个日期）
    dates = ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17"]
    if date_str not in dates:
        # 动态生成日期列表：以目标日期为基准，前后各推
        # 简单起见，要求目标在已知列表中，否则报错
        log("错误：目标日期 %s 不在字形库日期列表中" % date_str)
        return False

    try:
        bank = dpcv.make_bank(dpcv.read_image(ref_path), dates)
        log("字形库构建成功: %d 个日期" % len(dates))
    except Exception as e:
        log("字形库构建失败: %s" % e)
        return False

    # 打开下拉列表
    gray = grab_gray()
    if gray is None:
        return False
    handle_popup(gray)
    gray = grab_gray()
    if gray is None:
        return False
    found, conf, cx, cy = match_template(gray, "btn_date")
    if not found:
        log("TEMPLATE_MISS: 日期下拉框；停止，不尝试固定坐标")
        return False
    confirmed = confirm_match("btn_date", gray, cx, cy)
    if confirmed is None:
        log("日期下拉框未通过双帧确认")
        return False
    x, y, fresh = confirmed
    tap(x, y, fresh.shape)
    time.sleep(1.5)

    # 用字形识别找目标日期并点击
    adb = dpcv.ADB(ADB_PATH, SERIAL)
    try:
        debug_dir = Path(os.path.dirname(os.path.abspath(__file__))) / "date_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        result = dpcv.select_date(adb, bank, date_str, debug_dir)
        log("日期选择成功: %s (score=%.3f)" % (result.get("selected"), result.get("field_score", 0)))
    except Exception as e:
        log("日期选择失败: %s" % e)
        return False

    # 点刷新
    time.sleep(1)
    gray = grab_gray()
    if gray is not None:
        handle_popup(gray)
        gray = grab_gray()
    if gray is None:
        return False
    found, conf, cx, cy = match_template(gray, "btn_refresh")
    if not found:
        log("TEMPLATE_MISS: 刷新按钮；停止")
        return False
    confirmed = confirm_match("btn_refresh", gray, cx, cy)
    if confirmed is None:
        log("刷新按钮未通过双帧确认")
        return False
    log("点刷新 conf=%.3f" % conf)
    x, y, fresh = confirmed
    tap(x, y, fresh.shape)
    time.sleep(2.5)
    # The refresh control can remain visible on the same page. A fresh frame is
    # still required here; whether the record list loaded needs manual review.
    refreshed = grab_gray()
    if refreshed is None or refreshed.shape != fresh.shape:
        log("刷新后截图失败或显示尺寸变化，停止采集")
        return False
    return True

def harvest_rows(out_dir, max_pages=MAX_SCROLL_PAGES):
    """采集当前战绩页的所有回放行，支持翻页。返回 (done, total)。"""
    log("=== 步骤4：逐行采集回放 ===")
    done = 0
    total = 0
    seen = set()  # 页码与行位置组合；不同页可有相同y坐标

    for page in range(max_pages):
        gray = grab_gray()
        if gray is None:
            break
        handle_popup(gray)
        gray = grab_gray()
        if gray is None:
            raise RuntimeError("关闭弹窗后截图失败，停止采集")

        rows = match_all(gray, "btn_chakan_huifang")
        # 过滤已采过的（翻页后可能重复）
        new_rows = [(cx, cy, cf) for (cx, cy, cf) in rows
                    if not any(p == page and abs(cy - sy) < 30 for p, sy in seen)]
        if not new_rows:
            log("第 %d 页无新行，停止翻页" % (page + 1))
            break

        log("第 %d 页：%d 行新回放" % (page + 1, len(new_rows)))
        total += len(new_rows)

        for (cx, cy, conf) in new_rows:
            log("--- conf=%.3f @(%d,%d) ---" % (conf, cx, cy))
            if DRY_RUN:
                log("[DRY-RUN] 跳过")
                seen.add((page, cy))
                continue
            confirmed = confirm_match("btn_chakan_huifang", gray, cx, cy, multiple=True)
            if confirmed is None:
                raise RuntimeError("回放按钮未通过双帧确认，停止采集")
            x, y, fresh = confirmed
            tap(x, y, fresh.shape)
            time.sleep(WAIT_AFTER_TAP)
            opened = grab_gray()
            if opened is None or opened.shape != fresh.shape:
                raise RuntimeError("点击回放后截图失败或分辨率变化，停止采集")
            time.sleep(.4)
            opened_again = grab_gray()
            if opened_again is None or opened_again.shape != fresh.shape:
                raise RuntimeError("回放二次验证截图失败或分辨率变化，停止采集")
            if any(abs(px - x) < 20 and abs(py - y) < 20
                   for frame in (opened, opened_again)
                   for px, py, _ in match_all(frame, "btn_chakan_huifang")):
                raise RuntimeError("回放按钮仍在原位，未确认页面切换，停止采集")
            if np.mean(cv2.absdiff(fresh, opened_again)) < 2.0:
                raise RuntimeError("点击后画面几乎未变化，停止采集")
            # 截图
            for s in range(SHOTS_PER_REPLAY):
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                path = os.path.join(out_dir, "replay_p%d_%s_%d.png" % (page + 1, ts, s + 1))
                if save_screenshot(path):
                    log("截图: %s" % os.path.basename(path))
                if s < SHOTS_PER_REPLAY - 1:
                    time.sleep(SHOT_INTERVAL)
            press_back()  # 红线：只BACK，不点下一局
            time.sleep(WAIT_AFTER_TAP)
            seen.add((page, cy))
            done += 1

        # 翻页看更多
        if page < max_pages - 1:
            log("上滑翻页...")
            swipe_up(gray.shape)

    return done, total

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    global ADB_PATH, SERIAL, TPL_DIR, DRY_RUN, LOG_FH
    ap = argparse.ArgumentParser(description="温州茶回放采集（统一版）")
    ap.add_argument("--adb", default=DEFAULT_ADB)
    ap.add_argument("--serial", default=DEFAULT_SERIAL)
    ap.add_argument("--tpl-dir", default=DEFAULT_TPL_DIR)
    ap.add_argument("--out", default="")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="默认：只识别，不点击")
    mode.add_argument("--execute", action="store_true", help="显式启用 ADB 点击；仍需用户授权")
    ap.add_argument("--date", default="", help="目标日期 YYYY-MM-DD，如 2026-10-16")
    ap.add_argument("--from-zhanji", action="store_true", help="已在战绩页，直接开采")
    ap.add_argument("--max-pages", type=int, default=MAX_SCROLL_PAGES)
    args = ap.parse_args()

    ADB_PATH, SERIAL, TPL_DIR = args.adb, args.serial, args.tpl_dir
    DRY_RUN = not args.execute
    out_dir = args.out or ("harvest_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    LOG_FH = open(os.path.join(out_dir, "harvest.log"), "w", encoding="utf-8")

    log("harvest_tea.py 启动")
    log("serial=%s dry_run=%s date=%s from_zhanji=%s" % (SERIAL, DRY_RUN, args.date, args.from_zhanji))
    for name in TEMPLATES:
        load_template(name)
    rc, out, _ = adb("devices", timeout=10)
    log("adb devices:\n%s" % out.decode(errors="replace").strip())

    if not args.from_zhanji:
        ok, _ = goto_bisai()
        if not ok:
            log("FATAL: 主大厅找不到比赛按钮")
            return 1
        ok, _ = goto_changci()
        if not ok:
            log("FATAL: 比赛场找不到进入按钮")
            return 1
        ok, _ = goto_zhanji()
        if not ok:
            log("FATAL: 场次页找不到战绩按钮")
            return 1

    if args.date:
        if not select_date(args.date):
            log("FATAL: 日期未验证，停止采集")
            return 1

    done, total = harvest_rows(out_dir, max_pages=args.max_pages)
    log("完成：%d/%d 行，输出目录 %s" % (done, total, out_dir))
    if LOG_FH:
        LOG_FH.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
