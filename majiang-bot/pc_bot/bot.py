# -*- coding: utf-8 -*-
"""
温州红中放炮麻将：实机只读观察与离线验证。

目标为112张、红中百搭；温麻4人仅供视觉采样。
ADB截图 -> detect-then-classify -> 带来源的未复核观察记录。
当前CLI仅观察，不点击、不生成实时弃牌建议、不写自动战绩。
模板与局数必须用目标场实图验收，测试通过不代表16局实战完成。
"""
import argparse
import logging
import os
import subprocess
import sys
import time
import traceback
import json as _json
_STATS_F=None
def _slog(o):
 global _STATS_F
 try:
  import os,time as _t
  if _STATS_F is None:
   d=os.path.join(os.path.dirname(__file__),'logs');os.makedirs(d,exist_ok=True);_STATS_F=open(os.path.join(d,'stats_%s.jsonl'%_t.strftime('%Y%m%d')),'a',encoding='utf-8')
  _STATS_F.write(_json.dumps(o,ensure_ascii=False)+'\n');_STATS_F.flush()
 except:pass
from collections import Counter
from datetime import datetime
from pathlib import Path

from adb_transport import AdbTransport
from tile_vision import TileVision, NoTemplatesError as VerifiedTemplateError
from runtime_guard import RoundTracker, ActionGate, PendingDiscard, PendingPeng, valid_target_hand, TARGET_IDS
from observation import ObservationRecorder

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# 路径：复用 ../brain_v0/engine（shanten / advisor / scoring，已验证）
# PC 上保持目录结构：majiang-bot/pc_bot/bot.py 与 majiang-bot/brain_v0/engine/
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR.parent / "brain_v0"))

try:
    from engine.advisor import Advisor
    from engine.shanten import calc_shanten
    from engine.tiles import CAISHEN_ID, TILE_NAMES
    ENGINE_OK = True
except Exception as e:  # engine 缺失也不让 bot 直接崩，给出明确提示
    ENGINE_OK = False
    ENGINE_IMPORT_ERROR = e
    CAISHEN_ID = 55
    TILE_NAMES = {}

# 数据闭环：storage.py (SQLite) + game_log.py (JSONL)，失败不影响打牌
try:
    from engine.storage import save_round, DEFAULT_DB_PATH
    from engine.game_log import GameLogger, new_round_record
    STORAGE_OK = True
except Exception as e:
    STORAGE_OK = False
    STORAGE_IMPORT_ERROR = e

# ---------------------------------------------------------------------------
# 配置（1600x900 实测坐标）
# ---------------------------------------------------------------------------
CONFIG = {
    # ADB
    "adb_serial": "emulator-5554",   # 雷电模拟器默认
    "adb_path": r"D:\leidian\LDPlayer14\adb.exe",                # PC 上 adb 在 PATH 里；否则写绝对路径
    "shot_timeout": 15,               # 截图超时秒
    "tap_timeout": 15,

    # 主循环
    "tick_interval": 0.5,             # 每轮间隔秒
    "max_rounds": 16,                 # 16 局后自动停

    # 手牌区域（1600x900 截图，底部一排）
    "hand_roi": (0, 740, 1600, 900),

    # 固定按钮坐标（1600x900）
    "bujiabei_btn": (480, 640),       # "不加倍"（开局保守，不花钻）
    "next_round_btn": (1330, 830),    # 结算页"下一局"
    "tap_center": (800, 450),         # 取消托管随便点中间

    # 识牌
    "template_dir": str(BASE_DIR / "templates_new"),
    "match_scales": (0.85, 1.0, 1.15),
    "match_threshold": 0.65,
    # 无模板时的兜底： "skip"（不出牌，只看） / "rightmost"（打最右边一张）
    "no_template_fallback": "skip",

    # 手牌计数（轮廓法，不依赖模板，换皮肤也work）
    "count_min_area": 3000,
    "count_max_area": 16000,
    "count_min_aspect": 0.55,         # w/h
    "count_max_aspect": 1.0,

    # 托管检测（实验性）：中央青色机器人图标像素数阈值
    "tuoguan_check": False,
    "tuoguan_cyan_pixels": 4000,

    # 动作按钮检测（碰/杠/胡/过）：右下区域
    "action_btn_roi": (1050, 550, 1600, 750),  # (x1,y1,x2,y2)
    "action_btn_templates": {
        "hu": "btn_hu.png",      # 胡：必点
        "gang": "btn_gang.png",  # 杠：点
        "peng": "btn_peng.png",  # 碰：点（v1 保守策略）
        "guo": "btn_guo.png",    # 过：最后才点
    },
    "action_btn_threshold": 0.8,

    "tile_template_dir": str(BASE_DIR / "templates_hongzhong_v2"),
    "control_template_dir": str(BASE_DIR / "templates_controls_v2"),
    "capture_dir": None,
    "capture_interval": 2.0,
    "duration": 0,
    "log_dir": str(BASE_DIR / "logs"),
}

# 牌 id 扩展（含字牌/花牌；engine 原生只到红中 55）
TILE_NAMES_EX = dict(TILE_NAMES)
TILE_NAMES_EX.update({
    61: "东", 62: "南", 63: "西", 64: "北",
    65: "中", 66: "發", 67: "白", 70: "花",
})
HONOR_IDS = {61, 62, 63, 64, 65, 66, 67}
FLOWER_ID = 70
WILD_ID = CAISHEN_ID  # 55，红中财神

# 模板文件名 -> 牌 id（新皮肤模板放 templates_new/ 下按此命名）
TEMPLATE_NAME_TO_ID = {}
for _n in range(1, 10):
    TEMPLATE_NAME_TO_ID[f"w{_n}"] = _n            # x万
    TEMPLATE_NAME_TO_ID[f"t{_n}"] = 20 + _n       # x筒
    TEMPLATE_NAME_TO_ID[f"s{_n}"] = 40 + _n       # x条
TEMPLATE_NAME_TO_ID.update({
    "hongzhong": 55, "caishen": 55,
    "dong": 61, "nan": 62, "xi": 63, "bei": 64,
    "zhong": 65, "fa": 66, "bai": 67, "hua": 70,
})


def tile_name(tid):
    return TILE_NAMES_EX.get(int(tid), f"?{tid}")


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def setup_logger():
    log_dir = Path(CONFIG["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"bot_{ts}.log"

    logger = logging.getLogger("majong_bot")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    logger.info(f"日志文件: {log_file}")
    return logger, log_dir


LOG, LOG_DIR = setup_logger()


def save_error_shot(png_bytes, tag="err"):
    """异常时把当时的截图存下来，方便复盘。"""
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        p = LOG_DIR / f"{tag}_{ts}.png"
        p.write_bytes(png_bytes)
        LOG.info(f"已保存异常截图: {p}")
    except Exception as e:
        LOG.warning(f"保存异常截图失败: {e}")


# ---------------------------------------------------------------------------
# ADB 封装（直连模拟器，不经过 pc-server，少一跳更稳）
# ---------------------------------------------------------------------------
class ADB(AdbTransport):
    def __init__(self, serial=None, adb_path=None):
        super().__init__(serial or CONFIG['adb_serial'], adb_path or CONFIG['adb_path'])
    def check_connection(self):
        connected = self.is_connected()
        return connected, self.serial if connected else 'device is not online'
    def screencap(self, retries=None):
        return self.capture_png()


def decode_png(png_bytes):
    arr = np.frombuffer(png_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("PNG 解码失败")
    return img


# ---------------------------------------------------------------------------
# 识牌
# ---------------------------------------------------------------------------
NoTemplatesError = VerifiedTemplateError


class TileRecognizer(TileVision):
    """Detect real tile boxes, then classify verified colour glyphs."""
    def __init__(self, template_dir=None):
        super().__init__(template_dir or CONFIG['tile_template_dir'])

# ---------------------------------------------------------------------------
# 对局状态（尽量用"张数变化"这种不依赖模板的信号）
# ---------------------------------------------------------------------------
class GameState(RoundTracker):
    pass


# ---------------------------------------------------------------------------
# Brain：Advisor + 字牌 heuristic
# ---------------------------------------------------------------------------
class Brain:
    """Reuse the verified 112-tile, red-Zhong wildcard decision engine."""
    def __init__(self):
        if not ENGINE_OK:
            raise RuntimeError(f'决策引擎加载失败: {ENGINE_IMPORT_ERROR}')
        self.advisor = Advisor()
        LOG.info('Brain: 温州红中放炮本地引擎已加载')
    def decide(self, hand, open_meld_count=0, turn_no=1, played_counts=None):
        if not valid_target_hand(hand, open_meld_count):
            raise ValueError('牌面不是已确认的目标玩法合法手牌')
        if calc_shanten(hand, open_meld_count=open_meld_count) == -1:
            raise ValueError('已胡牌，禁止生成弃牌动作')
        result = self.advisor.local_advise(hand, played_counts or {}, turn_no,
                                           open_meld_count=open_meld_count)
        options = [row for row in result['options'] if row['tile'] in hand]
        if any(t != WILD_ID for t in hand):
            options = [row for row in options if row['tile'] != WILD_ID]
        if not options:
            raise ValueError('本地引擎没有合法弃牌候选')
        return int(options[0]['tile'])
    def shanten_of(self, hand, open_meld_count=0):
        return calc_shanten(hand, open_meld_count=open_meld_count)


# ---------------------------------------------------------------------------
# Bot 主循环
# ---------------------------------------------------------------------------
class Bot:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.adb = ADB()
        self.recognizer = TileRecognizer()
        self.state = GameState()
        self.brain = Brain()
        self.running = True
        self._hand_gate = ActionGate()
        self._button_gate = ActionGate()
        self._pending_discard = None
        self._pending_meld = None
        self._cur_round_plays = []
        self._cur_round_start = None
        self._cur_round_no = 0
        self._cur_starting_hand = None
        self._game_logger = None
        self._last_observed_hand = None
        self._last_frame = None
        self._last_frame_is_candidate = False
        self._observation_status = None
        self.stop_reason = None
        self.started_at = time.monotonic()
        self.capture_dir = Path(CONFIG['capture_dir']) if CONFIG['capture_dir'] else None
        if self.capture_dir:
            self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.recorder = (ObservationRecorder(self.capture_dir, self.adb.serial,
                                            CONFIG['capture_interval'])
                         if self.capture_dir else None)
        if STORAGE_OK and not self.dry_run:
            self._game_logger = GameLogger(str(LOG_DIR / ('rounds_%s.jsonl' % time.strftime('%Y%m%d'))))

    # -- 小动作 ------------------------------------------------------------
    def do_tap(self, x, y, why=""):
        if self.dry_run:
            LOG.info(f"[dry-run] 跳过点击 ({x},{y}) {why}")
            return
        self.adb.tap(x, y)
        LOG.info(f"点击 ({x},{y}) {why}")

    def check_action_buttons(self, img):
        # Only manually verified full control templates may authorize a click.
        x0,y0,x1,y1 = CONFIG['action_btn_roi']
        gray = cv2.cvtColor(img[y0:y1,x0:x1], cv2.COLOR_BGR2GRAY)
        for action in ('hu','gang','peng','guo'):
            path = Path(CONFIG['control_template_dir']) / ('btn_%s.png' % action)
            if not path.is_file():
                continue
            tmpl = cv2.imdecode(np.fromfile(str(path),np.uint8),cv2.IMREAD_GRAYSCALE)
            if tmpl is None or any(a>b for a,b in zip(tmpl.shape,gray.shape)):
                raise ValueError(f'动作模板不可用: {path}')
            _,score,_,pos = cv2.minMaxLoc(cv2.matchTemplate(gray,tmpl,cv2.TM_CCOEFF_NORMED))
            if score >= .96:
                return action,x0+pos[0]+tmpl.shape[1]//2,y0+pos[1]+tmpl.shape[0]//2
        return None

    def check_xiayiju(self, img):
        # Reject the old in-game (1069,676) and lobby (943,732) false positives.
        x0,y0,x1,y1 = 1120,740,1490,885
        path = Path(CONFIG['control_template_dir']) / 'btn_xiayiju.png'
        if not path.is_file():
            return None
        roi = img[y0:y1,x0:x1]
        gray = cv2.cvtColor(roi,cv2.COLOR_BGR2GRAY)
        tmpl = cv2.imdecode(np.fromfile(str(path),np.uint8),cv2.IMREAD_GRAYSCALE)
        if tmpl is None or any(a>b for a,b in zip(tmpl.shape,gray.shape)):
            raise ValueError(f'下一局模板不可用: {path}')
        _,score,_,pos = cv2.minMaxLoc(cv2.matchTemplate(gray,tmpl,cv2.TM_CCOEFF_NORMED))
        # The captured settlement dims the table; a bright live table is not settlement.
        dim_table = float(cv2.cvtColor(img[150:700,300:1250],cv2.COLOR_BGR2GRAY).mean()) < 95
        if score >= .96 and dim_table:
            return x0+pos[0]+tmpl.shape[1]//2,y0+pos[1]+tmpl.shape[0]//2
        return None

    def find_tile_box(self, recognized, tile_id):
        """在识别结果里找第一张 tile_id 的框，返回中心点。"""
        for tid, x1, y1, x2, y2 in recognized:
            if tid == tile_id:
                return (x1 + x2) // 2, (y1 + y2) // 2
        return None

    def tap_tile_by_index(self, boxes, index):
        """兜底：按从左到右第 index 张的框中心点。"""
        if 0 <= index < len(boxes):
            x1, y1, x2, y2 = boxes[index]
            self.do_tap((x1 + x2) // 2, (y1 + y2) // 2, "按序号出牌(兜底)")
            return True
        return False

    # -- 一轮 --------------------------------------------------------------
    def tick(self):
        tick_start = time.monotonic()
        self._last_frame_is_candidate = False
        img = self.adb.capture()
        self._last_frame = img
        if img.shape[:2] != (900,1600):
            raise ValueError(f'尚未标定此分辨率: {img.shape[:2]}')
        count,boxes = self.recognizer.count_tiles(img)
        next_point = self.check_xiayiju(img)
        self._last_frame_is_candidate = len(boxes) >= 3 or next_point is not None
        recognized, hand, complete, diagnostics = [], [], False, []
        if count and next_point is None:
            recognized = self.recognizer.recognize_hand(img)
            hand = [t for t,*_ in recognized]
            complete = self.recognizer.last_complete
            diagnostics = self.recognizer.diagnostics
        tracked_count = count if complete and set(hand) <= TARGET_IDS else 0
        event,my_turn = self.state.update(tracked_count,settlement=next_point is not None)
        LOG.info(f'[TICK] count={count} classified={len(hand)} complete={complete} tracked={tracked_count} my_turn={my_turn} event={event} observe={self.dry_run}')
        if event == 'round_start':
            self._cur_round_plays = []
            self._cur_round_start = time.strftime('%Y-%m-%d %H:%M:%S')
            self._cur_round_no = self.state.rounds_started
            self._cur_starting_hand = None
            self._pending_discard = self._pending_meld = None
            self._hand_gate.reset(); self._button_gate.reset()
            LOG.info(f'===== 第 {self.state.rounds_started} 局开始 =====')
        if event == 'round_end':
            LOG.info(f'===== 第 {self.state.rounds_done} 局结束 =====')
            self._pending_discard = self._pending_meld = None
            if STORAGE_OK and not self.dry_run and self._cur_round_start:
                record = {'started_at':self._cur_round_start,'ended_at':time.strftime('%Y-%m-%d %H:%M:%S'),
                          'self_score':None,'is_win':None,'result':'unknown',
                          'rounds_played':self.state.rounds_done,'note':'confirmed_bot_actions_only'}
                rid = save_round(record,self._cur_round_plays)
                LOG.info(f'[storage] round_id={rid} confirmed_plays={len(self._cur_round_plays)}')
        # The round limit must precede every possible next-round click.
        if self.state.rounds_done >= CONFIG['max_rounds']:
            LOG.info(f'已确认完成 {self.state.rounds_done} 局，停止，不点下一局。')
            self.running = False
            self.stop_reason = 'candidate_round_limit'
        elif next_point is not None:
            if self.state.rounds_started and not self.dry_run:
                key = ('next',self.state.rounds_done,next_point)
                if self._button_gate.ready(key):
                    self.do_tap(*next_point,'下一局（已验证结算画面）')
                    self._button_gate.consume(key)
        elif self.state.active:
            if complete and hand != self._last_observed_hand:
                LOG.info('识牌: '+' '.join(tile_name(t) for t in hand))
                self._last_observed_hand = hand
            if self._pending_discard is not None:
                pending = self._pending_discard
                if complete and pending.confirmed(hand):
                    self._cur_round_plays.append({'tile_id':pending.choice,'tile_name':tile_name(pending.choice),
                        'player':'bottom','turn_no':self.state.turn_no,'timestamp':time.strftime('%Y-%m-%d %H:%M:%S')})
                    LOG.info(f'出牌已确认: {tile_name(pending.choice)}')
                    self.state.turn_no += 1
                    self._pending_discard = None
                    self._hand_gate.reset()
                elif time.monotonic()-pending.created_at > 4:
                    raise RuntimeError('点击后4秒未确认弃牌，停止避免重复误点')
                elif complete and tuple(hand)==pending.hand and pending.attempts==1 and time.monotonic()-pending.created_at>1:
                    self.do_tap(*pending.point,'同一牌面第二次点击')
                    pending.attempts += 1
            elif complete:
                # A meld is confirmed only by the observed result of our requested action.
                if self._pending_meld is not None:
                    expected = 14-3*(self.state.open_meld_count+1)
                    if count == expected and self._pending_meld.confirmed(hand):
                        self.state.open_meld_count += 1
                        LOG.info(f'副露已确认: {self._pending_meld} fixed_melds={self.state.open_meld_count}')
                        self._pending_meld = None
                        my_turn = True
                    elif time.monotonic()-self._pending_meld.created_at>6:
                        raise RuntimeError('碰杠点击后未确认副露变化，停止')
                if self._pending_meld is not None:
                    LOG.info('等待碰牌结果确认，不发新动作')
                elif valid_target_hand(hand,self.state.open_meld_count):
                    action = self.check_action_buttons(img)
                    if action is not None and not self.dry_run:
                        name,x,y = action
                        key = ('action',name,tuple(hand),x,y)
                        if name == 'gang':
                            LOG.warning('杠牌类型未确认，保持观察，不把补杠计为新增副露')
                        elif self._button_gate.ready(key):
                            self.do_tap(x,y,name)
                            self._button_gate.consume(key)
                            if name == 'peng':
                                self._pending_meld = PendingPeng(tuple(hand),time.monotonic())
                    elif my_turn and not self.dry_run:
                        key = ('discard',self.state.rounds_started,self.state.open_meld_count,tuple(hand))
                        if self._hand_gate.ready(key):
                            self.play_turn(img,None,count,boxes)
                            self._hand_gate.consume(key)
                else:
                    LOG.warning('完整识牌不符合目标红中玩法/已确认副露，保持观察')
            else:
                LOG.warning('识牌未完整确认，禁止点击: '+str([(d['label'],round(d['score'],3),d['reason']) for d in self.recognizer.diagnostics if not d['accepted']]))
        if self.capture_dir:
            self.recorder.record(img, boxes=boxes, predictions=diagnostics,
                recognized=hand, complete=complete, rounds_started=self.state.rounds_started,
                rounds_done=self.state.rounds_done, event=event, settlement=next_point is not None)
            status = {'timestamp':datetime.now().isoformat(),'serial':self.adb.serial,'observe_only':self.dry_run,
                      'rounds_started':self.state.rounds_started,'rounds_done':self.state.rounds_done,
                      'round_counts_verified':False,
                      'count':count,'running':self.running,'last_hand':hand,
                      'recognition_complete':bool(complete),
                      'session_id':self.recorder.session_id,'saved_frames':self.recorder.saved_frames,
                      'observation_log':self.recorder.log_path.name,
                      'capture_ms':round((time.monotonic()-tick_start)*1000,1)}
            self._write_status(status)

    def _write_status(self, status=None):
        if not self.capture_dir:
            return
        if status is not None:
            self._observation_status = status
        if self._observation_status is None:
            self._observation_status = {'serial':self.adb.serial,'observe_only':self.dry_run,
                'round_counts_verified':False,'count':None,'last_hand':[],
                'recognition_complete':False,'session_id':self.recorder.session_id,
                'saved_frames':self.recorder.saved_frames,
                'observation_log':self.recorder.log_path.name}
        self._observation_status.update(timestamp=datetime.now().isoformat(),running=self.running,
            stop_reason=self.stop_reason,rounds_started=self.state.rounds_started,
            rounds_done=self.state.rounds_done)
        encoded=_json.dumps(self._observation_status,ensure_ascii=False,indent=2)
        # Independent observer sessions must not share a temporary filename.
        session=self.recorder.session_id
        temp=self.capture_dir/f'status_{session}.tmp'
        temp.write_text(encoded,encoding='utf-8')
        temp.replace(self.capture_dir/f'status_{session}.json')
        latest=self.capture_dir/f'latest_status_{session}.tmp'
        latest.write_text(encoded,encoding='utf-8')
        latest.replace(self.capture_dir/'status.json')

    def play_turn(self, img, png, count, boxes):
        recognized = self.recognizer.recognize_hand(img)
        hand = [t for t,*_ in recognized]
        if not self.recognizer.last_complete or not valid_target_hand(hand,self.state.open_meld_count):
            raise ValueError('出牌前牌面校验失败')
        if self.brain.shanten_of(hand,self.state.open_meld_count) == -1:
            LOG.info('已胡手牌：保持牌面，等待可靠胡按钮或人工操作')
            return
        t0=time.monotonic()
        choice = self.brain.decide(hand,self.state.open_meld_count,self.state.turn_no)
        LOG.info(f'决策: 打 {tile_name(choice)} decision_ms={(time.monotonic()-t0)*1000:.1f}')
        if self._cur_starting_hand is None:
            self._cur_starting_hand = hand.copy()
        if not self.dry_run:
            point=self.find_tile_box(recognized,choice)
            if point is None:
                raise ValueError('合法决策未找到对应牌框')
            self.do_tap(*point,f'出 {tile_name(choice)}')
            self._pending_discard = PendingDiscard(tuple(hand),choice,point,time.monotonic())

    # -- 跑 ----------------------------------------------------------------
    def run(self):
        try:
            ok,info=self.adb.check_connection()
            if not ok:
                LOG.error(f'ADB 未在线: {info}')
                self.stop_reason = 'connection_unavailable'
                return 1
            self.recognizer.load_templates()
            LOG.info(f'启动温州红中放炮循环 serial={self.adb.serial} observe_only={self.dry_run} max_rounds={CONFIG["max_rounds"]}')
            missing=[name for name in ('hu','gang','peng','guo') if not (Path(CONFIG['control_template_dir'])/f'btn_{name}.png').is_file()]
            if missing:
                LOG.warning(f'尚缺目标场动作模板: {missing}；对应自动动作未启用')
            while self.running:
                if CONFIG['duration'] and time.monotonic()-self.started_at>=CONFIG['duration']:
                    LOG.info('达到观察时长，结束')
                    self.stop_reason = 'duration_limit'
                    break
                t0=time.monotonic()
                try:
                    self.tick()
                except Exception:
                    LOG.error('运行失败，停止循环:'+traceback.format_exc())
                    if self._last_frame is not None and self._last_frame_is_candidate:
                        save_error_shot(self.adb.capture_png(self._last_frame),'runtime_guard')
                    self.stop_reason = 'runtime_error'
                    return 1
                time.sleep(max(0,CONFIG['tick_interval']-(time.monotonic()-t0)))
        except KeyboardInterrupt:
            LOG.info('用户停止')
            self.stop_reason = 'keyboard_interrupt'
        except Exception:
            LOG.error('启动失败:'+traceback.format_exc())
            self.stop_reason = 'startup_error'
            return 1
        finally:
            self.running=False
            try:
                self._write_status()
            finally:
                self.adb.close()
        LOG.info(f'结束：开始{self.state.rounds_started}局，已确认完成{self.state.rounds_done}局')
        return 0


def main():
    ap = argparse.ArgumentParser(description="温州麻将 PC 自动代打 bot")
    ap.add_argument("--once", action="store_true", help="只截一张图，数手牌并尝试识牌，然后退出")
    ap.add_argument("--dry-run", action="store_true", help="全流程跑，但不点屏幕")
    ap.add_argument("--serial", default=None, help="ADB 序列号，默认 emulator-5554；启动前确认设备")
    ap.add_argument("--max-rounds", type=int, default=None, help="打几局后停，默认 16")
    ap.add_argument("--observe-only", action="store_true", help="只读观察，不点击、不写自动战绩")
    ap.add_argument("--capture-dir", default=None, help="真实对局截图与观察状态目录")
    ap.add_argument("--duration", type=int, default=0, help="观察秒数，0为不限时")
    ap.add_argument("--adb", default=None, help="显式指定ADB可执行文件")
    args = ap.parse_args()
    if args.max_rounds is not None and args.max_rounds <= 0:
        ap.error('--max-rounds 必须大于0')
    if args.duration < 0:
        ap.error('--duration 不能小于0')

    if args.serial:
        CONFIG["adb_serial"] = args.serial
    if args.max_rounds:
        CONFIG["max_rounds"] = args.max_rounds

    CONFIG["duration"] = args.duration
    CONFIG["capture_dir"] = args.capture_dir
    if args.adb:
        CONFIG["adb_path"] = args.adb
    # Current target has RMB settlement; this entrypoint only observes.
    # Autonomous real-money gameplay is not launched by this tool.
    bot = Bot(dry_run=True)

    if args.once:
        try:
            ok, info = bot.adb.check_connection()
            print("ADB:", "OK" if ok else "FAIL", info)
            if not ok:
                return 1
            png = bot.adb.screencap()
            img = decode_png(png)
            count, boxes = bot.recognizer.count_tiles(img)
            print(f"候选牌张数: {count}")
            rec = bot.recognizer.recognize_hand(img)
            print("识别:", " ".join(tile_name(t) for t, *_ in rec))
            print("全部候选已分类:", bot.recognizer.last_complete)
            if len(boxes) >= 3:
                path = LOG_DIR / ('once_%s.png' % datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
                path.write_bytes(png)
                print(f"候选牌列截图已存: {path}")
            return 0
        except NoTemplatesError as e:
            print("识牌:", e)
            return 1
        finally:
            bot.running = False
            bot.stop_reason = 'once_finished'
            try:
                bot._write_status()
            finally:
                bot.adb.close()

    return bot.run()


if __name__ == "__main__":
    sys.exit(main())
