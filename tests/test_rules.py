"""Catalogue integrity complements, but does not replace, linked behavior tests."""

from __future__ import annotations

import ast
import unittest
from collections import defaultdict
from pathlib import Path

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
    code_for,
    get_rule,
)
from tests.support import recorded_findings

TESTS = Path(__file__).resolve().parent

# Checks that no clean case covers yet and whose linked tests never record a passed result of
# the check. Writing a clean case (see ``tests/test_clean_cases.py``) or a linked test that
# records a passed result removes a code from here; the check below fails until it is
# removed, so the list can only shrink. A new check starts with a clean case instead.
CHECKS_WITHOUT_CLEAN_CASE: frozenset[str] = frozenset(
    {
        "BIB006",
        "BIB008",
        "BLD010",
        "BLD015",
        "BLD107",
        "PKG006",
        "PKG007",
        "PKG008",
        "PKG303",
        "RPT102",
        "RPT103",
        "TEX011",
        "TEX303",
        "TEX307",
        "TEX311",
        "TEX312",
        "TEX324",
    }
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


def clean_cases_by_code() -> dict[str, list[str]]:
    """Find the tests marked ``@clean_case("CODE")``, by the check codes they are the case of.

    The decorators are read from the source of ``tests/test_*.py`` without importing it.

    Returns:
        The dotted ids of the marked tests, for each check code that has any.
    """
    found: dict[str, list[str]] = defaultdict(list)
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for method in node.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for decorator in method.decorator_list:
                    if (
                        isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Name)
                        and decorator.func.id == "clean_case"
                    ):
                        identifier = f"tests.{path.stem}.{node.name}.{method.name}"
                        for argument in decorator.args:
                            assert isinstance(argument, ast.Constant)
                            found[str(argument.value)].append(identifier)
    return found


def run_recording_findings(
    loader: unittest.TestLoader, identifier: str
) -> tuple[list[tuple[str, str]], list[str], bool]:
    """Run one test by its dotted id and record the findings that code built meanwhile.

    Args:
        loader: The loader that finds the test.
        identifier: The dotted id of the test.

    Returns:
        The (rule name, status) of each finding built, the descriptions of every failure or
        error, and whether the test was skipped.
    """
    result = unittest.TestResult()
    with recorded_findings() as seen:
        loader.loadTestsFromName(identifier).run(result)
    problems = [f"{identifier}: {text[-400:]}" for _, text in (*result.failures, *result.errors)]
    return seen, problems, bool(result.skipped)


class RegisteredTestEvidenceTests(unittest.TestCase):
    """What the tests linked to a check show: that it fires, and that it can pass."""

    def test_each_linked_test_builds_a_finding_of_its_check_and_each_check_has_a_clean_case(self):
        loader = unittest.TestLoader()
        emitted: dict[str, list[tuple[str, str]]] = {}
        skipped: set[str] = set()
        problems: list[str] = []
        for identifier in sorted({test for rule in RULES for test in rule.tests}):
            seen, failures, was_skipped = run_recording_findings(loader, identifier)
            emitted[identifier] = seen
            problems.extend(failures)
            if was_skipped:
                skipped.add(identifier)
        self.assertEqual(problems, [], "a linked test fails when the catalogue check runs it")
        clean_cases = clean_cases_by_code()
        self.assertEqual(sorted(set(clean_cases) - set(BY_CODE)), [], "unknown check codes")
        without_evidence: set[str] = set()
        checkable: set[str] = set()
        for rule in RULES:
            with self.subTest(code=rule.code):
                statuses = [
                    status
                    for test in rule.tests
                    for name, status in emitted[test]
                    if code_for(name) == rule.code
                ]
                runnable = [test for test in rule.tests if test not in skipped]
                if runnable:
                    checkable.add(rule.code)
                    self.assertTrue(
                        statuses,
                        f"no test linked to {rule.code} ({rule.name}) builds a finding of it; "
                        f"linked: {', '.join(rule.tests)}",
                    )
                if runnable and "passed" not in statuses and rule.code not in clean_cases:
                    without_evidence.add(rule.code)
        # A check whose linked tests all skip on this host cannot be judged here.
        self.assertEqual(
            sorted(without_evidence),
            sorted(CHECKS_WITHOUT_CLEAN_CASE & checkable),
            'Every check needs a clean case: a test marked @clean_case("CODE") that runs '
            "the check on allowed input (tests/test_clean_cases.py), or a linked test that "
            "builds a passed result. Codes only in the left list lack one; codes only in "
            "the right list have one now and leave CHECKS_WITHOUT_CLEAN_CASE.",
        )


if __name__ == "__main__":
    unittest.main()
