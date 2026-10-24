"""Settings storage.

Everything user-adjustable lives in %APPDATA%/AIRenamer/settings.json and is
edited from the Settings window. API keys are kept out of that file and stored
in the Windows Credential Manager via `keyring`.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path

import keyring
import keyring.errors

APP_NAME = "AIRenamer"
KEYRING_SERVICE = "ai-renamer"

DATA_DIR = Path(os.environ.get("AI_RENAMER_HOME") or Path(os.environ.get("APPDATA", Path.home())) / APP_NAME)
SETTINGS_FILE = DATA_DIR / "settings.json"
HISTORY_FILE = DATA_DIR / "history.jsonl"
LOG_FILE = DATA_DIR / "ai-renamer.log"

DEFAULT_PROMPT = """You rename files based on their content. You receive a file's extracted text and/or images, plus hints such as the current filename (which may be meaningless, e.g. "IMG_2041" or "scan001").

Reply with ONLY a JSON object:
{
  "title": "short descriptive filename in English, 2-8 words, no date, no extension",
  "category": "one or two words, e.g. Invoice, Contract, Recipe, Photo, Screenshot, Notes",
  "date": "YYYY-MM-DD, YYYY-MM or YYYY, or null",
  "date_relevant": true or false,
  "confidence": number between 0 and 1
}

Rules for "title":
- Always English, even if the document is in another language: translate titles and descriptions (e.g. "Tajine de poulet aux olives" -> "Chicken Tajine With Olives Recipe"). Only names of people, companies, places and products keep their original spelling.
- Be specific: say who/what it is about, e.g. "ACME Invoice INV-2041", "Rental Agreement Dar Immobilier", "Chicken Tajine With Olives Recipe", "Passport Louai", "Sunset Over Oran Harbor".
- Include an identifying number (invoice, order, contract number) when there is one.
- Do not put the date in the title. Do not invent facts that are not in the content.

Rules for "date":
- The date the document is about (issue date, statement period, meeting date, photo date), not today's date.
- Ambiguous numeric dates like 03/04/2025 are day-first unless the document is clearly American.
- Use month precision for monthly things (statements, payslips) and year precision for yearly things (tax returns).

Rules for "date_relevant":
- true only when the date identifies the document: invoices, receipts, bank statements, payslips, letters, tickets, bookings, meeting notes, reports, event photos.
- false for timeless content: recipes, manuals, books, articles, CVs, code, IDs, generic images, artwork.

If the content is unreadable or empty, give your best guess from the hints and set confidence below 0.3."""

DEFAULTS: dict = {
    "provider": "local",
    "providers": {
        "local": {
            "label": "Local (Ollama)",
            "kind": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen3-vl:4b-instruct",
            "needs_key": False,
            "rpm": 0,
            "workers": 1,
            "key_url": "",
        },
        "gemini": {
            "label": "Google Gemini",
            "kind": "openai",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "model": "gemini-3.5-flash", "extra": {"reasoning_effort": "minimal"},
            "needs_key": True,
            "rpm": 5,
            "workers": 3,
            "key_url": "https://aistudio.google.com/apikey",
        },
    },
    "prompt": "",                  # empty = DEFAULT_PROMPT
    "temperature": 0.2,
    "timeout": 300,
    "naming": {
        "separator": " ",          # " ", "_" or "-"
        "case": "title",           # "title", "lower" or "keep"
        "date_position": "start",  # "start", "end" or "never"
        "max_length": 80,
    },
    "files": {
        "recursive": False,
        "skip_renamed": True,      # when scanning folders, skip files this app already renamed
        "max_chars": 6000,         # text sent to the model per file
        "image_max_side": 1024,    # images are shrunk to this before sending
        "pdf_scan_pages": 2,       # pages rendered as images when a PDF has no text
        "extensions": [
            ".pdf", ".docx", ".xlsx", ".pptx",
            ".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".rtf", ".log",
            ".py", ".js", ".ts", ".java", ".c", ".cpp", ".cs", ".go", ".rs", ".sql", ".yaml", ".yml", ".ini",
            ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic",
        ],
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key != "providers":
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@dataclass
class Provider:
    id: str
    label: str
    kind: str
    base_url: str
    model: str
    api_key: str | None
    rpm: int
    workers: int


class Settings:
    def __init__(self, data: dict | None = None):
        self.data = _merge(DEFAULTS, data or {})
        if not self.data["providers"]:
            self.data["providers"] = copy.deepcopy(DEFAULTS["providers"])
        if self.data["provider"] not in self.data["providers"]:
            self.data["provider"] = next(iter(self.data["providers"]))

    @classmethod
    def load(cls) -> "Settings":
        if SETTINGS_FILE.exists():
            try:
                return cls(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
        return cls()

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)

    @property
    def naming(self) -> dict:
        return self.data["naming"]

    @property
    def files(self) -> dict:
        return self.data["files"]

    @property
    def prompt(self) -> str:
        return self.data.get("prompt") or DEFAULT_PROMPT

    @property
    def extensions(self) -> set[str]:
        return {e.lower() if e.startswith(".") else "." + e.lower() for e in self.files["extensions"]}

    def provider(self, provider_id: str | None = None) -> Provider:
        provider_id = provider_id or self.data["provider"]
        if provider_id not in self.data["providers"]:
            raise ValueError(f"Unknown provider '{provider_id}'. Available: {', '.join(self.data['providers'])}")
        p = self.data["providers"][provider_id]
        return Provider(
            id=provider_id,
            label=p.get("label") or provider_id,
            kind=p.get("kind", "openai"),
            base_url=fast_local(p["base_url"]),
            model=p.get("model", ""),
            api_key=get_api_key(provider_id) if p.get("needs_key") else None,
            rpm=int(p.get("rpm") or 0),
            workers=max(1, int(p.get("workers") or 1)),
        )


def fast_local(url: str) -> str:
    """On Windows `localhost` tries IPv6 first and stalls ~2 s per connection; local servers listen on IPv4."""
    return url.rstrip("/").replace("://localhost:", "://127.0.0.1:").replace("://localhost/", "://127.0.0.1/")


def get_api_key(provider_id: str) -> str | None:
    env = os.environ.get(f"AI_RENAMER_{provider_id.upper()}_KEY")
    if env:
        return env
    try:
        return keyring.get_password(KEYRING_SERVICE, provider_id)
    except keyring.errors.KeyringError:
        return None


def set_api_key(provider_id: str, key: str | None) -> None:
    if key and key.strip():
        keyring.set_password(KEYRING_SERVICE, provider_id, key.strip())
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, provider_id)
        except keyring.errors.PasswordDeleteError:
            pass
