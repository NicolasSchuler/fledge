"""Readable reports: one root cause stays visible instead of its restated consequences."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from latexprep.bibliography_checks import check_bibliography_details
from latexprep.build_checks import qualify_citation_findings
from latexprep.config import Settings
from latexprep.core import JobRequest, run_job
from latexprep.models import Finding, Report
from latexprep.presentation import render_compact, render_terminal
from latexprep.reporting import not_checked_areas, not_checked_summary
from latexprep.runtime import BuildResult
from latexprep.submission_checks import SubmissionOptions

from .support import write_project

BIB = "@book{knuth, author={Donald Knuth}, title={The TeXbook}, year={1984}, publisher={AW}}\n"
CITING = (
    "\\documentclass{article}\n\\begin{document}\nA \\cite{knuth}.\n"
    "\\bibliographystyle{plain}\n\\bibliography{refs}\n\\end{document}\n"
)


def blocked(task: str, document: str | None = None) -> Finding:
    details: dict[str, object] = {"task": task, "blocked_by": ["baseline build"]}
    if document:
        details["document"] = document
    return Finding(
        "execution.task",
        f"{task} was not run: Prerequisite failed: baseline build",
        "info",
        "skipped",
        details=details,
    )


def tex_error_build(work: Path) -> BuildResult:
    """A build that stopped on a TeX error before the bibliography backend ran."""
    work.mkdir(parents=True, exist_ok=True)
    return BuildResult(
        success=False,
        findings=[
            Finding("build.tex_error", "./main.tex:4: Undefined control sequence.", "error"),
            Finding(
                "build.undefined_citation",
                "LaTeX Warning: Citation `knuth' on page 1 undefined on input line 3.",
                "error",
                details={"document": "main.tex", "input_line": 3},
            ),
        ],
        command=["controlled-build"],
    )


class BlockedTaskSummaryTests(unittest.TestCase):
    def test_tasks_blocked_by_one_failure_render_as_one_line(self):
        names = ("baseline PDF inspection", "baseline configured PDF checks", "pdf fonts")
        report = Report("check", findings=[blocked(name) for name in names])
        expected = (
            "3 checks did not run because baseline build failed: "
            "baseline PDF inspection, baseline configured PDF checks, pdf fonts"
        )
        for show_passed in (False, True):
            terminal = render_terminal(report, show_passed=show_passed)
            self.assertEqual(terminal.count(expected), 1, terminal)
            self.assertNotIn("was not run", terminal)
            self.assertNotIn("execution.task", terminal)
        compact = render_compact(report)
        self.assertIn(f"not run: {expected}", compact)
        self.assertNotIn("was not run", compact)
        # Automation keeps one entry per task.
        rows = [row for row in report.to_dict()["findings"] if row["rule"] == "execution.task"]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["details"]["blocked_by"] == ["baseline build"] for row in rows))

    def test_documents_are_summarized_separately(self):
        report = Report(
            "check", findings=[blocked("pdf a", "one.tex"), blocked("pdf b", "two.tex")]
        )
        terminal = render_terminal(report)
        self.assertIn("one.tex: 1 check did not run because baseline build failed: pdf a", terminal)
        self.assertIn("two.tex: 1 check did not run because baseline build failed: pdf b", terminal)

    def test_a_failed_task_without_prerequisite_remains_a_finding(self):
        failure = Finding("execution.task", "pdf fonts: OSError: gone", "error", "inconclusive")
        terminal = render_terminal(Report("check", findings=[failure]))
        self.assertIn("pdf fonts: OSError: gone", terminal)
        self.assertNotIn("did not run because", terminal)

    def test_job_records_failed_prerequisites_for_blocked_tasks(self):
        async def failing_build(tree, main, work, engine, runner):
            return tex_error_build(work)

        with tempfile.TemporaryDirectory() as directory:
            source = write_project(Path(directory) / "paper", {"main.tex": CITING, "refs.bib": BIB})
            with patch("latexprep.core.build_project", side_effect=failing_build):
                report = asyncio.run(run_job(JobRequest("check", source, Settings())))
        tasks = [item for item in report.findings if item.rule == "execution.task"]
        self.assertTrue(tasks)
        self.assertTrue(all(item.details["blocked_by"] == ["baseline build"] for item in tasks))
        terminal = render_terminal(report)
        self.assertEqual(terminal.count("did not run because baseline build failed"), 1, terminal)
        # The citation is defined; a TeX error stopped the build before BibTeX ran.
        citation = [item for item in report.findings if item.code == "BLD001"]
        self.assertEqual(len(citation), 1)
        self.assertEqual((citation[0].severity, citation[0].status), ("warning", "inconclusive"))
        self.assertEqual(report.outcome, "blocked")


class DuplicateFindingTests(unittest.TestCase):
    def test_identical_recheck_at_a_later_stage_is_shown_once(self):
        marker = Finding(
            "source-edit-marker",
            "Possible unfinished editing or placeholder text: 'TODO'.",
            path="main.tex",
            line=4,
        )
        recheck = Finding(
            marker.rule,
            marker.message,
            path="main.tex",
            line=4,
            details={"stage": "final source checks"},
        )
        report = Report("prepare", findings=[marker, recheck])
        terminal = render_terminal(report)
        self.assertEqual(terminal.count("TEX001"), 1, terminal)
        self.assertNotIn("final source checks", terminal)
        self.assertIn("0 errors, 1 warning,", terminal)
        self.assertEqual(render_compact(report).count("TEX001"), 1)
        self.assertEqual(len(report.to_dict()["findings"]), 2)

    def test_a_changed_final_result_is_still_shown(self):
        initial = Finding("source-edit-marker", "Marker 'TODO'.", path="main.tex", line=4)
        changed = Finding(
            initial.rule,
            "Marker 'FIXME'.",
            path="main.tex",
            line=4,
            details={"stage": "final source checks"},
        )
        terminal = render_terminal(Report("prepare", findings=[initial, changed]))
        self.assertEqual(terminal.count("TEX001"), 2, terminal)
        self.assertIn("[final source checks]", terminal)

    def test_build_log_findings_list_every_stage_once(self):
        findings = [
            Finding(
                "build.duplicate_label",
                "LaTeX Warning: Label `sec:a' multiply defined.",
                details={"document": "main.tex", "stage": stage},
            )
            for stage in ("baseline", "transformed", "archive")
        ]
        terminal = render_terminal(Report("prepare", findings=findings))
        self.assertEqual(terminal.count("BLD003"), 1, terminal)
        self.assertIn("[baseline, transformed, archive]", terminal)

    def test_distinct_locations_stay_separate(self):
        findings = [
            Finding(
                "source-duplicate-label",
                "Label 'sec:a' is defined twice.",
                path="main.tex",
                line=line,
            )
            for line in (3, 5)
        ]
        terminal = render_terminal(Report("inspect", findings=findings))
        self.assertIn("main.tex:3", terminal)
        self.assertIn("main.tex:5", terminal)


class CitationAfterTexErrorTests(unittest.TestCase):
    def test_citations_after_a_tex_error_are_inconclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            build = tex_error_build(Path(directory))
        extra = Finding(
            "build.undefined_citation",
            "LaTeX Warning: Citation `lamport' on page 1 undefined on input line 5.",
            "error",
        )
        findings = qualify_citation_findings([*build.findings, extra], build_succeeded=False)
        citations = [item for item in findings if item.rule == "build.undefined_citation"]
        self.assertEqual(len(citations), 1)
        self.assertEqual((citations[0].severity, citations[0].status), ("warning", "inconclusive"))
        self.assertIn("'knuth', 'lamport'", citations[0].message)
        self.assertIn("BLD009", citations[0].message)
        self.assertEqual(citations[0].details["blocked_by"], "BLD009")
        self.assertEqual(citations[0].details["citation_keys"], ["knuth", "lamport"])
        self.assertTrue(any(item.rule == "build.tex_error" for item in findings))

    def test_undefined_references_after_a_tex_error_are_inconclusive(self):
        references = [
            Finding(
                "build.undefined_reference",
                f"LaTeX Warning: Reference `{key}' on page 1 undefined on input line {line}.",
                "error",
                details={"document": "main.tex", "input_line": line},
            )
            for key, line in (("sec:intro", 6), ("fig:plot", 7))
        ]
        summary = Finding(
            "build.undefined_reference", "LaTeX Warning: There were undefined references.", "error"
        )
        with tempfile.TemporaryDirectory() as directory:
            build = tex_error_build(Path(directory))
        findings = qualify_citation_findings(
            [*build.findings, *references, summary], build_succeeded=False
        )
        qualified = [item for item in findings if item.rule == "build.undefined_reference"]
        self.assertEqual(len(qualified), 1)
        self.assertEqual((qualified[0].severity, qualified[0].status), ("warning", "inconclusive"))
        self.assertIn("'sec:intro', 'fig:plot'", qualified[0].message)
        self.assertIn("BLD009", qualified[0].message)
        self.assertEqual(qualified[0].details["blocked_by"], "BLD009")
        self.assertEqual(qualified[0].details["reference_keys"], ["sec:intro", "fig:plot"])
        self.assertEqual(len(qualified[0].details["log_messages"]), 3)
        # Citations are qualified separately, and the TeX error stays visible.
        citations = [item for item in findings if item.rule == "build.undefined_citation"]
        self.assertEqual([item.status for item in citations], ["inconclusive"])
        self.assertTrue(any(item.rule == "build.tex_error" for item in findings))
        # Without a stopping TeX error, undefined references remain errors.
        self.assertEqual(
            qualify_citation_findings([*references, summary], build_succeeded=False),
            [*references, summary],
        )

    def test_undefined_citations_without_a_tex_error_still_fail(self):
        citation = Finding(
            "build.undefined_citation",
            "LaTeX Warning: Citation `missing' on page 1 undefined on input line 3.",
            "error",
        )
        for findings, succeeded in (([citation], False), ([citation], True)):
            self.assertEqual(
                qualify_citation_findings(findings, build_succeeded=succeeded), [citation]
            )
        error = Finding("build.tex_error", "./main.tex:4: Undefined control sequence.", "error")
        # A completed build's log is complete evidence even if it recorded an error.
        self.assertEqual(
            qualify_citation_findings([error, citation], build_succeeded=True), [error, citation]
        )


class BibliographyCascadeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.options = Settings().bibliography_checks

    def assert_one_note(self, findings: list[Finding], cause: str) -> None:
        incomplete = [
            item
            for item in findings
            if item.code in {"BIB101", "BIB102", "BIB105", "BIB106"}
            and item.status == "inconclusive"
        ]
        self.assertEqual(incomplete, [])
        notes = [item for item in findings if item.rule == "bibliography.source_error"]
        self.assertEqual(len(notes), 1, findings)
        self.assertEqual(notes[0].status, "inconclusive")
        self.assertIn(cause, notes[0].message)
        self.assertEqual(
            notes[0].details["inconclusive_checks"], ["BIB101", "BIB102", "BIB105", "BIB106"]
        )

    def test_missing_figure_yields_one_bibliography_note(self):
        main = CITING.replace("\\begin{document}\n", "\\begin{document}\n\\includegraphics{gone}\n")
        project = write_project(self.root / "paper", {"main.tex": main, "refs.bib": BIB})
        findings = check_bibliography_details(project, "main.tex", self.options)
        self.assert_one_note(findings, "Literal dependency 'gone' is missing")

    def test_ambiguous_main_yields_one_bibliography_note(self):
        project = write_project(
            self.root / "paper", {"a.tex": CITING, "b.tex": CITING, "refs.bib": BIB}
        )
        findings = check_bibliography_details(project, None, self.options)
        self.assert_one_note(findings, "A unique readable main source could not be selected")

    def test_other_uncertainty_keeps_individual_summaries(self):
        main = CITING.replace("\\cite{knuth}", "\\def\\mycite{\\cite}\\cite{knuth}").replace(
            "\\begin{document}\n", "\\begin{document}\n\\includegraphics{gone}\n"
        )
        project = write_project(self.root / "paper", {"main.tex": main, "refs.bib": BIB})
        findings = check_bibliography_details(project, "main.tex", self.options)
        self.assertFalse(any(item.rule == "bibliography.source_error" for item in findings))
        self.assertTrue(
            any(item.code == "BIB101" and item.status == "inconclusive" for item in findings)
        )

    def test_complete_project_is_unchanged(self):
        project = write_project(self.root / "paper", {"main.tex": CITING, "refs.bib": BIB})
        findings = check_bibliography_details(project, "main.tex", self.options)
        self.assertFalse(any(item.rule == "bibliography.source_error" for item in findings))
        self.assertTrue(any(item.code == "BIB101" and item.status == "passed" for item in findings))


class MainSelectionTests(unittest.TestCase):
    def test_ambiguous_roots_are_reported_once(self):
        with tempfile.TemporaryDirectory() as directory:
            source = write_project(
                Path(directory) / "paper", {"a.tex": CITING, "b.tex": CITING, "refs.bib": BIB}
            )
            report = asyncio.run(run_job(JobRequest("inspect", source, Settings())))
        roots = [
            item
            for item in report.findings
            if item.rule
            in {"project.main_selection", "source-root-ambiguous", "source-root-missing"}
        ]
        self.assertEqual([item.rule for item in roots], ["project.main_selection"])
        self.assertIn("a.tex, b.tex", roots[0].message)
        self.assertEqual(report.outcome, "blocked")


class NotCheckedFooterTests(unittest.TestCase):
    def test_default_settings_name_major_opt_in_areas(self):
        entries = not_checked_areas(Settings())
        areas = [entry["area"] for entry in entries]
        self.assertEqual(
            areas,
            [
                "anonymity",
                "page_limit",
                "archive_size",
                "forbidden_packages",
                "font_embedding",
                "image_resolution",
                "other_opt_in_checks",
            ],
        )
        self.assertEqual(entries[0]["settings"], ["submission_checks.identity_terms"])
        self.assertEqual(entries[1]["settings"], ["max_pages"])
        others = entries[-1]["codes"]
        assert isinstance(others, list)
        self.assertIn("TEX101", others)
        self.assertNotIn("PDF001", others)

    def test_configured_areas_are_omitted(self):
        settings = Settings(
            max_pages=8, submission_checks=SubmissionOptions(identity_terms=("Ada Lovelace",))
        )
        areas = [entry["area"] for entry in not_checked_areas(settings)]
        self.assertNotIn("page_limit", areas)
        self.assertNotIn("anonymity", areas)
        self.assertIn("image_resolution", areas)

    def test_deselected_check_is_listed_as_not_checked(self):
        from latexprep.check_policy import effective_settings
        from latexprep.check_selection import CheckSelection

        settings = effective_settings(
            Settings(max_pages=8, checks=CheckSelection(ignore=("PDF001",)))
        )
        self.assertIn("page_limit", [entry["area"] for entry in not_checked_areas(settings)])

    def test_terminal_footer_is_one_line_and_compact_omits_it(self):
        report = Report("check", outcome="passed")
        report.execution["not_checked"] = not_checked_areas(Settings())
        summary = not_checked_summary(report.execution["not_checked"])
        assert summary is not None
        self.assertTrue(
            summary.startswith(
                "Not checked (needs your settings): anonymity, page limit, ZIP size limit, "
                "forbidden packages, strict font embedding, image resolution, "
            )
        )
        self.assertIn("fledge init --preset arxiv|anonymous-review|camera-ready", summary)
        self.assertIn("configuration.html", summary)
        terminal = render_terminal(report)
        self.assertIn(summary, terminal.splitlines())
        self.assertNotIn("Not checked", render_compact(report))

    def test_footer_points_at_the_loaded_settings_file_instead_of_init(self):
        report = Report("check", outcome="passed")
        report.execution["not_checked"] = not_checked_areas(Settings())
        report.execution["config_path"] = "/work/paper/fledge.toml"
        summary = not_checked_summary(
            report.execution["not_checked"], report.execution["config_path"]
        )
        assert summary is not None
        self.assertIn("Add them to /work/paper/fledge.toml; see ", summary)
        self.assertNotIn("fledge init", summary)
        self.assertIn(summary, render_terminal(report).splitlines())
        for missing in (None, ""):
            with self.subTest(config_path=missing):
                fallback = not_checked_summary(report.execution["not_checked"], missing)
                assert fallback is not None
                self.assertIn("fledge init --preset", fallback)

    def test_job_reports_not_checked_areas(self):
        with tempfile.TemporaryDirectory() as directory:
            source = write_project(Path(directory) / "paper", {"main.tex": CITING, "refs.bib": BIB})
            report = asyncio.run(run_job(JobRequest("inspect", source, Settings(max_pages=4))))
        entries = report.to_dict()["execution"]["not_checked"]
        areas = [entry["area"] for entry in entries]
        self.assertIn("anonymity", areas)
        self.assertNotIn("page_limit", areas)
        self.assertIn("Not checked (needs your settings)", render_terminal(report))

    def test_reports_without_the_field_have_no_footer(self):
        self.assertNotIn("Not checked", render_terminal(Report("pdf", outcome="passed")))
        self.assertIsNone(not_checked_summary([]))
        self.assertIsNone(not_checked_summary(None))


if __name__ == "__main__":
    unittest.main()
