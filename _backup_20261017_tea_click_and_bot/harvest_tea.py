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
    python harvest_tea.py --date 2026-10-16          # 采指定日期
    python harvest_tea.py --date 2026-10-16 --dry-run # 只看不点
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

WAIT_AFTER_TAP = 2.0
SHOTS_PER_REPLAY = 3
SHOT_INTERVAL = 2.0
MAX_SCROLL_PAGES = 5  # 最多翻页数，防止无限翻

ADB_PATH = DEFAULT_ADB
SERIAL = DEFAULT_SERIAL
TPL_DIR = DEFAULT_TPL_DIR
DRY_RUN = False
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
    cv2.imwrite(path, img)
    return True

def tap(x, y):
    if DRY_RUN:
        log("[DRY-RUN] tap(%d,%d)" % (x, y))
        return
    adb("shell", "input", "tap", str(x), str(y), timeout=10)
    log("tap(%d,%d)" % (x, y))

def press_back():
    if DRY_RUN:
        log("[DRY-RUN] BACK")
        return
    adb("shell", "input", "keyevent", "4", timeout=10)

def swipe_up():
    """上滑翻页（看更多回放行）"""
    if DRY_RUN:
        log("[DRY-RUN] swipe up")
        return
    # 从屏幕中下部滑到中上部
    adb("shell", "input", "swipe", "800", "1200", "800", "400", "500", timeout=10)
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
    img = cv2.imread(full, cv2.IMREAD_GRAYSCALE)
    if img is None:
        log("模板读取失败: %s" % full)
        _tpl_cache[name] = None
        return None
    _tpl_cache[name] = (img, thresh, desc)
    log("模板加载: %s %dx%d 阈值=%.2f (%s)" % (path, img.shape[1], img.shape[0], thresh, desc))
    return _tpl_cache[name]

def match_template(gray, name):
    t = load_template(name)
    if t is None:
        return False, 0, 0, 0
    tpl, thresh, desc = t
    if gray.shape[0] < tpl.shape[0] or gray.shape[1] < tpl.shape[1]:
        return False, 0, 0, 0
    res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, ml = cv2.minMaxLoc(res)
    if mx >= thresh:
        h, w = tpl.shape
        return True, mx, ml[0] + w // 2, ml[1] + h // 2
    return False, mx, 0, 0

def match_all(gray, name):
    """找所有匹配点（用于多行查看回放），带NMS去重"""
    t = load_template(name)
    if t is None:
        return []
    tpl, thresh, desc = t
    if gray.shape[0] < tpl.shape[0] or gray.shape[1] < tpl.shape[1]:
        return []
    res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
    h, w = tpl.shape
    ys, xs = np.where(res >= thresh)
    pts = []
    for y, x in zip(ys, xs):
        cx, cy = x + w // 2, y + h // 2
        conf = res[y, x]
        # NMS：去掉距离太近的重复点
        dup = False
        for (pcx, pcy, pconf) in pts:
            if abs(cx - pcx) < w // 2 and abs(cy - pcy) < h // 2:
                dup = True
                break
        if not dup:
            pts.append((cx, cy, conf))
    pts.sort(key=lambda p: p[1])  # 按y排序，从上到下
    return pts

# ---------------------------------------------------------------------------
# 弹窗处理（红线：只关X）
# ---------------------------------------------------------------------------
def handle_popup(gray, max_tries=3):
    """关闭弹窗。返回 True=处理过，False=没发现。"""
    for _ in range(max_tries):
        found, conf, cx, cy = match_template(gray, "btn_close_x")
        if found:
            log("发现弹窗X conf=%.3f @(%d,%d)，关闭" % (conf, cx, cy))
            tap(cx, cy)
            time.sleep(1.0)
            gray = grab_gray()
            if gray is None:
                return True
            continue
        return False
    log("警告：弹窗 %d 次关不掉" % max_tries)
    return True

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
        tap(cx, cy)
        time.sleep(WAIT_AFTER_TAP)
        gray = grab_gray()
        if gray is None:
            return False, None
        # 关可能新弹出的弹窗
        handle_popup(gray)
        gray = grab_gray()
        if verify_tpl:
            vfound, vconf, _, _ = match_template(gray, verify_tpl)
            log("验证[%s]: found=%s conf=%.3f" % (verify_desc, vfound, vconf))
            if not vfound and attempt < retries:
                log("验证失败，重试")
                time.sleep(1.5)
                continue
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
    handle_popup(gray)
    gray = grab_gray()
    found, conf, cx, cy = match_template(gray, "btn_date")
    if found:
        tap(cx, cy)
    else:
        tap(392, 145)
    time.sleep(1.5)

    # 用字形识别找目标日期并点击
    adb = dpcv.ADB(ADB_PATH, SERIAL)
    try:
        debug_dir = Path(os.path.dirname(os.path.abspath(__file__))) / "date_debug"
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
    found, conf, cx, cy = match_template(gray, "btn_refresh")
    if found:
        log("点刷新 conf=%.3f" % conf)
        tap(cx, cy)
        time.sleep(2.5)
    return True

def harvest_rows(out_dir, max_pages=MAX_SCROLL_PAGES):
    """采集当前战绩页的所有回放行，支持翻页。返回 (done, total)。"""
    log("=== 步骤4：逐行采集回放 ===")
    done = 0
    total = 0
    seen = set()  # 去重：记已采的y坐标

    for page in range(max_pages):
        gray = grab_gray()
        if gray is None:
            break
        handle_popup(gray)
        gray = grab_gray()

        rows = match_all(gray, "btn_chakan_huifang")
        # 过滤已采过的（翻页后可能重复）
        new_rows = [(cx, cy, cf) for (cx, cy, cf) in rows
                    if not any(abs(cy - sy) < 30 for sy in seen)]
        if not new_rows:
            log("第 %d 页无新行，停止翻页" % (page + 1))
            break

        log("第 %d 页：%d 行新回放" % (page + 1, len(new_rows)))
        total += len(new_rows)

        for (cx, cy, conf) in new_rows:
            log("--- conf=%.3f @(%d,%d) ---" % (conf, cx, cy))
            if DRY_RUN:
                log("[DRY-RUN] 跳过")
                seen.add(cy)
                continue
            tap(cx, cy)
            time.sleep(WAIT_AFTER_TAP)
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
            seen.add(cy)
            done += 1

        # 翻页看更多
        if page < max_pages - 1:
            log("上滑翻页...")
            swipe_up()

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
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date", default="", help="目标日期 YYYY-MM-DD，如 2026-10-16")
    ap.add_argument("--from-zhanji", action="store_true", help="已在战绩页，直接开采")
    ap.add_argument("--max-pages", type=int, default=MAX_SCROLL_PAGES)
    args = ap.parse_args()

    ADB_PATH, SERIAL, TPL_DIR = args.adb, args.serial, args.tpl_dir
    DRY_RUN = args.dry_run
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
        select_date(args.date)

    done, total = harvest_rows(out_dir, max_pages=args.max_pages)
    log("完成：%d/%d 行，输出目录 %s" % (done, total, out_dir))
    if LOG_FH:
        LOG_FH.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
