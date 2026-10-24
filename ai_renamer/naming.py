"""Build safe filenames from a model suggestion (no model involved here)."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from .llm import Suggestion

INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "vs", "via", "with", "from"}


def title_case(text: str) -> str:
    words = text.split(" ")
    out = []
    for i, w in enumerate(words):
        # Leave mixed case, acronyms and tokens with digits (v2, 3rd, x86) alone.
        if w[:1].isalpha() and w.islower() and not any(c.isdigit() for c in w) and not (i > 0 and w in SMALL_WORDS):
            w = w[0].upper() + w[1:]
        out.append(w)
    return " ".join(out)


def clean_title(title: str, case: str = "title") -> str:
    title = unicodedata.normalize("NFC", title)
    title = INVALID.sub(" ", title)
    title = re.sub(r"[_\s]+", " ", title).strip(" .-")
    if case == "title":
        title = title_case(title)
    elif case == "lower":
        title = title.lower()
    return title


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit(" ", 1)[0]
    return (cut if len(cut) > max_len // 2 else text[:max_len]).strip(" .-_")


def build_stem(s: Suggestion, naming: dict) -> str:
    sep = naming.get("separator", " ")
    max_len = int(naming.get("max_length", 80))
    position = naming.get("date_position", "start")
    title = clean_title(s.title, naming.get("case", "title"))
    date = s.date if (s.date_relevant and s.date and position != "never") else None
    if date:
        # The date is added separately; don't repeat it (or a year-only date) inside the title.
        stripped = re.sub(rf"(?<![\w-]){re.escape(date)}(?![\w-])", " ", title)
        stripped = re.sub(r"\s+", " ", stripped).strip(" -_")
        title = stripped or title
    room = max_len - (len(date) + len(sep) if date else 0)
    title = _truncate(title, max(room, 10))
    if sep != " ":
        title = title.replace(" ", sep)
        title = re.sub(re.escape(sep) + "{2,}", sep, title)
    parts = [title]
    if date:
        parts.insert(0 if position == "start" else 1, date)
    stem = sep.join(parts).strip(" .")
    if stem.split(".")[0].upper() in RESERVED:
        stem = f"{stem}{sep}file"
    return stem or "Untitled"


def numbered(stem: str, n: int, sep: str) -> str:
    return f"{stem} ({n})" if sep == " " else f"{stem}{sep}{n}"


def resolve_target(source: Path, stem: str, sep: str, taken: set[str]) -> Path:
    """Pick a target path that does not overwrite anything.

    `taken` holds lower-cased paths already claimed by other files in this batch.
    """
    ext = source.suffix
    n = 1
    while True:
        candidate = source.with_name((stem if n == 1 else numbered(stem, n, sep)) + ext)
        key = str(candidate).lower()
        same_file = key == str(source).lower()
        if same_file or (key not in taken and not candidate.exists()):
            taken.add(key)
            return candidate
        n += 1
