"""Command line: `rename-ai <files or folders>` plus a few management commands."""

from __future__ import annotations

import argparse
import getpass
import sys
import threading

from . import engine, journal, shell
from .config import Settings, set_api_key
from .engine import ERROR, READY, SAME, Item
from .llm import LLMError

COMMANDS = {"undo", "history", "providers", "use", "set-key", "models", "test", "install", "uninstall", "gui"}

USAGE = """rename-ai PATH [PATH ...] [options]      rename files/folders (preview first, then asks)
rename-ai undo [BATCH]                     undo the last (or a given) rename batch
rename-ai history                          list past batches
rename-ai providers                        list AI providers
rename-ai use PROVIDER                     switch the default provider
rename-ai set-key PROVIDER                 store an API key (hidden input)
rename-ai models [PROVIDER]                list models a provider offers
rename-ai test [PROVIDER]                  check a provider works
rename-ai install | uninstall              add/remove right-click menu, shortcuts, PATH
rename-ai gui [PATH ...]                   open the window
Everything else is in the Settings window (rename-ai gui)."""


def _rename(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rename-ai", usage=USAGE)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("-p", "--provider", help="provider id for this run (see `rename-ai providers`)")
    ap.add_argument("-r", "--recursive", action="store_true", default=None, help="include subfolders")
    ap.add_argument("-y", "--yes", action="store_true", help="rename without asking")
    ap.add_argument("-n", "--dry-run", action="store_true", help="only show the proposed names")
    args = ap.parse_args(argv)

    settings = Settings.load()
    provider = settings.provider(args.provider)
    files, skipped = engine.collect(args.paths, settings, args.recursive)
    for path, reason in skipped:
        print(f"  skip  {path.name}  ({reason})")
    if not files:
        print("No supported files found.")
        return 1

    print(f"Naming {len(files)} file(s) with {provider.label} / {provider.model} ...")
    items = [Item(f) for f in files]
    lock = threading.Lock()

    def report(item: Item) -> None:
        if item.status in (READY, SAME, ERROR):
            with lock:
                mark = {"ready": "->", "same": "==", "error": "!!"}[item.status]
                detail = item.new_name if item.status != ERROR else item.message
                print(f"  {item.path.name}\n     {mark} {detail}" + (f"   [{item.message}]" if item.status == READY else ""))

    analyzer = engine.Analyzer(settings, args.provider, on_update=report)
    try:
        analyzer.run(items)
    except KeyboardInterrupt:
        analyzer.cancel()
        print("\nCancelled.")
        return 130

    todo = [i for i in items if i.status == READY]
    review = [i for i in todo if not i.checked]
    if not todo:
        print("Nothing to rename.")
        return 0
    if args.dry_run:
        print(f"\nDry run: {len(todo)} file(s) would be renamed.")
        return 0
    if review:
        print(f"\n{len(review)} low-confidence name(s) are excluded unless you answer 'a' (all).")
    if not args.yes:
        answer = input(f"\nRename {len(todo) - len(review)} file(s)? [y]es / [a]ll incl. low-confidence / [N]o: ").strip().lower()
        if answer == "a":
            for i in review:
                i.checked = True
        elif answer != "y":
            print("Nothing renamed.")
            return 0
    batch_id, done = engine.apply(items, settings, f"{provider.label} / {provider.model}")
    failed = [i for i in items if i.status == ERROR and i.message.startswith("Rename failed")]
    for i in failed:
        print(f"  !! {i.path.name}: {i.message}")
    print(f"Renamed {len(done)} file(s)." + (f"  Undo with: rename-ai undo {batch_id}" if batch_id else ""))
    return 0 if not failed else 2


def _undo(args: list[str]) -> int:
    batch = (next((b for b in journal.batches() if b["id"] == args[0]), None) if args else journal.last_batch())
    if not batch:
        print("Nothing to undo.")
        return 1
    restored, problems = journal.undo(batch["id"])
    for p in problems:
        print("  !!", p)
    print(f"Restored {restored} of {len(batch['ops'])} file(s) from batch {batch['id']} ({batch['time']}).")
    return 0


def _history() -> int:
    batches = journal.batches()
    if not batches:
        print("No history yet.")
    for b in batches[:30]:
        flag = " (undone)" if b["undone"] else ""
        print(f"{b['id']}  {b['time']}  {len(b['ops'])} file(s)  {b.get('provider', '')}{flag}")
    return 0


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:] if argv is None else argv
    engine.setup_logging()
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        sys.exit(0)
    cmd, rest = argv[0], argv[1:]
    settings = Settings.load()
    try:
        if cmd not in COMMANDS:
            code = _rename(argv)
        elif cmd == "undo":
            code = _undo(rest)
        elif cmd == "history":
            code = _history()
        elif cmd == "providers":
            for pid, p in settings.data["providers"].items():
                star = "*" if pid == settings.data["provider"] else " "
                print(f"{star} {pid:10} {p['label']:30} {p['model']}")
            code = 0
        elif cmd == "use":
            settings.provider(rest[0])  # validates
            settings.data["provider"] = rest[0]
            settings.save()
            print(f"Default provider is now {rest[0]}.")
            code = 0
        elif cmd == "set-key":
            settings.provider(rest[0])
            set_api_key(rest[0], getpass.getpass(f"API key for {rest[0]} (input hidden): "))
            print("Saved to Windows Credential Manager.")
            code = 0
        elif cmd == "models":
            print("\n".join(engine.make_client(settings, rest[0] if rest else None).list_models()))
            code = 0
        elif cmd == "test":
            print(engine.make_client(settings, rest[0] if rest else None).test())
            code = 0
        elif cmd == "install":
            shell.install()
            print("Installed: right-click menu, Send To, desktop + Start menu shortcuts, `rename-ai` on PATH.")
            print("Open a new terminal to use `rename-ai` from anywhere.")
            code = 0
        elif cmd == "uninstall":
            shell.uninstall()
            print("Removed right-click menu, shortcuts and PATH entry.")
            code = 0
        else:  # gui
            from .gui import main as gui_main
            gui_main(rest)
            code = 0
    except (LLMError, ValueError, IndexError) as e:
        print(f"Error: {e}" if not isinstance(e, IndexError) else USAGE)
        code = 1
    sys.exit(code)
