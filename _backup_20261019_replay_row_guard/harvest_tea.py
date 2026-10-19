# -*- coding: utf-8 -*-
"""
harvest_tea.py — 温州茶回放采集（统一版）

整合了 Grok 的 PS1 脚本经验 + 模板匹配的鲁棒性。

为什么不用 uiautomator：
    游戏是 Cocos 引擎，UI 是画在 SurfaceView 上的像素，
    uiautomator 看不到游戏内按钮。必须用图片模板匹配。

链路：
    主大厅 --[比赛]--> 比赛场列表 --[进入]--> 场次页 --[战绩]-->
    比赛战绩页 --[选日期]--> 逐行[详情] --> [回放选项]
    --> 逐局[回放] --> 录屏MP4及预览截图 --> [绿色X] --> [返回列表]

红线（不可绕过）：
    1. 邀请弹窗只点取消；其他弹窗只关 X，绝不点领取/购买/充值
    2. 回放页只录屏和截图，不点"下一局"/"再来一局"
    3. 不进雷电游戏中心
    4. 房卡为0立即停

用法：
    python harvest_tea.py --date 2026-10-16          # 默认只看不点
    python harvest_tea.py --date 2026-10-16 --execute # 授权后才启用点击
    python harvest_tea.py --from-zhanji               # 已在战绩页，直接开采
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
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
    "btn_xiangqing":       ("btn_xiangqing.png",       0.90, "战绩行详情"),
    "btn_fanhui_list":     ("btn_fanhui_list.png",     0.90, "返回战绩列表"),
    "btn_replay_tab":      ("btn_replay_tab.png",      0.90, "回放选项"),
    "btn_huifang":         ("btn_huifang.png",         0.90, "逐局回放"),
    "btn_replay_close":    ("btn_replay_close.png",    0.90, "关闭回放"),
    "invite_phrase":       ("invite_phrase.png",       0.92, "邀请您进入文案"),
    "invite_cancel":       ("invite_cancel.png",       0.94, "邀请取消按钮"),
    "btn_close_x":        ("btn_close_x.png",        0.85, "弹窗X"),
    "promo_discount_title": ("promo_discount_title.png", 0.94, "三折金币礼包标题"),
    "promo_discount_close": ("promo_discount_close.png", 0.94, "三折金币礼包关闭X"),
    "friend_list_title": ("friend_list_title.png", 0.94, "牌友列表标题"),
    "friend_list_collapse": ("friend_list_collapse.png", 0.94, "牌友列表收起箭头"),
    "daily_task_title": ("daily_task_title.png", 0.90, "每日任务标题"),
    "btn_date_dropdown": ("btn_date_dropdown.png", 0.90, "日期下拉箭头"),
    "btn_refresh":        ("btn_refresh.png",        0.85, "刷新按钮"),
}
# All active templates are crops of the actual drawn control; their centers
# fall inside that control in native ADB screenshot coordinates.
TAP_ANCHORS = {}

WAIT_AFTER_TAP = 2.0
SHOTS_PER_REPLAY = 3
SHOT_INTERVAL = 2.0
RECORD_SECONDS = 100
RECORD_BIT_RATE = 4_000_000
EXPECTED_PACKAGE = "com.xm.zjgamecenter"
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

def save_screenshot(path, expected_template=None):
    rc, out, _ = adb("exec-out", "screencap", "-p", timeout=30)
    if rc != 0 or not out:
        return False
    img = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return False
    if expected_template:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if not match_template(gray, expected_template)[0]:
            log("截图页面验证失败: 未找到 %s，拒绝写入 %s"
                % (expected_template, path))
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
    require_game_foreground()
    rc, _, err = adb("shell", "input", "tap", str(x), str(y), timeout=10)
    if rc != 0:
        raise RuntimeError("ADB 点击失败: %s" % err.decode(errors="replace"))
    log("tap(%d,%d)" % (x, y))

def press_back():
    if DRY_RUN:
        log("[DRY-RUN] BACK")
        return
    require_game_foreground()
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
    require_game_foreground()
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
def discount_promo_target(gray):
    """Only accept the X paired with the observed discount-promo title."""
    title, _, tx, ty = match_template(gray, "promo_discount_title")
    if not title:
        return None
    close, score, cx, cy = match_template(gray, "promo_discount_close")
    h, w = gray.shape[:2]
    if (not close or not (.30 * w <= cx - tx <= .50 * w)
            or not (-.16 * h <= cy - ty <= .03 * h)
            or cx < .75 * w or cy > .25 * h):
        raise RuntimeError("识别到三折礼包，但关闭X的位置未可靠确认，停止导航")
    return score, cx, cy


def friend_list_target(gray):
    """Only fold the drawer when its title and adjacent arrow agree."""
    title, _, tx, ty = match_template(gray, "friend_list_title")
    if not title:
        return None
    arrow, score, cx, cy = match_template(gray, "friend_list_collapse")
    h, w = gray.shape[:2]
    if (not arrow or not (.20 * w <= cx - tx <= .33 * w)
            or not (.30 * h <= cy - ty <= .52 * h)
            or cx > .38 * w or cy > .65 * h):
        raise RuntimeError("识别到牌友列表，但收起箭头的位置未可靠确认，停止导航")
    return score, cx, cy


def daily_task_target(gray):
    """Pair the daily-task heading with its upper-right close control."""
    title, _, tx, ty = match_template(gray, "daily_task_title")
    if not title:
        return None
    close, score, cx, cy = match_template(gray, "btn_close_x")
    h, w = gray.shape[:2]
    if (not close or not (.62 * w <= cx - tx <= .80 * w)
            or not (-.08 * h <= cy - ty <= .12 * h)
            or cx < .90 * w or cy > .25 * h):
        raise RuntimeError("识别到每日任务，但关闭X的位置未可靠确认，停止导航")
    return score, cx, cy


def handle_popup(gray, max_tries=5):
    """Reject room invitations before navigating; never accept one."""
    handled = False
    for _ in range(max_tries):
        invited, _, phrase_x, phrase_y = match_template(gray, "invite_phrase")
        if invited:
            found, conf, cx, cy = match_template(gray, "invite_cancel")
            if not found or cx >= phrase_x or cy <= phrase_y:
                raise RuntimeError("发现入桌邀请，但未可靠定位取消按钮，停止导航")
            confirmed = confirm_match("invite_cancel", gray, cx, cy)
            if confirmed is None:
                raise RuntimeError("邀请取消按钮未通过双帧确认，停止点击")
            x, y, fresh = confirmed
            if not match_template(fresh, "invite_phrase")[0]:
                raise RuntimeError("取消前邀请文案已变化，停止点击")
            log("发现入桌邀请，点取消 conf=%.3f @(%d,%d)" % (conf, x, y))
            tap(x, y, fresh.shape)
            if DRY_RUN:
                raise RuntimeError("[DRY-RUN] 邀请弹窗未关闭，停止导航")
            handled = True
            time.sleep(.7)
            gray = grab_gray()
            if gray is None:
                raise RuntimeError("取消邀请后截图失败")
            continue
        promo = discount_promo_target(gray)
        if promo is not None:
            score, cx, cy = promo
            confirmed = confirm_match("promo_discount_close", gray, cx, cy)
            if confirmed is None:
                raise RuntimeError("三折礼包关闭X未通过双帧确认，停止点击")
            x, y, fresh = confirmed
            if discount_promo_target(fresh) is None:
                raise RuntimeError("点击前礼包画面已变化，停止点击")
            log("关闭三折礼包 conf=%.3f @(%d,%d)" % (score, x, y))
            tap(x, y, fresh.shape)
            if DRY_RUN:
                raise RuntimeError("[DRY-RUN] 礼包弹窗未关闭，停止导航")
            handled = True
            time.sleep(.7)
            gray = grab_gray()
            if gray is None or discount_promo_target(gray) is not None:
                raise RuntimeError("点击后礼包弹窗仍在，停止重复点击")
            continue
        drawer = friend_list_target(gray)
        if drawer is not None:
            score, cx, cy = drawer
            confirmed = confirm_match("friend_list_collapse", gray, cx, cy)
            if confirmed is None:
                raise RuntimeError("牌友列表箭头未通过双帧确认，停止点击")
            x, y, fresh = confirmed
            if friend_list_target(fresh) is None:
                raise RuntimeError("点击前牌友列表已变化，停止点击")
            log("收起牌友列表 conf=%.3f @(%d,%d)" % (score, x, y))
            tap(x, y, fresh.shape)
            if DRY_RUN:
                raise RuntimeError("[DRY-RUN] 牌友列表未收起，停止导航")
            handled = True
            time.sleep(.7)
            gray = grab_gray()
            if gray is None or friend_list_target(gray) is not None:
                raise RuntimeError("点击后牌友列表仍展开，停止重复点击")
            continue
        daily_task = daily_task_target(gray)
        if daily_task is not None:
            score, cx, cy = daily_task
            confirmed = confirm_match("btn_close_x", gray, cx, cy)
            if confirmed is None:
                raise RuntimeError("每日任务关闭X未通过双帧确认，停止点击")
            x, y, fresh = confirmed
            if daily_task_target(fresh) is None:
                raise RuntimeError("点击前每日任务画面已变化，停止点击")
            log("关闭每日任务 conf=%.3f @(%d,%d)" % (score, x, y))
            tap(x, y, fresh.shape)
            if DRY_RUN:
                raise RuntimeError("[DRY-RUN] 每日任务弹窗未关闭，停止导航")
            handled = True
            time.sleep(.7)
            gray = grab_gray()
            if gray is None or daily_task_target(gray) is not None:
                raise RuntimeError("点击后每日任务弹窗仍在，停止重复点击")
            continue
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
    if (gray is not None and not match_template(gray, "invite_phrase")[0]
            and discount_promo_target(gray) is None
            and friend_list_target(gray) is None
            and daily_task_target(gray) is None
            and not match_template(gray, "btn_close_x")[0]):
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
                             verify_tpl="btn_date_dropdown",
                             verify_desc="战绩日期下拉箭头")


def choose_date_bank(dpcv, opened, date_str, references):
    """Use exactly one complete, observed date list; never guess a row."""
    matches = []
    for ref_path, dates in references:
        if not ref_path.is_file():
            continue
        candidate = dpcv.make_bank(dpcv.read_image(ref_path), dates)
        try:
            dpcv.find_list(opened, candidate)
        except RuntimeError:
            continue
        matches.append(candidate)
    if len(matches) != 1 or date_str not in matches[0]:
        raise RuntimeError("当前日期列表未唯一匹配字形库或不含目标日期")
    return matches[0]


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

    bot_dir = Path(__file__).resolve().parent
    references = (
        (bot_dir / "date_reference.png",
         ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17"]),
        (bot_dir.parent / "采集" / "2026-10-19" / "date_15_19" / "reference.png",
         ["2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19"]),
    )
    if not any(date_str in dates for _, dates in references):
        log("错误：目标日期 %s 不在已采集字形库中" % date_str)
        return False

    # 打开下拉列表
    gray = grab_gray()
    if gray is None:
        return False
    handle_popup(gray)
    gray = grab_gray()
    if gray is None:
        return False
    found, conf, cx, cy = match_template(gray, "btn_date_dropdown")
    if not found:
        log("TEMPLATE_MISS: 日期下拉框；停止，不尝试固定坐标")
        return False
    confirmed = confirm_match("btn_date_dropdown", gray, cx, cy)
    if confirmed is None:
        log("日期下拉框未通过双帧确认")
        return False
    x, y, fresh = confirmed
    tap(x, y, fresh.shape)
    time.sleep(1.5)

    # 用字形识别找目标日期并点击
    class GuardedDateADB(dpcv.ADB):
        def tap(self, x, y):
            require_game_foreground()
            super().tap(x, y)

    adb = GuardedDateADB(ADB_PATH, SERIAL)
    try:
        opened = adb.shot()
        bank = choose_date_bank(dpcv, opened, date_str, references)
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

def checked_click(name, prior_gray, cx, cy, verify_name, multiple=False,
                  verify_timeout=WAIT_AFTER_TAP):
    """Click a fresh match once and require the expected next screen."""
    confirmed = confirm_match(name, prior_gray, cx, cy, multiple=multiple)
    if confirmed is None:
        raise RuntimeError("%s 未通过双帧确认，停止点击" % name)
    x, y, fresh = confirmed
    tap(x, y, fresh.shape)
    if DRY_RUN:
        return None
    time.sleep(WAIT_AFTER_TAP)
    probes = max(1, 1 + int(max(0, verify_timeout - WAIT_AFTER_TAP)))
    for probe in range(probes):
        after = grab_gray()
        if after is None or after.shape != fresh.shape:
            raise RuntimeError("%s 点击后截图失败或尺寸变化" % name)
        if match_template(after, verify_name)[0]:
            return after
        if probe < probes - 1:
            time.sleep(1)
    raise RuntimeError("%s 点击后未出现 %s，停止且不重试" % (name, verify_name))

def row_fingerprint(gray, cy, width_fraction):
    """Use the room/round text at left to avoid capturing overlap after a swipe."""
    h, w = gray.shape[:2]
    radius = max(20, round(h * .045))
    left = gray[max(0, cy - radius):min(h, cy + radius), :round(w * width_fraction)]
    if left.size == 0:
        raise RuntimeError("战绩行裁图为空")
    compact = cv2.resize(left, (96, 32), interpolation=cv2.INTER_AREA)
    return hashlib.sha256((compact // 8).tobytes()).hexdigest()

def require_game_foreground():
    """Fail closed before any game input or replay recording."""
    for command, patterns in (
        (("shell", "dumpsys", "activity", "activities"),
         (r"topResumedActivity=.*?\s([A-Za-z0-9_.]+)/",
          r"ResumedActivity:.*?\s([A-Za-z0-9_.]+)/")),
        (("shell", "dumpsys", "window", "windows"),
         (r"mCurrentFocus=.*?\s([A-Za-z0-9_.]+)/",
          r"mFocusedApp=.*?\s([A-Za-z0-9_.]+)/")),
    ):
        rc, out, _ = adb(*command, timeout=10)
        if rc != 0:
            continue
        dump = out.decode(errors="replace")
        for pattern in patterns:
            match = re.search(pattern, dump)
            if match:
                package = match.group(1)
                if package != EXPECTED_PACKAGE:
                    raise RuntimeError("前景应用不是游戏: %s" % package)
                return
    raise RuntimeError("无法确认前景应用，拒绝录屏或点击回放")


def check_video(path, ffprobe):
    """Accept only a decodable MP4 with a video stream and useful duration."""
    if path.stat().st_size < 4096:
        raise RuntimeError("回放视频过小: %s" % path)
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError("ffprobe 无法解码回放: %s" % result.stderr.strip())
    try:
        info = json.loads(result.stdout)
        duration = float(info["format"]["duration"])
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        width, height = int(video["width"]), int(video["height"])
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        raise RuntimeError("回放视频缺少有效时长或画面") from exc
    if duration < 5 or width <= 0 or height <= 0:
        raise RuntimeError("回放视频时长或画面无效")
    return duration, width, height


def capture_round(out_dir, room_index, round_index, prior_gray, cx, cy):
    """Record the identified round, then keep three verified previews."""
    if DRY_RUN:
        raise RuntimeError("只读模式禁止录制回放")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("找不到 ffprobe，拒绝开始回放采集")
    require_game_foreground()
    unique = uuid.uuid4().hex
    stem = "room%d_round%d_%s" % (room_index, round_index, unique[:8])
    final = Path(out_dir) / (stem + ".mp4")
    partial = Path(out_dir) / (stem + ".mp4.part")
    remote = "/sdcard/tea_replay_%s.mp4" % unique
    if final.exists() or partial.exists():
        raise FileExistsError(final)
    recorder = None
    try:
        recorder = subprocess.Popen(
            [ADB_PATH, "-s", SERIAL, "shell", "screenrecord",
             "--time-limit", str(RECORD_SECONDS), "--bit-rate",
             str(RECORD_BIT_RATE), remote],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        time.sleep(1)
        if recorder.poll() is not None:
            raise RuntimeError("screenrecord 未能启动")
        # The replay may briefly show a waiting table. Never tap Start Game.
        replay = checked_click("btn_huifang", prior_gray, cx, cy,
                               "btn_replay_close", multiple=True,
                               verify_timeout=12)
        require_game_foreground()
        for shot in range(SHOTS_PER_REPLAY):
            path = Path(out_dir) / (stem + "_%d.png" % (shot + 1))
            if not save_screenshot(str(path), expected_template="btn_replay_close"):
                raise RuntimeError("回放预览截图失败: %s" % path)
            if shot < SHOTS_PER_REPLAY - 1:
                time.sleep(SHOT_INTERVAL)
        try:
            recorder.wait(timeout=RECORD_SECONDS + 20)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("screenrecord 超时") from exc
        if recorder.returncode != 0:
            raise RuntimeError("screenrecord 退出码: %s" % recorder.returncode)
        require_game_foreground()
        last_frame = grab_gray()
        if last_frame is None or not match_template(last_frame, "btn_replay_close")[0]:
            raise RuntimeError("录屏结束时不在回放页面，拒绝保存为有效回放")
        rc, _, err = adb("pull", remote, str(partial), timeout=120)
        if rc != 0:
            raise RuntimeError("回放视频拉取失败: %s" % err.decode(errors="replace"))
        duration, width, height = check_video(partial, ffprobe)
        os.rename(partial, final)  # Windows refuses to overwrite an existing file.
        log("回放视频: %s %.1f秒 %dx%d" % (final.name, duration, width, height))
        return replay
    finally:
        if recorder is not None and recorder.poll() is None:
            recorder.kill()
            recorder.wait(timeout=5)
        rc, _, _ = adb("shell", "rm", "-f", remote, timeout=10)
        if rc != 0:
            log("远端临时回放未清理: %s" % remote)
        if partial.exists():
            partial.unlink()


def harvest_detail(out_dir, room_index, max_pages):
    """Switch a room detail page to replay mode and capture its visible rounds."""
    gray = grab_gray()
    if gray is None or not match_template(gray, "btn_fanhui_list")[0]:
        raise RuntimeError("未进入战绩详情页")
    found, _, cx, cy = match_template(gray, "btn_replay_tab")
    if found:
        gray = checked_click("btn_replay_tab", gray, cx, cy, "btn_huifang")
    elif not match_template(gray, "btn_huifang")[0]:
        raise RuntimeError("详情页未找到可用的回放选项")

    captured = 0
    seen_rounds = set()
    for page in range(max_pages):
        gray = grab_gray()
        if gray is None or not match_template(gray, "btn_fanhui_list")[0]:
            raise RuntimeError("逐局采集前详情页验证失败")
        rounds = match_all(gray, "btn_huifang")
        new_rounds = [(x, y, score, row_fingerprint(gray, y, .16))
                      for x, y, score in rounds]
        new_rounds = [row for row in new_rounds if row[3] not in seen_rounds]
        if not new_rounds:
            break
        for cx, cy, score, fingerprint in new_rounds:
            replay = capture_round(out_dir, room_index, captured + 1,
                                   gray, cx, cy)
            found, _, close_x, close_y = match_template(replay, "btn_replay_close")
            if not found:
                raise RuntimeError("截图后找不到回放关闭按钮")
            checked_click("btn_replay_close", replay, close_x, close_y,
                          "btn_fanhui_list")
            seen_rounds.add(fingerprint)
            captured += 1
            gray = grab_gray()
            if gray is None:
                raise RuntimeError("关闭回放后截图失败")
        if page < max_pages - 1:
            before = gray
            swipe_up(before.shape)
            gray = grab_gray()
            if gray is None or gray.shape != before.shape:
                raise RuntimeError("详情页翻页后截图失败")
            if np.mean(cv2.absdiff(before, gray)) < 2.0:
                break
    return captured

def harvest_rows(out_dir, max_pages=MAX_SCROLL_PAGES):
    """From each result row enter detail, then its per-round replay buttons."""
    log("=== 步骤4：战绩详情 → 逐局回放 ===")
    done = 0
    total = 0
    seen = set()

    for page in range(max_pages):
        gray = grab_gray()
        if gray is None:
            break
        handle_popup(gray)
        gray = grab_gray()
        if gray is None:
            raise RuntimeError("关闭弹窗后截图失败，停止采集")

        if not match_template(gray, "btn_date_dropdown")[0]:
            raise RuntimeError("当前不是比赛战绩列表，停止采集")
        rows = match_all(gray, "btn_xiangqing")
        new_rows = [(x, y, score, row_fingerprint(gray, y, .32))
                    for x, y, score in rows]
        new_rows = [row for row in new_rows if row[3] not in seen]
        if not new_rows:
            log("第 %d 页无新战绩行，停止翻页" % (page + 1))
            break

        log("第 %d 页：%d 行战绩" % (page + 1, len(new_rows)))
        total += len(new_rows)

        for (cx, cy, conf, fingerprint) in new_rows:
            log("--- conf=%.3f @(%d,%d) ---" % (conf, cx, cy))
            if DRY_RUN:
                log("[DRY-RUN] 识别详情行，跳过点击")
                seen.add(fingerprint)
                continue
            checked_click("btn_xiangqing", gray, cx, cy,
                          "btn_fanhui_list", multiple=True)
            rounds_done = harvest_detail(out_dir, done + 1, max_pages)
            log("战绩行 %d：采集 %d 局" % (done + 1, rounds_done))
            detail = grab_gray()
            if detail is None:
                raise RuntimeError("返回战绩列表前截图失败")
            found, _, back_x, back_y = match_template(detail, "btn_fanhui_list")
            if not found:
                raise RuntimeError("找不到返回列表按钮")
            checked_click("btn_fanhui_list", detail, back_x, back_y,
                          "btn_date_dropdown")
            seen.add(fingerprint)
            done += 1
            gray = grab_gray()
            if gray is None:
                raise RuntimeError("返回战绩列表后截图失败")

        # 翻页看更多
        if page < max_pages - 1:
            log("上滑翻页...")
            swipe_up(gray.shape)

    return done, total

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    global ADB_PATH, SERIAL, TPL_DIR, DRY_RUN, LOG_FH, RECORD_SECONDS
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
    ap.add_argument("--record-seconds", type=int, default=RECORD_SECONDS,
                    help="每局录屏秒数，5-180，默认100")
    args = ap.parse_args()
    if not 5 <= args.record_seconds <= 180:
        ap.error("--record-seconds 必须在 5 到 180 秒之间")

    ADB_PATH, SERIAL, TPL_DIR = args.adb, args.serial, args.tpl_dir
    DRY_RUN = not args.execute
    RECORD_SECONDS = args.record_seconds
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
