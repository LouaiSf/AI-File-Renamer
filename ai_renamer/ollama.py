"""Manage the local Ollama runtime: install, start, list/pull/delete models."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"
REGISTRY = "https://registry.ollama.ai/v2"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Shown in Settings as one-click suggestions; any name from ollama.com/library also works.
RECOMMENDED = [
    ("qwen3-vl:4b-instruct", "Reads text + images, ~3-5 s per file. Best fit for an 8 GB GPU (recommended)"),
    ("qwen3-vl:2b-instruct", "Reads text + images, fastest, a bit less accurate. 1.9 GB"),
    ("qwen3-vl:8b-instruct", "Reads text + images, most accurate, slower. Needs the full 8 GB of VRAM"),
    ("gemma3:4b", "Reads text + images. Google's small model"),
    ("qwen3-vl:4b", "Thinking edition: reasons before answering, ~45 s per file. Not recommended for renaming"),
]

Progress = Callable[[str, int, int], None]  # (status, done, total)


def api_base(base_url: str) -> str:
    from .config import fast_local

    base = fast_local(base_url)
    return base[:-3] if base.endswith("/v1") else base


def exe() -> Path | None:
    found = shutil.which("ollama")
    if found:
        return Path(found)
    default = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return default if default.exists() else None


def is_running(base: str) -> bool:
    try:
        return httpx.get(f"{base}/api/version", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def version(base: str) -> str | None:
    try:
        return httpx.get(f"{base}/api/version", timeout=2).json().get("version")
    except (httpx.HTTPError, ValueError):
        return None


def start(base: str, wait: float = 20) -> bool:
    if is_running(base):
        return True
    path = exe()
    if not path:
        return False
    app = path.with_name("ollama app.exe")
    cmd = [str(app)] if app.exists() else [str(path), "serve"]
    subprocess.Popen(cmd, creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if is_running(base):
            return True
        time.sleep(0.5)
    return False


def list_models(base: str) -> list[dict]:
    r = httpx.get(f"{base}/api/tags", timeout=5)
    r.raise_for_status()
    return [{"name": m["name"], "size": m.get("size", 0)} for m in r.json().get("models", [])]


def remote_size(name: str) -> int | None:
    """Download size of a model from the Ollama registry, without downloading it."""
    repo, _, tag = name.partition(":")
    repo = repo if "/" in repo else f"library/{repo}"
    try:
        r = httpx.get(f"{REGISTRY}/{repo}/manifests/{tag or 'latest'}", timeout=10,
                      headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
        if r.status_code != 200:
            return None
        return sum(layer.get("size", 0) for layer in r.json().get("layers", []))
    except (httpx.HTTPError, ValueError):
        return None


def installer_size() -> int | None:
    try:
        r = httpx.head(INSTALLER_URL, follow_redirects=True, timeout=10)
        return int(r.headers.get("content-length", 0)) or None
    except (httpx.HTTPError, ValueError):
        return None


def pull(base: str, name: str, progress: Progress, cancel: threading.Event | None = None) -> None:
    with httpx.stream("POST", f"{base}/api/pull", json={"model": name, "stream": True}, timeout=None) as r:
        if r.status_code >= 400:
            r.read()
            raise RuntimeError(r.text[:300])
        layers: dict[str, tuple[int, int]] = {}
        for line in r.iter_lines():
            if cancel and cancel.is_set():
                raise RuntimeError("Cancelled")
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("error"):
                raise RuntimeError(msg["error"])
            if msg.get("digest") and msg.get("total"):
                layers[msg["digest"]] = (msg.get("completed", 0), msg["total"])
            done = sum(c for c, _ in layers.values())
            total = sum(t for _, t in layers.values())
            progress(msg.get("status", ""), done, total)


def delete(base: str, name: str) -> None:
    r = httpx.request("DELETE", f"{base}/api/delete", json={"model": name}, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(r.text[:300])


def install(progress: Progress, cancel: threading.Event | None = None) -> None:
    """Download the official installer and run it silently (per-user, no admin needed)."""
    target = Path(tempfile.gettempdir()) / "OllamaSetup.exe"
    with httpx.stream("GET", INSTALLER_URL, follow_redirects=True, timeout=httpx.Timeout(30, read=120)) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(target, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                if cancel and cancel.is_set():
                    raise RuntimeError("Cancelled")
                f.write(chunk)
                done += len(chunk)
                progress("Downloading Ollama", done, total)
    progress("Installing Ollama...", 0, 0)
    subprocess.run([str(target), "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"], check=True, creationflags=NO_WINDOW)
    target.unlink(missing_ok=True)


def human(n: int | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit in ("GB", "MB") else f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
