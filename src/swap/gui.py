"""Swappy desktop GUI — a clean, dependency-free window built on Tkinter.

Run it with::

    python -m swap gui

Tkinter ships with the standard CPython installer on Windows and macOS (and is
a one-line ``apt install python3-tk`` on Linux), so there is **nothing extra to
buy or license** — unlike the previous PySimpleGUI-based UI.

Like the TUI, this is a thin front-end: it launches the engine as a subprocess
(the swap keeps running if you close the window, and Cancel is always safe
because the engine is resumable) and polls ``swap.db`` read-only for live
progress. All non-UI logic lives in :mod:`swap.uikit`, which is unit-tested.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover - depends on the Python build
    raise SystemExit(
        "The Swappy GUI needs Tkinter, which is missing from this Python.\n"
        "  • Windows/macOS: reinstall Python with the Tcl/Tk option enabled.\n"
        "  • Debian/Ubuntu: sudo apt install python3-tk\n"
        "Or use the terminal UI instead:  python -m swap tui"
    )

from . import uikit

# Palette
BG = "#15171c"
PANEL = "#1d2027"
FG = "#e6e6e6"
MUTED = "#9aa3b2"
ACCENT = "#00d4ff"
OK = "#42d977"
WARN = "#ffc043"
ERR = "#ff5d5d"

POLL_MS = 250


class SwappyGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.proc = uikit.SwapProcess()
        self.reader: uikit.ProgressReader | None = None
        self._polling = False

        root.title("Swappy — crash-safe folder swap")
        root.configure(bg=BG)
        root.minsize(820, 640)

        self._init_style()
        self._build_header()
        self._build_form()
        self._build_actions()
        self._build_progress()
        self._build_log()
        self._build_statusbar()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_running(False)

    # -- styling -----------------------------------------------------------

    def _init_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=PANEL, foreground=FG, fieldbackground="#262a33",
                        bordercolor="#333", lightcolor=PANEL, darkcolor=PANEL)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Card.TLabel", background=PANEL, foreground=FG)
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Header.TLabel", background=BG, foreground=ACCENT,
                        font=("Segoe UI", 18, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED)
        style.configure("TEntry", fieldbackground="#262a33", foreground=FG)
        style.configure("TCombobox", fieldbackground="#262a33", foreground=FG)
        style.configure("TCheckbutton", background=PANEL, foreground=FG)
        style.configure("Accent.TButton", background=ACCENT, foreground="#04222b",
                        font=("Segoe UI", 10, "bold"), padding=8, borderwidth=0)
        style.map("Accent.TButton", background=[("active", "#33e0ff"), ("disabled", "#2a3a40")])
        style.configure("TButton", background="#2a2f3a", foreground=FG, padding=7, borderwidth=0)
        style.map("TButton", background=[("active", "#39414f"), ("disabled", "#22262e")])
        style.configure("Swappy.Horizontal.TProgressbar", troughcolor="#262a33",
                        background=ACCENT, bordercolor=PANEL, lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("Treeview", background="#1a1d23", fieldbackground="#1a1d23",
                        foreground=FG, rowheight=22, borderwidth=0)
        style.configure("Treeview.Heading", background="#2a2f3a", foreground=MUTED,
                        font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#264653")])

    # -- layout ------------------------------------------------------------

    def _build_header(self) -> None:
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=16, pady=(14, 6))
        ttk.Label(bar, text="Swappy", style="Header.TLabel").pack(side="left")
        ttk.Label(bar, text="  swap two big folders across drives, safely",
                  style="Sub.TLabel").pack(side="left", padx=8)

    def _card(self, title: str) -> ttk.Frame:
        wrap = ttk.Frame(self.root, style="TFrame")
        wrap.pack(fill="x", padx=16, pady=6)
        ttk.Label(wrap, text=title, style="Sub.TLabel").pack(anchor="w", pady=(0, 2))
        card = ttk.Frame(wrap, style="Card.TFrame", padding=12)
        card.pack(fill="x")
        return card

    def _folder_row(self, parent: ttk.Frame, label: str, row: int) -> tk.StringVar:
        var = tk.StringVar()
        ttk.Label(parent, text=label, style="Card.TLabel", width=12).grid(
            row=row, column=0, sticky="w", pady=4)
        entry = ttk.Entry(parent, textvariable=var)
        entry.grid(row=row, column=1, sticky="ew", padx=6, pady=4)
        ttk.Button(parent, text="Browse…",
                   command=lambda: self._browse(var)).grid(row=row, column=2, pady=4)
        parent.columnconfigure(1, weight=1)
        return var

    def _build_form(self) -> None:
        card = self._card("Folders")
        self.var_a = self._folder_row(card, "Folder A", 0)
        self.var_b = self._folder_row(card, "Folder B", 1)
        self.var_work = self._folder_row(card, "Work dir", 2)

        adv = self._card("Settings")
        self.var_chunk = tk.StringVar(value="64MB")
        self.var_margin = tk.StringVar(value="64MB")
        self.var_workers = tk.IntVar(value=8)
        self.var_backend = tk.StringVar(
            value="winapi" if sys.platform == "win32" else "python")
        self.var_verify = tk.BooleanVar(value=True)
        self.var_sim = tk.BooleanVar(value=False)

        def field(col: int, label: str, widget: tk.Widget) -> None:
            holder = ttk.Frame(adv, style="Card.TFrame")
            holder.grid(row=0, column=col, sticky="w", padx=(0, 18), pady=2)
            ttk.Label(holder, text=label, style="Muted.TLabel").pack(anchor="w")
            widget.pack(anchor="w")

        field(0, "Chunk size", ttk.Entry(adv, textvariable=self.var_chunk, width=10))
        field(1, "Safety margin", ttk.Entry(adv, textvariable=self.var_margin, width=10))
        field(2, "Workers", ttk.Spinbox(adv, from_=1, to=64, textvariable=self.var_workers, width=6))
        field(3, "Backend", ttk.Combobox(adv, textvariable=self.var_backend, width=9,
                                          state="readonly", values=list(uikit.BACKENDS)))
        checks = ttk.Frame(adv, style="Card.TFrame")
        checks.grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Checkbutton(checks, text="Verify chunks (SHA-256)",
                        variable=self.var_verify).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(checks, text="Same-disk test mode (simulate-distinct)",
                        variable=self.var_sim).pack(side="left")

    def _build_actions(self) -> None:
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=16, pady=(8, 4))
        self.btn_run = ttk.Button(bar, text="▶  Run swap", style="Accent.TButton",
                                  command=self._on_run)
        self.btn_run.pack(side="left")
        self.btn_resume = ttk.Button(bar, text="Resume", command=self._on_resume)
        self.btn_resume.pack(side="left", padx=6)
        self.btn_rollback = ttk.Button(bar, text="Rollback", command=self._on_rollback)
        self.btn_rollback.pack(side="left", padx=6)
        self.btn_status = ttk.Button(bar, text="Status", command=self._on_status)
        self.btn_status.pack(side="left", padx=6)
        self.btn_cancel = ttk.Button(bar, text="✕  Cancel", command=self._on_cancel)
        self.btn_cancel.pack(side="right")

    def _build_progress(self) -> None:
        card = self._card("Progress")
        self.var_stage = tk.StringVar(value="Idle")
        top = ttk.Frame(card, style="Card.TFrame")
        top.pack(fill="x")
        ttk.Label(top, textvariable=self.var_stage, style="Card.TLabel",
                  font=("Segoe UI", 11, "bold")).pack(side="left")
        self.var_stats = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.var_stats, style="Muted.TLabel").pack(side="right")

        self.pbar = ttk.Progressbar(card, style="Swappy.Horizontal.TProgressbar",
                                    maximum=1000, value=0)
        self.pbar.pack(fill="x", pady=8)

        self.tree = ttk.Treeview(card, columns=("state", "file", "done", "speed"),
                                 show="headings", height=6)
        for col, txt, w, anchor in (("state", "State", 90, "w"), ("file", "File", 360, "w"),
                                    ("done", "Done", 110, "e"), ("speed", "Speed", 110, "e")):
            self.tree.heading(col, text=txt)
            self.tree.column(col, width=w, anchor=anchor)
        self.tree.pack(fill="x")

    def _build_log(self) -> None:
        card = self._card("Engine log")
        self.log = tk.Text(card, height=8, bg="#0f1115", fg=MUTED, insertbackground=FG,
                           relief="flat", font=("Consolas", 9), wrap="none")
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")

    def _build_statusbar(self) -> None:
        self.var_status = tk.StringVar(value="Ready.")
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self.var_status, style="Sub.TLabel").pack(
            side="left", padx=16, pady=6)

    # -- helpers -----------------------------------------------------------

    def _browse(self, var: tk.StringVar) -> None:
        path = filedialog.askdirectory(initialdir=var.get() or str(Path.home()))
        if path:
            var.set(path)

    def _log_write(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, text: str) -> None:
        self.var_status.set(text)

    def _config(self) -> uikit.SwapConfig:
        return uikit.SwapConfig(
            folder_a=self.var_a.get().strip(),
            folder_b=self.var_b.get().strip(),
            work_dir=self.var_work.get().strip(),
            chunk_size=self.var_chunk.get().strip() or "64MB",
            safety_margin=self.var_margin.get().strip() or "64MB",
            workers=int(self.var_workers.get() or 1),
            backend=self.var_backend.get(),
            verify_chunks=bool(self.var_verify.get()),
            volume_mode="simulate-distinct" if self.var_sim.get() else "real",
        )

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        for btn in (self.btn_run, self.btn_resume, self.btn_rollback, self.btn_status):
            btn.configure(state=state)
        self.btn_cancel.configure(state="normal" if running else "disabled")

    # -- actions -----------------------------------------------------------

    def _start(self, argv: list[str], work_dir: str, what: str) -> None:
        if self.proc.is_running():
            messagebox.showwarning("Busy", "An operation is already running.")
            return
        self.reader = uikit.ProgressReader(work_dir)
        try:
            self.proc.start(argv)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Failed to start", str(exc))
            return
        self._log_write(f"$ {self.proc.command}")
        self._set_running(True)
        self._set_status(f"{what}…")
        if not self._polling:
            self._polling = True
            self.root.after(POLL_MS, self._poll)

    def _on_run(self) -> None:
        cfg = self._config()
        problems = cfg.validate()
        if problems:
            messagebox.showerror("Check the form", "\n".join(problems))
            return
        # Warn if a session already exists in the work dir.
        existing = uikit.ProgressReader(cfg.work_dir).read()
        if existing.exists and not existing.is_finished:
            if not messagebox.askyesno(
                "Session exists",
                f"A session is already at stage '{existing.stage_label}'.\n"
                "Start a NEW swap anyway? (Usually you want Resume.)",
            ):
                return
        if not messagebox.askyesno("Confirm", "Start the swap with these settings?"):
            return
        self._start(uikit.run_argv(cfg), cfg.work_dir, "Running swap")

    def _on_resume(self) -> None:
        work = self.var_work.get().strip()
        if not work:
            messagebox.showerror("Work dir required", "Pick the work directory to resume.")
            return
        self._start(uikit.resume_argv(work), work, "Resuming")

    def _on_rollback(self) -> None:
        work = self.var_work.get().strip()
        if not work:
            messagebox.showerror("Work dir required", "Pick the work directory to roll back.")
            return
        if not messagebox.askyesno(
            "Confirm rollback", "Undo the swap and restore the original layout?"):
            return
        self._start(uikit.rollback_argv(work), work, "Rolling back")

    def _on_status(self) -> None:
        work = self.var_work.get().strip()
        if not work:
            messagebox.showerror("Work dir required", "Pick the work directory.")
            return
        snap = uikit.ProgressReader(work).read()
        if not snap.exists:
            messagebox.showinfo("Status", "No session found in that work directory.")
            return
        counts = ", ".join(f"{k}={snap.counts.get(k, 0)}"
                           for k in ("pending", "sparse", "done"))
        messagebox.showinfo("Status", f"Stage: {snap.stage_label}\nEntries: {counts}")

    def _on_cancel(self) -> None:
        if not self.proc.is_running():
            return
        if messagebox.askyesno(
            "Cancel", "Stop the running operation?\n(It's safe — you can Resume later.)"):
            self.proc.cancel()
            self._set_status("Cancelled. Resume to continue.")

    # -- polling loop ------------------------------------------------------

    def _poll(self) -> None:
        for line in self.proc.drain_logs():
            self._log_write(line)

        if self.reader is not None:
            snap = self.reader.read()
            self._render(snap)

        if self.proc.is_running():
            self.root.after(POLL_MS, self._poll)
            return

        # finished
        self._polling = False
        self._set_running(False)
        rc = self.proc.returncode
        if rc == 0:
            self._set_status("✓ Operation complete.")
        elif rc is not None:
            self._set_status(f"✗ Engine exited with code {rc}.")

    def _render(self, snap: uikit.UiSnapshot) -> None:
        if not snap.exists:
            self.var_stage.set("Starting…")
            return
        self.var_stage.set(snap.stage_label)
        self.pbar.configure(value=int(snap.fraction * 1000))
        s = snap.summary
        if s:
            self.var_stats.set(
                f"{uikit.format_bytes(s.bytes_copied)} / {uikit.format_bytes(s.total_bytes)}"
                f"   ·   {s.done_files}/{s.total_files} files"
                f"   ·   {uikit.format_rate(snap.rate_bps)}"
                f"   ·   ETA {uikit.format_seconds(snap.eta_seconds)}"
            )
        self.tree.delete(*self.tree.get_children())
        for w in snap.workers:
            pct = ""
            if w.file_size and w.state in ("copying", "chunking"):
                pct = f" {100.0 * w.bytes_copied / max(w.file_size, 1):.0f}%"
            done = (uikit.format_bytes(w.bytes_copied) + pct) if w.bytes_copied else "—"
            self.tree.insert("", "end", values=(
                w.state, uikit.short_path(w.rel_path, 60),
                done, uikit.format_rate(w.throughput_bps)))

    def _on_close(self) -> None:
        if self.proc.is_running():
            if not messagebox.askyesno(
                "Quit",
                "The swap is still running. It will keep running in the background.\n"
                "Quit the window?",
            ):
                return
        if self.reader is not None:
            self.reader.close()
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    root = tk.Tk()
    SwappyGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
