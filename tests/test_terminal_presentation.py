"""Readable, literal report text and isolated progress across terminal capabilities."""

from __future__ import annotations

import os
import re
import sys
import unittest
from io import StringIO
from unittest.mock import patch

from rich.console import Console
from rich.text import Text

from latexprep.finding_view import RenderOptions
from latexprep.models import Finding, Outcome, Report
from latexprep.terminal_progress import TerminalProgress
from latexprep.terminal_report import (
    print_report,
    render_compact,
    render_terminal,
    sanitize_terminal_text,
)


class TerminalBuffer(StringIO):
    def isatty(self) -> bool:
        return True


def render(report: Report, width: int, *, color: bool = False, show_files: bool = False) -> str:
    output = StringIO()
    console = Console(
        file=output,
        width=width,
        force_terminal=color,
        color_system="standard" if color else None,
        no_color=not color,
        markup=True,
    )
    print_report(report, console, options=RenderOptions(show_files=show_files))
    return output.getvalue()


def package(status: str = "verified") -> dict:
    measured = status == "verified"
    return {
        "document": "main.tex",
        "status": status,
        "files": [
            {
                "source": "sections/results.tex",
                "path": "sections__results.tex",
                "status": "included",
                "reasons": ["Literal input dependency"],
                "size_bytes": 100,
                "compressed_bytes": 10 if measured else None,
            },
            {
                "source": "notes[bold].tex\x1b[31m\x85",
                "path": "notes[bold].tex\x1b[31m\x85",
                "status": "excluded",
                "reasons": ["Unused by the selected document [red]literal[/red]"],
                "size_bytes": 30,
                "compressed_bytes": None,
            },
        ],
        "archive_bytes": 58 if measured else None,
        "overhead_bytes": 48 if measured else None,
    }


class TerminalPresentationTests(unittest.TestCase):
    def test_paths_actions_and_scope_wrap_without_losing_text(self):
        path = "sections/" + "long-results-directory/" * 8 + "[bold]results.tex"
        report = Report(
            "prepare",
            outcome=Outcome.BLOCKED,
            scope="selected source, build, preservation and fresh ZIP rebuild checks",
            main=path,
            findings=[
                Finding(
                    "source.missing_dependency",
                    "The dependency is missing [red]literal[/red].",
                    severity="error",
                    path=path,
                    suggestion="Review the full source path and repeat the check. ACTION END",
                )
            ],
            artifacts={"report": "/tmp/" + "nested-artifacts/" * 12 + "report.json"},
        )
        report.execution["not_checked"] = [
            {
                "area": "page_limit",
                "label": "page limit",
                "settings": ["max_pages"],
                "codes": ["PDF001"],
            }
        ]
        for width, color in ((width, color) for width in (60, 80, 120) for color in (False, True)):
            with self.subTest(width=width, color=color):
                text = Text.from_ansi(render(report, width, color=color)).plain
                self.assertTrue(all(len(line) <= width for line in text.splitlines()), text)
                joined = "".join(text.split())
                self.assertIn(path, joined)
                self.assertIn("[red]literal[/red]", joined)
                self.assertIn("ACTIONEND", joined)
                self.assertIn(report.artifacts["report"], joined)
                self.assertIn("Notchecked", joined)
                self.assertNotIn("…", text)
                self.assertNotIn("\x1b", text)

    def test_wrapped_detail_lines_keep_indentation_and_short_lines_keep_no_padding(self):
        report = Report(
            "check",
            findings=[
                Finding(
                    "manuscript.forbidden_packages",
                    "Forbidden.",
                    severity="error",
                    details={"matches": [{"path": "a.tex", "line": 3}]},
                )
            ],
        )
        text = render(report, 60)
        self.assertIn("  Evidence: a.tex:3\n  Next: ", text)
        next_lines = text[text.index("  Next:") :].split("\n\n", 1)[0].splitlines()
        self.assertGreater(len(next_lines), 1)
        self.assertTrue(all(line.startswith("  ") for line in next_lines), next_lines)

    def test_outcomes_have_semantic_colors(self):
        for outcome, ansi in (
            ("passed", "32"),
            ("planned", "36"),
            ("passed_with_advisories", "33"),
            ("accepted_exceptions", "33"),
            ("blocked", "31"),
            ("error", "31"),
        ):
            with self.subTest(outcome=outcome):
                text = render(Report("check", outcome=outcome), 80, color=True)
                self.assertRegex(text, rf"\x1b\[[0-9;]*{ansi}m")
                self.assertIn(outcome.replace("_", " ").capitalize(), text)

    def test_source_controls_and_rich_tags_are_literal_in_all_sections(self):
        malicious = "[bold]paper[/bold]\x1b]8;;https://example.invalid\x07\x1b[31m\x85.tex"
        report = Report(
            "check",
            outcome=Outcome.BLOCKED,
            main=malicious,
            findings=[
                Finding(
                    malicious, malicious, severity="error", path=malicious, suggestion=malicious
                )
            ],
            artifacts={malicious: malicious},
        )
        for text in (render(report, 80), render_compact(report)):
            self.assertIn("[bold]paper[/bold]", text)
            self.assertNotIn("example.invalid", text)
            self.assertNotRegex(text, r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

    def test_package_summary_full_inventory_and_measurement_boundary(self):
        for status in ("proposed", "prepared", "verified", "incomplete"):
            with self.subTest(status=status):
                report = Report(
                    "prepare", outcome=Outcome.PLANNED, package_contents=[package(status)]
                )
                summary = render(report, 80)
                self.assertIn(f"[{status}]", summary)
                self.assertIn("1 included, 1 excluded", summary)
                self.assertIn("sections/results.tex -> sections__results.tex", summary)
                self.assertNotIn("notes[bold].tex", summary)
                full = render(report, 60, show_files=True)
                self.assertIn("notes[bold].tex", full)
                self.assertIn("Unused by the selected document", full)
                self.assertIn("[red]literal[/red]", full)
                self.assertNotIn("\x1b", full)
                self.assertNotIn("\x85", full)
                self.assertTrue(all(len(line) <= 60 for line in full.splitlines()), full)
                if status == "verified":
                    self.assertIn("ZIP: 58 B; ZIP overhead: 48 B", summary)
                    self.assertIn("Largest compressed entries:", summary)
                    self.assertNotIn("has not been verified", summary)
                else:
                    self.assertIn("has not been verified", summary)
                    self.assertIn("ZIP: not measured", summary)
                    self.assertNotIn("Largest compressed entries", summary)

    def test_package_contents_are_redacted_with_the_report(self):
        report = Report(
            "prepare",
            outcome=Outcome.PASSED,
            settings={"password": "SyntheticInventoryCredential"},
            package_contents=[package()],
        )
        report.package_contents[0]["files"][0]["path"] = "SyntheticInventoryCredential.tex"
        text = render_terminal(report, options=RenderOptions(show_files=True))
        self.assertNotIn("SyntheticInventoryCredential", text)
        self.assertIn("REDACTED", text)

    def test_compact_records_keep_their_existing_scope(self):
        report = Report("prepare", outcome=Outcome.PLANNED, package_contents=[package("proposed")])
        self.assertEqual(len(render_compact(report).splitlines()), 1)
        self.assertNotIn("Package contents", render_compact(report))


class InvisibleCharacterTests(unittest.TestCase):
    def test_format_characters_such_as_bidi_overrides_are_shown_not_applied(self):
        text = "gpj.\u202etxet\u2066x\u200d\U000e0001"
        self.assertEqual(sanitize_terminal_text(text), "gpj.\\u202etxet\\u2066x\\u200d\\U000e0001")

    def test_escape_sequences_and_controls_are_still_removed_and_text_is_kept(self):
        self.assertEqual(
            sanitize_terminal_text("a\x1b[31mb\x1b]0;title\x07c\x00d\u0085e\nf\tg"),
            "abcde\nf\tg",
        )

    def test_a_reordering_override_in_a_file_name_is_visible_in_every_terminal_report(self):
        report = Report(
            "inspect",
            outcome=Outcome.PASSED_WITH_ADVISORIES,
            findings=[Finding("source.edit_marker", "TODO", path="figure\u202egpj.tex", line=1)],
        )
        for text in (render_terminal(report), render_compact(report)):
            self.assertNotIn("\u202e", text)
            self.assertIn("\\u202e", text)


class OnlineDisclosureTests(unittest.TestCase):
    def report(self) -> Report:
        return Report(
            "inspect",
            outcome=Outcome.PASSED,
            findings=[
                Finding(
                    "online.sharing",
                    "Explicit online checks share DOI identifiers and URL destinations.",
                    severity="info",
                    status="passed",
                ),
                Finding("pdf.metadata", "Metadata inventory", severity="info", status="passed"),
            ],
        )

    def test_the_privacy_disclosure_is_listed_without_show_passed(self):
        for text in (render_terminal(self.report()), render_compact(self.report())):
            self.assertIn("online.sharing", text)
            self.assertIn("share DOI identifiers", text)
            self.assertNotIn("Metadata inventory", text)

    def test_other_passed_results_stay_hidden_and_the_no_issues_line_remains(self):
        text = render_terminal(self.report())
        self.assertIn("No issues found within this command's stated scope.", text)
        shown = render_terminal(self.report(), options=RenderOptions(show_passed=True))
        self.assertIn("Metadata inventory", shown)
        self.assertNotIn("No issues found", shown)


class TerminalProgressTests(unittest.TestCase):
    def test_redirected_progress_is_plain_literal_stages(self):
        output = StringIO()
        console = Console(file=output, width=80, markup=True)
        with TerminalProgress(console) as update:
            self.assertIsNotNone(update)
            update("Starting [bold]source[/bold]\x1b[31m")
            update("baseline build: succeeded (0.1s)")
        self.assertEqual(
            output.getvalue(),
            "Starting [bold]source[/bold]\nbaseline build: succeeded (0.1s)\n",
        )

    def test_quiet_progress_emits_nothing(self):
        for terminal in (False, True):
            output = TerminalBuffer() if terminal else StringIO()
            with TerminalProgress(Console(file=output), quiet=True) as update:
                self.assertIsNone(update)
            self.assertEqual(output.getvalue(), "")

    def test_terminal_progress_has_stage_elapsed_and_restores_on_exception(self):
        output = TerminalBuffer()
        stdout, stderr = sys.stdout, sys.stderr
        progress = TerminalProgress(
            Console(file=output, width=60, force_terminal=True, color_system=None)
        )
        with self.assertRaisesRegex(RuntimeError, "controlled failure"):
            with progress as update:
                self.assertIs(sys.stdout, stdout)
                self.assertIs(sys.stderr, stderr)
                update("Checking [bold]literal[/bold]\x1b[31m")
                self.assertIsNotNone(progress._progress)
                raise RuntimeError("controlled failure")
        text = output.getvalue()
        self.assertIn("Checking [bold]literal[/bold]", text)
        self.assertRegex(text, r"\d+:\d\d:\d\d")
        self.assertNotIn("\x1b[31m", text)
        self.assertIsNone(progress._progress)
        self.assertIs(sys.stdout, stdout)
        self.assertIs(sys.stderr, stderr)

    def test_dumb_terminal_uses_plain_progress(self):
        with patch.dict(os.environ, {"TERM": "dumb"}, clear=False):
            output = TerminalBuffer()
            with TerminalProgress(Console(file=output)) as update:
                update("Starting baseline build")
            self.assertEqual(output.getvalue(), "Starting baseline build\n")

    def test_no_color_terminal_retains_progress_without_color_codes(self):
        with patch.dict(os.environ, {"NO_COLOR": "1", "TERM": "xterm"}, clear=False):
            output = TerminalBuffer()
            with TerminalProgress(Console(file=output, width=60)) as update:
                update("Checking sources")
            text = output.getvalue()
            self.assertIn("Checking sources", text)
            # Cursor controls are expected for live progress, SGR colors are not.
            self.assertFalse(re.search(r"\x1b\[[0-9;]*(?:3[0-7]|9[0-7])m", text), text)


if __name__ == "__main__":
    unittest.main()
