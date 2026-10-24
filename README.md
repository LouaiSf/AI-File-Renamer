# AI Renamer

Renames files from what's inside them. `scan0001.pdf` becomes `2025-01 BNP Paribas Bank Statement Account 0042.pdf`, and `IMG_20250712_093112.jpg` becomes `2025-07-12 Air Algerie Boarding Pass Algiers to Paris AH1004.jpg`.

- Reads PDFs (including scanned ones), Word, Excel, PowerPoint, text and code files, and photos and screenshots.
- Names are always in English. A date is added only when it matters (invoices, statements, tickets), never for recipes, manuals and the like.
- Uses a **local model** (Ollama, private and free) or a **cloud API** (Gemini, OpenRouter, Groq, OpenAI, Claude…). You switch in the app.
- Safe:
  - You always see the new names before anything changes.
  - Files are never overwritten.
  - Every batch can be undone.

## Using it

| Where | How |
|---|---|
| Desktop / Start menu | **AI Renamer** icon. Drop files or folders on the window. |
| Explorer | Right-click files, a folder, or empty space in a folder → **Rename with AI**. On Windows 11 it's under *Show more options* (or Shift + right-click). |
| Many files at once | Select them → right-click → **Send to → Rename with AI**. |
| Terminal | `rename-ai <files or folders>` (see below). |

In the window:
- New names appear as each file is analyzed.
- Double-click a name to edit it. Click ☑ to include or exclude a file.
- **Rename** applies the checked names.
- Low-confidence names start unchecked so you can review them.
- **History / Undo** reverts any past batch.

## Settings (everything is editable, no code changes)

- **AI provider:**
  - Switch providers, add any OpenAI-compatible service from presets, and set the model (with **Fetch list**).
  - Paste API keys and run **Test connection**. Keys are stored in Windows Credential Manager.
  - Set the requests-per-minute limit and how many files run at once.
- **Local models:** install or start Ollama, see installed models with their sizes, check a model's size before downloading it, download or delete models.
- **Naming:**
  - Separator (space, `_` or `-`), capitalization and maximum length
  - Where the date goes, or never add one
  - Live preview
- **Prompt:** the instructions sent to the AI. Edit freely; *Reset to default* restores them.
- **Files:** subfolders, skipping already-renamed files, how much text and image detail is sent, and which file types are processed.
- **Integration:** turn the right-click menu, Send To, desktop icon, Start menu entry and terminal command on or off.

### Getting a Gemini key (free)
Settings → AI provider → *Google Gemini* → **Get a key** opens https://aistudio.google.com/apikey. Create a key, copy it, click **Paste**, then **Test connection** and **Use this provider**.
The free tier is small (reportedly about 5 requests per minute and 20 per day for gemini-3.5-flash; check yours in AI Studio). The app spaces requests automatically. Google may use free-tier data to improve its products, so prefer the local model for private documents.

## Terminal

```
rename-ai PATH [PATH ...]      preview, then asks before renaming
  -p gemini                    use another provider for this run
  -r                           include subfolders
  -n                           dry run (only show names)
  -y                           don't ask
rename-ai undo [BATCH]         undo the last (or a given) batch
rename-ai history              list past batches
rename-ai providers | use ID   list / switch providers
rename-ai set-key ID           store an API key (hidden input)
rename-ai models [ID]          list models a provider offers
rename-ai test [ID]            check a provider works
rename-ai install | uninstall  add/remove Windows integration
```

## Data and files

Settings, history and the log are in `%APPDATA%\AIRenamer` (Settings → *Open data folder*). Local models are in `%USERPROFILE%\.ollama`.

Data use with a cloud provider:
- Documents: only the first ~6,000 characters of text are sent.
- Photos: shrunk to 1024 px first (about 150–300 KB each).

## Development

```
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\python -m unittest discover -s tests -t .
.venv\Scripts\python tools\make_icon.py      # regenerate the icon
```

Code layout (`ai_renamer/`):

| Module | Role |
|---|---|
| `content.py` | file → text / images / hints |
| `llm.py` | OpenAI-compatible client, prompt, reply parsing, rate limiting |
| `naming.py` | suggestion → safe filename, conflict handling |
| `engine.py` | scan → analyze → rename, shared by CLI and GUI |
| `journal.py` | history and undo |
| `ollama.py` | local runtime management |
| `shell.py` | Windows integration and single-window handoff |
| `gui.py`, `gui_settings.py` | the windows |
| `cli.py` | the `rename-ai` command |

If you move this folder, run `rename-ai install` again from the new location (or use Settings → Integration).
