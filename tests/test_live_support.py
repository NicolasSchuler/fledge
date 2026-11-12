"""The failure messages and artifacts of the live tests, checked without TeX or any tool."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from latexprep.findings import Finding
from latexprep.report import Outcome, Report
from tests.live_support import (
    ARTIFACT_DIRECTORY_VARIABLE,
    PASSING_OUTCOMES,
    LiveProjectTestCase,
    artifact_file_name,
    blocking_summary,
    finding_statuses,
    live_settings,
    png_bytes,
    save_report_artifact,
)
from tests.support import TemporaryRoot


def blocked_report() -> Report:
    """Return a blocked report with an uncertain error, a warning, a pass and a note."""
    return Report(
        "prepare",
        outcome=Outcome.BLOCKED,
        findings=[
            Finding(
                "build.tex_error",
                "Undefined control sequence.",
                severity="error",
                status="inconclusive",
                incomplete=True,
                details={
                    "stage": "baseline",
                    "uncertainty": ["a dynamic path", "a conditional input"],
                },
            ),
            Finding("source.edit_marker", "TODO left in text.", severity="warning"),
            Finding("source.duplicate_label", "Fine.", severity="warning", status="passed"),
            Finding("source.eps_figures", "An EPS figure.", severity="info", status="failed"),
        ],
    )


class BlockingSummaryTests(unittest.TestCase):
    """The summary names what an assertion message needs to explain an outcome."""

    def test_each_unresolved_finding_has_its_code_rule_stage_status_message_and_uncertainty(self):
        summary = blocking_summary(blocked_report())
        self.assertIn("Unresolved findings (2):", summary)
        self.assertIn(
            "BLD009 build.tex_error | stage: baseline | inconclusive/error (incomplete) | "
            "Undefined control sequence.",
            summary,
        )
        self.assertIn("uncertainty: a dynamic path; a conditional input", summary)
        self.assertIn("TEX001 source.edit_marker | stage: - | failed/warning | TODO left", summary)

    def test_passed_findings_and_informational_notes_are_left_out(self):
        summary = blocking_summary(blocked_report())
        self.assertNotIn("source.duplicate_label", summary)
        self.assertNotIn("source.eps_figures", summary)

    def test_a_parsed_json_report_gives_the_same_summary(self):
        report = blocked_report()
        self.assertEqual(blocking_summary(report.to_dict()), blocking_summary(report))

    def test_a_report_without_unresolved_findings_says_so(self):
        self.assertEqual(blocking_summary(Report("check")), "No finding is unresolved.")

    def test_many_findings_are_counted_beyond_the_listed_ones(self):
        findings = [Finding("source.edit_marker", f"Marker {number}.") for number in range(45)]
        summary = blocking_summary(Report("check", findings=findings))
        self.assertIn("Unresolved findings (45):", summary)
        self.assertIn("... and 5 more", summary)

    def test_findings_are_selected_by_code_and_stage_for_status_lists(self):
        report = Report(
            "prepare",
            findings=[
                Finding("source.edit_marker", "a", status="passed", details={"stage": "baseline"}),
                Finding("source.edit_marker", "b", details={"stage": "final"}),
                Finding("source.duplicate_label", "c"),
            ],
        )
        self.assertEqual(finding_statuses(report, "TEX001"), ["passed", "failed"])
        self.assertEqual(finding_statuses(report, "TEX001", "final"), ["failed"])


class ArtifactTests(TemporaryRoot):
    """A failed outcome assertion saves the report where the environment asks."""

    def use_artifact_directory(self, directory: Path | None) -> None:
        """Set, or remove, the artifact directory variable until the test ends."""
        patched = patch.dict(os.environ)
        patched.start()
        self.addCleanup(patched.stop)
        if directory is None:
            os.environ.pop(ARTIFACT_DIRECTORY_VARIABLE, None)
        else:
            os.environ[ARTIFACT_DIRECTORY_VARIABLE] = str(directory)

    def test_the_file_name_is_the_test_id_without_unsafe_characters(self):
        self.assertEqual(
            artifact_file_name("tests.test_x.Case.test_a[b/c] d"),
            "tests.test_x.Case.test_a_b_c_d.json",
        )
        self.assertEqual(artifact_file_name("../.."), "test.json")
        self.assertLessEqual(len(artifact_file_name("x" * 500)), 165)

    def test_nothing_is_written_when_the_variable_is_unset_or_empty(self):
        self.use_artifact_directory(None)
        self.assertIsNone(save_report_artifact("tests.a.test_b", blocked_report()))
        with patch.dict(os.environ, {ARTIFACT_DIRECTORY_VARIABLE: ""}):
            self.assertIsNone(save_report_artifact("tests.a.test_b", blocked_report()))

    def test_the_report_is_saved_in_a_directory_that_is_created_on_demand(self):
        directory = self.root / "new" / "artifacts"
        self.use_artifact_directory(directory)
        saved = save_report_artifact("tests.a.Case.test_b", blocked_report())
        self.assertEqual(saved, directory / "tests.a.Case.test_b.json")
        assert saved is not None
        document = json.loads(saved.read_text(encoding="utf-8"))
        self.assertEqual(document["outcome"], "blocked")
        (uncertain,) = [item for item in document["findings"] if item["code"] == "BLD009"]
        self.assertEqual(uncertain["details"]["uncertainty"][0], "a dynamic path")

    def test_a_second_report_of_one_test_keeps_the_first(self):
        self.use_artifact_directory(self.root)
        first = save_report_artifact("tests.a.Case.test_twice", blocked_report())
        second = save_report_artifact("tests.a.Case.test_twice", Report("check"))
        assert first is not None and second is not None
        self.assertNotEqual(first, second)
        self.assertEqual(json.loads(first.read_text(encoding="utf-8"))["outcome"], "blocked")
        self.assertEqual(json.loads(second.read_text(encoding="utf-8"))["command"], "check")

    def test_a_failed_outcome_assertion_saves_the_report_before_it_raises(self):
        self.use_artifact_directory(self.root / "artifacts")
        case = LiveProjectTestCase()
        with self.assertRaises(AssertionError) as raised:
            case.assert_outcome(blocked_report(), PASSING_OUTCOMES)
        message = str(raised.exception)
        self.assertIn("Outcome 'blocked' is not one of passed, passed_with_advisories.", message)
        self.assertIn("uncertainty: a dynamic path; a conditional input", message)
        saved = self.root / "artifacts" / artifact_file_name(case.id())
        self.assertIn(f"The full report is saved in {saved}.", message)
        self.assertEqual(json.loads(saved.read_text(encoding="utf-8"))["command"], "prepare")

    def test_a_failed_assertion_without_the_variable_writes_nothing(self):
        self.use_artifact_directory(None)
        with self.assertRaises(AssertionError) as raised:
            LiveProjectTestCase().assert_outcome(blocked_report(), "passed")
        self.assertNotIn("saved", str(raised.exception))

    def test_an_unwritable_artifact_directory_does_not_hide_the_failure(self):
        blocker = self.write("blocker", "a file, not a directory")
        self.use_artifact_directory(blocker / "artifacts")
        with self.assertRaises(AssertionError) as raised:
            LiveProjectTestCase().assert_outcome(blocked_report(), "passed")
        self.assertIn("Outcome 'blocked' is not one of passed.", str(raised.exception))
        self.assertIn("The report could not be saved:", str(raised.exception))


class OutcomeAssertionTests(unittest.TestCase):
    """The outcome assertion accepts a single outcome, a collection, enums and JSON reports."""

    def test_accepted_outcomes_pass_in_every_spelling(self):
        case = LiveProjectTestCase()
        passed = Report("check", outcome=Outcome.PASSED)
        case.assert_outcome(passed)
        case.assert_outcome(passed, "passed")
        case.assert_outcome(passed, Outcome.PASSED)
        case.assert_outcome(passed, {"blocked", Outcome.PASSED})
        case.assert_outcome(passed.to_dict(), PASSING_OUTCOMES)

    def test_another_outcome_fails(self):
        case = LiveProjectTestCase()
        with self.assertRaises(AssertionError):
            case.assert_outcome(Report("check", outcome=Outcome.PASSED), Outcome.BLOCKED)
        with self.assertRaises(AssertionError):
            case.assert_outcome({"outcome": "blocked", "findings": []})


class SettingsAndFixtureTests(unittest.TestCase):
    """The shared settings and the PNG builder."""

    def test_live_settings_pin_the_main_file_the_date_and_the_deadline_unless_overridden(self):
        settings = live_settings()
        self.assertEqual(
            (settings.main, settings.source_date_epoch, settings.job_timeout_seconds),
            ("main.tex", 1_700_000_000, 600),
        )
        flat = live_settings(layout="flat", main="paper.tex")
        self.assertEqual((flat.layout, flat.main), ("flat", "paper.tex"))

    def test_the_png_has_the_signature_and_the_color_of_its_pixel(self):
        data = png_bytes(b"\x00\x80\xff")
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertNotEqual(data, png_bytes())
        self.assertEqual(png_bytes(), png_bytes(b"\xff\x00\x00"))


if __name__ == "__main__":
    unittest.main()
