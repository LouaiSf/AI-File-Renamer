"""Main window."""

from __future__ import annotations

import ctypes
import os
import queue
import subprocess
import sys
import time
import tkinter as tk
import winreg
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import sv_ttk

from . import engine, journal, shell
from .config import Settings
from .engine import DONE, ERROR, PENDING, READY, SAME, SKIPPED, WORKING, Item

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:  # drag & drop is a nicety, not a requirement
    TkinterDnD = None

STATUS_TEXT = {PENDING: "Waiting", WORKING: "Working", READY: "Ready", SAME: "Unchanged",
               ERROR: "Error", DONE: "Renamed", SKIPPED: "Skipped"}
CHECK, UNCHECK, NOCHECK = "☑", "☐", ""


def style_window(win: tk.Misc) -> None:
    """Match the Windows title bar to the theme and set the app icon."""
    try:
        win.iconbitmap(str(shell.ICON))
    except tk.TclError:
        pass
    if sv_ttk.get_theme() == "dark":
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))


def scaled(win: tk.Misc, w: int, h: int, as_tuple: bool = False):
    """Window size in pixels adjusted for Windows display scaling (sizes are given at 100%)."""
    s = max(1.0, win.winfo_fpixels("1i") / 96)
    size = (int(w * s), int(h * s))
    return size if as_tuple else f"{size[0]}x{size[1]}"


def tk_colors(text: bool = True) -> dict:
    """Colors for classic tk widgets (Listbox, Text), which sv_ttk does not theme."""
    dark = sv_ttk.get_theme() == "dark"
    colors = dict(background="#1c1c1c" if dark else "#ffffff", foreground="#fafafa" if dark else "#1c1c1c",
                  selectbackground="#2f60d8", selectforeground="#ffffff",
                  highlightbackground="#3a3a3a" if dark else "#d0d0d0", highlightcolor="#57a0ff" if dark else "#2f60d8")
    if text:
        colors["insertbackground"] = colors["foreground"]
    return colors


def system_dark() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


class App:
    def __init__(self, root: tk.Tk, settings: Settings, inbox: queue.Queue):
        self.root = root
        self.settings = settings
        self.items: dict[int, Item] = {}
        # Row updates (item ids) from worker threads and path lists from other launches.
        self.updates: queue.Queue[int | list[str]] = inbox
        self.pool: ThreadPoolExecutor | None = None
        self.analyzer: engine.Analyzer | None = None
        self.edit_entry: ttk.Entry | None = None

        root.title("AI Renamer")
        root.geometry(scaled(root, 1100, 640))
        root.minsize(*scaled(root, 760, 420, as_tuple=True))
        style_window(root)
        self._build()
        self.refresh_provider()
        root.after(100, self._drain_updates)

    # ------------------------------------------------------------ layout
    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=(14, 12, 14, 6))
        top.pack(fill="x")
        ttk.Label(top, text="AI Renamer", font=("Segoe UI Semibold", 16)).pack(side="left")
        ttk.Button(top, text="Settings", command=self.open_settings).pack(side="right")
        ttk.Button(top, text="History / Undo", command=self.open_history).pack(side="right", padx=6)
        self.provider_var = tk.StringVar()
        self.provider_box = ttk.Combobox(top, textvariable=self.provider_var, state="readonly", width=34)
        self.provider_box.pack(side="right", padx=(6, 12))
        self.provider_box.bind("<<ComboboxSelected>>", self._provider_changed)
        ttk.Label(top, text="AI:").pack(side="right")

        bar = ttk.Frame(self.root, padding=(14, 4))
        bar.pack(fill="x")
        ttk.Button(bar, text="Add files...", command=self.add_files_dialog).pack(side="left")
        ttk.Button(bar, text="Add folder...", command=self.add_folder_dialog).pack(side="left", padx=6)
        self.recursive_var = tk.BooleanVar(value=self.settings.files["recursive"])
        ttk.Checkbutton(bar, text="Include subfolders", variable=self.recursive_var).pack(side="left", padx=8)
        ttk.Button(bar, text="Clear list", command=self.clear).pack(side="right")
        ttk.Button(bar, text="Retry failed", command=self.retry_failed).pack(side="right", padx=6)
        ttk.Button(bar, text="Check all", command=lambda: self.set_all(True)).pack(side="right")
        ttk.Button(bar, text="Uncheck all", command=lambda: self.set_all(False)).pack(side="right", padx=6)

        body = ttk.Frame(self.root, padding=(14, 6))
        body.pack(fill="both", expand=True)
        cols = ("sel", "current", "new", "status", "folder")
        self.tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="extended")
        for col, text, width, stretch in (("sel", "", 36, False), ("current", "Current name", 260, True),
                                          ("new", "New name (double-click to edit)", 340, True),
                                          ("status", "Status", 230, True), ("folder", "Folder", 160, True)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=stretch, anchor="center" if col == "sel" else "w")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        dark = sv_ttk.get_theme() == "dark"
        self.tree.tag_configure("error", foreground="#ff6b6b" if dark else "#c62828")
        self.tree.tag_configure("done", foreground="#5fd38d" if dark else "#2e7d32")
        self.tree.tag_configure("muted", foreground="#8a8a8a")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<space>", lambda e: self.toggle_selected())

        self.empty = ttk.Label(body, text="Drop files or folders here\nor use  Add files...  /  Add folder...",
                               justify="center", foreground="#8a8a8a", font=("Segoe UI", 13))
        self.empty.place(relx=0.5, rely=0.5, anchor="center")

        if TkinterDnD and isinstance(self.root, TkinterDnD.Tk):
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", lambda e: self.add_paths(self.root.tk.splitlist(e.data)))

        bottom = ttk.Frame(self.root, padding=(14, 8, 14, 14))
        bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="determinate", length=220)
        self.progress.pack(side="left")
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left", padx=12)
        self.rename_btn = ttk.Button(bottom, text="Rename", style="Accent.TButton", command=self.rename)
        self.rename_btn.pack(side="right")
        self.undo_btn = ttk.Button(bottom, text="Undo last rename", command=self.undo_last)
        self.undo_btn.pack(side="right", padx=8)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Edit name", command=self._edit_focused)
        self.menu.add_command(label="Generate again", command=self.regenerate_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Open file", command=lambda: self._open_selected(folder=False))
        self.menu.add_command(label="Show in folder", command=lambda: self._open_selected(folder=True))
        self.menu.add_separator()
        self.menu.add_command(label="Remove from list", command=self.remove_selected)
        self._refresh_buttons()

    # ------------------------------------------------------------ provider / analyzer
    def refresh_provider(self) -> None:
        providers = self.settings.data["providers"]
        self._provider_ids = list(providers)
        self.provider_box["values"] = [f"{p['label']}  -  {p.get('model') or 'no model'}" for p in providers.values()]
        self.provider_box.current(self._provider_ids.index(self.settings.data["provider"]))
        self.reset_analyzer()

    def _provider_changed(self, _event=None) -> None:
        self.settings.data["provider"] = self._provider_ids[self.provider_box.current()]
        self.settings.save()
        self.reset_analyzer()
        self.status_var.set(f"Using {self.settings.provider().label}.")

    def reset_analyzer(self) -> None:
        """Called when settings change: pending work restarts with the new provider."""
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)
        if self.analyzer:
            self.analyzer.cancel()
        provider = self.settings.provider()
        self.analyzer = engine.Analyzer(self.settings, on_update=lambda item: self.updates.put(item.id))
        self.pool = ThreadPoolExecutor(max_workers=provider.workers)
        requeue = [i for i in self.items.values() if i.status in (PENDING, WORKING)]
        for item in requeue:
            self._submit(item)

    def _submit(self, item: Item) -> None:
        item.status, item.message, item.stem, item.checked = PENDING, "", "", True
        self._update_row(item)
        analyzer = self.analyzer
        self.pool.submit(lambda: analyzer.cancelled.is_set() or analyzer.analyze_one(item))

    # ------------------------------------------------------------ adding files
    def add_files_dialog(self) -> None:
        paths = filedialog.askopenfilenames(title="Choose files to rename")
        if paths:
            self.add_paths(paths)

    def add_folder_dialog(self) -> None:
        path = filedialog.askdirectory(title="Choose a folder")
        if path:
            self.add_paths([path])

    def add_paths(self, paths) -> None:
        files, skipped = engine.collect(paths, self.settings, self.recursive_var.get())
        known = {str(i.path).lower() for i in self.items.values() if i.status != DONE}
        added = 0
        for f in files:
            if str(f).lower() in known:
                continue
            item = Item(f)
            self.items[item.id] = item
            self.tree.insert("", "end", iid=str(item.id), values=self._row(item))
            self._submit(item)
            added += 1
        msg = f"Added {added} file(s)."
        if skipped:
            reasons = {}
            for _, r in skipped:
                reasons[r.split(" (")[0]] = reasons.get(r.split(" (")[0], 0) + 1
            msg += "  Skipped: " + ", ".join(f"{n} {r}" for r, n in reasons.items()) + "."
        self.status_var.set(msg)
        self._refresh_buttons()

    # ------------------------------------------------------------ rows
    def _row(self, item: Item) -> tuple:
        if item.status in (READY,):
            sel = CHECK if item.checked else UNCHECK
        else:
            sel = NOCHECK
        current = item.path.name
        new = item.new_name if item.status != DONE else (item.target.name if item.target else "")
        status = STATUS_TEXT[item.status] + (f" - {item.message}" if item.message else "")
        return sel, current, new, status, item.path.parent.name

    def _update_row(self, item: Item) -> None:
        iid = str(item.id)
        if not self.tree.exists(iid):
            return
        tag = {ERROR: "error", DONE: "done", SAME: "muted", PENDING: "muted"}.get(item.status, "")
        self.tree.item(iid, values=self._row(item), tags=(tag,) if tag else ())

    def _drain_updates(self) -> None:
        changed = False
        try:
            while True:
                msg = self.updates.get_nowait()
                if isinstance(msg, list):
                    self.root.deiconify()
                    self.root.lift()
                    self.root.focus_force()
                    if msg:
                        self.add_paths(msg)
                elif msg in self.items:
                    self._update_row(self.items[msg])
                    changed = True
        except queue.Empty:
            pass
        if changed:
            self._refresh_buttons()
        self.root.after(120, self._drain_updates)

    def _refresh_buttons(self) -> None:
        items = list(self.items.values())
        total = len(items)
        finished = sum(1 for i in items if i.status not in (PENDING, WORKING))
        todo = sum(1 for i in items if i.status == READY and i.checked)
        self.progress["maximum"] = max(total, 1)
        self.progress["value"] = finished
        self.rename_btn.configure(text=f"Rename {todo} file(s)", state="normal" if todo else "disabled")
        self.undo_btn.configure(state="normal" if journal.last_batch() else "disabled")
        if total:
            self.empty.place_forget()
            if finished < total:
                self.status_var.set(f"Generating names... {finished}/{total}")
            elif self.status_var.get().startswith("Generating"):
                errors = sum(1 for i in items if i.status == ERROR)
                self.status_var.set(f"Done. {todo} ready to rename" + (f", {errors} error(s)." if errors else "."))
        else:
            self.empty.place(relx=0.5, rely=0.5, anchor="center")

    # ------------------------------------------------------------ interactions
    def _item_at(self, iid: str) -> Item | None:
        return self.items.get(int(iid)) if iid else None

    def _on_click(self, event) -> str | None:
        if self.tree.identify_region(event.x, event.y) == "cell" and self.tree.identify_column(event.x) == "#1":
            item = self._item_at(self.tree.identify_row(event.y))
            if item and item.status == READY:
                item.checked = not item.checked
                self._update_row(item)
                self._refresh_buttons()
            return "break"
        return None

    def toggle_selected(self) -> None:
        for iid in self.tree.selection():
            item = self._item_at(iid)
            if item and item.status == READY:
                item.checked = not item.checked
                self._update_row(item)
        self._refresh_buttons()

    def set_all(self, value: bool) -> None:
        for item in self.items.values():
            if item.status == READY:
                item.checked = value
                self._update_row(item)
        self._refresh_buttons()

    def _on_double(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            self._edit(iid)

    def _edit_focused(self) -> None:
        if self.tree.focus():
            self._edit(self.tree.focus())

    def _edit(self, iid: str) -> None:
        item = self._item_at(iid)
        if not item or item.status in (PENDING, WORKING, DONE):
            return
        bbox = self.tree.bbox(iid, "new")
        if not bbox:
            return
        x, y, w, h = bbox
        entry = ttk.Entry(self.tree)
        entry.insert(0, item.stem or item.path.stem)
        entry.select_range(0, "end")
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        self.edit_entry = entry

        def commit(_=None):
            stem = engine.sanitize_user_stem(entry.get())
            entry.destroy()
            self.edit_entry = None
            if stem and stem != item.path.stem:
                item.stem, item.status, item.checked, item.message = stem, READY, True, "Edited by you"
            self._update_row(item)
            self._refresh_buttons()

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)
        entry.bind("<Escape>", lambda e: (entry.destroy(), setattr(self, "edit_entry", None)))

    def _on_right_click(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.menu.tk_popup(event.x_root, event.y_root)

    def _selected_items(self) -> list[Item]:
        return [i for i in (self._item_at(iid) for iid in self.tree.selection()) if i]

    def _open_selected(self, folder: bool) -> None:
        for item in self._selected_items()[:5]:
            path = item.target if item.status == DONE and item.target else item.path
            if folder:
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                os.startfile(path)

    def regenerate_selected(self) -> None:
        for item in self._selected_items():
            if item.status not in (PENDING, WORKING, DONE):
                self._submit(item)
        self._refresh_buttons()

    def retry_failed(self) -> None:
        self.settings.data = Settings.load().data  # picks up a key or model changed since the failure
        self.refresh_provider()
        for item in self.items.values():
            if item.status == ERROR:
                self._submit(item)
        self._refresh_buttons()

    def remove_selected(self) -> None:
        for item in self._selected_items():
            if item.status != WORKING:
                self.tree.delete(str(item.id))
                del self.items[item.id]
        self._refresh_buttons()

    def clear(self) -> None:
        if any(i.status in (PENDING, WORKING) for i in self.items.values()):
            self.reset_analyzer()
        for item in list(self.items.values()):
            if item.status not in (PENDING, WORKING):
                self.tree.delete(str(item.id))
                del self.items[item.id]
        self.status_var.set("List cleared.")
        self._refresh_buttons()

    # ------------------------------------------------------------ renaming
    def rename(self) -> None:
        if self.edit_entry:
            self.edit_entry.event_generate("<Return>")
        provider = self.settings.provider()
        batch_id, done = engine.apply(list(self.items.values()), self.settings, f"{provider.label} / {provider.model}")
        for item in self.items.values():
            self._update_row(item)
        failed = sum(1 for i in self.items.values() if i.status == ERROR and i.message.startswith("Rename failed"))
        self._refresh_buttons()
        self.status_var.set(f"Renamed {len(done)} file(s)." + (f" {failed} failed (see list)." if failed else "")
                            + ("  You can undo this." if done else ""))

    def undo_last(self) -> None:
        batch = journal.last_batch()
        if not batch:
            return
        if not messagebox.askyesno("Undo", f"Restore the original names of {len(batch['ops'])} file(s) renamed at {batch['time']}?"):
            return
        self._undo(batch["id"])

    def _undo(self, batch_id: str) -> None:
        restored, problems = journal.undo(batch_id)
        restored_paths = {op["new"].lower(): op["old"] for b in journal.batches() if b["id"] == batch_id for op in b["ops"]}
        for item in self.items.values():
            if item.status == DONE and item.target and str(item.target).lower() in restored_paths:
                item.status, item.message, item.target, item.checked = READY, "Restored original name", None, False
                self._update_row(item)
        self._refresh_buttons()
        text = f"Restored {restored} file(s)."
        if problems:
            messagebox.showwarning("Undo", text + "\n\n" + "\n".join(problems[:15]))
        self.status_var.set(text)

    # ------------------------------------------------------------ windows
    def open_settings(self) -> None:
        from .gui_settings import SettingsWindow

        SettingsWindow(self.root, self.settings, on_saved=self._settings_saved)

    def _settings_saved(self) -> None:
        self.recursive_var.set(self.settings.files["recursive"])
        self.refresh_provider()
        self.status_var.set("Settings saved.")

    def open_history(self) -> None:
        HistoryWindow(self.root, self)


class HistoryWindow(tk.Toplevel):
    def __init__(self, master, app: App):
        super().__init__(master)
        self.app = app
        self.title("History")
        self.geometry(scaled(self, 860, 480))
        self.transient(master)
        style_window(self)
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=("when", "what", "who"), show="tree headings")
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=28, stretch=False)
        for col, text, w in (("when", "When", 150), ("what", "Files", 480), ("who", "AI used", 180)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=w, stretch=col == "what")
        self.tree.pack(fill="both", expand=True)
        buttons = ttk.Frame(frame, padding=(0, 10, 0, 0))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Undo selected batch", style="Accent.TButton", command=self.undo).pack(side="right")
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right", padx=8)
        self.load()

    def load(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for b in journal.batches():
            label = f"{len(b['ops'])} file(s)" + ("   (undone)" if b["undone"] else "")
            parent = self.tree.insert("", "end", iid=b["id"], values=(b["time"].replace("T", " "), label, b.get("provider", "")))
            for op in b["ops"]:
                self.tree.insert(parent, "end", values=("", f"{Path(op['old']).name}  ->  {Path(op['new']).name}", ""))

    def undo(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        while self.tree.parent(iid):
            iid = self.tree.parent(iid)
        batch = next((b for b in journal.batches() if b["id"] == iid), None)
        if not batch or batch["undone"]:
            messagebox.showinfo("Undo", "That batch was already undone.", parent=self)
            return
        if messagebox.askyesno("Undo", f"Restore {len(batch['ops'])} file(s) to their original names?", parent=self):
            self.app._undo(iid)
            self.load()


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    paths = [a for a in argv if not a.startswith("-")]
    # Explorer starts one process per selected file: forward to the open window if there is one.
    inbox: queue.Queue = queue.Queue()
    while not shell.serve(lambda p: inbox.put(list(p))):
        if shell.hand_off(paths):
            return
        time.sleep(0.1)  # another instance is starting up right now
    engine.setup_logging()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    sv_ttk.set_theme("dark" if system_dark() else "light")
    App(root, Settings.load(), inbox)
    if paths:
        inbox.put(paths)
    root.mainloop()


if __name__ == "__main__":
    main()
