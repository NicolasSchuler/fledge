"""Typed nested-check configuration, validation, and serialization contracts."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from latexprep.bibliography_checks import BibliographyOptions
from latexprep.build_checks import BuildCheckOptions
from latexprep.config import Settings, load_settings
from latexprep.manuscript_checks import ManuscriptCheckOptions
from latexprep.models import PreparationError
from latexprep.online_checks import OnlineOptions
from latexprep.pdf_checks import PdfCheckOptions, PdfRegion, PdfSectionBudget
from latexprep.submission_checks import SubmissionOptions

_EXAMPLE = """
main = "main.tex"
match_source_pdf_metadata = ["title"]

[bibliography_checks]
all_roots = true
doi_entry_types = ["article", "dataset"]
check_fuzzy_duplicates = true
fuzzy_duplicate_threshold = 0.9
[bibliography_checks.required_fields]
software = ["title", "version"]
article = ["title", "author|editor", "year|date"]

[build_checks]
overfull_tolerance_pt = 0
allowed_loaded_packages = ["article.cls", "geometry.sty"]
required_loaded_packages = ["geometry.sty"]
[build_checks.minimum_package_dates]
"geometry.sty" = "2025/11/20"
"article.cls" = "2026-01-01"

[manuscript_checks]
allowed_packages = []
inventory_packages = true
abstract_abbreviation_policy = "define"
abstract_abbreviation_exceptions = ["PDF"]
required_metadata = ["title", "author"]
required_declarations = ["Data Availability"]
require_figure_descriptions = true

[submission_checks]
identity_terms = ["Example Author"]
allowed_extensions = [".tex", ".bib", ".pdf"]
scan_secrets = true
[submission_checks.required_deliverables]
"supplement.pdf" = "pdf"
"cover.txt" = "text"
[submission_checks.template_references]
"template.cls" = "/reference/template.cls"

[pdf_checks]
printable_margins_pt = [36, 42.5, 36, 42.5]
min_text_contrast = 4.5
contrast_background_rgb = [1, 1, 1]
sparse_page_exemptions = [1, 4]
required_metadata = ["Title", "Author"]
require_tagged = true
require_language = true
expected_language = "en-US"
[pdf_checks.expected_metadata]
Subject = "Prepared manuscript"
Author = "Example Author"

[[pdf_checks.section_budgets]]
name = "Main text"
start_page = 1
end_page = 8
max_pages = 8

[[pdf_checks.section_budgets]]
name = "References"
start_page = 9
end_page = 10
max_pages = 2

[[pdf_checks.text_size_regions]]
name = "Figure labels"
page = 2
left_pt = 36
top_pt = 50
right_pt = 280.5
bottom_pt = 300
min_text_size_pt = 7.5

[[pdf_checks.printable_regions]]
name = "Left column"
page = 2
left_pt = 36
top_pt = 42.5
right_pt = 280
bottom_pt = 740

[online_checks]
online = false
online_metadata = true
online_replication_urls = ["https://example.org/artifact"]
online_max_requests = 12
online_provider_interval_seconds = 0.5
"""


class CheckConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def load(self, text: str = _EXAMPLE) -> Settings:
        path = self.root / "settings.toml"
        path.write_text(text, encoding="utf-8")
        return load_settings(path, {})

    def test_nested_toml_policies_become_typed_immutable_options(self) -> None:
        settings = self.load()
        for attribute, expected in (
            ("bibliography_checks", BibliographyOptions),
            ("build_checks", BuildCheckOptions),
            ("manuscript_checks", ManuscriptCheckOptions),
            ("submission_checks", SubmissionOptions),
            ("pdf_checks", PdfCheckOptions),
            ("online_checks", OnlineOptions),
        ):
            with self.subTest(attribute=attribute):
                self.assertIsInstance(getattr(settings, attribute), expected)
        self.assertEqual(settings.main, "main.tex")
        self.assertEqual(settings.match_source_pdf_metadata, ("title",))
        self.assertTrue(settings.bibliography_checks.all_roots)
        self.assertEqual(settings.bibliography_checks.doi_entry_types, ("article", "dataset"))
        self.assertEqual(settings.manuscript_checks.allowed_packages, ())
        self.assertEqual(settings.manuscript_checks.abstract_abbreviation_policy, "define")
        self.assertEqual(settings.submission_checks.identity_terms, ("Example Author",))
        field_name = "all_roots"
        with self.assertRaises(FrozenInstanceError):
            setattr(settings.bibliography_checks, field_name, False)

    def test_named_pair_maps_preserve_metadata_requirements_and_reference_paths(self) -> None:
        settings = self.load()
        self.assertEqual(
            settings.bibliography_checks.required_fields,
            (
                ("article", ("title", "author|editor", "year|date")),
                ("software", ("title", "version")),
            ),
        )
        self.assertEqual(
            settings.build_checks.minimum_package_dates,
            (("article.cls", "2026-01-01"), ("geometry.sty", "2025/11/20")),
        )
        self.assertEqual(
            settings.submission_checks.required_deliverables,
            (("cover.txt", "text"), ("supplement.pdf", "pdf")),
        )
        self.assertEqual(
            settings.submission_checks.template_references,
            (("template.cls", "/reference/template.cls"),),
        )
        self.assertEqual(
            settings.pdf_checks.expected_metadata,
            (("Author", "Example Author"), ("Subject", "Prepared manuscript")),
        )

    def test_pdf_region_and_section_arrays_create_nested_dataclasses(self) -> None:
        pdf = self.load().pdf_checks
        self.assertEqual(
            pdf.section_budgets,
            (PdfSectionBudget("Main text", 1, 8, 8), PdfSectionBudget("References", 9, 10, 2)),
        )
        self.assertEqual(
            pdf.text_size_regions,
            (PdfRegion("Figure labels", 2, 36, 50, 280.5, 300, 7.5),),
        )
        self.assertEqual(
            pdf.printable_regions,
            (PdfRegion("Left column", 2, 36, 42.5, 280, 740),),
        )
        self.assertEqual(pdf.printable_margins_pt, (36, 42.5, 36, 42.5))
        self.assertEqual(pdf.contrast_background_rgb, (1, 1, 1))
        self.assertEqual(pdf.sparse_page_exemptions, (1, 4))

    def test_relative_template_reference_is_resolved_from_settings_directory(self) -> None:
        settings = self.load(
            '[submission_checks.template_references]\n"template.cls" = "reference/template.cls"\n'
        )
        self.assertEqual(
            settings.submission_checks.template_references,
            (("template.cls", str((self.root / "reference/template.cls").resolve())),),
        )

    def test_settings_dictionary_and_json_roundtrip_preserve_typed_options(self) -> None:
        for original in (load_settings(None, {}), self.load()):
            with self.subTest(configured=original.main is not None):
                values = original.to_dict()
                before = copy.deepcopy(values)
                restored = load_settings(None, values)
                self.assertEqual(restored, original)
                self.assertEqual(values, before, "Loading must not mutate serialized settings")
                restored_json = load_settings(None, json.loads(json.dumps(values)))
                self.assertEqual(restored_json, original)
                if restored.pdf_checks.section_budgets:
                    self.assertIsInstance(restored.pdf_checks.section_budgets[0], PdfSectionBudget)
                    self.assertIsInstance(restored.pdf_checks.text_size_regions[0], PdfRegion)

    def test_preconstructed_options_and_explicit_empty_policies_are_preserved(self) -> None:
        bibliography = BibliographyOptions(required_fields=(("dataset", ("title", "version")),))
        manuscript = ManuscriptCheckOptions(allowed_packages=())
        settings = load_settings(
            None,
            {"bibliography_checks": bibliography, "manuscript_checks": manuscript},
        )
        self.assertIs(settings.bibliography_checks, bibliography)
        self.assertIs(settings.manuscript_checks, manuscript)
        self.assertEqual(settings.manuscript_checks.allowed_packages, ())
        self.assertIsNone(load_settings(None, {}).manuscript_checks.allowed_packages)

    def test_online_defaults_and_nested_selection_do_not_connect_to_network(self) -> None:
        with (
            patch("socket.create_connection", side_effect=AssertionError("Unexpected connection")),
            patch("socket.getaddrinfo", side_effect=AssertionError("Unexpected DNS lookup")),
        ):
            default = load_settings(None, {})
            selected = self.load()
            enabled = self.load("[online_checks]\nonline = true\nonline_doi_resolution = true\n")
        self.assertEqual(default.online_checks, OnlineOptions())
        self.assertFalse(default.online_checks.online)
        self.assertTrue(selected.online_checks.online_metadata)
        self.assertFalse(selected.online_checks.online)
        self.assertEqual(selected.online_checks.online_max_requests, 12)
        self.assertEqual(
            selected.online_checks.online_replication_urls, ("https://example.org/artifact",)
        )
        self.assertTrue(enabled.online_checks.online)
        self.assertTrue(enabled.online_checks.online_doi_resolution)

    def test_invalid_group_types_and_unknown_nested_names_raise_preparation_errors(self) -> None:
        cases: tuple[tuple[dict[str, object], str], ...] = (
            ({"bibliography_checks": []}, "bibliography_checks"),
            ({"build_checks": "defaults"}, "build_checks"),
            ({"manuscript_checks": 1}, "manuscript_checks"),
            ({"pdf_checks": {"min_font_size": 7}}, "min_font_size"),
            ({"submission_checks": {"publisher": "example"}}, "publisher"),
            ({"online_checks": {"provider": "unconfigured"}}, "provider"),
            ({"pdf_checks": {"printable_regions": [{"unknown": 1}]}}, "unknown"),
        )
        for overrides, message in cases:
            with (
                self.subTest(overrides=overrides),
                self.assertRaisesRegex(PreparationError, message),
            ):
                load_settings(None, overrides)

    def test_nested_scalar_types_and_tuple_lengths_are_strict(self) -> None:
        cases: tuple[dict[str, object], ...] = (
            {"bibliography_checks": {"check_urls": 1}},
            {"bibliography_checks": {"required_fields": {"article": "title"}}},
            {"bibliography_checks": {"required_fields": {"article": ["title", 1]}}},
            {"build_checks": {"overfull_tolerance_pt": True}},
            {"build_checks": {"minimum_package_dates": [["geometry.sty"]]}},
            {"manuscript_checks": {"allowed_packages": "graphicx"}},
            {"manuscript_checks": {"max_heading_depth": 1.5}},
            {"submission_checks": {"identity_terms": [None]}},
            {"submission_checks": {"required_deliverables": [["cover.txt", "text", "extra"]]}},
            {"pdf_checks": {"printable_margins_pt": [36, 36, 36]}},
            {"pdf_checks": {"contrast_background_rgb": [1, 1, "white"]}},
            {
                "pdf_checks": {
                    "section_budgets": [
                        {"name": "Body", "start_page": True, "end_page": 8, "max_pages": 8}
                    ]
                }
            },
            {"pdf_checks": {"text_size_regions": ["whole page"]}},
            {"online_checks": {"online": "false"}},
            {"online_checks": {"online_jobs": True}},
            {"online_checks": {"online_replication_urls": [7]}},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(PreparationError):
                load_settings(None, overrides)

    def test_invalid_values_and_cross_field_constraints_reach_option_validation(self) -> None:
        cases: tuple[dict[str, object], ...] = (
            {"bibliography_checks": {"fuzzy_duplicate_threshold": 1.1}},
            {"bibliography_checks": {"required_fields": {"article": ["author||editor"]}}},
            {"build_checks": {"minimum_package_dates": {"geometry.sty": "2026-02-30"}}},
            {
                "build_checks": {
                    "required_loaded_packages": ["geometry.sty"],
                    "allowed_loaded_packages": [],
                }
            },
            {"manuscript_checks": {"abstract_abbreviation_policy": "infer"}},
            {"manuscript_checks": {"required_metadata": ["publisher"]}},
            {"submission_checks": {"required_deliverables": {"../cover.txt": "text"}}},
            {"pdf_checks": {"min_text_contrast": 4.5}},
            {
                "match_source_pdf_metadata": ["title"],
                "pdf_checks": {"expected_metadata": {"Title": "Conflicting value"}},
            },
            {"pdf_checks": {"min_text_size_pt": float("nan")}},
            {
                "pdf_checks": {
                    "section_budgets": [
                        {"name": "Body", "start_page": 8, "end_page": 1, "max_pages": 8}
                    ]
                }
            },
            {
                "pdf_checks": {
                    "printable_regions": [
                        {
                            "name": "Column",
                            "page": 1,
                            "left_pt": 40,
                            "top_pt": 40,
                            "right_pt": 30,
                            "bottom_pt": 700,
                        }
                    ]
                }
            },
            {
                "pdf_checks": {
                    "text_size_regions": [
                        {
                            "name": "Labels",
                            "page": 1,
                            "left_pt": 30,
                            "top_pt": 40,
                            "right_pt": 300,
                            "bottom_pt": 700,
                        }
                    ]
                }
            },
            {"online_checks": {"online_max_requests": 0}},
            {"online_checks": {"online_provider_interval_seconds": float("inf")}},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(PreparationError):
                load_settings(None, overrides)

    def test_nonstring_named_pair_keys_raise_preparation_error_before_sorting(self) -> None:
        for group, option in (
            ("bibliography_checks", "required_fields"),
            ("build_checks", "minimum_package_dates"),
            ("pdf_checks", "expected_metadata"),
            ("submission_checks", "required_deliverables"),
        ):
            overrides: dict[str, object] = {group: {option: {7: "invalid", "valid": "invalid"}}}
            with self.subTest(group=group), self.assertRaises(PreparationError):
                load_settings(None, overrides)

    def test_malformed_source_pdf_metadata_requests_raise_preparation_error(self) -> None:
        cases = (
            "title",
            1,
            [7],
            [None],
            [[]],
            [{}],
            ["title", ["author"]],
            ["Title"],
            ["title", "title"],
            {"title": True},
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaises(PreparationError):
                load_settings(None, {"match_source_pdf_metadata": value})

    def test_settings_file_errors_and_missing_nested_fields_are_actionable(self) -> None:
        with self.assertRaisesRegex(PreparationError, "Cannot read settings"):
            load_settings(self.root / "missing.toml", {})
        for source in (
            "[pdf_checks\nrequire_tagged = true\n",
            "[pdf_checks]\nrequire_tagged = true\nrequire_tagged = false\n",
        ):
            with (
                self.subTest(source=source),
                self.assertRaisesRegex(PreparationError, "Cannot read settings"),
            ):
                self.load(source)
        with self.assertRaisesRegex(PreparationError, "section_budgets"):
            self.load('[[pdf_checks.section_budgets]]\nname = "Body"\nstart_page = 1\n')
        with self.assertRaisesRegex(PreparationError, "minimum_package_dates"):
            self.load('[build_checks.minimum_package_dates]\n"geometry.sty" = 2026-01-01\n')


if __name__ == "__main__":
    unittest.main()
