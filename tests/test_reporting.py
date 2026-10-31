"""Report rendering: the JSON, terminal and offline HTML reports."""

from __future__ import annotations

import unittest
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path

from latexprep.ci_annotations import render_ci_annotations
from latexprep.finding_view import RenderOptions, evidence_excerpt
from latexprep.findings import Finding
from latexprep.html_report import render_html
from latexprep.measurement_findings import measurement_finding
from latexprep.report import Change, ChangeKind, Outcome, Report
from latexprep.review_policy import (
    ReportingOptions,
    ReviewRule,
    apply_reviews,
)


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
        self.assertEqual(len(annotation.splitlines()), 1)
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
        self.assertEqual(len(annotations.splitlines()), 2)
        for hidden in ("Offline run", "No backend", "1 page"):
            self.assertNotIn(hidden, annotations)
        everything = render_ci_annotations(report, options=RenderOptions(show_passed=True))
        self.assertEqual(len(everything.splitlines()), 5)
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
        self.assertEqual(len(annotation.splitlines()), 1)
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
        self.assertEqual(len(annotations), 2)
        self.assertTrue(any(line.startswith("::error ") for line in annotations))
        self.assertTrue(any("Accepted exception" in line for line in annotations))
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


if __name__ == "__main__":
    unittest.main()
