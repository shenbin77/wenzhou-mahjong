"""One local viewer for Wenzhou Tea and Shu. No game input is sent."""

from __future__ import annotations

import json
import os
import tkinter as tk
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageTk

from assist_tea import analyze_manual_hand, capture_and_analyze, import_and_analyze
from assist_shu import (analyze_shu_manual_hand, capture_shu_and_analyze,
                        import_shu_and_analyze)
from harvest_tea import DEFAULT_SERIAL
from tea_bot import tile_name, vision_to_engine
from tile_vision import TARGET_IDS, TILE_IDS


TEA_ROOT = Path(__file__).resolve().parent.parent
TILE_CHOICES = {
    f"{tile_name(vision_to_engine(tid))} ({label})": label
    for label, tid in TILE_IDS.items() if tid in TARGET_IDS
}


def append_feedback(record_path: str | Path, verdict: str, actual_tile: str | None = None) -> Path:
    """Keep user corrections separate from immutable capture results."""
    if verdict not in {"useful", "wrong_advice", "wrong_vision"}:
        raise ValueError("未知反馈类型")
    record = Path(record_path)
    if not record.is_file() or record.suffix != ".json":
        raise FileNotFoundError("本次截图记录不存在")
    event = {
        "at": datetime.now().astimezone().isoformat(),
        "record": record.name,
        "verdict": verdict,
        "actual_tile": actual_tile,
    }
    target = record.parent / "feedback.jsonl"
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return target


class TeaAssistWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("温州麻将 · 茶 / 熟辅助（只建议，不点击）")
        root.geometry("980x780")
        self.session_dir = (
            TEA_ROOT / "采集" / "assist_sessions" /
            datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.future: Future | None = None
        self.future_mode: str | None = None
        self.continuous = False
        self.timer: str | None = None
        self.current: dict | None = None
        self.preview = None
        self.serial = tk.StringVar(value=DEFAULT_SERIAL)
        self.mode = tk.StringVar(value="温州茶 · 2人")
        self.caishen = tk.StringVar()
        self.confirm_mode = tk.BooleanVar(value=False)
        self.actual_tile = tk.StringVar()
        self.status = tk.StringVar(value="选择玩法并确认局数，再抓图或导入截图。")
        self.hand = tk.StringVar(value="手牌：—")
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)

    def _build(self) -> None:
        controls = ttk.Frame(self.root, padding=10)
        controls.pack(fill="x")
        ttk.Label(controls, text="玩法").grid(row=0, column=0, sticky="w")
        mode_choice = ttk.Combobox(
            controls, textvariable=self.mode,
            values=("温州茶 · 2人", "温州熟 · 4人"), state="readonly", width=16,
        )
        mode_choice.grid(row=0, column=1, padx=6)
        mode_choice.bind("<<ComboboxSelected>>", self._on_mode_change)
        ttk.Label(controls, text="模拟器").grid(row=0, column=2, sticky="w")
        ttk.Entry(controls, textvariable=self.serial, width=16).grid(row=0, column=3, padx=6)
        ttk.Label(controls, text="财神牌（茶）").grid(row=0, column=4, sticky="w")
        self.caishen_choice = ttk.Combobox(
            controls, textvariable=self.caishen, values=list(TILE_CHOICES),
            state="readonly", width=19,
        )
        self.caishen_choice.grid(row=0, column=5, padx=6)
        self.confirm_choice = ttk.Checkbutton(
            controls, text="已确认是 2 人温州茶", variable=self.confirm_mode,
        )
        self.confirm_choice.grid(row=1, column=0, columnspan=3, padx=6, sticky="w")
        ttk.Button(controls, text="抓图并分析", command=self.capture).grid(
            row=2, column=0, columnspan=2, pady=8, sticky="ew",
        )
        ttk.Button(controls, text="导入截图", command=self.import_image).grid(
            row=2, column=2, pady=8, sticky="ew",
        )
        ttk.Button(controls, text="修正手牌并重算", command=self.manual_correction).grid(
            row=2, column=3, pady=8, sticky="ew",
        )
        ttk.Button(controls, text="连续观察（约 3 秒一次）", command=self.start).grid(
            row=3, column=0, columnspan=2, pady=8, sticky="ew",
        )
        ttk.Button(controls, text="停止", command=self.stop).grid(
            row=3, column=2, pady=8, sticky="ew",
        )
        ttk.Label(controls, text="熟：红中赖子；只依据手牌，未识别河牌风险").grid(
            row=3, column=3, columnspan=3, sticky="w",
        )
        ttk.Label(
            self.root, textvariable=self.status, padding=10,
            font=("Microsoft YaHei UI", 16, "bold"), wraplength=900,
        ).pack(fill="x")
        ttk.Label(
            self.root, textvariable=self.hand, padding=(10, 0, 10, 8),
            wraplength=900,
        ).pack(fill="x")
        self.picture = ttk.Label(self.root, anchor="center")
        self.picture.pack(fill="both", expand=True, padx=10, pady=5)
        bottom = ttk.Frame(self.root, padding=10)
        bottom.pack(fill="x")
        ttk.Label(bottom, text="我实际打的牌").grid(row=0, column=0, padx=4)
        self.actual_choice = ttk.Combobox(
            bottom, textvariable=self.actual_tile, state="readonly", width=19,
        )
        self.actual_choice.grid(row=0, column=1, padx=4)
        ttk.Button(bottom, text="建议可用", command=lambda: self.feedback("useful")).grid(
            row=0, column=2, padx=4,
        )
        ttk.Button(bottom, text="建议不对", command=lambda: self.feedback("wrong_advice")).grid(
            row=0, column=3, padx=4,
        )
        ttk.Button(bottom, text="识牌错误", command=lambda: self.feedback("wrong_vision")).grid(
            row=0, column=4, padx=4,
        )
        ttk.Button(bottom, text="打开记录目录", command=self.open_folder).grid(
            row=0, column=5, padx=4,
        )
        ttk.Label(
            bottom, text=f"本地记录：{self.session_dir}", wraplength=900,
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=8)

    def _on_mode_change(self, _event=None) -> None:
        self.stop()
        self.confirm_mode.set(False)
        is_tea = self.mode.get() == "温州茶 · 2人"
        self.caishen_choice.configure(state="readonly" if is_tea else "disabled")
        self.confirm_choice.configure(
            text="已确认是 2 人温州茶" if is_tea else "已确认是 4 人温州熟",
        )
        self.current = None
        self.status.set("已切换玩法。请重新确认当前对局。")

    def capture(self) -> None:
        if self.future is not None:
            return
        if not self.confirm_mode.get():
            self.status.set("先确认当前对局玩法，再抓图。")
            return
        label = TILE_CHOICES.get(self.caishen.get(), "")
        self.status.set("正在抓图和分析……")
        self.future_mode = self.mode.get()
        if self.mode.get() == "温州茶 · 2人":
            self.future = self.executor.submit(
                capture_and_analyze, serial=self.serial.get().strip(),
                session_dir=self.session_dir, caishen_label=label,
                confirmed_two_player=self.confirm_mode.get(),
            )
        else:
            self.future = self.executor.submit(
                capture_shu_and_analyze, serial=self.serial.get().strip(),
                session_dir=self.session_dir,
                confirmed_four_player=self.confirm_mode.get(),
            )
        self.root.after(100, self._poll)

    def import_image(self) -> None:
        if self.future is not None:
            return
        selected = filedialog.askopenfilename(
            title="导入游戏截图", filetypes=[("截图", "*.png *.jpg *.jpeg")],
        )
        if not selected:
            return
        self.status.set("正在识别导入截图……")
        self.future_mode = self.mode.get()
        if self.mode.get() == "温州茶 · 2人":
            self.future = self.executor.submit(
                import_and_analyze, selected, session_dir=self.session_dir,
                caishen_label=TILE_CHOICES.get(self.caishen.get(), ""),
                confirmed_two_player=self.confirm_mode.get(),
            )
        else:
            self.future = self.executor.submit(
                import_shu_and_analyze, selected, session_dir=self.session_dir,
                confirmed_four_player=self.confirm_mode.get(),
            )
        self.root.after(100, self._poll)

    def manual_correction(self) -> None:
        if self.future is not None:
            return
        labels = " ".join((self.current or {}).get("hand") or [])
        typed = simpledialog.askstring(
            "修正手牌", "按顺序输入牌名，空格分隔，例如 w1 t3 s9 hongzhong",
            initialvalue=labels, parent=self.root,
        )
        if typed is None:
            return
        previous = self.current or {}
        self.status.set("正在按修正手牌重算……")
        self.future_mode = self.mode.get()
        if self.mode.get() == "温州茶 · 2人":
            self.future = self.executor.submit(
                analyze_manual_hand, typed,
                caishen_label=TILE_CHOICES.get(self.caishen.get(), ""),
                confirmed_two_player=self.confirm_mode.get(),
                session_dir=self.session_dir, image_path=previous.get("image"),
                corrected_from=previous.get("record"),
            )
        else:
            self.future = self.executor.submit(
                analyze_shu_manual_hand, typed,
                confirmed_four_player=self.confirm_mode.get(),
                meld_count=previous.get("meld_count", 0),
                session_dir=self.session_dir, image_path=previous.get("image"),
                corrected_from=previous.get("record"),
            )
        self.root.after(100, self._poll)

    def _poll(self) -> None:
        if self.future is None:
            return
        if not self.future.done():
            self.root.after(100, self._poll)
            return
        future, self.future = self.future, None
        try:
            result = future.result()
            if self.future_mode == self.mode.get():
                self._show(result)
            else:
                self.status.set("旧玩法的结果已保存；请按当前玩法重新分析。")
        except Exception as exc:
            self.status.set(f"采集停止：{exc}")
            self.stop()
        if self.continuous:
            self.timer = self.root.after(3000, self.capture)

    def _show(self, result: dict) -> None:
        self.current = result
        status = result["status"]
        if status == "advise":
            suffix = "红框是这张牌。" if result.get("suggested_boxes") else "手工修正后无定位框。"
            self.status.set(f"牌效率建议：打 {result['suggestion']}。{suffix}由你操作。")
        elif status == "hu":
            self.status.set("识别为可胡牌，请你核对游戏按钮和番型。")
        elif status == "wait":
            self.status.set("当前为等待摸牌状态。")
        else:
            self.status.set(f"暂停建议：{result['reason']}")
        labels = result.get("hand") or result.get("recognized") or []
        self.hand.set("手牌：" + (" ".join(labels) if labels else "未完整识别"))
        self.actual_choice["values"] = sorted(set(label for label in labels if label))
        self.actual_tile.set("")
        source_path = result.get("annotation") or result.get("image")
        if source_path and Path(source_path).is_file():
            with Image.open(source_path) as source:
                image = source.convert("RGB")
                image.thumbnail((940, 505))
                self.preview = ImageTk.PhotoImage(image)
            self.picture.configure(image=self.preview)

    def start(self) -> None:
        self.continuous = True
        if self.timer is None and self.future is None:
            self.capture()

    def stop(self) -> None:
        self.continuous = False
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None

    def feedback(self, verdict: str) -> None:
        if self.current is None or not self.current.get("record"):
            messagebox.showinfo("温州茶辅助", "先抓图，生成本次记录。")
            return
        if verdict in {"useful", "wrong_advice"} and self.current["status"] != "advise":
            messagebox.showinfo("温州茶辅助", "本次没有出牌建议。")
            return
        actual = self.actual_tile.get() or None
        path = append_feedback(self.current["record"], verdict, actual)
        self.status.set(f"反馈已存本地：{path.name}")

    def open_folder(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(self.session_dir)

    def close(self) -> None:
        self.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


if __name__ == "__main__":
    app_root = tk.Tk()
    TeaAssistWindow(app_root)
    app_root.mainloop()
