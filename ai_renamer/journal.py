"""Rename history: every batch is appended to history.jsonl so it can be undone."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

from . import config

_lock = threading.Lock()


def _append(entry: dict) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock, open(config.HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _entries() -> list[dict]:
    if not config.HISTORY_FILE.exists():
        return []
    out = []
    with open(config.HISTORY_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a half-written last line after a crash
    return out


def record(ops: list[tuple[Path, Path]], provider: str = "") -> str:
    batch_id = uuid.uuid4().hex[:10]
    _append({
        "type": "batch",
        "id": batch_id,
        "time": datetime.now().isoformat(timespec="seconds"),
        "provider": provider,
        "ops": [{"old": str(a), "new": str(b)} for a, b in ops],
    })
    return batch_id


def batches() -> list[dict]:
    """All batches, newest first, each with an `undone` flag."""
    entries = _entries()
    undone = {e["id"] for e in entries if e.get("type") == "undo"}
    result = [dict(e, undone=e["id"] in undone) for e in entries if e.get("type") == "batch" and e.get("ops")]
    return list(reversed(result))


def last_batch() -> dict | None:
    return next((b for b in batches() if not b["undone"]), None)


def undo(batch_id: str) -> tuple[int, list[str]]:
    batch = next((b for b in batches() if b["id"] == batch_id), None)
    if not batch:
        raise ValueError(f"No batch {batch_id}")
    if batch["undone"]:
        raise ValueError("That batch was already undone")
    restored, problems = 0, []
    for op in reversed(batch["ops"]):
        old, new = Path(op["old"]), Path(op["new"])
        if not new.exists():
            problems.append(f"{new.name}: no longer exists")
        elif old.exists() and str(old).lower() != str(new).lower():
            problems.append(f"{new.name}: can't restore, '{old.name}' exists again")
        else:
            try:
                new.rename(old)
                restored += 1
            except OSError as e:
                problems.append(f"{new.name}: {e}")
    _append({"type": "undo", "id": batch_id, "time": datetime.now().isoformat(timespec="seconds")})
    return restored, problems


def renamed_paths() -> set[str]:
    """Lower-cased paths of files this app produced (and that weren't undone)."""
    return {op["new"].lower() for b in batches() if not b["undone"] for op in b["ops"]}
