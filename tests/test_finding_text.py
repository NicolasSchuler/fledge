"""The shared wording of finding text and the redaction of secrets in findings."""

from __future__ import annotations

import unittest

from latexprep.finding_text import (
    check_status,
    list_locations,
    more_suffix,
    plural,
    redact_findings,
)
from latexprep.findings import Finding
from latexprep.redaction import LIMIT_REDACTION, MAX_DEPTH, redact_known_secrets, redact_secrets

TOKEN = "ghp_" + "a1B2c3D4" * 4


class WordingTests(unittest.TestCase):
    """Check statuses, location lists and plural counts in finding text."""

    def test_status_prefers_a_confirmed_failure_over_missing_evidence(self) -> None:
        self.assertEqual(check_status(failed=True, incomplete=True), "failed")
        self.assertEqual(check_status(failed=False, incomplete=True), "inconclusive")
        self.assertEqual(check_status(failed=False, incomplete=False), "passed")

    def test_locations_are_distinct_limited_and_counted_in_one_style(self) -> None:
        items = [{"path": "a.tex", "line": line} for line in range(1, 6)]
        items += [{"path": "a.tex", "line": 1}, {"path": "b.tex"}, {"line": 9}, {"path": 3}]
        self.assertEqual(list_locations(items), "a.tex:1, a.tex:2, a.tex:3 (+3 more)")
        self.assertEqual(
            list_locations(items, limit=10), "a.tex:1, a.tex:2, a.tex:3, a.tex:4, a.tex:5, b.tex"
        )
        self.assertEqual(list_locations([]), "")
        self.assertEqual(more_suffix(0), "")
        self.assertEqual(more_suffix(2), " (+2 more)")

    def test_plural_counts_regular_nouns(self) -> None:
        self.assertEqual(plural(1, "float"), "1 float")
        self.assertEqual(plural(0, "float"), "0 floats")


class RedactionTests(unittest.TestCase):
    """Redaction of known and secret-shaped text in findings and nested values."""

    def test_known_secrets_and_secret_shaped_text_are_replaced_everywhere(self) -> None:
        finding = Finding(
            "submission.secret",
            f"Found hunter2hunter2 near {TOKEN}",
            severity="error",
            path="notes-hunter2hunter2.txt",
            suggestion="Remove hunter2hunter2.",
            details={"matches": [{"line": 3, "value": "hunter2hunter2"}], "note": (TOKEN,)},
        )
        [redacted] = redact_findings([finding], {"hunter2hunter2"})
        flat = repr((redacted.message, redacted.path, redacted.suggestion, redacted.details))
        self.assertNotIn("hunter2hunter2", flat)
        self.assertNotIn(TOKEN, flat)
        self.assertEqual(redacted.details["matches"], [{"line": 3, "value": "[REDACTED]"}])
        self.assertEqual(redacted.rule, finding.rule)
        self.assertEqual(redact_secrets(f"key {TOKEN}"), "key [REDACTED]")

    def test_a_finding_too_deep_to_scan_is_withheld_instead_of_overflowing(self) -> None:
        nested: object = TOKEN
        for _ in range(MAX_DEPTH + 5):
            nested = [nested]
        finding = Finding("submission.secret", "Deep", severity="error", details={"deep": nested})
        [redacted] = redact_findings([finding], {TOKEN})
        self.assertEqual(redacted.message, LIMIT_REDACTION)
        self.assertEqual(redacted.details, {})
        self.assertIsNone(redacted.path)

    def test_a_cyclic_value_is_withheld(self) -> None:
        cycle: list[object] = []
        cycle.append(cycle)
        self.assertEqual(redact_known_secrets(cycle, {"x"}), LIMIT_REDACTION)

    def test_values_that_hold_no_text_are_kept(self) -> None:
        self.assertEqual(
            redact_known_secrets({"n": 3, "flag": True, "none": None}, set()),
            {"n": 3, "flag": True, "none": None},
        )


if __name__ == "__main__":
    unittest.main()
