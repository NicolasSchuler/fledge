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
    record_check_selection,
    record_not_checked,
    record_pdf_previews,
    record_settings_source,
)
from latexprep.findings import DetailKey, Finding
from latexprep.html_report_previews import read_preview_comparisons
from latexprep.report import Report


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

    def test_what_a_job_records_reads_back_as_the_same_records(self):
        selection = CheckSelectionRecord(("TEX",), ("TEX001",), ("TEX005", "PKG001"))
        areas = (
            NotCheckedArea("page_limit", "page limit", ("PDF001",), ("max_pages",)),
            NotCheckedArea(OTHER_OPT_IN_AREA, "other opt-in checks", ("BIB103",), ()),
        )
        execution: dict[str, object] = {}
        record_settings_source(execution, config_path="/work/fledge.toml", preset="arxiv")
        record_check_selection(execution, selection)
        record_not_checked(execution, areas)
        records = read_execution_records(execution)
        self.assertEqual(records.config_path, "/work/fledge.toml")
        self.assertEqual(records.preset, "arxiv")
        self.assertEqual(records.check_selection, selection)
        self.assertEqual(records.not_checked, areas)

    def test_entries_keep_their_documented_json_layout(self):
        execution: dict[str, object] = {}
        record_settings_source(execution, config_path=None, preset=None)
        record_check_selection(execution, CheckSelectionRecord(("ALL",), (), ("TEX005",)))
        record_not_checked(
            execution, [NotCheckedArea("page_limit", "page limit", ("PDF001",), ("max_pages",))]
        )
        self.assertEqual(
            execution,
            {
                "config_path": None,
                "preset": None,
                "check_selection": {
                    "select": ["ALL"],
                    "ignore": [],
                    "always_enforced_when_applicable": ["TEX005"],
                },
                "not_checked": [
                    {
                        "area": "page_limit",
                        "label": "page limit",
                        "codes": ["PDF001"],
                        "settings": ["max_pages"],
                    }
                ],
            },
        )
        record_not_checked(execution, [])
        self.assertEqual(execution["not_checked"], [])

    def test_preview_summaries_are_recorded_where_the_html_report_reads_them(self):
        report = Report("check")
        record_pdf_previews(
            report.execution,
            {"comparisons": [{"stage": "baseline", "pages": [{"page": 2, "status": "omitted"}]}]},
        )
        [comparison] = read_preview_comparisons(report.execution)
        self.assertEqual(comparison.title, "baseline")
        self.assertTrue(comparison.pages[0].omitted)

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
