"""Windows integration: Explorer menu, Send To, shortcuts, PATH, single-instance window."""

from __future__ import annotations

import ctypes
import getpass
import os
import subprocess
import sys
import threading
import winreg
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Callable

ICON = Path(__file__).with_name("assets") / "icon.ico"
MENU_TEXT = "Rename with AI"
SHORTCUT_NAME = "AI Renamer"
VERB = "AIRenamer"
MENU_KEYS = {
    r"Software\Classes\*\shell\AIRenamer": "%1",
    r"Software\Classes\Directory\shell\AIRenamer": "%1",
    r"Software\Classes\Directory\Background\shell\AIRenamer": "%V",
}
PIPE = r"\\.\pipe\ai-renamer-" + "".join(c for c in getpass.getuser() if c.isalnum())
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- single instance

def hand_off(paths: list[str]) -> bool:
    """Send paths to an already-open window. Returns False if none is open."""
    try:
        conn = Client(PIPE, family="AF_PIPE")
    except OSError:
        return False
    with conn:
        conn.send(paths)
    return True


def serve(on_paths: Callable[[list[str]], None]) -> bool:
    """Become the window that receives paths from later launches. False if another already is."""
    try:
        listener = Listener(PIPE, family="AF_PIPE")
    except OSError:
        return False

    def loop():
        while True:
            try:
                with listener.accept() as conn:
                    on_paths(conn.recv())
            except (OSError, EOFError):
                continue

    threading.Thread(target=loop, daemon=True).start()
    return True


# ---------------------------------------------------------------- integration

def scripts_dir() -> Path:
    return Path(sys.executable).parent


def gui_exe() -> Path:
    exe = scripts_dir() / "rename-ai-gui.exe"
    return exe if exe.exists() else Path(sys.executable).with_name("pythonw.exe")


def _gui_command(arg: str) -> str:
    exe = gui_exe()
    if exe.name.lower() == "pythonw.exe":
        return f'"{exe}" -m ai_renamer.gui "{arg}"'
    return f'"{exe}" "{arg}"'


def _ps(script: str) -> str:
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, creationflags=NO_WINDOW)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    return out.stdout.strip()


def _q(s: str | Path) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _special_folder(name: str) -> Path:
    return Path(_ps(f"[Environment]::GetFolderPath({_q(name)})"))


def _shortcut_paths() -> dict[str, Path]:
    return {
        "desktop": _special_folder("Desktop") / f"{SHORTCUT_NAME}.lnk",
        "start_menu": _special_folder("Programs") / f"{SHORTCUT_NAME}.lnk",
        "send_to": _special_folder("SendTo") / f"{MENU_TEXT}.lnk",
    }


def _make_shortcut(link: Path, description: str) -> None:
    exe = gui_exe()
    args = "-m ai_renamer.gui" if exe.name.lower() == "pythonw.exe" else ""
    _ps(
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({_q(link)});"
        f"$s.TargetPath={_q(exe)};$s.Arguments={_q(args)};$s.IconLocation={_q(str(ICON) + ',0')};"
        f"$s.Description={_q(description)};$s.WorkingDirectory={_q(Path.home())};$s.Save()"
    )


def _user_path() -> list[str]:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
        try:
            value, _ = winreg.QueryValueEx(k, "Path")
        except FileNotFoundError:
            value = ""
    return [p for p in value.split(";") if p]


def _set_user_path(parts: list[str]) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, ";".join(parts))
    # Tell Explorer so new terminals see the change without logging out.
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, None)


def _same_dir(a: str, b: Path) -> bool:
    return os.path.normcase(os.path.expandvars(a)).rstrip("\\") == os.path.normcase(str(b)).rstrip("\\")


def status() -> dict[str, bool]:
    links = _shortcut_paths()
    menu = True
    for key in MENU_KEYS:
        try:
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, key).Close()
        except FileNotFoundError:
            menu = False
    return {
        "context_menu": menu,
        "send_to": links["send_to"].exists(),
        "desktop": links["desktop"].exists(),
        "start_menu": links["start_menu"].exists(),
        "path": any(_same_dir(p, scripts_dir()) for p in _user_path()),
    }


def install(what: set[str] | None = None) -> None:
    what = what or {"context_menu", "send_to", "desktop", "start_menu", "path"}
    links = _shortcut_paths()
    if "context_menu" in what:
        for key, arg in MENU_KEYS.items():
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
                winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, MENU_TEXT)
                winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, str(ICON))
                winreg.SetValueEx(k, "MultiSelectModel", 0, winreg.REG_SZ, "Player")
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key + r"\command") as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, _gui_command(arg))
    for name in ("send_to", "desktop", "start_menu"):
        if name in what:
            links[name].parent.mkdir(parents=True, exist_ok=True)
            _make_shortcut(links[name], "Rename files from their content with AI")
    if "path" in what:
        parts = _user_path()
        if not any(_same_dir(p, scripts_dir()) for p in parts):
            _set_user_path(parts + [str(scripts_dir())])


def uninstall(what: set[str] | None = None) -> None:
    what = what or {"context_menu", "send_to", "desktop", "start_menu", "path"}
    if "context_menu" in what:
        for key in MENU_KEYS:
            for sub in (key + r"\command", key):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
                except FileNotFoundError:
                    pass
    links = _shortcut_paths()
    for name in ("send_to", "desktop", "start_menu"):
        if name in what:
            links[name].unlink(missing_ok=True)
    if "path" in what:
        parts = _user_path()
        kept = [p for p in parts if not _same_dir(p, scripts_dir())]
        if kept != parts:
            _set_user_path(kept)
