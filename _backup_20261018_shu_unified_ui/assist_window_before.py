"""温州茶截图助手 - 极简版
点一下按钮，截图，认牌，给建议。
"""
import tkinter as tk
from tkinter import messagebox
from pathlib import Path
import sys

BOT_DIR = Path("D:/温州麻将/温州茶（传统麻将）/bot")
sys.path.insert(0, str(BOT_DIR))

class AssistWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("温州茶助手")
        self.root.geometry("400x320")
        self.caishen = tk.StringVar(value="bei")

        tk.Label(self.root, text="温州茶截图助手", font=("微软雅黑", 16)).pack(pady=10)

        # 财神选择
        f1 = tk.Frame(self.root)
        f1.pack(pady=5)
        tk.Label(f1, text="财神:").pack(side=tk.LEFT)
        for v, t in [("bei", "北"), ("dong", "东"), ("nan", "南"), ("xi", "西")]:
            tk.Radiobutton(f1, text=t, variable=self.caishen, value=v).pack(side=tk.LEFT)

        # 截图分析按钮
        tk.Button(self.root, text="📷 截图分析", font=("微软雅黑", 14),
                  command=self.analyze, width=20, height=2).pack(pady=10)

        # 结果显示
        self.result = tk.Label(self.root, text="等你截图", font=("微软雅黑", 12),
                               wraplength=350, justify=tk.LEFT)
        self.result.pack(pady=10)

    def analyze(self):
        try:
            from tile_vision import detect_hand
            from tea_bot import TeaBot, vision_to_engine, tile_name
            import subprocess
            import cv2
            import numpy as np

            # ADB 截图
            r = subprocess.run(
                ["C:/leidian/LDPlayer14/adb.exe", "-s", "emulator-5556",
                 "exec-out", "screencap", "-p"],
                capture_output=True, timeout=10)
            if r.returncode != 0:
                raise RuntimeError("截图失败")

            # 解码并裁剪下方 1/3 ROI（手牌区，阿法也是这么干的）
            img = cv2.imdecode(np.frombuffer(r.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                raise RuntimeError("截图解码失败")
            h = img.shape[0]
            roi = img[int(h * 2 / 3):, :]
            ok, buf = cv2.imencode(".png", roi)
            if not ok:
                raise RuntimeError("ROI 编码失败")

            # 认牌
            hand_vision = detect_hand(buf.tobytes())
            n = len(hand_vision)
            if n == 0:
                self.result.config(text="没认出牌，换个时机再试")
                return
            if n not in (16, 17):
                self.result.config(
                    text=f"认出 {n} 张（应为16/17张），可能误认\n"
                         f"识别: {' '.join(tile_name(t) for t in hand_vision)}")
                return

            # 转引擎编码并算牌
            hand = [vision_to_engine(t) for t in hand_vision]

            # 财神
            caishen_map = {"dong": 61, "nan": 62, "xi": 63, "bei": 64}
            bot = TeaBot(caishen_id=caishen_map.get(self.caishen.get()))
            think = bot.think(hand)

            tiles_str = " ".join(tile_name(t) for t in hand_vision)
            suggest = tile_name(think.discard) if think.discard else "?"

            self.result.config(
                text=f"手牌({n}张): {tiles_str}\n向听: {think.shanten}\n建议打: {suggest}")
        except Exception as e:
            messagebox.showerror("出错", str(e))

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    AssistWindow().run()
