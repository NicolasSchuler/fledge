"""One mapping from outcome to severity, and the standard wording of declaration gaps."""

from __future__ import annotations

import unittest

from latexprep.finding_weight import ADVISORY, DIRECT_POLICY, NOTE, POLICY, Weight
from latexprep.findings import EvidenceKind
from latexprep.tex_lexer import conditional_spans, scan_commands
from latexprep.uncertainty_reasons import CONDITIONAL_FLOW, context_reasons, grouped_reason


class WeightTests(unittest.TestCase):
    """The severity that each weight gives to each check status."""

    def test_a_pass_is_always_information(self) -> None:
        for weight in (POLICY, DIRECT_POLICY, ADVISORY, NOTE):
            with self.subTest(weight=weight):
                self.assertEqual(weight.severity("passed"), "info")

    def test_policies_block_when_they_fail_or_cannot_decide(self) -> None:
        for weight in (POLICY, DIRECT_POLICY):
            self.assertEqual(weight.severity("failed"), "error")
            self.assertEqual(weight.severity("inconclusive"), "error")
        self.assertEqual(POLICY.evidence_kind, EvidenceKind.DERIVED)
        self.assertEqual(DIRECT_POLICY.evidence_kind, EvidenceKind.DIRECT)

    def test_advisories_warn_and_notes_only_inform(self) -> None:
        self.assertEqual(
            (ADVISORY.severity("failed"), ADVISORY.severity("inconclusive")),
            ("warning", "warning"),
        )
        self.assertEqual(ADVISORY.evidence_kind, EvidenceKind.HEURISTIC)
        self.assertEqual((NOTE.severity("failed"), NOTE.severity("inconclusive")), ("info", "info"))

    def test_a_weight_can_treat_an_incomplete_scan_more_gently_than_a_failure(self) -> None:
        scan = Weight("warning", "info", EvidenceKind.HEURISTIC)
        self.assertEqual(
            (scan.severity("failed"), scan.severity("inconclusive")), ("warning", "info")
        )
        # Statuses without a verdict of their own report as a failure.
        self.assertEqual(scan.severity("skipped"), "warning")


class ContextReasonTests(unittest.TestCase):
    """The standard reasons that a declaration inside a group or a conditional is uncertain."""

    def reasons(self, source: str, noun: str = "heading") -> list[str]:
        r"""Return the context reasons of the first ``\section`` command in ``source``.

        Args:
            source: The LaTeX source to scan.
            noun: The noun that names the declaration in the reasons.

        Returns:
            The reasons that :func:`context_reasons` gives for the command.
        """
        commands = scan_commands(source)
        target = next(command for command in commands if command.name == "section")
        return context_reasons(target, conditional_spans(commands), noun)

    def test_a_plain_declaration_has_no_reason(self) -> None:
        self.assertEqual(self.reasons(r"\section{A}"), [])

    def test_grouped_and_conditional_declarations_use_the_standard_wording(self) -> None:
        self.assertEqual(self.reasons(r"{\section{A}}"), [grouped_reason("heading")])
        self.assertEqual(grouped_reason("heading"), "heading inside a group or macro argument")
        self.assertEqual(self.reasons(r"\iftrue\section{A}\fi"), [CONDITIONAL_FLOW])
        self.assertEqual(
            self.reasons(r"\iftrue{\section{A}}\fi", "caption"),
            ["caption inside a group or macro argument", CONDITIONAL_FLOW],
        )


if __name__ == "__main__":
    unittest.main()
