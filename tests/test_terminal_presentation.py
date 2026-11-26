"""Readable, literal report text across terminal capabilities."""

from __future__ import annotations

import os
import unittest
from dataclasses import replace
from io import StringIO
from unittest.mock import patch

from rich.console import Console
from rich.padding import Padding
from rich.text import Text

from latexprep.compact_report import render_compact
from latexprep.finding_view import RenderOptions
from latexprep.findings import Finding
from latexprep.report import Change, ChangeKind, Outcome, Report
from latexprep.terminal_console import stderr_console, stdout_console
from latexprep.terminal_report import print_report, render_terminal
from latexprep.text_safety import sanitize_terminal_text


class TerminalBuffer(StringIO):
    """A string buffer that claims to be a terminal."""

    def isatty(self) -> bool:
        """Return ``True``, as a terminal would."""
        return True


def render(
    report: Report,
    width: int,
    *,
    color: bool = False,
    show_files: bool = False,
    show_diff: bool = False,
) -> str:
    """Render ``report`` as the terminal report on a console ``width`` columns wide.

    Args:
        report: The report to render.
        width: The console width, in columns.
        color: Whether to render as a terminal with the standard colour system.
        show_files: Whether to show the full file inventory of the package.
        show_diff: Whether to show the diff of every change.

    Returns:
        The rendered text, with colour codes when ``color`` is set.
    """
    output = StringIO()
    console = Console(
        file=output,
        width=width,
        force_terminal=color,
        color_system="standard" if color else None,
        no_color=not color,
        markup=True,
    )
    options = RenderOptions(show_files=show_files, show_diff=show_diff)
    # Rich reads TERM and NO_COLOR when it draws, so a dumb or colourless host terminal
    # would otherwise change the coloured rendering.
    with patch.dict(os.environ):
        if color:
            os.environ["TERM"] = "xterm-256color"
            os.environ.pop("NO_COLOR", None)
        print_report(report, console, options=options)
    return output.getvalue()


def styled(report: Report, **options: bool) -> dict[str, set[str]]:
    """Return the styles of each piece of text in the coloured terminal report.

    Args:
        report: The report to render, 100 columns wide.
        **options: ``show_files`` and ``show_diff``, as for :func:`render`.

    Returns:
        The styles of every run of text that has one, by the text without its surrounding
        blanks; text printed in the terminal's own colour and weight is not listed.
    """
    text = Text.from_ansi(render(report, 100, color=True, **options))
    styles: dict[str, set[str]] = {}
    for span in text.spans:
        piece = text.plain[span.start : span.end].strip()
        if piece:
            style = str(span.style)
            for number, name in _ANSI_COLOURS.items():
                style = style.replace(f"color({number})", name)
            styles.setdefault(piece, set()).add(style)
    return styles


# The standard colours, as Rich names them when it reads escape sequences back.
_ANSI_COLOURS = {1: "red", 2: "green", 3: "yellow", 6: "cyan"}


def package(status: str = "verified") -> dict:
    """Return a package inventory of one document with an included and an excluded file.

    The excluded file's name holds Rich markup, an escape sequence and a C1 control, and its
    reason holds Rich markup. Compressed and archive sizes are measured only for a ``verified``
    package.

    Args:
        status: The status of the package.

    Returns:
        The inventory, as a report's ``package_contents`` holds it.
    """
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
    """The terminal report: wrapping, colours, literal text, the package summary and redaction."""

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
        # The opt-in areas left unchecked are named only after a run that passed.
        passed = replace(report, outcome=Outcome.PASSED_WITH_ADVISORIES)
        for width, color in ((width, color) for width in (60, 80, 120) for color in (False, True)):
            with self.subTest(width=width, color=color):
                text = Text.from_ansi(render(report, width, color=color)).plain
                self.assertTrue(all(len(line) <= width for line in text.splitlines()), text)
                joined = "".join(text.split())
                self.assertIn(path, joined)
                self.assertIn("[red]literal[/red]", joined)
                self.assertIn("ACTIONEND", joined)
                self.assertIn(report.artifacts["report"], joined)
                self.assertNotIn("Notchecked", joined)
                self.assertNotIn("…", text)
                self.assertNotIn("\x1b", text)
                text = Text.from_ansi(render(passed, width, color=color)).plain
                self.assertTrue(all(len(line) <= width for line in text.splitlines()), text)
                self.assertIn("Notchecked(needsyoursettings):pagelimit", "".join(text.split()))

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

    def test_only_the_outcome_carries_its_colour_in_the_summary(self):
        # The scope and the counts are read as text: in the outcome's colour, and dimmed
        # on top of it, they were hard to read on dark and on light backgrounds alike.
        report = Report("check", outcome=Outcome.BLOCKED, scope="source checks only")
        styles = styled(report)
        self.assertEqual(styles["Blocked"], {"bold red"})
        counts = next(piece for piece in styles if piece.startswith("0 errors"))
        self.assertEqual(styles[counts], {"dim"})
        self.assertNotIn("— source checks only", styles)
        self.assertFalse([piece for piece in styles if "source checks only" in piece])

    def test_a_finding_reads_as_heading_labels_and_plain_text(self):
        report = Report(
            "check",
            outcome=Outcome.BLOCKED,
            findings=[
                Finding(
                    "manuscript.forbidden_packages",
                    "A forbidden package is loaded.",
                    severity="error",
                    path="main.tex",
                    line=3,
                    details={"stage": "manuscript details"},
                )
            ],
        )
        styles = styled(report)
        heading = next(piece for piece in styles if piece.startswith("TEX103"))
        self.assertEqual(styles[heading], {"bold red"})
        # The severity and status stand beside the heading in its colour, not in bold.
        self.assertEqual(styles["[ERROR / failed]"], {"red"})
        self.assertEqual(styles["Where:"], {"cyan"})
        # The place is what the reader acts on: plain text. Only the stage is dimmed.
        self.assertEqual(styles["[manuscript details]"], {"dim"})
        self.assertNotIn("main.tex:3", styles)
        self.assertFalse([piece for piece in styles if "A forbidden package" in piece])

    def test_follow_on_results_keep_their_message_readable(self):
        problem = Finding(
            "build.tex_error",
            "./extra.tex:3: Undefined control sequence.",
            severity="error",
            path="extra.tex",
            line=3,
            details={"document": "main.tex", "stage": "baseline"},
        )
        summary = Finding(
            "package.independent_documents",
            "At least one selected document package failed verification.",
            severity="error",
        )
        styles = styled(Report("prepare", outcome=Outcome.BLOCKED, findings=[summary, problem]))
        follow_on = next(piece for piece in styles if piece.startswith("PKG301"))
        self.assertEqual(styles[follow_on], {"red"})
        self.assertFalse([piece for piece in styles if "failed verification" in piece])

    def test_a_diff_marks_removed_and_added_lines_and_changes_are_labelled(self):
        diff = "--- main.tex\n+++ main.tex\n@@ -1,2 +1,2 @@\n \\title{A}\n-% note\n+%\n"
        report = Report(
            "prepare",
            outcome=Outcome.PASSED,
            changes=[
                Change("main.tex", kind=ChangeKind.REWRITE, reason="Comments removed.", diff=diff)
            ],
            artifacts={"archive": "/tmp/out/submission.zip"},
        )
        styles = styled(report, show_diff=True)
        self.assertEqual(styles["--- main.tex"], {"bold"})
        self.assertEqual(styles["+++ main.tex"], {"bold"})
        self.assertEqual(styles["@@ -1,2 +1,2 @@"], {"cyan"})
        self.assertEqual(styles["-% note"], {"red"})
        self.assertEqual(styles["+%"], {"green"})
        self.assertNotIn("\\title{A}", styles)
        self.assertEqual(styles["rewrite:"], {"cyan"})
        self.assertEqual(styles["archive:"], {"cyan"})
        plain = render(report, 100, show_diff=True)
        self.assertIn("\n  rewrite: main.tex — Comments removed.\n", plain)
        self.assertIn("\nArtifacts\n  archive: /tmp/out/submission.zip\n", plain)
        # Without colour the same text is printed, and no escape sequence.
        self.assertNotIn("\x1b", plain)

    def test_package_files_are_listed_under_their_headings(self):
        report = Report("prepare", outcome=Outcome.PASSED, package_contents=[package()])
        plain = render(report, 100, show_files=True)
        self.assertIn("\n  Largest compressed entries:\n    10 B: sections__results.tex\n", plain)
        self.assertIn("\n  Files:\n    [included] sections__results.tex", plain)
        self.assertIn("\n      Reason: Literal input dependency\n", plain)
        styles = styled(report, show_files=True)
        self.assertEqual(styles["Package contents"], {"bold cyan"})
        self.assertEqual(styles["[verified]"], {"green"})
        self.assertEqual(styles["[included]"], {"cyan"})
        # Names and sizes are text, not labels.
        self.assertFalse([piece for piece in styles if "uncompressed 100 B" in piece])

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
        # The outcome line and the verdict; the package inventory stays in the JSON report.
        self.assertEqual(
            render_compact(report).splitlines(),
            [
                "planned | 0 errors, 0 warnings, 0 incomplete, 0 not applicable, "
                "0 passed results | scope: source analysis",
                "verdict: Planned: a dry run; nothing was verified or written.",
            ],
        )
        self.assertNotIn("Package contents", render_compact(report))


class InvisibleCharacterTests(unittest.TestCase):
    """Invisible format characters, escape sequences and controls in terminal text."""

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
    """The privacy disclosure of online checks, listed even when passed results are hidden."""

    def report(self) -> Report:
        """Return a passed report with the online sharing disclosure and one other finding."""
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
            self.assertIn("NET008", text)
            self.assertIn("share DOI identifiers", text)
            self.assertNotIn("Metadata inventory", text)

    def test_other_passed_results_stay_hidden_and_the_no_issues_line_remains(self):
        text = render_terminal(self.report())
        self.assertIn("No issues found within this command's stated scope.", text)
        shown = render_terminal(self.report(), options=RenderOptions(show_passed=True))
        self.assertIn("Metadata inventory", shown)
        self.assertNotIn("No issues found", shown)


class ConsoleFactoryTests(unittest.TestCase):
    """The consoles of standard output and standard error: blanks, terminals and literal text."""

    def test_redirected_report_carries_no_trailing_blanks(self):
        sink = StringIO()
        with patch("sys.stdout", sink):
            console = stdout_console()
            console.print(Padding(Text("x" * 30 + " " + "y" * 30), (0, 0, 0, 2), expand=False))
            console.print(Text("done   "))
        lines = sink.getvalue().splitlines()
        self.assertGreaterEqual(len(lines), 2)
        for line in lines:
            self.assertEqual(line, line.rstrip(), repr(line))

    def test_blanks_split_across_writes_are_still_dropped_before_a_line_break(self):
        sink = StringIO()
        with patch("sys.stdout", sink):
            console = stdout_console()
            console.file.write("abc   ")
            console.file.write("\nnext  text")
            console.file.write("  \n")
        self.assertEqual(sink.getvalue(), "abc\nnext  text\n")

    def test_a_terminal_is_written_to_directly(self):
        terminal = TerminalBuffer()
        with patch("sys.stdout", terminal):
            self.assertIs(stdout_console().file, terminal)

    def test_consoles_print_text_literally_and_progress_goes_to_stderr(self):
        buffer = StringIO()
        with patch("sys.stdout", buffer):
            stdout_console().print("[bold]literal[/bold]")
        self.assertEqual(buffer.getvalue().strip(), "[bold]literal[/bold]")
        self.assertTrue(stderr_console().stderr)


if __name__ == "__main__":
    unittest.main()
