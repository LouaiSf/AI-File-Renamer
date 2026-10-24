"""Scan -> analyze (LLM) -> plan -> rename. Shared by the CLI and the GUI."""

from __future__ import annotations

import itertools
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import config, journal
from .config import Settings
from .content import ExtractionError, extract
from .llm import Client, LLMError, Suggestion, suggest
from .naming import INVALID, build_stem, resolve_target

log = logging.getLogger("ai_renamer")
_ids = itertools.count(1)

# Item statuses
PENDING, WORKING, READY, SAME, ERROR, DONE, SKIPPED = "pending", "working", "ready", "same", "error", "done", "skipped"


@dataclass
class Item:
    path: Path
    status: str = PENDING
    stem: str = ""
    message: str = ""
    checked: bool = True
    suggestion: Suggestion | None = None
    target: Path | None = None
    id: int = field(default_factory=lambda: next(_ids))

    @property
    def new_name(self) -> str:
        return self.stem + self.path.suffix if self.stem else ""


def setup_logging() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not log.handlers:
        h = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)


def collect(paths: Iterable[str | Path], settings: Settings, recursive: bool | None = None) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Expand files/folders into supported files. Returns (files, skipped-with-reason)."""
    recursive = settings.files["recursive"] if recursive is None else recursive
    exts = settings.extensions
    renamed = journal.renamed_paths() if settings.files["skip_renamed"] else set()
    files: dict[str, Path] = {}
    skipped: list[tuple[Path, str]] = []

    def hidden(p: Path) -> bool:
        return p.name.startswith((".", "~$")) or p.name.lower() in ("desktop.ini", "thumbs.db")

    for raw in paths:
        p = Path(raw).expanduser().resolve()
        if p.is_dir():
            walker = p.rglob("*") if recursive else p.iterdir()
            for f in walker:
                if not f.is_file() or hidden(f) or any(part.startswith(".") for part in f.relative_to(p).parts[:-1]):
                    continue
                if f.suffix.lower() not in exts:
                    continue
                if str(f).lower() in renamed:
                    skipped.append((f, "already renamed"))
                    continue
                files[str(f).lower()] = f
        elif p.is_file():
            if p.suffix.lower() in exts:
                files[str(p).lower()] = p  # explicitly chosen files are never skipped as "already renamed"
            else:
                skipped.append((p, f"unsupported type ({p.suffix or 'no extension'})"))
        else:
            skipped.append((p, "not found"))
    return sorted(files.values(), key=lambda f: (str(f.parent).lower(), f.name.lower())), skipped


def make_client(settings: Settings, provider_id: str | None = None) -> Client:
    provider = settings.provider(provider_id)
    extra = settings.data["providers"][provider.id].get("extra") or {}
    return Client(provider, timeout=float(settings.data["timeout"]), temperature=float(settings.data["temperature"]), extra_body=extra)


def sanitize_user_stem(text: str) -> str:
    return INVALID.sub("", text).strip(" .")


class Analyzer:
    """Generates names for items in parallel (respecting the provider's rate limit)."""

    def __init__(self, settings: Settings, provider_id: str | None = None, on_update: Callable[[Item], None] | None = None):
        self.settings = settings
        self.provider = settings.provider(provider_id)
        self.client = make_client(settings, provider_id)
        self.on_update = on_update or (lambda item: None)
        self.cancelled = threading.Event()

    def cancel(self) -> None:
        self.cancelled.set()

    def run(self, items: list[Item]) -> None:
        try:
            with ThreadPoolExecutor(max_workers=self.provider.workers) as pool:
                list(pool.map(self.analyze_one, items))
        finally:
            self.client.close()

    def analyze_one(self, item: Item) -> None:
        if self.cancelled.is_set():
            return
        item.status, item.message = WORKING, "Reading..."
        self.on_update(item)
        f = self.settings.files
        try:
            content = extract(item.path, int(f["max_chars"]), int(f["image_max_side"]), int(f["pdf_scan_pages"]))
            item.message = "Asking the model..."
            self.on_update(item)
            item.suggestion = suggest(self.client, self.settings.prompt, content)
            item.stem = build_stem(item.suggestion, self.settings.naming)
            if item.stem == item.path.stem:
                item.status, item.message, item.checked = SAME, "Already well named", False
            else:
                item.status = READY
                conf = item.suggestion.confidence
                item.message = f"{item.suggestion.category or 'File'} - {conf:.0%} sure"
                if conf < 0.35:
                    item.checked = False
                    item.message += " (please review)"
        except (ExtractionError, LLMError) as e:
            item.status, item.message, item.checked = ERROR, str(e), False
        except Exception as e:  # never let one file kill the batch
            log.exception("Unexpected error on %s", item.path)
            item.status, item.message, item.checked = ERROR, f"Unexpected error: {e}", False
        log.info("%s | %s | %s", item.path, item.stem, item.message)
        self.on_update(item)


def apply(items: list[Item], settings: Settings, provider_label: str = "") -> tuple[str | None, list[Item]]:
    """Rename every checked, ready item. Never overwrites. Returns (batch id, renamed items)."""
    sep = settings.naming.get("separator", " ")
    taken: set[str] = set()
    ops: list[tuple[Path, Path]] = []
    done: list[Item] = []
    for item in items:
        if not (item.checked and item.status == READY and item.stem):
            continue
        target = resolve_target(item.path, item.stem, sep, taken)
        if target == item.path:
            item.status, item.message = SAME, "Unchanged"
            continue
        try:
            os.rename(item.path, target)  # fails instead of overwriting if target appeared meanwhile
        except OSError as e:
            item.status, item.message = ERROR, f"Rename failed: {e.strerror or e}"
            log.error("Rename failed %s -> %s: %s", item.path, target, e)
            continue
        ops.append((item.path, target))
        item.target, item.status, item.message = target, DONE, "Renamed"
        done.append(item)
    batch_id = journal.record(ops, provider_label) if ops else None
    for old, new in ops:
        log.info("RENAMED %s -> %s", old, new.name)
    return batch_id, done
