"""Catalogue integrity complements, but does not replace, linked behavior tests."""

from __future__ import annotations

import unittest

from latexprep.models import Finding, Report
from latexprep.rules import BY_CODE, BY_NAME, RULES, get_rule


class RuleRegistryTests(unittest.TestCase):
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
        self.assertIn("missing or ambiguous", rows[finding.rule]["suggestion"])
        self.assertIsNone(rows["execution.task"]["code"])
        self.assertEqual(get_rule("tex001").code, "TEX001")
        with self.assertRaisesRegex(ValueError, "Unknown check code"):
            get_rule("TEX999")

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
