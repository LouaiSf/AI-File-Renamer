import tempfile
import unittest
from pathlib import Path

from ai_renamer.llm import Suggestion
from ai_renamer.naming import build_stem, clean_title, resolve_target

NAMING = {"separator": " ", "case": "title", "date_position": "start", "max_length": 80}


class BuildStemTests(unittest.TestCase):
    def test_relevant_date_goes_first(self):
        s = Suggestion("ACME invoice INV-2041", date="2025-03-04", date_relevant=True)
        self.assertEqual(build_stem(s, NAMING), "2025-03-04 ACME Invoice INV-2041")

    def test_irrelevant_date_is_dropped(self):
        s = Suggestion("chicken tajine with olives recipe", date="2019-05-01", date_relevant=False)
        self.assertEqual(build_stem(s, NAMING), "Chicken Tajine with Olives Recipe")

    def test_date_end_and_never(self):
        s = Suggestion("bank statement", date="2025-01", date_relevant=True)
        self.assertEqual(build_stem(s, {**NAMING, "date_position": "end"}), "Bank Statement 2025-01")
        self.assertEqual(build_stem(s, {**NAMING, "date_position": "never"}), "Bank Statement")

    def test_date_not_repeated_in_title(self):
        s = Suggestion("2026 monthly budget 2026 spreadsheet", date="2026", date_relevant=True)
        self.assertEqual(build_stem(s, NAMING), "2026 Monthly Budget Spreadsheet")
        s = Suggestion("Tax return 2025", date="2025", date_relevant=False)
        self.assertEqual(build_stem(s, NAMING), "Tax Return 2025")  # no date added -> keep it in the title

    def test_underscore_separator(self):
        s = Suggestion("ACME  invoice  INV-2041", date="2025-03-04", date_relevant=True)
        self.assertEqual(build_stem(s, {**NAMING, "separator": "_"}), "2025-03-04_ACME_Invoice_INV-2041")

    def test_lower_and_keep_case(self):
        s = Suggestion("Passport of Louai")
        self.assertEqual(build_stem(s, {**NAMING, "case": "lower"}), "passport of louai")
        self.assertEqual(build_stem(s, {**NAMING, "case": "keep"}), "Passport of Louai")

    def test_invalid_characters_removed(self):
        self.assertEqual(clean_title('Report: Q3/Q4 "final"? <v2>*'), "Report Q3 Q4 Final v2")

    def test_reserved_windows_name(self):
        self.assertEqual(build_stem(Suggestion("con"), NAMING), "Con file")

    def test_truncates_on_word_boundary(self):
        s = Suggestion("word " * 40)
        stem = build_stem(s, {**NAMING, "max_length": 30})
        self.assertLessEqual(len(stem), 30)
        self.assertFalse(stem.endswith(" "))

    def test_empty_title_fallback(self):
        self.assertEqual(build_stem(Suggestion("???"), NAMING), "Untitled")


class ResolveTargetTests(unittest.TestCase):
    def test_conflicts_get_numbered(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "a.pdf"
            src.write_text("x")
            (d / "Invoice.pdf").write_text("existing")
            taken: set[str] = set()
            self.assertEqual(resolve_target(src, "Invoice", " ", taken).name, "Invoice (2).pdf")
            other = d / "b.pdf"
            other.write_text("y")
            self.assertEqual(resolve_target(other, "Invoice", " ", taken).name, "Invoice (3).pdf")
            self.assertEqual(resolve_target(d / "c.pdf", "Invoice", "_", set()).name, "Invoice_2.pdf")

    def test_same_name_returns_source(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "Invoice.pdf"
            src.write_text("x")
            self.assertEqual(resolve_target(src, "Invoice", " ", set()), src)


if __name__ == "__main__":
    unittest.main()
