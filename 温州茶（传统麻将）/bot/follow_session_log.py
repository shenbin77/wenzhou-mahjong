# -*- coding: utf-8 -*-
"""Read-only live viewer for follow_session.jsonl gate/abstain/decision lines.

Can embed as a compact strip in the assist main window, or open as a
detached More-menu Toplevel. Never enables claim taps and never writes
to the session logger — it only tails the existing JSONL.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from action_policy import CLAIM_TAP_CHANNEL_READY
from session_logger import (
    FOLLOW_SESSION_FILENAME,
    read_follow_lines,
    resolve_follow_session_path,
)


class FollowSessionLog:
    """Scrollable, read-only panel of the latest follow-session decision lines."""

    REFRESH_MS = 800
    LINE_LIMIT = 250
    EMBED_LINE_LIMIT = 80
    EMBED_TEXT_HEIGHT = 7

    def __init__(
        self,
        parent: tk.Misc,
        session_dir: str | Path,
        *,
        search_roots: list[str | Path] | None = None,
        container: tk.Misc | None = None,
        compact: bool = False,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.search_roots = [Path(p) for p in (search_roots or [])]
        self.path: Path | None = resolve_follow_session_path(
            self.session_dir, search_roots=self.search_roots
        )
        self.compact = bool(compact or container is not None)
        self.line_limit = self.EMBED_LINE_LIMIT if self.compact else self.LINE_LIMIT
        # The operational log always tails the current file.  There is no
        # manual refresh mode in the product UI.
        self.follow = tk.BooleanVar(value=True)
        self.path_var = tk.StringVar(value=self._path_label())
        self.status_var = tk.StringVar(
            value="只读尾随 follow_session.jsonl；不会开启吃碰杠点击通道。"
        )
        self.timer: str | None = None
        # Sentinel so first refresh paints empty-state when no jsonl yet.
        self._last_signature = object()
        self._detach_callback = None
        # The embedded log is a permanent operational console.  It never
        # collapses: account/stage/errors must remain visible during play.
        self._collapsed = False
        self._body_frame = None
        self._path_label_widget = None
        self._status_label_widget = None
        self._collapse_btn = None
        self.window: tk.Toplevel | None
        self.host: tk.Misc

        if container is not None:
            self.window = None
            self.host = container
            self._build_into(container, detached=False)
        else:
            self.window = tk.Toplevel(parent)
            self.window.title("跟随会话日志 · 只读（门控 / 弃权 / 决策）")
            self.window.geometry("780x420")
            self.window.minsize(520, 280)
            self.host = self.window
            self._build_into(self.window, detached=True)
            self.window.protocol("WM_DELETE_WINDOW", self.close)

        self.refresh()

    def _build_into(self, parent: tk.Misc, *, detached: bool) -> None:
        pad = 4 if self.compact else 6
        controls = ttk.Frame(parent, padding=pad)
        controls.pack(fill="x")
        if detached or not self.compact:
            ttk.Button(controls, text="打开会话目录…", command=self.pick_session).pack(
                side="left", padx=4
            )
            ttk.Button(controls, text="清空显示", command=self.clear_view).pack(
                side="left", padx=4
            )
        else:
            ttk.Button(
                controls, text="打开完整日志", command=self._request_detach
            ).pack(side="left", padx=4)
        ttk.Label(
            controls,
            text=("自动动作通道：已就绪" if CLAIM_TAP_CHANNEL_READY
                  else "自动动作通道：未就绪"),
            foreground=("#146c2e" if CLAIM_TAP_CHANNEL_READY else "#c62828"),
        ).pack(side="right")

        wrap = 720 if self.compact else 760
        self._path_label_widget = ttk.Label(
            parent, textvariable=self.path_var, wraplength=wrap, foreground="#555555"
        )
        if not self.compact:
            self._path_label_widget.pack(fill="x", padx=6)

        self._body_frame = ttk.Frame(parent, padding=(6, 2))
        if not self.compact:
            self._body_frame.pack(fill="both", expand=True)
        text_kwargs: dict = dict(
            wrap="none",
            state="disabled",
            font=("Consolas", 9),
            background="#111111",
            foreground="#e8e8e8",
            insertbackground="#e8e8e8",
            relief="flat",
        )
        if self.compact:
            text_kwargs["height"] = self.EMBED_TEXT_HEIGHT
        self.text = tk.Text(self._body_frame, **text_kwargs)
        yscroll = ttk.Scrollbar(
            self._body_frame, orient="vertical", command=self.text.yview
        )
        xscroll = ttk.Scrollbar(
            self._body_frame, orient="horizontal", command=self.text.xview
        )
        self.text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.text.tag_configure("ok", foreground="#66d17a")
        self.text.tag_configure("wait", foreground="#f0b24a")
        self.text.tag_configure("error", foreground="#ff6b6b")
        self.text.tag_configure("normal", foreground="#e8e8e8")
        self.text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        self._body_frame.rowconfigure(0, weight=1)
        self._body_frame.columnconfigure(0, weight=1)

        self._status_label_widget = ttk.Label(
            parent, textvariable=self.status_var, wraplength=wrap
        )
        if not self.compact:
            self._status_label_widget.pack(fill="x", padx=6, pady=(0, 4))

        if self.compact:
            self._apply_collapsed()

    def _toggle_collapsed(self) -> None:
        """Compatibility no-op: the embedded operational log stays visible."""
        self._collapsed = False
        self._apply_collapsed()

    def _apply_collapsed(self) -> None:
        if not self.compact:
            return
        body = self._body_frame
        path = self._path_label_widget
        status = self._status_label_widget
        btn = self._collapse_btn
        if body is None:
            return
        self._collapsed = False
        if path is not None:
            path.pack(fill="x", padx=6)
        body.pack(fill="both", expand=True)
        if status is not None:
            status.pack(fill="x", padx=6, pady=(0, 4))
        if btn is not None:
            btn.pack_forget()

    def set_detach_callback(self, callback) -> None:
        """Optional: More-menu / 弹出完整窗口 opens the detached viewer."""
        self._detach_callback = callback

    def _request_detach(self) -> None:
        if callable(self._detach_callback):
            self._detach_callback()

    def _path_label(self) -> str:
        if self.path is None:
            return f"尚未找到 {FOLLOW_SESSION_FILENAME}（当前会话或自动对局目录）"
        return f"日志文件：{self.path}"

    def pick_session(self) -> None:
        chosen = filedialog.askdirectory(
            title="选择含 follow_session.jsonl 的会话目录",
            initialdir=str(self.session_dir if self.session_dir.is_dir() else Path.cwd()),
        )
        if not chosen:
            return
        self.session_dir = Path(chosen)
        candidate = self.session_dir / FOLLOW_SESSION_FILENAME
        self.path = candidate if candidate.is_file() else resolve_follow_session_path(
            self.session_dir, search_roots=self.search_roots
        )
        self.path_var.set(self._path_label())
        self._last_signature = None
        self.refresh()

    def clear_view(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._last_signature = None

    def _host_alive(self) -> bool:
        try:
            return bool(self.host.winfo_exists())
        except tk.TclError:
            return False

    def refresh(self) -> None:
        if not self._host_alive():
            return
        if self.path is None or not self.path.is_file():
            self.path = resolve_follow_session_path(
                self.session_dir, search_roots=self.search_roots
            )
            self.path_var.set(self._path_label())
        lines = read_follow_lines(self.path, limit=self.line_limit)
        signature: tuple[int, int] | None = None
        if self.path is not None and self.path.is_file():
            try:
                st = self.path.stat()
                signature = (st.st_mtime_ns, st.st_size)
            except OSError:
                signature = None
        if signature != self._last_signature:
            self._last_signature = signature
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            if lines:
                for line in lines:
                    self.text.insert("end", line + "\n", self._line_level(line))
            else:
                self.text.insert(
                    "1.0",
                    "（暂无门控/弃权/决策行。开始自动对局或跟随后，"
                    f"{FOLLOW_SESSION_FILENAME} 有内容时会显示在这里。）\n",
                    "wait",
                )
            self.text.configure(state="disabled")
            self.text.see("end")
            count = len(lines)
            self.status_var.set(
                f"已显示最近 {count} 行 · 绿=已验证/正常 · 橙=等待/弃权 · 红=停手/错误"
            )
        if self.timer is not None:
            try:
                self.host.after_cancel(self.timer)
            except tk.TclError:
                pass
        try:
            self.timer = self.host.after(self.REFRESH_MS, self.refresh)
        except tk.TclError:
            self.timer = None

    @staticmethod
    def _line_level(line: str) -> str:
        """Map operational meaning to color without changing stored evidence."""
        text = str(line).lower()
        if ("stop=" in text or "error" in text or "failed" in text
                or "aborted" in text or "unhandled_exception" in text):
            return "error"
        if "evt=action" in text or "evt=invitation_declined" in text:
            return "ok"
        if "abstain=" in text or "evt=idle" in text or "gate=wait" in text:
            return "wait"
        return "normal"

    def stop(self) -> None:
        """Cancel refresh timer without destroying an embedded host frame."""
        if self.timer is not None:
            try:
                self.host.after_cancel(self.timer)
            except tk.TclError:
                pass
            self.timer = None

    def close(self) -> None:
        self.stop()
        if self.window is not None:
            try:
                if self.window.winfo_exists():
                    self.window.destroy()
            except tk.TclError:
                pass
            self.window = None
