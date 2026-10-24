"""Settings window: every option is editable here, nothing requires touching code."""

from __future__ import annotations

import copy
import json
import os
import queue
import re
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk
from typing import Callable

from . import ollama, shell
from .config import fast_local, DATA_DIR, DEFAULT_PROMPT, DEFAULTS, LOG_FILE, Provider, Settings, get_api_key, set_api_key
from .llm import Client, LLMError, Suggestion
from .naming import build_stem


def tk_colors(text: bool = True) -> dict:
    from .gui import tk_colors as colors
    return colors(text)

KINDS = {"openai": "OpenAI-compatible API", "ollama": "Ollama (local)"}

PRESETS = {
    "Google Gemini": {"kind": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-3.5-flash", "extra": {"reasoning_effort": "minimal"},
                      "needs_key": True, "rpm": 5, "workers": 3, "key_url": "https://aistudio.google.com/apikey"},
    "OpenRouter": {"kind": "openai", "base_url": "https://openrouter.ai/api/v1", "model": "", "needs_key": True,
                   "rpm": 20, "workers": 2, "key_url": "https://openrouter.ai/keys"},
    "Groq": {"kind": "openai", "base_url": "https://api.groq.com/openai/v1", "model": "", "needs_key": True,
             "rpm": 30, "workers": 2, "key_url": "https://console.groq.com/keys"},
    "OpenAI": {"kind": "openai", "base_url": "https://api.openai.com/v1", "model": "", "needs_key": True,
               "rpm": 60, "workers": 4, "key_url": "https://platform.openai.com/api-keys"},
    "Anthropic (Claude)": {"kind": "openai", "base_url": "https://api.anthropic.com/v1", "model": "claude-haiku-5-5", "needs_key": True,
                           "rpm": 50, "workers": 4, "key_url": "https://console.anthropic.com/settings/keys"},
    "Mistral": {"kind": "openai", "base_url": "https://api.mistral.ai/v1", "model": "", "needs_key": True,
                "rpm": 30, "workers": 2, "key_url": "https://console.mistral.ai/api-keys"},
    "LM Studio (local)": {"kind": "openai", "base_url": "http://127.0.0.1:1234/v1", "model": "", "needs_key": False,
                          "rpm": 0, "workers": 1, "key_url": ""},
    "Another Ollama model": {"kind": "ollama", "base_url": "http://127.0.0.1:11434/v1", "model": "", "needs_key": False,
                             "rpm": 0, "workers": 1, "key_url": ""},
    "Blank": {"kind": "openai", "base_url": "", "model": "", "needs_key": True, "rpm": 0, "workers": 1, "key_url": ""},
}

INTEGRATIONS = [
    ("context_menu", "Right-click menu in Explorer (\"Rename with AI\" on files, folders and folder background)"),
    ("send_to", "\"Send to > Rename with AI\" (best for many selected files)"),
    ("desktop", "Desktop icon"),
    ("start_menu", "Start menu entry"),
    ("path", "`rename-ai` command in any terminal (adds to your user PATH)"),
]


class SettingsWindow(tk.Toplevel):
    def __init__(self, master, settings: Settings, on_saved: Callable[[], None]):
        super().__init__(master)
        self.settings = settings
        self.on_saved = on_saved
        self.data = copy.deepcopy(settings.data)
        self.keys = {pid: get_api_key(pid) or "" for pid in self.data["providers"]}
        self.current_pid: str | None = None
        self.results: queue.Queue = queue.Queue()
        self.cancel_download = threading.Event()

        from .gui import scaled, style_window

        self.title("Settings")
        self.geometry(scaled(self, 920, 680))
        self.minsize(*scaled(self, 780, 560, as_tuple=True))
        self.transient(master)
        style_window(self)

        bottom = ttk.Frame(self, padding=12)
        bottom.pack(side="bottom", fill="x")
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=(12, 0))
        for title, builder in (("AI provider", self._tab_providers), ("Local models", self._tab_local),
                               ("Naming", self._tab_naming), ("Prompt", self._tab_prompt),
                               ("Files", self._tab_files), ("Integration", self._tab_integration)):
            frame = ttk.Frame(nb, padding=14)
            nb.add(frame, text=title)
            builder(frame)

        ttk.Button(bottom, text="Save", style="Accent.TButton", command=self.save).pack(side="right")
        ttk.Button(bottom, text="Cancel", command=self.close).pack(side="right", padx=8)
        self.protocol("WM_DELETE_WINDOW", self.close)
        ttk.Button(bottom, text="Open data folder", command=lambda: os.startfile(DATA_DIR)).pack(side="left")
        ttk.Button(bottom, text="Open log", command=self._open_log).pack(side="left", padx=8)

        self.after(100, self._poll)
        self.provider_list.selection_set(list(self.data["providers"]).index(self.data["provider"]))
        self._on_provider_select()

    # ------------------------------------------------------------ background helper
    def run_bg(self, work: Callable, done: Callable | None = None) -> None:
        """Run `work` in a thread, then call `done(result_or_exception)` on the UI thread."""
        def target():
            try:
                result = work()
            except Exception as e:
                result = e
            self.results.put((done, result))
        threading.Thread(target=target, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                done, result = self.results.get_nowait()
                if done and self.winfo_exists():
                    done(result)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll)

    def ui(self, fn: Callable, *args) -> None:
        """Schedule a UI update from a worker thread."""
        self.results.put((lambda _: fn(*args), None))

    # ------------------------------------------------------------ providers tab
    def _tab_providers(self, f: ttk.Frame) -> None:
        left = ttk.Frame(f)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Providers", font=("Segoe UI Semibold", 11)).pack(anchor="w")
        self.provider_list = tk.Listbox(left, width=28, height=14, exportselection=False, activestyle="none",
                                        font=("Segoe UI", 10), borderwidth=0, highlightthickness=1, **tk_colors(text=False))
        self.provider_list.pack(fill="y", expand=True, pady=6)
        self.provider_list.bind("<<ListboxSelect>>", lambda e: self._on_provider_select())
        row = ttk.Frame(left)
        row.pack(fill="x")
        add = ttk.Menubutton(row, text="Add")
        menu = tk.Menu(add, tearoff=0)
        for name in PRESETS:
            menu.add_command(label=name, command=lambda n=name: self._add_provider(n))
        add["menu"] = menu
        add.pack(side="left")
        ttk.Button(row, text="Remove", command=self._remove_provider).pack(side="left", padx=6)
        self._fill_provider_list()

        form = ttk.Frame(f, padding=(18, 0, 0, 0))
        form.pack(side="left", fill="both", expand=True)
        form.columnconfigure(1, weight=1)
        self.pv = {k: tk.StringVar() for k in ("label", "kind", "base_url", "model", "key", "rpm", "workers")}
        self.needs_key = tk.BooleanVar()
        r = 0

        def label(text):
            ttk.Label(form, text=text).grid(row=r, column=0, sticky="w", pady=5, padx=(0, 10))

        label("Name")
        ttk.Entry(form, textvariable=self.pv["label"]).grid(row=r, column=1, columnspan=3, sticky="ew"); r += 1
        label("Type")
        kind = ttk.Combobox(form, textvariable=self.pv["kind"], values=list(KINDS.values()), state="readonly")
        kind.grid(row=r, column=1, columnspan=3, sticky="ew"); r += 1
        label("Base URL")
        ttk.Entry(form, textvariable=self.pv["base_url"]).grid(row=r, column=1, columnspan=3, sticky="ew"); r += 1
        label("Model")
        self.model_box = ttk.Combobox(form, textvariable=self.pv["model"])
        self.model_box.grid(row=r, column=1, columnspan=2, sticky="ew")
        ttk.Button(form, text="Fetch list", command=self._fetch_models).grid(row=r, column=3, padx=(6, 0)); r += 1
        ttk.Checkbutton(form, text="Requires an API key", variable=self.needs_key,
                        command=self._key_state).grid(row=r, column=1, sticky="w", pady=5); r += 1
        label("API key")
        self.key_entry = ttk.Entry(form, textvariable=self.pv["key"], show="•")
        self.key_entry.grid(row=r, column=1, sticky="ew")
        self.show_btn = ttk.Button(form, text="Show", width=6, command=self._toggle_key)
        self.show_btn.grid(row=r, column=2, padx=(6, 0))
        ttk.Button(form, text="Paste", width=6, command=self._paste_key).grid(row=r, column=3, padx=(6, 0)); r += 1
        self.key_link = ttk.Button(form, text="Get a key", command=self._open_key_url)
        self.key_link.grid(row=r, column=1, sticky="w"); r += 1
        label("Requests / minute")
        ttk.Spinbox(form, from_=0, to=10000, textvariable=self.pv["rpm"], width=8).grid(row=r, column=1, sticky="w")
        ttk.Label(form, text="0 = no limit. Free Gemini: 5", foreground="#8a8a8a").grid(row=r, column=2, columnspan=2, sticky="w"); r += 1
        label("Files at once")
        ttk.Spinbox(form, from_=1, to=32, textvariable=self.pv["workers"], width=8).grid(row=r, column=1, sticky="w"); r += 1
        label("Extra request\nfields (JSON)")
        self.extra_text = tk.Text(form, height=4, font=("Consolas", 10), wrap="word", borderwidth=0, highlightthickness=1, **tk_colors())
        self.extra_text.grid(row=r, column=1, columnspan=3, sticky="ew", pady=5); r += 1
        ttk.Label(form, text='Advanced, optional. Example: {"reasoning_effort": "low"}', foreground="#8a8a8a").grid(
            row=r, column=1, columnspan=3, sticky="w"); r += 1

        actions = ttk.Frame(form)
        actions.grid(row=r, column=0, columnspan=4, sticky="ew", pady=(14, 0)); r += 1
        self.active_btn = ttk.Button(actions, text="Use this provider", style="Accent.TButton", command=self._make_active)
        self.active_btn.pack(side="left")
        ttk.Button(actions, text="Test connection", command=self._test).pack(side="left", padx=8)
        self.test_var = tk.StringVar()
        ttk.Label(form, textvariable=self.test_var, wraplength=520).grid(row=r, column=0, columnspan=4, sticky="w", pady=8)

    def _fill_provider_list(self) -> None:
        self.provider_list.delete(0, "end")
        for pid, p in self.data["providers"].items():
            star = "★ " if pid == self.data["provider"] else "    "
            self.provider_list.insert("end", star + (p.get("label") or pid))

    def _selected_pid(self) -> str | None:
        sel = self.provider_list.curselection()
        return list(self.data["providers"])[sel[0]] if sel else None

    def _store_form(self) -> bool:
        pid = self.current_pid
        if not pid or pid not in self.data["providers"]:
            return True
        p = self.data["providers"][pid]
        p["label"] = self.pv["label"].get().strip() or pid
        p["kind"] = next((k for k, v in KINDS.items() if v == self.pv["kind"].get()), "openai")
        p["base_url"] = self.pv["base_url"].get().strip()
        p["model"] = self.pv["model"].get().strip()
        p["needs_key"] = self.needs_key.get()
        for field in ("rpm", "workers"):
            try:
                p[field] = max(0 if field == "rpm" else 1, int(self.pv[field].get()))
            except ValueError:
                pass
        self.keys[pid] = self.pv["key"].get().strip()
        raw = self.extra_text.get("1.0", "end").strip()
        try:
            p["extra"] = json.loads(raw) if raw else {}
            if not isinstance(p["extra"], dict):
                raise ValueError
        except ValueError:
            messagebox.showerror("Settings", f"'Extra request fields' for {p['label']} must be a JSON object, like {{\"key\": \"value\"}}.", parent=self)
            return False
        return True

    def _on_provider_select(self) -> None:
        pid = self._selected_pid()
        if not pid or pid == self.current_pid:
            return
        if not self._store_form():
            self.provider_list.selection_clear(0, "end")
            self.provider_list.selection_set(list(self.data["providers"]).index(self.current_pid))
            return
        self._fill_provider_list()
        self.provider_list.selection_set(list(self.data["providers"]).index(pid))
        self.current_pid = pid
        p = self.data["providers"][pid]
        self.pv["label"].set(p.get("label", pid))
        self.pv["kind"].set(KINDS.get(p.get("kind", "openai")))
        self.pv["base_url"].set(p.get("base_url", ""))
        self.pv["model"].set(p.get("model", ""))
        self.pv["key"].set(self.keys.get(pid, ""))
        self.pv["rpm"].set(str(p.get("rpm", 0)))
        self.pv["workers"].set(str(p.get("workers", 1)))
        self.needs_key.set(bool(p.get("needs_key")))
        self.extra_text.delete("1.0", "end")
        if p.get("extra"):
            self.extra_text.insert("1.0", json.dumps(p["extra"]))
        self.model_box["values"] = []
        self.test_var.set("")
        self.key_link.configure(text=f"Get a key ({p['key_url'].split('/')[2]})" if p.get("key_url") else "Get a key",
                                state="normal" if p.get("key_url") else "disabled")
        self.active_btn.configure(text="Active provider" if pid == self.data["provider"] else "Use this provider")
        self._key_state()

    def _key_state(self) -> None:
        state = "normal" if self.needs_key.get() else "disabled"
        self.key_entry.configure(state=state)

    def _toggle_key(self) -> None:
        hidden = self.key_entry.cget("show") != ""
        self.key_entry.configure(show="" if hidden else "•")
        self.show_btn.configure(text="Hide" if hidden else "Show")

    def _paste_key(self) -> None:
        try:
            self.pv["key"].set(self.clipboard_get().strip())
        except tk.TclError:
            pass

    def _open_key_url(self) -> None:
        url = self.data["providers"].get(self.current_pid, {}).get("key_url")
        if url:
            webbrowser.open(url)

    def _add_provider(self, preset: str) -> None:
        if not self._store_form():
            return
        base = re.sub(r"[^a-z0-9]+", "-", preset.lower()).strip("-")
        pid, n = base, 2
        while pid in self.data["providers"]:
            pid, n = f"{base}-{n}", n + 1
        self.data["providers"][pid] = {"label": preset if preset != "Blank" else "New provider", **copy.deepcopy(PRESETS[preset])}
        self.keys[pid] = ""
        self._fill_provider_list()
        self.provider_list.selection_clear(0, "end")
        self.provider_list.selection_set(list(self.data["providers"]).index(pid))
        self._on_provider_select()

    def _remove_provider(self) -> None:
        pid = self._selected_pid()
        if not pid:
            return
        if len(self.data["providers"]) == 1:
            messagebox.showinfo("Settings", "Keep at least one provider.", parent=self)
            return
        if not messagebox.askyesno("Remove", f"Remove '{self.data['providers'][pid]['label']}'?", parent=self):
            return
        del self.data["providers"][pid]
        self.keys[pid] = ""  # deleted from Credential Manager on save
        if self.data["provider"] == pid:
            self.data["provider"] = next(iter(self.data["providers"]))
        self.current_pid = None
        self._fill_provider_list()
        self.provider_list.selection_set(0)
        self._on_provider_select()

    def _make_active(self) -> None:
        if self.current_pid and self._store_form():
            self.data["provider"] = self.current_pid
            self._fill_provider_list()
            self.provider_list.selection_set(list(self.data["providers"]).index(self.current_pid))
            self.active_btn.configure(text="Active provider")

    def _form_provider(self) -> Provider | None:
        if not self._store_form():
            return None
        p = self.data["providers"][self.current_pid]
        return Provider(id=self.current_pid, label=p["label"], kind=p["kind"], base_url=fast_local(p["base_url"]),
                        model=p["model"], api_key=self.keys[self.current_pid] if p["needs_key"] else None,
                        rpm=0, workers=1)

    def _fetch_models(self) -> None:
        provider = self._form_provider()
        if not provider:
            return
        self.test_var.set("Fetching models...")

        def done(result):
            if isinstance(result, Exception):
                self.test_var.set(f"Could not fetch models: {result}")
            else:
                self.model_box["values"] = result
                self.test_var.set(f"{len(result)} model(s) available - open the Model list to choose.")
        self.run_bg(lambda: Client(provider, timeout=20).list_models(), done)

    def _test(self) -> None:
        provider = self._form_provider()
        if not provider:
            return
        self.test_var.set(f"Testing {provider.model or '(no model)'}...")
        extra = self.data["providers"][self.current_pid].get("extra") or {}

        def work():
            if provider.kind == "ollama":
                ollama.start(ollama.api_base(provider.base_url))
            client = Client(provider, timeout=60, extra_body=extra)
            try:
                return client.test()
            finally:
                client.close()

        self.run_bg(work, lambda r: self.test_var.set(f"Failed: {r}" if isinstance(r, Exception) else r))

    # ------------------------------------------------------------ local models tab
    def _ollama_provider_id(self) -> str | None:
        if self.data["providers"].get(self.data["provider"], {}).get("kind") == "ollama":
            return self.data["provider"]
        return next((pid for pid, p in self.data["providers"].items() if p.get("kind") == "ollama"), None)

    def _ollama_base(self) -> str:
        pid = self._ollama_provider_id()
        url = self.data["providers"][pid]["base_url"] if pid else DEFAULTS["providers"]["local"]["base_url"]
        return ollama.api_base(url)

    def _tab_local(self, f: ttk.Frame) -> None:
        head = ttk.Frame(f)
        head.pack(fill="x")
        self.ollama_status = tk.StringVar(value="Checking Ollama...")
        ttk.Label(head, textvariable=self.ollama_status, font=("Segoe UI Semibold", 11)).pack(side="left")
        ttk.Button(head, text="Refresh", command=self._refresh_local).pack(side="right")
        self.start_btn = ttk.Button(head, text="Start Ollama", command=self._start_ollama)
        self.start_btn.pack(side="right", padx=6)
        self.install_btn = ttk.Button(head, text="Install Ollama", command=self._install_ollama)
        self.install_btn.pack(side="right")

        ttk.Label(f, text="Installed models", font=("Segoe UI Semibold", 11)).pack(anchor="w", pady=(16, 4))
        box = ttk.Frame(f)
        box.pack(fill="both", expand=True)
        self.models_tree = ttk.Treeview(box, columns=("name", "size"), show="headings", height=6)
        self.models_tree.heading("name", text="Model")
        self.models_tree.heading("size", text="Size on disk")
        self.models_tree.column("size", width=120, stretch=False)
        self.models_tree.pack(side="left", fill="both", expand=True)
        side = ttk.Frame(box, padding=(10, 0, 0, 0))
        side.pack(side="left", fill="y")
        ttk.Button(side, text="Use this model", style="Accent.TButton", command=self._use_local_model).pack(fill="x")
        ttk.Button(side, text="Delete", command=self._delete_local_model).pack(fill="x", pady=6)

        ttk.Label(f, text="Download a model", font=("Segoe UI Semibold", 11)).pack(anchor="w", pady=(16, 4))
        dl = ttk.Frame(f)
        dl.pack(fill="x")
        self.pull_var = tk.StringVar(value=ollama.RECOMMENDED[0][0])
        pull_box = ttk.Combobox(dl, textvariable=self.pull_var, values=[n for n, _ in ollama.RECOMMENDED], width=30)
        pull_box.pack(side="left")
        pull_box.bind("<<ComboboxSelected>>", lambda e: self._describe_pull())
        ttk.Button(dl, text="Check size", command=self._check_size).pack(side="left", padx=6)
        self.pull_btn = ttk.Button(dl, text="Download", command=self._pull)
        self.pull_btn.pack(side="left")
        ttk.Button(dl, text="Cancel", command=self.cancel_download.set).pack(side="left", padx=6)
        ttk.Button(dl, text="Browse ollama.com", command=lambda: webbrowser.open("https://ollama.com/search?c=vision")).pack(side="right")
        self.pull_info = tk.StringVar()
        ttk.Label(f, textvariable=self.pull_info, foreground="#8a8a8a").pack(anchor="w", pady=4)
        self.pull_progress = ttk.Progressbar(f, mode="determinate")
        self.pull_progress.pack(fill="x", pady=(4, 0))
        self.pull_status = tk.StringVar()
        ttk.Label(f, textvariable=self.pull_status).pack(anchor="w", pady=4)
        ttk.Label(f, text="Tip: models that read images (\"vision\") can also name photos and scanned PDFs.",
                  foreground="#8a8a8a").pack(anchor="w", side="bottom")
        self._describe_pull()
        self._refresh_local()

    def _describe_pull(self) -> None:
        desc = dict(ollama.RECOMMENDED).get(self.pull_var.get(), "Any model name from ollama.com/library works, e.g. llava:7b")
        self.pull_info.set(desc)

    def _refresh_local(self) -> None:
        base = self._ollama_base()

        def work():
            installed = ollama.exe() is not None
            running = ollama.is_running(base)
            models = ollama.list_models(base) if running else []
            return installed, running, ollama.version(base) if running else None, models

        def done(result):
            if isinstance(result, Exception):
                self.ollama_status.set(f"Ollama: error - {result}")
                return
            installed, running, ver, models = result
            if running:
                self.ollama_status.set(f"Ollama {ver}: running  ✓")
            elif installed:
                self.ollama_status.set("Ollama: installed, not running")
            else:
                self.ollama_status.set("Ollama: not installed (needed for local models)")
            self.install_btn.configure(state="disabled" if installed or running else "normal")
            self.start_btn.configure(state="normal" if installed and not running else "disabled")
            self.models_tree.delete(*self.models_tree.get_children())
            for m in models:
                self.models_tree.insert("", "end", iid=m["name"], values=(m["name"], ollama.human(m["size"])))
        self.run_bg(work, done)

    def _start_ollama(self) -> None:
        self.ollama_status.set("Starting Ollama...")
        self.run_bg(lambda: ollama.start(self._ollama_base()), lambda r: self._refresh_local())

    def _install_ollama(self) -> None:
        self.pull_status.set("Checking download size...")

        def confirm(size):
            size_text = ollama.human(size) if isinstance(size, int) else "about 1 GB"
            if not messagebox.askyesno("Install Ollama", f"Download and install Ollama ({size_text})?\n\n"
                                       "It installs for your user only and runs local models on your GPU.", parent=self):
                self.pull_status.set("")
                return
            self.cancel_download.clear()
            self.run_bg(lambda: ollama.install(self._progress, self.cancel_download), installed)

        def installed(result):
            self.pull_status.set(f"Install failed: {result}" if isinstance(result, Exception) else "Ollama installed.")
            self._start_ollama()
        self.run_bg(ollama.installer_size, confirm)

    def _progress(self, status: str, done: int, total: int) -> None:
        def update():
            if total:
                self.pull_progress.configure(mode="determinate", maximum=total, value=done)
                self.pull_status.set(f"{status}  {ollama.human(done)} / {ollama.human(total)}  ({done / total:.0%})")
            else:
                self.pull_status.set(status)
        self.ui(update)

    def _check_size(self) -> None:
        name = self.pull_var.get().strip()
        self.pull_info.set("Checking...")
        self.run_bg(lambda: ollama.remote_size(name),
                    lambda size: self.pull_info.set(f"{name}: {ollama.human(size)} download" if isinstance(size, int)
                                                    else f"{name}: size unknown (check the name)"))

    def _pull(self) -> None:
        name = self.pull_var.get().strip()
        if not name:
            return
        base = self._ollama_base()

        def confirm(size):
            size_text = ollama.human(size) if isinstance(size, int) else "unknown size"
            if not messagebox.askyesno("Download model", f"Download {name} ({size_text})?", parent=self):
                return
            self.cancel_download.clear()
            self.pull_btn.configure(state="disabled")

            def work():
                if not ollama.start(base):
                    raise RuntimeError("Ollama is not installed or won't start")
                ollama.pull(base, name, self._progress, self.cancel_download)

            def done(result):
                self.pull_btn.configure(state="normal")
                self.pull_status.set(f"Download failed: {result}" if isinstance(result, Exception) else f"{name} is ready.")
                self._refresh_local()
            self.run_bg(work, done)
        self.run_bg(lambda: ollama.remote_size(name), confirm)

    def _use_local_model(self) -> None:
        sel = self.models_tree.selection()
        pid = self._ollama_provider_id()
        if not sel or not pid:
            return
        self._store_form()
        self.data["providers"][pid]["model"] = sel[0]
        if self.current_pid == pid:
            self.pv["model"].set(sel[0])
        self.pull_status.set(f"'{self.data['providers'][pid]['label']}' will use {sel[0]} (click Save).")

    def _delete_local_model(self) -> None:
        sel = self.models_tree.selection()
        if not sel or not messagebox.askyesno("Delete model", f"Delete {sel[0]} from disk?", parent=self):
            return
        self.run_bg(lambda: ollama.delete(self._ollama_base(), sel[0]), lambda r: self._refresh_local())

    # ------------------------------------------------------------ naming tab
    def _tab_naming(self, f: ttk.Frame) -> None:
        n = self.data["naming"]
        self.nv = {
            "separator": tk.StringVar(value=n["separator"]),
            "case": tk.StringVar(value=n["case"]),
            "date_position": tk.StringVar(value=n["date_position"]),
            "max_length": tk.StringVar(value=str(n["max_length"])),
        }
        groups = [
            ("Word separator", "separator", [(" ", "Space   (ACME Invoice INV-2041)"), ("_", "Underscore   (ACME_Invoice_INV-2041)"),
                                             ("-", "Hyphen   (ACME-Invoice-INV-2041)")]),
            ("Capitalization", "case", [("title", "Title Case"), ("lower", "lowercase"), ("keep", "Exactly as the AI wrote it")]),
            ("Date (only added when the date matters, e.g. invoices, statements)", "date_position",
             [("start", "At the start (sorts by date)"), ("end", "At the end"), ("never", "Never add dates")]),
        ]
        for title, key, options in groups:
            ttk.Label(f, text=title, font=("Segoe UI Semibold", 11)).pack(anchor="w", pady=(8, 2))
            row = ttk.Frame(f)
            row.pack(anchor="w")
            for value, text in options:
                ttk.Radiobutton(row, text=text, value=value, variable=self.nv[key], command=self._naming_preview).pack(side="left", padx=(0, 16))
        ttk.Label(f, text="Maximum name length (characters)", font=("Segoe UI Semibold", 11)).pack(anchor="w", pady=(12, 2))
        spin = ttk.Spinbox(f, from_=20, to=200, textvariable=self.nv["max_length"], width=8, command=self._naming_preview)
        spin.pack(anchor="w")
        spin.bind("<KeyRelease>", lambda e: self._naming_preview())
        ttk.Label(f, text="Preview", font=("Segoe UI Semibold", 11)).pack(anchor="w", pady=(18, 2))
        self.preview_var = tk.StringVar()
        ttk.Label(f, textvariable=self.preview_var, font=("Consolas", 11), justify="left").pack(anchor="w")
        ttk.Label(f, text="Names are always in English. Change what the AI writes in the Prompt tab.",
                  foreground="#8a8a8a").pack(anchor="w", pady=(18, 0))
        self._naming_preview()

    def _naming_values(self) -> dict:
        try:
            max_len = max(20, min(200, int(self.nv["max_length"].get())))
        except ValueError:
            max_len = self.data["naming"]["max_length"]
        return {"separator": self.nv["separator"].get(), "case": self.nv["case"].get(),
                "date_position": self.nv["date_position"].get(), "max_length": max_len}

    def _naming_preview(self) -> None:
        naming = self._naming_values()
        examples = [
            (Suggestion("ACME invoice INV-2041", date="2025-03-04", date_relevant=True), ".pdf"),
            (Suggestion("bank statement CPA account 0042", date="2025-01", date_relevant=True), ".pdf"),
            (Suggestion("chicken tajine with olives recipe", date="2019-05-01", date_relevant=False), ".docx"),
            (Suggestion("sunset over Oran harbor", date=None), ".jpg"),
        ]
        self.preview_var.set("\n".join(build_stem(s, naming) + ext for s, ext in examples))

    # ------------------------------------------------------------ prompt tab
    def _tab_prompt(self, f: ttk.Frame) -> None:
        ttk.Label(f, text="Instructions sent to the AI with every file. Edit freely; the reply must stay the same JSON shape.",
                  wraplength=820).pack(anchor="w")
        self.prompt_text = tk.Text(f, font=("Consolas", 10), wrap="word", height=20, borderwidth=0, highlightthickness=1, undo=True, **tk_colors())
        self.prompt_text.pack(fill="both", expand=True, pady=8)
        self.prompt_text.insert("1.0", self.data.get("prompt") or DEFAULT_PROMPT)
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Button(row, text="Reset to default",
                   command=lambda: (self.prompt_text.delete("1.0", "end"), self.prompt_text.insert("1.0", DEFAULT_PROMPT))).pack(side="left")
        self.temp_var = tk.StringVar(value=str(self.data["temperature"]))
        self.timeout_var = tk.StringVar(value=str(self.data["timeout"]))
        ttk.Spinbox(row, from_=10, to=900, increment=10, textvariable=self.timeout_var, width=6).pack(side="right")
        ttk.Label(row, text="Timeout (s)").pack(side="right", padx=(16, 6))
        ttk.Spinbox(row, from_=0, to=1.5, increment=0.1, textvariable=self.temp_var, width=6).pack(side="right")
        ttk.Label(row, text="Creativity (temperature)").pack(side="right", padx=6)

    # ------------------------------------------------------------ files tab
    def _tab_files(self, f: ttk.Frame) -> None:
        fl = self.data["files"]
        self.fv = {
            "recursive": tk.BooleanVar(value=fl["recursive"]),
            "skip_renamed": tk.BooleanVar(value=fl["skip_renamed"]),
            "max_chars": tk.StringVar(value=str(fl["max_chars"])),
            "image_max_side": tk.StringVar(value=str(fl["image_max_side"])),
            "pdf_scan_pages": tk.StringVar(value=str(fl["pdf_scan_pages"])),
        }
        ttk.Checkbutton(f, text="Include subfolders when adding a folder", variable=self.fv["recursive"]).pack(anchor="w")
        ttk.Checkbutton(f, text="Skip files this app already renamed (when adding folders)", variable=self.fv["skip_renamed"]).pack(anchor="w", pady=4)
        grid = ttk.Frame(f)
        grid.pack(anchor="w", pady=10)
        for i, (key, text, lo, hi, step) in enumerate((
                ("max_chars", "Characters of text sent per file (more = better but slower)", 500, 50000, 500),
                ("image_max_side", "Image size sent, in pixels (smaller = less data)", 256, 3000, 128),
                ("pdf_scan_pages", "Pages read from scanned PDFs", 1, 10, 1))):
            ttk.Label(grid, text=text).grid(row=i, column=0, sticky="w", pady=4, padx=(0, 12))
            ttk.Spinbox(grid, from_=lo, to=hi, increment=step, textvariable=self.fv[key], width=8).grid(row=i, column=1, sticky="w")
        ttk.Label(f, text="File types to process (separate with spaces). Unknown types are read as plain text.",
                  font=("Segoe UI Semibold", 11)).pack(anchor="w", pady=(10, 4))
        self.ext_text = tk.Text(f, height=5, font=("Consolas", 10), wrap="word", borderwidth=0, highlightthickness=1, **tk_colors())
        self.ext_text.pack(fill="x")
        self.ext_text.insert("1.0", " ".join(fl["extensions"]))
        ttk.Button(f, text="Reset file types", command=lambda: (self.ext_text.delete("1.0", "end"),
                   self.ext_text.insert("1.0", " ".join(DEFAULTS["files"]["extensions"])))).pack(anchor="w", pady=6)

    # ------------------------------------------------------------ integration tab
    def _tab_integration(self, f: ttk.Frame) -> None:
        ttk.Label(f, text="Where AI Renamer appears in Windows", font=("Segoe UI Semibold", 11)).pack(anchor="w")
        self.iv = {key: tk.BooleanVar() for key, _ in INTEGRATIONS}
        for key, text in INTEGRATIONS:
            ttk.Checkbutton(f, text=text, variable=self.iv[key]).pack(anchor="w", pady=3)
        ttk.Label(f, text="On Windows 11 the right-click entry is under \"Show more options\" (or Shift + right-click).",
                  foreground="#8a8a8a").pack(anchor="w", pady=(6, 0))
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=12)
        ttk.Button(row, text="Apply these", style="Accent.TButton", command=self._apply_integration).pack(side="left")
        self.int_status = tk.StringVar()
        ttk.Label(row, textvariable=self.int_status).pack(side="left", padx=12)
        self.run_bg(shell.status, self._show_integration)

    def _show_integration(self, result) -> None:
        if isinstance(result, Exception):
            self.int_status.set(f"Could not read status: {result}")
            return
        for key, value in result.items():
            self.iv[key].set(value)

    def _apply_integration(self) -> None:
        wanted = {k for k, v in self.iv.items() if v.get()}
        unwanted = {k for k, v in self.iv.items() if not v.get()}
        self.int_status.set("Applying...")

        def work():
            if unwanted:
                shell.uninstall(unwanted)
            if wanted:
                shell.install(wanted)
            return shell.status()

        def done(result):
            if isinstance(result, Exception):
                self.int_status.set(f"Failed: {result}")
            else:
                self._show_integration(result)
                self.int_status.set("Done.")
        self.run_bg(work, done)

    # ------------------------------------------------------------ save
    def _open_log(self) -> None:
        if LOG_FILE.exists():
            os.startfile(LOG_FILE)

    def _changed(self) -> bool:
        if not self._collect(quiet=True):
            return True
        saved = copy.deepcopy(self.settings.data)
        saved["files"]["extensions"] = sorted(saved["files"]["extensions"])
        keys_changed = any(key != (get_api_key(pid) or "") for pid, key in self.keys.items()
                           if self.data["providers"].get(pid, {}).get("needs_key"))
        return keys_changed or json.dumps(saved, sort_keys=True) != json.dumps(self.data, sort_keys=True)

    def close(self) -> None:
        """Cancel / window X: never drop changes (like a pasted API key) without asking."""
        if self._changed():
            answer = messagebox.askyesnocancel("Settings", "Save your changes?", parent=self)
            if answer is None:
                return
            if answer:
                self.save()
                return
        self.destroy()

    def _collect(self, quiet: bool = False) -> bool:
        """Copy every tab's widgets into self.data. False if something is invalid."""
        if not self._store_form():
            return False
        self.data["naming"] = self._naming_values()
        prompt = self.prompt_text.get("1.0", "end").strip()
        self.data["prompt"] = "" if prompt == DEFAULT_PROMPT.strip() else prompt
        try:
            self.data["temperature"] = max(0.0, min(2.0, float(self.temp_var.get())))
            self.data["timeout"] = max(10, int(float(self.timeout_var.get())))
            files = self.data["files"]
            files["recursive"] = self.fv["recursive"].get()
            files["skip_renamed"] = self.fv["skip_renamed"].get()
            for key in ("max_chars", "image_max_side", "pdf_scan_pages"):
                files[key] = max(1, int(self.fv[key].get()))
        except ValueError:
            if not quiet:
                messagebox.showerror("Settings", "Some number fields are not valid numbers.", parent=self)
            return False
        exts = re.split(r"[\s,;]+", self.ext_text.get("1.0", "end").strip().lower())
        self.data["files"]["extensions"] = sorted({e if e.startswith(".") else "." + e for e in exts if e.strip(".")})
        return True

    def save(self) -> None:
        if not self._collect():
            return
        for pid, key in self.keys.items():
            if pid not in self.data["providers"] or not self.data["providers"][pid].get("needs_key"):
                if pid not in self.data["providers"]:
                    set_api_key(pid, None)
                continue
            if key != (get_api_key(pid) or ""):
                set_api_key(pid, key or None)
        self.settings.data = self.data
        self.settings.save()
        self.on_saved()
        self.destroy()
