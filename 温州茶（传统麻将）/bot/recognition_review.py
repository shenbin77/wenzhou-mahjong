"""Live-follow review of the assistant's captured frames and predictions."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import simpledialog, ttk

from PIL import Image, ImageDraw, ImageTk

from recognition_audit import append_review, read_appended_events


FLAG_NAMES = {
    "analysis_error": "识别过程报错",
    "abstain": "已弃权",
    "no_tile_evidence": "没有逐牌识别证据",
    "unrecognized_tile": "有未识别的牌",
    "near_acceptance_boundary": "有临界分数的牌",
    "unexpected_hand_count": "手牌数量异常",
    "more_than_four": "同一种牌超过 4 张",
}


def draw_recognition_boxes(image: Image.Image, event: dict) -> Image.Image:
    """Paint the same recognition evidence in the live and history views."""
    marked = image.convert("RGB")
    draw = ImageDraw.Draw(marked)
    suggested = {tuple(box) for box in event.get("suggested_boxes") or []
                 if len(box) == 4}
    for index, item in enumerate(event.get("diagnostics") or [], 1):
        box = item.get("box") or []
        if len(box) != 4:
            continue
        color = ("#d62828" if tuple(box) in suggested else
                 "#1a9d38" if item.get("accepted") else "#ef8a00")
        draw.rectangle(tuple(box), outline=color, width=4)
        x0, y0 = int(box[0]), int(box[1])
        label = f"{index}:{item.get('label') or '?'}"
        draw.text((x0 + 2, y0 + 2), label, fill=color, stroke_width=1,
                  stroke_fill="black")
    return marked


class RecognitionReview:
    def __init__(self, parent: tk.Misc, session_dir: str | Path):
        self.session_dir = Path(session_dir)
        self.window = tk.Toplevel(parent)
        self.window.title("历史逐帧复核 · 可跟随最新帧")
        self.window.geometry("1120x760")
        self.window.minsize(760, 580)
        self.events: list[dict] = []
        self.reviews: dict[str, dict] = {}
        self.audit_offset = 0
        self.review_offset = 0
        self.index = -1
        self.follow = tk.BooleanVar(value=True)
        self.summary = tk.StringVar(value="等待辅助器记录新画面……")
        self.details = tk.StringVar(value="")
        self.photo: ImageTk.PhotoImage | None = None
        self.timer: str | None = None

        controls = ttk.Frame(self.window, padding=6)
        controls.pack(fill="x")
        ttk.Button(controls, text="上一帧", command=lambda: self.move(-1)).pack(side="left")
        ttk.Button(controls, text="下一帧", command=lambda: self.move(1)).pack(side="left")
        ttk.Button(controls, text="下个疑点", command=self.next_flagged).pack(side="left")
        ttk.Checkbutton(controls, text="跟随最新画面", variable=self.follow,
                        command=self.refresh).pack(side="left", padx=8)
        ttk.Button(controls, text="整帧识牌正确", command=lambda: self.mark("correct")).pack(side="right")
        ttk.Button(controls, text="识牌有错", command=lambda: self.mark("wrong")).pack(side="right")
        ttk.Label(self.window, textvariable=self.summary, wraplength=1070).pack(
            fill="x", padx=8, pady=(0, 4))
        self.picture = ttk.Label(self.window, anchor="center")
        self.picture.pack(fill="both", expand=True, padx=8)
        ttk.Label(self.window, textvariable=self.details, wraplength=1070,
                  justify="left").pack(fill="x", padx=8, pady=6)
        self.window.bind("<Left>", lambda _event: self.move(-1))
        self.window.bind("<Right>", lambda _event: self.move(1))
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def refresh(self) -> None:
        if not self.window.winfo_exists():
            return
        added, self.audit_offset = read_appended_events(
            self.session_dir / "recognition_audit.jsonl", self.audit_offset)
        self.events.extend(added)
        reviewed, self.review_offset = read_appended_events(
            self.session_dir / "recognition_reviews.jsonl", self.review_offset)
        self.reviews.update({row["frame"]: row for row in reviewed})
        if self.follow.get() and self.events:
            self.index = len(self.events) - 1
        elif self.index >= len(self.events):
            self.index = len(self.events) - 1
        self.show()
        if self.timer is not None:
            self.window.after_cancel(self.timer)
        self.timer = self.window.after(1000, self.refresh)

    def move(self, offset: int) -> None:
        if not self.events:
            return
        self.follow.set(False)
        self.index = max(0, min(len(self.events) - 1, self.index + offset))
        self.show()

    def next_flagged(self) -> None:
        self.follow.set(False)
        for index in range(self.index + 1, len(self.events)):
            if self.events[index].get("flags"):
                self.index = index
                self.show()
                return
        self.summary.set("后面没有自动标出的疑点；仍需人工检查未标记画面。")

    def mark(self, verdict: str) -> None:
        if not 0 <= self.index < len(self.events):
            return
        corrected = ""
        if verdict == "wrong":
            answer = simpledialog.askstring(
                "识牌纠错", "实际手牌（可留空；建议按画面从左到右填写）：",
                parent=self.window)
            if answer is None:
                return
            corrected = answer
        append_review(self.session_dir, self.events[self.index], verdict, corrected)
        self.reviews[self.events[self.index]["frame"]] = {
            "verdict": verdict, "corrected_hand": corrected}
        self.show()
        if self.index + 1 < len(self.events):
            self.move(1)

    def show(self) -> None:
        if not 0 <= self.index < len(self.events):
            self.summary.set("等待辅助器记录新画面……")
            self.details.set("原始截图和逐帧识牌结果会自动保存在本次会话目录。")
            return
        event = self.events[self.index]
        reviewed = self.reviews.get(event["frame"], {})
        flags = "、".join(FLAG_NAMES.get(flag, flag) for flag in event.get("flags", [])) or "无自动疑点"
        verdict = {"correct": "人工确认正确", "wrong": "人工标记错误"}.get(
            reviewed.get("verdict"), "尚未人工复核")
        self.summary.set(
            f"{self.index + 1}/{len(self.events)}  {event.get('at', '')}  "
            f"{event.get('mode', '')}  {event.get('status', '')}  {verdict}  "
            f"已复核 {sum(row['frame'] in self.reviews for row in self.events)}/{len(self.events)}  "
            f"疑点：{flags}" +
            (f"  录像约第 {event['approx_second']} 秒" if "approx_second" in event else ""))
        self.details.set(
            "识牌：" + (" ".join(event.get("hand") or []) or "无") +
            "\n原因：" + str(event.get("reason") or "—") +
            "\n绿色=已接受预测，橙色=未识别，红色=建议牌；自动标记只能提示可疑。")
        try:
            with Image.open(event["frame"]) as original:
                image = draw_recognition_boxes(original, event)
        except OSError as exc:
            self.picture.configure(image="", text=f"原图无法读取：{exc}")
            self.photo = None
            return
        available_w = max(720, self.window.winfo_width() - 40)
        available_h = max(350, self.window.winfo_height() - 190)
        image.thumbnail((available_w, available_h), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(image)
        self.picture.configure(image=self.photo, text="")

    def close(self) -> None:
        if self.timer is not None:
            self.window.after_cancel(self.timer)
            self.timer = None
        self.window.destroy()
