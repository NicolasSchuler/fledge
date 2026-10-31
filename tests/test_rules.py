"""Catalogue integrity complements, but does not replace, linked behavior tests."""

from __future__ import annotations

import unittest

from latexprep.check_policy import opt_in_activation
from latexprep.coverage_hints import not_checked_areas
from latexprep.execution_records import OTHER_OPT_IN_AREA
from latexprep.findings import Finding
from latexprep.report import Report
from latexprep.rules import (
    BY_CODE,
    BY_NAME,
    MANDATORY_CODES,
    REVIEWABLE_CODES,
    RULES,
    get_rule,
)


class RuleRegistryTests(unittest.TestCase):
    """The rule catalogue: linked tests, serialized codes, derived sets and report order."""

    def test_every_unique_code_links_to_existing_behavioral_unit_tests(self):
        loader = unittest.TestLoader()
        self.assertEqual(len(RULES), len(BY_CODE))
        self.assertEqual(sum(1 + len(rule.aliases) for rule in RULES), len(BY_NAME))
        for rule in RULES:
            with self.subTest(code=rule.code):
                self.assertRegex(rule.code, r"^[A-Z]{3}[0-9]{3}$")
                self.assertTrue(rule.title and rule.description and rule.fix)
                self.assertTrue(rule.tests, "Each code must identify its behavior tests")
                for identifier in rule.tests:
                    self.assertTrue(identifier.split(".")[-1].startswith("test_"))
                    suite = loader.loadTestsFromName(identifier)
                    self.assertEqual(suite.countTestCases(), 1)
                self.assertEqual(loader.errors, [])

    def test_codes_and_fix_guidance_are_serialized_without_replacing_rule_names(self):
        finding = Finding(
            "bibliography.relationship_unresolved", "Target uses a macro.", status="inconclusive"
        )
        report = Report("bib", findings=[finding, Finding("execution.task", "Tool unavailable.")])
        rows = {row["rule"]: row for row in report.to_dict()["findings"]}
        self.assertEqual(rows[finding.rule]["code"], "BIB005")
        self.assertEqual(rows[finding.rule]["rule"], "bibliography.relationship_unresolved")
        self.assertIn("missing or ambiguous", rows[finding.rule]["next_step"])
        self.assertIsNone(rows["execution.task"]["code"])
        self.assertEqual(get_rule("tex001").code, "TEX001")
        with self.assertRaisesRegex(ValueError, "Unknown check code"):
            get_rule("TEX999")

    def test_rule_facts_are_fields_and_the_code_sets_are_derived_from_them(self):
        self.assertEqual(MANDATORY_CODES, {rule.code for rule in RULES if rule.mandatory})
        self.assertEqual(REVIEWABLE_CODES, {rule.code for rule in RULES if rule.reviewable})
        self.assertTrue(get_rule("TEX005").to_dict()["mandatory"])
        self.assertFalse(get_rule("TEX005").to_dict()["reviewable"])
        self.assertTrue(get_rule("PDF001").to_dict()["reviewable"])
        self.assertEqual(get_rule("PDF001").to_dict()["not_checked_area"], "page_limit")
        self.assertFalse(get_rule("BIB101").to_dict()["mandatory"])
        # An opt-in check is switched on by a setting; a guard that selection cannot remove
        # is not one.
        self.assertFalse(MANDATORY_CODES & set(opt_in_activation()))

    def test_every_not_checked_area_of_a_rule_is_one_a_run_reports(self):
        reported = {
            entry.area: entry.codes
            for entry in not_checked_areas(frozenset())
            if entry.area != OTHER_OPT_IN_AREA
        }
        named = {rule.not_checked_area for rule in RULES if rule.not_checked_area}
        self.assertEqual(set(reported), named)
        for area, codes in reported.items():
            self.assertTrue(set(codes) <= set(opt_in_activation()), area)

    def test_report_order_is_stable_across_completion_orders_and_numeric_pages(self):
        findings = [
            Finding("pdf.test", "Same message", details={"stage": stage, "page": page})
            for stage in ("final", "baseline")
            for page in (10, 2, 1)
        ]
        first = Report("check", findings=findings)
        second = Report("check", findings=list(reversed(findings)))
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(
            [(item.details["stage"], item.details["page"]) for item in first.sorted_findings()],
            [(stage, page) for stage in ("baseline", "final") for page in (1, 2, 10)],
        )


if __name__ == "__main__":
    unittest.main()
