import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_renamer import config, engine, journal
from ai_renamer.config import Settings
from ai_renamer.engine import DONE, ERROR, READY, SAME, Item
from ai_renamer.llm import LLMError, Suggestion

FAKE = {
    "invoice.txt": Suggestion("ACME invoice INV-2041", "Invoice", "2025-03-04", True, 0.9),
    "recipe.txt": Suggestion("chicken tajine recipe", "Recipe", None, False, 0.8),
    "blurry.txt": Suggestion("unknown scan", "Scan", None, False, 0.1),
    "Already Good.txt": Suggestion("already good", "Notes", None, False, 0.9),
}


def fake_suggest(client, prompt, content):
    name = content.hints["current_filename"]
    if name == "broken.txt":
        raise LLMError("model exploded")
    return FAKE[name]


class EngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.dir = root / "files"
        self.dir.mkdir()
        patches = [mock.patch.object(config, "DATA_DIR", root / "data"),
                   mock.patch.object(config, "HISTORY_FILE", root / "data" / "history.jsonl"),
                   mock.patch.object(config, "LOG_FILE", root / "data" / "log.txt"),
                   mock.patch.object(engine, "suggest", fake_suggest)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for name in [*FAKE, "broken.txt", ".hidden.txt", "movie.mp4"]:
            (self.dir / name).write_text(f"content of {name}")
        (self.dir / "sub").mkdir()
        (self.dir / "sub" / "deep.txt").write_text("deep")
        self.settings = Settings()

    def tearDown(self):
        self._tmp.cleanup()

    def analyze(self, recursive=False):
        files, skipped = engine.collect([self.dir], self.settings, recursive)
        items = [Item(f) for f in files]
        engine.Analyzer(self.settings).run(items)
        return {i.path.name: i for i in items}, skipped

    def test_collect_filters(self):
        files, _ = engine.collect([self.dir], self.settings, recursive=False)
        names = {f.name for f in files}
        self.assertNotIn(".hidden.txt", names)
        self.assertNotIn("movie.mp4", names)
        self.assertNotIn("deep.txt", names)
        files, _ = engine.collect([self.dir], self.settings, recursive=True)
        self.assertIn("deep.txt", {f.name for f in files})
        _, skipped = engine.collect([self.dir / "movie.mp4"], self.settings)
        self.assertIn("unsupported", skipped[0][1])

    def test_full_cycle_rename_and_undo(self):
        items, _ = self.analyze()
        self.assertEqual(items["invoice.txt"].stem, "2025-03-04 ACME Invoice INV-2041")
        self.assertEqual(items["recipe.txt"].status, READY)
        self.assertFalse(items["blurry.txt"].checked)          # low confidence -> needs review
        self.assertEqual(items["Already Good.txt"].status, SAME)
        self.assertEqual(items["broken.txt"].status, ERROR)

        (self.dir / "Chicken Tajine Recipe.txt").write_text("someone else's file")
        batch_id, done = engine.apply(list(items.values()), self.settings, "test")
        self.assertEqual(len(done), 2)
        self.assertTrue((self.dir / "2025-03-04 ACME Invoice INV-2041.txt").exists())
        self.assertEqual((self.dir / "Chicken Tajine Recipe.txt").read_text(), "someone else's file")  # never overwritten
        self.assertTrue((self.dir / "Chicken Tajine Recipe (2).txt").exists())
        self.assertTrue((self.dir / "blurry.txt").exists())

        # Renamed files are skipped on the next folder scan...
        files, skipped = engine.collect([self.dir], self.settings)
        self.assertEqual(sum(1 for _, r in skipped if r == "already renamed"), 2)

        restored, problems = journal.undo(batch_id)
        self.assertEqual((restored, problems), (2, []))
        self.assertEqual((self.dir / "invoice.txt").read_text(), "content of invoice.txt")
        self.assertTrue(journal.batches()[0]["undone"])
        with self.assertRaises(ValueError):
            journal.undo(batch_id)
        # ...but not after being undone.
        files, skipped = engine.collect([self.dir], self.settings)
        self.assertFalse([r for _, r in skipped if r == "already renamed"])

    def test_undo_reports_missing_files(self):
        items, _ = self.analyze()
        batch_id, _ = engine.apply(list(items.values()), self.settings)
        (self.dir / "2025-03-04 ACME Invoice INV-2041.txt").unlink()
        restored, problems = journal.undo(batch_id)
        self.assertEqual(restored, 1)
        self.assertIn("no longer exists", problems[0])

    def test_rename_does_not_overwrite_file_created_meanwhile(self):
        items, _ = self.analyze()
        item = items["recipe.txt"]
        for other in items.values():
            other.checked = other is item
        with mock.patch("ai_renamer.engine.resolve_target", return_value=self.dir / "invoice.txt"):
            engine.apply([item], self.settings)
        self.assertEqual(item.status, ERROR)
        self.assertEqual((self.dir / "invoice.txt").read_text(), "content of invoice.txt")


if __name__ == "__main__":
    unittest.main()
