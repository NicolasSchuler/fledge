"""Report rendering: the JSON, terminal and offline HTML reports."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from latexprep.ci_annotations import render_ci_annotations
from latexprep.compact_report import render_compact
from latexprep.execution_records import CheckSelectionRecord
from latexprep.finding_view import RenderOptions, evidence_excerpt, selection_description
from latexprep.findings import Finding
from latexprep.html_report import render_html
from latexprep.measurement_findings import measurement_finding
from latexprep.report import Change, ChangeKind, Outcome, Report
from latexprep.report_files import (
    ChangedReportError,
    ReportFiles,
    ReportIdentity,
    save_reports,
    write_report_text,
)
from latexprep.report_formats import annotation_prefix, working_directory_path
from latexprep.review_policy import (
    ReportingOptions,
    ReviewRule,
    apply_reviews,
)
from latexprep.terminal_report import render_terminal


class _Elements(HTMLParser):
    """Every start tag of a rendered page and the attributes they carry.

    Attributes:
        tags: The name of every start tag, in document order.
        attributes: The name and value of every attribute of those tags, in document order.
    """

    def __init__(self):
        super().__init__()
        self.tags = []
        self.attributes = []

    def handle_starttag(self, tag, attrs):
        """Record ``tag`` and its ``attrs``."""
        self.tags.append(tag)
        self.attributes.extend(attrs)


class RenderTests(unittest.TestCase):
    """Escaping, redaction, evidence and review reasons in the HTML report and CI annotations."""

    def report(self):
        """Return a blocked report that carries an injection payload in its text fields.

        The payload closes a ``pre`` element and adds a script and a remote image. It is the
        scope, the main document, the suggestion and the path and reason of the change, and it
        starts the finding message and the diff; the finding path holds a script of its own. A
        synthetic secret appears in a setting, the finding message and the diff, and the PDF
        artifact points at a remote URL.

        Returns:
            The secret, the payload and the report.
        """
        secret = "SyntheticCredential_1234"
        payload = '</pre><script>alert(1)</script><img src="https://attacker.invalid" onerror="x">'
        report = Report(
            "check",
            outcome=Outcome.BLOCKED,
            scope=payload,
            main=payload,
            settings={"password": secret},
            findings=[
                Finding(
                    "pdf.printable_text_bounds",
                    payload + secret,
                    path='"/><script>alert(2)</script>.pdf',
                    suggestion=payload,
                    details={"page": 2, "bounds_pt": [1, 2, 30, 40]},
                )
            ],
            changes=[
                Change(
                    payload,
                    kind=ChangeKind.FORMAT,
                    reason=payload,
                    diff=f"--- a\n+++ b\n@@ -1 +1 @@\n-{payload}\n+{secret}\n",
                )
            ],
            artifacts={"pdf": "https://attacker.invalid/document.pdf"},
        )
        return secret, payload, report

    def test_html_escapes_every_surface_and_contains_no_remote_or_executable_resources(self):
        secret, payload, report = self.report()
        rendered = render_html(report)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(payload, rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("Before", rendered)
        self.assertIn("After", rendered)
        parser = _Elements()
        parser.feed(rendered)
        self.assertNotIn("script", parser.tags)
        self.assertNotIn("img", parser.tags)
        self.assertNotIn("iframe", parser.tags)
        for name, value in parser.attributes:
            self.assertFalse(name.startswith("on"))
            self.assertNotIn(name, {"src", "srcdoc"})
            if name == "href":
                self.assertFalse(value.startswith(("http:", "https:", "javascript:", "//")))
        self.assertIn("Content-Security-Policy", rendered)
        self.assertIn("Next action:", rendered)

    def test_pdf_region_diagrams_use_recorded_geometry_and_safe_local_links(self):
        report = Report(
            "check",
            findings=[
                Finding(
                    "pdf.geometry",
                    "Dimensions",
                    severity="info",
                    status="passed",
                    details={"dimensions_pt": [[100, 200], [300, 400]], "stage": "final"},
                ),
                Finding(
                    "pdf.text_overlap",
                    "Overlap",
                    details={
                        "stage": "final",
                        "violations": [{"page": 2, "bounds_pt": [[1, 2, 3, 4], [2, 3, 4, 5]]}],
                    },
                ),
            ],
            artifacts={"pdf": "/tmp/paper #1.pdf"},
        )
        rendered = render_html(report)
        self.assertIn('viewBox="0 0 300 400"', rendered)
        self.assertIn("/tmp/paper%20%231.pdf#page=2", rendered)
        self.assertIn("does not embed or modify the PDF", rendered)
        report.artifacts["pdf"] = "javascript:bad.pdf"
        self.assertNotIn('href="javascript', render_html(report))
        report.findings[-1] = replace(
            report.findings[-1],
            details={
                "page": 2,
                "bounds_pt": [0, 0, float("nan"), 20],
            },
        )
        self.assertNotIn("<svg", render_html(report))

    def test_ci_annotations_escape_injected_commands_paths_and_credentials(self):
        report = Report(
            "check",
            settings={"password": "Credential_1"},
            findings=[
                Finding(
                    "source.edit_marker",
                    "Credential_1\n::error::injected\r\x1b[2J %0A",
                    path="main,part.tex",
                    line=5,
                )
            ],
        )
        annotation = render_ci_annotations(report)
        self.assertEqual(len(annotation.splitlines()), 2)
        self.assertNotIn("Credential_1", annotation)
        self.assertNotIn("\x1b", annotation)
        self.assertIn("%0A::error::injected%0D%1B", annotation)
        self.assertIn("file=main%2Cpart.tex,line=5", annotation)
        literal = Report("check", findings=[Finding("source.edit_marker", "literal %0A")])
        self.assertIn("%250A", render_ci_annotations(literal))
        report.findings[0] = replace(report.findings[0], path="../../outside.tex")
        self.assertNotIn("file=", render_ci_annotations(report))

    def test_ci_hides_scope_disclaimers_unless_passed_results_are_requested(self):
        report = Report(
            "check",
            findings=[
                Finding("online.metadata", "Offline run.", severity="info", status="skipped"),
                Finding(
                    "build.bibliography_backend",
                    "No backend.",
                    severity="info",
                    status="not_applicable",
                ),
                Finding("pdf.pages", "1 page", severity="info", status="passed"),
                Finding("pdf.fonts", "Fonts unavailable", severity="error", status="skipped"),
                Finding("source.edit_marker", "Marker", path="main.tex", line=2),
            ],
        )
        annotations = render_ci_annotations(report)
        # Two open findings and the closing verdict.
        self.assertEqual(len(annotations.splitlines()), 3)
        for hidden in ("Offline run", "No backend", "1 page"):
            self.assertNotIn(hidden, annotations)
        everything = render_ci_annotations(report, options=RenderOptions(show_passed=True))
        self.assertEqual(len(everything.splitlines()), 6)
        self.assertIn("info/not applicable: No backend.", everything)
        self.assertTrue(report.findings[0].is_scope_disclaimer)
        self.assertFalse(report.findings[3].is_scope_disclaimer)
        self.assertFalse(replace(report.findings[0], status="failed").is_scope_disclaimer)

    def test_evidence_excerpt_uses_known_lists_in_order_and_counts_hidden_items(self):
        def excerpt(**details):
            return evidence_excerpt(Finding("pdf.fonts", "Generic.", details=details))

        self.assertEqual(excerpt(), "")
        self.assertEqual(excerpt(matches=[], violations=[{"page": 5}]), "page 5")
        self.assertEqual(
            excerpt(violations=[{"page": 5}], matches=[{"path": "a.tex"}, {"path": "b.tex"}]),
            "a.tex, b.tex",
        )
        self.assertEqual(excerpt(unrelated=["x"]), "")
        self.assertEqual(
            excerpt(
                matches=[{"path": f"{n}.tex", "line": n} for n in range(1, 8)],
            ),
            "1.tex:1, 2.tex:2, 3.tex:3 (+4 more)",
        )
        self.assertEqual(
            excerpt(violations=[{"page": 2}, {"page": 3}], violation_count=12),
            "page 2, page 3 (+10 more)",
        )
        self.assertEqual(excerpt(changed_pages=[4, 6]), "page 4, page 6")
        self.assertEqual(
            excerpt(nonembedded=["Helvetica", "Times", "Symbol", "Zapf"]),
            "Helvetica, Times, Symbol (+1 more)",
        )
        self.assertEqual(excerpt(candidates=["a.tex", "b.tex"]), "a.tex, b.tex")
        self.assertEqual(
            excerpt(candidates=[{"doi": "10.1/a", "title": "A"}, {"doi": "10.1/b", "title": "B"}]),
            "10.1/a, 10.1/b",
        )
        self.assertEqual(
            excerpt(issues=["Unreadable source: a.tex", "Unreadable source: b.tex"]),
            "Unreadable source: a.tex, Unreadable source: b.tex",
        )
        self.assertEqual(excerpt(matches=[], issues=[{"path": "a.png", "reason": "x"}]), "a.png")
        self.assertEqual(excerpt(matches=["a.tex"], issues=["unreadable"]), "a.tex")
        self.assertEqual(
            excerpt(violations=[{"page": 4, "reason": "Duplicate marked-content reference"}]),
            "Duplicate marked-content reference (page 4)",
        )
        clipped = excerpt(violations=[{"page": 4, "reason": "x" * 500}])
        self.assertEqual((len(clipped), clipped[-10:]), (120, "… (page 4)"))
        self.assertEqual(excerpt(missing=["twocolumn"]), "twocolumn")
        self.assertEqual(excerpt(entries=[{"name": "key1"}, {"key": "key2"}]), "key1, key2")
        self.assertEqual(excerpt(below_minimum=[{"page": 1, "number": 2, "x_dpi": 72}]), "page 1")
        self.assertEqual(
            excerpt(matches=[{"path": "a.pdf", "page": 3}, {"path": "a.pdf", "line": 4}]),
            "a.pdf (page 3), a.pdf:4",
        )
        # Repeated locations are shown once; unreadable items are counted as not shown.
        self.assertEqual(
            excerpt(matches=[{"path": "a.tex", "line": 1}] * 3 + [{"unknown": 1}]),
            "a.tex:1 (+1 more)",
        )
        self.assertEqual(excerpt(matches=[{"unknown": 1}], violations=[{"page": 2}]), "page 2")
        self.assertEqual(excerpt(matches=[True, None, 1.5, [1]]), "")
        self.assertEqual(len(excerpt(matches=["x" * 500])), 120)
        passed = Finding(
            "pdf.fonts", "ok", severity="info", status="passed", details={"matches": ["a.tex"]}
        )
        self.assertEqual(evidence_excerpt(passed), "")

    def test_ci_and_html_show_sanitized_evidence_excerpts(self):
        secret = "SyntheticCredential_1234"
        report = Report(
            "check",
            settings={"password": secret},
            findings=[
                Finding(
                    "manuscript.forbidden_packages",
                    "Generic message.",
                    details={
                        "matches": [
                            {"path": f"{secret}.tex", "line": 2},
                            {"path": "b\n::error::injected.tex", "line": 3},
                            {"path": "<script>.tex"},
                        ]
                    },
                )
            ],
        )
        annotation = render_ci_annotations(report)
        self.assertEqual(len(annotation.splitlines()), 2)
        self.assertIn("| Evidence: ", annotation)
        self.assertIn("b%0A::error::injected.tex:3", annotation)
        self.assertNotIn(secret, annotation)
        rendered = render_html(report)
        self.assertIn("<p>Evidence: ", rendered)
        self.assertIn("&lt;script&gt;.tex", rendered)
        self.assertNotIn("<script>.tex", rendered)

    def test_html_retains_distinct_violation_evidence_across_stages(self):
        baseline_violation = {"page": 1, "reported_size_pt": 8}
        for final_violation in (
            {"page": 2, "reported_size_pt": 8},
            {"page": 1, "reported_size_pt": 9},
        ):
            with self.subTest(final_violation=final_violation):
                report = Report(
                    "prepare",
                    findings=[
                        measurement_finding(
                            "pdf.rendered_text_size",
                            "Compared reported text sizes with configured minima.",
                            [violation],
                            details={"stage": stage},
                        )
                        for stage, violation in (
                            ("baseline", baseline_violation),
                            ("final verification", final_violation),
                        )
                    ],
                )
                rendered = render_html(report)
                self.assertEqual(rendered.count("<summary>Recorded evidence</summary>"), 2)
                self.assertIn("[baseline]", rendered)
                self.assertIn("[final verification]", rendered)
                for violation in (baseline_violation, final_violation):
                    for key, value in violation.items():
                        self.assertIn(f"&quot;{key}&quot;: {value}", rendered)

    def test_html_merges_identical_stage_evidence_regardless_of_key_order(self):
        report = Report(
            "prepare",
            findings=[
                measurement_finding(
                    "pdf.rendered_text_size",
                    "Compared reported text sizes with configured minima.",
                    [violation],
                    details={"stage": stage},
                )
                for stage, violation in (
                    ("baseline", {"page": 1, "reported_size_pt": 8}),
                    ("final verification", {"reported_size_pt": 8, "page": 1}),
                )
            ],
        )
        rendered = render_html(report)
        self.assertEqual(rendered.count("<summary>Recorded evidence</summary>"), 1)
        self.assertIn("[baseline, final verification]", rendered)

    def test_review_reasons_are_visible_and_redacted_in_html_and_ci(self):
        report = Report(
            "check",
            outcome=Outcome.PASSED,
            findings=[
                Finding(
                    "manuscript.abstract_citations",
                    "Citation",
                    severity="error",
                    path="main.tex",
                )
            ],
            settings={"password": "SensitiveExample"},
        )
        options = ReportingOptions(
            accepted_exceptions=(ReviewRule("MAN003", "Reviewed SensitiveExample <example>"),)
        )
        report = apply_reviews(report, options)
        for output in (render_html(report), render_ci_annotations(report)):
            self.assertIn("Accepted exception", output)
            self.assertNotIn("SensitiveExample", output)
            self.assertIn("failed", output)
        self.assertTrue(render_ci_annotations(report).startswith("::notice "))

    def test_accepted_stage_copy_does_not_hide_an_unaccepted_copy(self):
        copies = [
            Finding(
                "manuscript.abstract_citations",
                "Citation",
                severity="error",
                path="main.tex",
                details={"stage": stage},
            )
            for stage in ("baseline", "final source checks")
        ]
        options = ReportingOptions(
            accepted_exceptions=(ReviewRule("MAN003", "Reviewed baseline", stage="baseline"),)
        )
        report = apply_reviews(Report("check", findings=copies), options)
        self.assertEqual(report.outcome, Outcome.BLOCKED)
        annotations = render_ci_annotations(report).splitlines()
        # The two copies and the closing verdict.
        self.assertEqual(len(annotations), 3)
        self.assertTrue(any(line.startswith("::error ") for line in annotations))
        self.assertTrue(any("Accepted exception" in line for line in annotations))
        self.assertTrue(annotations[-1].startswith("::notice title=Fledge check::Blocked: "))
        self.assertEqual(render_html(report).count("Citation"), 2)


class _Structure(HTMLParser):
    """The landmark, title, heading and table structure of a rendered page.

    Attributes:
        title: The text of the ``title`` element.
        mains: The number of ``main`` elements.
        captions: The number of ``caption`` elements.
        table_headers: The attributes of every ``th`` element, in document order.
        tables: The number of ``table`` elements.
    """

    def __init__(self):
        super().__init__()
        self.title = ""
        self.mains = 0
        self.captions = 0
        self.table_headers: list[dict[str, str | None]] = []
        self.tables = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        """Count a ``main``, ``table`` or ``caption`` tag, keep ``th`` attributes, enter a title.

        Args:
            tag: The name of the tag.
            attrs: The name and value of each attribute of the tag.
        """
        if tag == "title":
            self._in_title = True
        elif tag == "main":
            self.mains += 1
        elif tag == "table":
            self.tables += 1
        elif tag == "caption":
            self.captions += 1
        elif tag == "th":
            self.table_headers.append(dict(attrs))

    def handle_endtag(self, tag):
        """Leave the title when ``tag`` closes it."""
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        """Append ``data`` to the title while inside the ``title`` element."""
        if self._in_title:
            self.title += data


class HtmlPageStructureTests(unittest.TestCase):
    """The landmarks, title, tables and stylesheet of the HTML report page."""

    def report(self, **fields):
        """Return a ``prepare`` report with advisories and one rewritten file with a diff.

        Args:
            **fields: Further fields of the :class:`Report`.

        Returns:
            The report.
        """
        return Report(
            "prepare",
            outcome=Outcome.PASSED_WITH_ADVISORIES,
            scope="selected checks",
            changes=[
                Change(
                    "sections/intro.tex",
                    kind=ChangeKind.REWRITE,
                    reason="Removed comments",
                    diff="--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n",
                )
            ],
            **fields,
        )

    def structure(self, report):
        """Render ``report`` as HTML and return the parsed structure of the page."""
        parser = _Structure()
        parser.feed(render_html(report))
        return parser

    def test_page_has_one_main_landmark_and_a_title_naming_command_and_document(self):
        page = self.structure(self.report(main="paper/main.tex"))
        self.assertEqual(page.mains, 1)
        self.assertEqual(page.title, "Fledge prepare report: paper/main.tex")
        self.assertEqual(self.structure(self.report()).title, "Fledge prepare report")

    def test_title_escapes_the_document_name(self):
        rendered = render_html(self.report(main="</title><script>alert(1)</script>"))
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;/title&gt;", rendered.split("</title>")[0])

    def test_every_table_has_a_caption_and_scoped_column_headers(self):
        page = self.structure(
            self.report(
                package_contents=[
                    {
                        "document": "main",
                        "status": "verified",
                        "files": [{"source": "a.tex", "path": "a.tex", "status": "included"}],
                    }
                ]
            )
        )
        self.assertEqual(page.tables, 2)
        self.assertEqual(page.captions, page.tables)
        self.assertTrue(page.table_headers)
        self.assertTrue(all(header.get("scope") == "col" for header in page.table_headers))

    def test_diff_caption_names_the_changed_file(self):
        self.assertIn(
            "<caption>Changes to sections/intro.tex</caption>", render_html(self.report())
        )

    def test_stylesheet_ships_as_package_data_with_dark_mode_and_print_rules(self):
        from importlib import resources

        css = resources.files("latexprep").joinpath("report.css").read_text(encoding="utf-8")
        self.assertIn("@media(prefers-color-scheme:dark)", css)
        self.assertIn("@media print", css)
        self.assertNotIn("</style", css)
        self.assertIn(css, render_html(self.report()))
        manifest = (Path(__file__).resolve().parents[1] / "MANIFEST.in").read_text()
        self.assertIn("include src/latexprep/report.css", manifest.splitlines())


class CiAnnotationTitleAndPathTests(unittest.TestCase):
    """CI annotations name the check, locate files in the repository and end with a verdict."""

    def report(self) -> Report:
        """Return a blocked report: a missing file, the TeX error it caused, a suggestion."""
        return Report(
            "check",
            outcome=Outcome.BLOCKED,
            findings=[
                Finding(
                    "source.missing_dependency",
                    "Missing figures/plot.",
                    severity="error",
                    path="main.tex",
                    line=4,
                ),
                Finding(
                    "build.tex_error",
                    "LaTeX Error: File `figures/plot' not found.",
                    severity="error",
                    path="main.tex",
                    line=4,
                ),
                Finding(
                    "source.edit_marker",
                    "Consider rewording.",
                    severity="suggestion",
                    path="main.tex",
                    line=9,
                ),
            ],
        )

    def test_titles_files_follow_ons_suggestions_and_the_verdict(self):
        lines = render_ci_annotations(
            self.report(), options=RenderOptions(annotation_prefix="paper")
        ).splitlines()
        self.assertEqual(len(lines), 4, lines)
        self.assertTrue(
            lines[0].startswith(
                "::error title=TEX005 Missing source dependencies,file=paper/main.tex,line=4::"
            ),
            lines[0],
        )
        # The TeX error that restates the missing file is a notice that names its cause.
        self.assertTrue(lines[1].startswith("::notice title=BLD009 TeX errors in build log,"))
        self.assertIn("::Follows from TEX005 main.tex:4. error/failed: ", lines[1])
        self.assertTrue(
            lines[2].startswith("::notice title=TEX001 Unfinished text markers (suggestion),")
        )
        self.assertEqual(
            lines[3],
            "::notice title=Fledge check::Blocked: 1 problem to fix. Start with TEX005 "
            "(Missing source dependencies) at main.tex:4.",
        )

    def test_the_prefix_is_the_project_relative_to_the_repository_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "paper" / "chapters").mkdir(parents=True)
            (root / "paper.zip").write_bytes(b"PK")
            with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(root)}):
                self.assertEqual(annotation_prefix(root / "paper"), "paper")
                self.assertEqual(annotation_prefix(root / "paper" / "chapters"), "paper/chapters")
                self.assertEqual(annotation_prefix(root), "")
                # Paths inside a ZIP and outside the repository cannot be located in it.
                self.assertEqual(annotation_prefix(root / "paper.zip"), "")
                self.assertEqual(annotation_prefix(root.parent), "")


class CheckSelectionLineTests(unittest.TestCase):
    """The line that says what the check selection changed, and what it cannot change."""

    GUARDS = ("PKG001", "TEX005")

    def test_an_unchanged_selection_has_no_line(self) -> None:
        self.assertIsNone(selection_description(None))
        self.assertIsNone(selection_description(CheckSelectionRecord(("ALL",), (), self.GUARDS)))

    def test_checks_reported_as_warnings_are_named(self) -> None:
        line = selection_description(
            CheckSelectionRecord(("ALL",), (), self.GUARDS, ("MAN012", "MAN014"))
        )
        self.assertEqual(line, "Checks: reported as warnings instead of errors: MAN012, MAN014")

    def test_a_selector_that_covers_a_guard_says_that_the_guard_still_blocks(self) -> None:
        for selector in ("ALL", "TEX", "TEX005"):
            with self.subTest(selector=selector):
                line = selection_description(
                    CheckSelectionRecord(("ALL",), (), self.GUARDS, (selector,))
                )
                self.assertEqual(
                    line,
                    f"Checks: reported as warnings instead of errors: {selector}; "
                    "always-enforced guards still block",
                )

    def test_a_restricted_selection_ends_with_what_it_retains(self) -> None:
        restricted = CheckSelectionRecord(("MAN",), ("MAN001",), self.GUARDS)
        self.assertEqual(
            selection_description(restricted),
            "Checks: select MAN; ignore MAN001; required validation retained",
        )
        with_advisory = CheckSelectionRecord(("MAN",), (), self.GUARDS, ("MAN012",))
        self.assertEqual(
            selection_description(with_advisory),
            "Checks: select MAN; ignore (none); reported as warnings instead of errors: "
            "MAN012; required validation retained",
        )


class SettingsLineTests(unittest.TestCase):
    """The terminal and compact headers say which settings file and preset were used."""

    def test_the_settings_file_and_preset_or_the_defaults_are_named(self):
        cases = (
            (
                {"config_path": "~/paper/fledge.toml", "preset": "arxiv"},
                None,
                "Settings: ~/paper/fledge.toml + preset arxiv",
            ),
            (
                {"config_path": "~/paper/fledge.toml", "preset": None},
                "fledge.toml",
                "Settings: fledge.toml",
            ),
            ({"config_path": None, "preset": None}, None, "Settings: defaults (no settings file)"),
            (
                {"config_path": None, "preset": "camera-ready"},
                None,
                "Settings: preset camera-ready",
            ),
        )
        for execution, shown, expected in cases:
            with self.subTest(expected):
                report = Report("inspect", outcome=Outcome.PASSED, execution=execution)
                options = RenderOptions(settings_file=shown)
                self.assertIn(expected, render_terminal(report, options=options).splitlines())
                self.assertIn(
                    expected[:1].lower() + expected[1:],
                    render_compact(report, options=options).splitlines(),
                )

    def test_a_report_that_does_not_record_its_settings_has_no_settings_line(self):
        report = Report("pdf", outcome=Outcome.PASSED)
        self.assertNotIn("Settings:", render_terminal(report))
        self.assertNotIn("settings:", render_compact(report))

    def test_the_settings_file_is_named_from_the_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "a" / "b" / "c" / "d").mkdir(parents=True)
            with patch("latexprep.report_formats.Path.cwd", return_value=root / "a" / "b"):
                self.assertEqual(
                    working_directory_path(root / "a" / "b" / "fledge.toml"), "fledge.toml"
                )
                self.assertEqual(
                    working_directory_path(root / "a" / "b" / "c" / "fledge.toml"), "c/fledge.toml"
                )
                self.assertEqual(working_directory_path(root / "fledge.toml"), "../../fledge.toml")
            with patch(
                "latexprep.report_formats.Path.cwd", return_value=root / "a" / "b" / "c" / "d"
            ):
                far = root / "fledge.toml"
                self.assertEqual(working_directory_path(far), str(far))


class ReportFileReplacementTests(unittest.TestCase):
    """A report file is created only where none exists, or replaced whole when allowed.

    A replacement takes the place only of nothing or of the earlier report that was
    validated, and keeps that report's permissions.
    """

    def setUp(self) -> None:
        """Create a temporary folder ``root`` for ``report.json``, under the usual umask."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "report.json"
        self.addCleanup(os.umask, os.umask(0o022))

    def validated(self) -> ReportIdentity | None:
        """Validate ``report.json`` for replacement as ``--force`` does; return what it holds."""
        files = ReportFiles(replace_existing=True)
        files.remember_earlier_report(self.destination)
        return files.earlier.get(self.destination)

    def test_a_new_file_is_never_written_over_an_existing_one(self):
        write_report_text(self.destination, "first\n", replace_existing=False)
        with self.assertRaises(FileExistsError):
            write_report_text(self.destination, "second\n", replace_existing=False)
        self.assertEqual(self.destination.read_text(), "first\n")

    def test_a_failed_first_write_leaves_no_partial_report(self):
        def fail_after_writing(stream, data):
            stream.write(data[:5])
            raise OSError("disk full")

        with (
            patch("latexprep.file_safety._write_file_bytes", side_effect=fail_after_writing),
            self.assertRaises(OSError),
        ):
            write_report_text(self.destination, "complete report\n", replace_existing=False)
        self.assertEqual(list(self.root.iterdir()), [])
        write_report_text(self.destination, "complete report\n", replace_existing=False)
        self.assertEqual(self.destination.read_text(), "complete report\n")

    def test_a_report_failure_retains_the_internal_error_outcome(self):
        report = Report("check", outcome=Outcome.INTERNAL_ERROR)
        with patch("latexprep.report_files.write_report_text", side_effect=OSError("disk full")):
            public, rendered = save_reports(report, ReportFiles(json=self.destination), None)
        self.assertEqual(public.outcome, Outcome.INTERNAL_ERROR)
        self.assertEqual(json.loads(rendered.json)["outcome"], "internal_error")
        self.assertEqual([item.rule for item in public.findings], ["report.write"])

    def test_preview_artifacts_and_sidecar_errors_report_the_correct_release_state(self):
        check = Report(
            "check", outcome=Outcome.PASSED, artifacts={"preview.1": "/output/page-0001.png"}
        )
        self.assertTrue(render_compact(check).endswith("Previews were written."))
        self.assertNotIn("submission was written", render_compact(check))
        for name in ("archive", "supplement.archive"):
            report = Report(
                "prepare", outcome=Outcome.PASSED, artifacts={name: "/output/submission.zip"}
            )
            with patch(
                "latexprep.report_files.write_report_text", side_effect=OSError("disk full")
            ):
                public, _ = save_reports(report, ReportFiles(json=self.destination), None)
            self.assertEqual(public.outcome, Outcome.ERROR)
            self.assertTrue(
                render_compact(public).endswith(
                    "Could not write all requested reports. The submission was written."
                )
            )

    def test_an_allowed_replacement_happens_only_once_the_new_file_is_complete(self):
        earlier_report = Report("inspect").to_json()
        self.destination.write_text(earlier_report)
        earlier = self.validated()
        with (
            patch("latexprep.report_files.Path.replace", side_effect=OSError("full")),
            self.assertRaises(OSError),
        ):
            write_report_text(
                self.destination, "new report\n", replace_existing=True, earlier=earlier
            )
        # A failure leaves the earlier report and no temporary file behind.
        self.assertEqual(self.destination.read_text(), earlier_report)
        self.assertEqual(sorted(self.root.iterdir()), [self.destination])
        write_report_text(self.destination, "new report\n", replace_existing=True, earlier=earlier)
        self.assertEqual(self.destination.read_text(), "new report\n")
        self.assertEqual(sorted(self.root.iterdir()), [self.destination])

    def test_a_replaced_report_keeps_its_permissions_and_is_private_while_written(self):
        self.destination.write_text(Report("inspect").to_json())
        self.destination.chmod(0o640)
        created_modes = []
        open_file = os.open

        def recording_open(path, flags, *arguments, **options):
            """Open like ``os.open`` and record the mode of each file it creates."""
            if flags & os.O_CREAT:
                created_modes.append(arguments[0])
            return open_file(path, flags, *arguments, **options)

        earlier = self.validated()
        with patch("latexprep.file_safety.os.open", side_effect=recording_open):
            write_report_text(
                self.destination, "new report\n", replace_existing=True, earlier=earlier
            )
        self.assertEqual(created_modes, [0o600])
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o640)
        self.assertEqual(self.destination.read_text(), "new report\n")

    def test_a_destination_that_changed_is_left_and_the_new_report_kept_beside_it(self):
        other = self.root / "other.json"

        def write_a_file() -> None:
            """Write a file where none was when the destination was validated."""
            self.destination.write_text("theirs\n")

        def move_another_report_in() -> None:
            """Move another Fledge report into the place of the validated one."""
            other.replace(self.destination)

        def link_another_report() -> None:
            """Put a link to another Fledge report in the place of the validated one."""
            self.destination.unlink()
            self.destination.symlink_to(other)

        cases = (
            ("a file appeared", False, write_a_file),
            ("another report took the place", True, move_another_report_in),
            ("a link took the place", True, link_another_report),
        )
        for name, earlier_report, change in cases:
            with self.subTest(name):
                other.write_text(Report("check").to_json())
                if earlier_report:
                    self.destination.write_text(Report("inspect").to_json())
                earlier = self.validated()
                change()
                before = self.destination.lstat()
                with self.assertRaises(ChangedReportError) as raised:
                    write_report_text(
                        self.destination, "new report\n", replace_existing=True, earlier=earlier
                    )
                self.assertEqual(self.destination.lstat(), before)
                kept = raised.exception.kept
                self.assertEqual(kept.read_text(), "new report\n")
                self.assertIn(str(kept), raised.exception.strerror)
                kept.unlink()
                self.destination.unlink()

    def test_a_refused_replacement_is_recorded_with_where_the_new_report_is(self):
        files = ReportFiles(json=self.destination, replace_existing=True)
        # Nothing was at the destination when it was validated; a file appeared since.
        self.destination.write_text("theirs\n")
        report = Report("inspect", outcome=Outcome.PASSED)
        save_reports(report, files, None)
        self.assertEqual(self.destination.read_text(), "theirs\n")
        self.assertEqual(report.outcome, Outcome.ERROR)
        (failure,) = [item for item in report.findings if item.rule == "report.write"]
        (kept,) = self.root.glob(".fledge-file-*.tmp")
        self.assertIn(str(kept), failure.message)
        self.assertIn(str(kept), failure.suggestion or "")
        self.assertEqual(json.loads(kept.read_text())["command"], "inspect")


class SetupLocationTests(unittest.TestCase):
    """Setup diagnostics without a project do not say that their results are in one."""

    def report(self, project: str | None) -> Report:
        """Return a doctor report, run for ``project`` or without one."""
        return Report(
            "doctor",
            outcome=Outcome.PASSED,
            execution={"project": project},
            findings=[
                Finding("doctor.sandbox", "Restricted execution started.", status="passed"),
                Finding(
                    "doctor.fixture_passed",
                    "Fixture passed.",
                    status="passed",
                    details={"stage": "pdflatex: compilation"},
                ),
            ],
        )

    def test_where_names_no_project_when_none_was_given(self):
        options = RenderOptions(show_passed=True)
        text = render_terminal(self.report(None), options=options)
        self.assertNotIn("project", text)
        self.assertIn("Where: pdflatex: compilation", text)
        named = render_terminal(self.report("paper"), options=options)
        self.assertIn("Where: project [pdflatex: compilation]", named)


if __name__ == "__main__":
    unittest.main()
