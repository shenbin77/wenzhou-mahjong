# -*- coding: utf-8 -*-
"""brain_v0 配置：牌定义、模板、截图区域（1600x900）"""
import os

# 28种牌（温州放炮麻将）
TILE_TYPES = [
    ("1m", "一万"), ("2m", "二万"), ("3m", "三万"),
    ("4m", "四万"), ("5m", "五万"), ("6m", "六万"),
    ("7m", "七万"), ("8m", "八万"), ("9m", "九万"),
    ("1s", "一条"), ("2s", "二条"), ("3s", "三条"),
    ("4s", "四条"), ("5s", "五条"), ("6s", "六条"),
    ("7s", "七条"), ("8s", "八条"), ("9s", "九条"),
    ("1p", "一筒"), ("2p", "二筒"), ("3p", "三筒"),
    ("4p", "四筒"), ("5p", "五筒"), ("6p", "六筒"),
    ("7p", "七筒"), ("8p", "八筒"), ("9p", "九筒"),
    ("red", "红中(财神)"),
]

# 字符串牌ID -> 引擎数字ID
ENGINE_TILE_IDS = {
    **{f"{n}m": n for n in range(1, 10)},      # 万: 1-9
    **{f"{n}p": 20 + n for n in range(1, 10)},  # 筒: 21-29
    **{f"{n}s": 40 + n for n in range(1, 10)},  # 条: 41-49
    "red": 55,                                   # 红中财神
}

MAX_HAND_TILES = 14
MATCH_THRESHOLD = 0.75
SCALE_RANGE = [0.8, 0.9, 1.0, 1.1, 1.2]

# 模板目录（可通过环境变量覆盖）
TEMPLATE_DIR = os.environ.get(
    "TEMPLATE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates"),
)

# 模板别名：字符串ID -> 模板文件名stem
TEMPLATE_ALIASES = {
    **{f"{n}m": f"mj_{n}c" for n in range(1, 10)},
    **{f"{n}s": f"mj_{n}b" for n in range(1, 10)},
    **{f"{n}p": f"mj_{n}d" for n in range(1, 10)},
    "red": "rd",
}

# 1600x900 截图区域 (x, y, w, h)
REGIONS_1600x900 = {
    "hand": (0, 710, 1600, 190),       # 底部手牌
    "river_top": (200, 40, 1200, 80),   # 上家牌河
    "river_left": (40, 240, 80, 240),   # 左家牌河
    "river_right": (1480, 240, 80, 240),# 右家牌河
}
