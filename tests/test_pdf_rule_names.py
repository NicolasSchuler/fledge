"""Every ``pdf.*`` rule the manuscript-PDF checks emit is catalogued or declared operational.

A rule name is written once, in ``PdfRule``. A finding whose rule is neither in the rule
catalogue nor declared operational has no public code and no fix guidance, so a typo or a
new name that skips the catalogue fails here instead of surfacing in a report.
"""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

from latexprep.findings import Finding
from latexprep.pdf_check_options import PdfCheckOptions, PdfRegion, PdfSectionBudget
from latexprep.pdf_check_pipeline import PdfCheckSettings, inspect_standalone_pdf
from latexprep.pdf_checks import inspect_included_pdf_figures, inspect_pdf_details
from latexprep.pdf_inspection import PdfInspectionOptions, inspect_pdf
from latexprep.pdf_rule_names import OPERATIONAL_PDF_RULES, PdfRule
from latexprep.pdf_structure_checks import (
    HeadingExpectation,
    StructureOptions,
    inspect_pdf_structure,
)
from latexprep.resource_budget import ResourceBudget
from latexprep.rules import BY_NAME
from tests.pdf_fakes import (
    ConfigurablePdfRunner,
    DetailRunner,
    color_trace,
    structured_pdf_objects,
)
from tests.support import AsyncProjectTestCase

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "latexprep"
# Modules that report or build findings for the manuscript PDF; they name rules only
# through PdfRule. The catalogue module passes the same members as data.
RULE_NAMING_MODULES = (
    "measurement_findings",
    "pdf_check_options",
    "pdf_check_pipeline",
    "pdf_checks",
    "pdf_geometry",
    "pdf_inspection",
    "pdf_object_checks",
    "pdf_paint_checks",
    "pdf_qpdf_graph",
    "pdf_sparse_page_checks",
    "pdf_structure_checks",
    "pdf_structure_tree",
    "pdf_text_geometry_checks",
    "pdf_tool_output",
    "pdf_tools",
)


class RuleNameTests(unittest.TestCase):
    """The ``PdfRule`` names: catalogued or operational, well formed, never string literals."""

    def test_every_name_is_catalogued_or_declared_operational(self) -> None:
        undeclared = [
            rule.value
            for rule in PdfRule
            if rule.value not in BY_NAME and rule not in OPERATIONAL_PDF_RULES
        ]
        self.assertEqual(undeclared, [])

    def test_operational_names_have_no_catalogue_entry_and_names_are_well_formed(self) -> None:
        for rule in OPERATIONAL_PDF_RULES:
            self.assertNotIn(rule.value, BY_NAME, "an operational rule must not be catalogued")
        for rule in PdfRule:
            self.assertRegex(rule.value, r"pdf\.[a-z0-9_]+")
        self.assertEqual(len({rule.value for rule in PdfRule}), len(PdfRule))

    def test_modules_do_not_spell_rule_names_as_string_literals(self) -> None:
        literals = []
        for module in RULE_NAMING_MODULES:
            tree = ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))
            literals += [
                f"{module}.py:{node.lineno} {node.value}"
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and re.fullmatch(r"pdf\.[a-z0-9_]+", node.value)
            ]
        self.assertEqual(literals, [])


class EmittedRuleTests(AsyncProjectTestCase):
    """The rules that the PDF check families emit on controlled tool output are known."""

    async def asyncSetUp(self) -> None:
        """Write a placeholder ``document.pdf`` into the project root."""
        self.pdf = self.root / "document.pdf"
        self.pdf.write_bytes(b"%PDF-fixture")
        self.counter = 0

    def work(self) -> Path:
        """Return a new, not yet created work directory path below the project root."""
        self.counter += 1
        return self.root / f"work-{self.counter}"

    def assert_names_known(self, findings: list[Finding]) -> set[str]:
        """Assert that every ``pdf.*`` rule of ``findings`` is a known, declared rule.

        A known rule is a member of ``PdfRule`` that is catalogued or declared operational.

        Returns:
            The ``pdf.*`` rules of ``findings``.
        """
        rules = {finding.rule for finding in findings if finding.rule.startswith("pdf.")}
        known = {rule.value for rule in PdfRule}
        self.assertEqual(sorted(rules - known), [])
        for rule in rules:
            self.assertTrue(rule in BY_NAME or rule in OPERATIONAL_PDF_RULES, rule)
        return rules

    async def test_every_check_family_reports_only_known_rules(self) -> None:
        emitted: set[str] = set()
        everything = PdfCheckOptions(
            section_budgets=(PdfSectionBudget("Main", 1, 2, 1),),
            min_text_size_pt=9,
            text_size_regions=(PdfRegion("Box", 1, 0, 0, 300, 400, 11),),
            printable_margins_pt=(30, 30, 30, 30),
            overlap_min_area_ratio=0.3,
            min_page_ink_ratio=0.2,
            required_metadata=("Title",),
            expected_metadata=(("Title", "Paper"),),
            require_tagged=True,
            expected_language="en-GB",
            forbidden_annotation_types=("Text",),
            forbidden_action_types=("JavaScript",),
            min_stroke_width_pt=1,
            allowed_color_spaces=("DeviceGray",),
            min_text_contrast=4.5,
            contrast_background_rgb=(1, 1, 1),
            min_grayscale_luminance_difference=0.05,
            require_embedded_figure_fonts=True,
            forbid_type3_figure_fonts=True,
        )
        emitted |= self.assert_names_known(
            await inspect_pdf_details(
                self.pdf,
                self.work(),
                DetailRunner(trace=color_trace()),
                options=everything,
            )
        )
        emitted |= self.assert_names_known(
            await inspect_included_pdf_figures(
                (self.pdf,), self.work(), DetailRunner(), options=everything
            )
        )
        emitted |= self.assert_names_known(
            await inspect_pdf_structure(
                self.pdf,
                self.work(),
                DetailRunner(
                    info="Pages: 1\n",
                    objects=json.dumps(structured_pdf_objects()),
                    text="2 Methods\nBody.\f",
                ),
                StructureOptions(
                    heading_expectations=(HeadingExpectation(1, "Methods", "2"),),
                    require_structure_tree=True,
                    check_table_structure=True,
                    check_link_structure=True,
                    require_figure_alt=True,
                ),
            )
        )
        inspection = PdfInspectionOptions(
            expected_page_width_pt=612,
            expected_page_height_pt=792,
            require_consistent_page_size=True,
            min_image_dpi=300,
            require_embedded_fonts=True,
            forbid_type3_fonts=True,
            forbid_encryption=True,
            forbid_forms=True,
            forbid_javascript=True,
            forbid_attachments=True,
        )
        emitted |= self.assert_names_known(
            await inspect_pdf(
                self.pdf,
                self.work(),
                ConfigurablePdfRunner(),
                2,
                options=inspection,
                budget=ResourceBudget(2, 1024),
            )
        )
        # A missing tool reports each inventory's unavailable diagnostic.
        for tool in ("pdfinfo", "pdffonts", "pdfimages", "pdftotext", "pdfdetach"):
            emitted |= self.assert_names_known(
                await inspect_pdf(
                    self.pdf,
                    self.work(),
                    ConfigurablePdfRunner(missing=tool),
                    2,
                    options=inspection,
                    budget=ResourceBudget(2, 1024),
                )
            )
        emitted |= self.assert_names_known(
            await inspect_standalone_pdf(
                self.pdf,
                self.work(),
                ConfigurablePdfRunner(),
                PdfCheckSettings(details=everything, match_source_metadata=("title",)),
                ResourceBudget(2, 1024),
            )
        )
        # The families and diagnostics above are the whole set of names the checks report.
        reported = {rule.value for rule in PdfRule if rule.value in emitted}
        self.assertGreaterEqual(len(reported), 40)


if __name__ == "__main__":
    unittest.main()
