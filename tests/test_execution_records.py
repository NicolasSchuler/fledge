"""Typed reads of the execution entries that renderers show, and the detail keys they share."""

from __future__ import annotations

import unittest

from latexprep.execution_records import (
    OTHER_OPT_IN_AREA,
    CheckSelectionRecord,
    NotCheckedArea,
    read_check_selection,
    read_execution_records,
    read_not_checked,
)
from latexprep.findings import DetailKey, Finding
from latexprep.html_report_previews import read_preview_comparisons


class ExecutionRecordTests(unittest.TestCase):
    def test_entries_are_read_into_typed_records(self):
        records = read_execution_records(
            {
                "config_path": "/work/fledge.toml",
                "preset": "arxiv",
                "check_selection": {"select": ["TEX"], "ignore": ["TEX001"]},
                "not_checked": [
                    {"area": "page_limit", "label": "page limit", "codes": ["PDF001"]},
                    {
                        "area": OTHER_OPT_IN_AREA,
                        "label": "other opt-in checks",
                        "codes": ["BIB103", "BIB104"],
                        "settings": ["bibliography_checks.required_fields"],
                    },
                ],
            }
        )
        self.assertEqual(records.config_path, "/work/fledge.toml")
        self.assertEqual(records.preset, "arxiv")
        self.assertEqual(records.check_selection, CheckSelectionRecord(("TEX",), ("TEX001",)))
        self.assertEqual(
            records.not_checked[0], NotCheckedArea("page_limit", "page limit", ("PDF001",), ())
        )
        self.assertEqual(records.not_checked[1].codes, ("BIB103", "BIB104"))

    def test_missing_or_malformed_entries_read_as_absent(self):
        records = read_execution_records(
            {
                "config_path": "",
                "preset": 3,
                "check_selection": "ALL",
                "not_checked": "page limit",
            }
        )
        self.assertIsNone(records.config_path)
        self.assertIsNone(records.preset)
        self.assertIsNone(records.check_selection)
        self.assertEqual(records.not_checked, ())
        self.assertEqual(read_execution_records({}), read_execution_records({"unrelated": 1}))

    def test_selection_defaults_to_all_and_rejects_non_lists(self):
        self.assertEqual(read_check_selection({}), CheckSelectionRecord(("ALL",), ()))
        self.assertIsNone(read_check_selection({"select": "TEX"}))
        self.assertIsNone(read_check_selection({"ignore": 4}))

    def test_not_checked_areas_without_a_label_are_skipped(self):
        areas = read_not_checked([{"area": "x"}, "text", {"area": "y", "label": "a label"}])
        self.assertEqual([area.label for area in areas], ["a label"])
        self.assertEqual(read_not_checked(None), ())

    def test_preview_comparisons_skip_malformed_entries_and_keep_page_notes(self):
        comparisons = read_preview_comparisons(
            {
                "pdf_previews": {
                    "comparisons": [
                        "bad",
                        {
                            "stage": "baseline versus prepared",
                            "document": "main",
                            "omitted_pages": 2,
                            "pages": [
                                {"page": 3, "text_only": True, "left": {"image_id": "a"}},
                                "bad",
                                {"page": 4, "status": "omitted"},
                            ],
                        },
                    ]
                }
            }
        )
        [comparison] = comparisons
        self.assertEqual(comparison.title, "main: baseline versus prepared")
        self.assertEqual(comparison.omitted_pages, 2)
        self.assertEqual([page.number for page in comparison.pages], [3, 4])
        self.assertTrue(comparison.pages[0].text_only)
        self.assertEqual(comparison.pages[0].left.image_id, "a")
        self.assertTrue(comparison.pages[1].omitted)
        self.assertEqual(read_preview_comparisons({}), ())


class DetailKeyTests(unittest.TestCase):
    def test_a_key_equals_its_plain_string_so_checks_may_write_either(self):
        finding = Finding(
            "pdf.page_size",
            "message",
            details={"document": "main", DetailKey.STAGE: "baseline", "page": 3},
        )
        self.assertEqual(finding.document, "main")
        self.assertEqual(finding.stage, "baseline")
        self.assertEqual(finding.page, 3)
        self.assertEqual(DetailKey.VIOLATION_COUNT, "violation_count")
        self.assertIn("review", {DetailKey.REVIEW})

    def test_scope_disclaimers_are_informational_notes_only(self):
        note = Finding("a.b", "x", severity="info", status="not_applicable")
        self.assertTrue(note.is_scope_disclaimer)
        self.assertTrue(Finding("a.b", "x", severity="info", status="skipped").is_scope_disclaimer)
        self.assertFalse(
            Finding("a.b", "x", severity="warning", status="skipped").is_scope_disclaimer
        )
        self.assertFalse(Finding("a.b", "x", severity="info", status="passed").is_scope_disclaimer)


if __name__ == "__main__":
    unittest.main()
