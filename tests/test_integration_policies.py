"""Opt-in live runs in which a configured policy or a preset finds a problem in a real build.

The other live tests assert passing outcomes. These build documents with real TeX and Poppler
that a misspelt citation, a page limit, configured identity terms or a missing generated
``.bbl`` keep from passing, mostly through the command line as a user runs it, and check the
exit codes, findings, reports and files that the documentation promises for them.

The command line has no option for ``source_date_epoch``: runs given ``--config`` pin it in that
file, while runs with ``--isolated`` or with the file ``fledge init`` writes use the current
time. No assertion here depends on the bytes of a PDF.
"""

from __future__ import annotations

import json
import re
import unittest
import zipfile
import zlib
from pathlib import Path
from typing import Any, NamedTuple

from click.testing import CliRunner, Result

from latexprep.cli import cli
from latexprep.job_context import JobRequest
from latexprep.report import Outcome
from tests.live_support import (
    LIVE_BUILD_EPOCH,
    PASSING_OUTCOMES,
    LiveProjectTestCase,
    blocking_summary,
    live_settings,
    run_job_sync,
)
from tests.support import directory_contents, live_test, pdf_bytes, pdf_stream, write_project

# The exit code of each outcome, as docs/reports.md "Exit codes" lists them.
EXIT_CODES = {
    "passed": 0,
    "passed_with_advisories": 1,
    "accepted_exceptions": 2,
    "blocked": 3,
    "error": 4,
    "internal_error": 70,
}

TWO_PAGES = r"""\documentclass{article}
\begin{document}
First page.
\newpage
Second page.
\end{document}
"""

CLEAN_ARTICLE = r"""\documentclass{article}
\usepackage{hyperref}
\hypersetup{pdftitle={A Camera-Ready Study}}
\title{A Camera-Ready Study}
\author{Ada Example}
\date{}
\begin{document}
\maketitle
\section{Results}
The measurements agree with the model.
\end{document}
"""

IDENTITY_TERM = "Rosa Newton"
# The author is named in \author (line 3) and again in the body (line 8).
NAMED_AUTHOR = r"""\documentclass{article}
\title{An Anonymous Study}
\author{Rosa Newton}
\date{}
\begin{document}
\maketitle
\section{Results}
As Rosa Newton showed earlier, the results hold.
\end{document}
"""

# A generated main.bbl in the shape BibTeX's plain style writes for the example's citation.
COVERING_BBL = r"""\begin{thebibliography}{1}

\bibitem{knuth1984}
Donald~E. Knuth.
\newblock {\em The {TeXbook}}.
\newblock Addison-Wesley, 1984.

\end{thebibliography}
"""
# The same shape for an entry the example does not cite, as a stale .bbl would hold.
STALE_BBL = r"""\begin{thebibliography}{1}

\bibitem{lamport1994}
Leslie Lamport.
\newblock {\em {\LaTeX}: A Document Preparation System}.
\newblock Addison-Wesley, 1994.

\end{thebibliography}
"""


class _QuickstartRun(NamedTuple):
    """The quick start's blocked ``prepare``: the command's result and the paths it was given.

    Attributes:
        result: The Click result of the command.
        output: The ``--output`` directory given to the command.
        report: The ``--report`` path given to the command.
        diagnostics: The ``--diagnostics`` path given to the command.
    """

    result: Result
    output: Path
    report: Path
    diagnostics: Path


def _invoke(command: str, *arguments: str) -> Result:
    """Run the job ``command`` in this process, as a user types it.

    The whole-run limit is given explicitly because other live builds may share the host; no
    test here asserts a duration.

    Args:
        command: The job command, such as ``check`` or ``prepare``.
        *arguments: The arguments of the command.

    Returns:
        The Click result of the command.
    """
    return CliRunner().invoke(cli, [command, *arguments, "--job-timeout-seconds", "600"])


def _findings(report: dict[str, Any], code: str) -> list[dict[str, Any]]:
    """List every finding of ``code`` in a JSON report, one per stage that ran the check."""
    return [item for item in report["findings"] if item["code"] == code]


def _matches(report: dict[str, Any], code: str) -> list[dict[str, Any]]:
    """List the matches that the findings of a scan with ``code`` record in ``details.matches``."""
    return [
        match for item in _findings(report, code) for match in item["details"].get("matches", ())
    ]


@live_test("Set FLEDGE_RUN_INTEGRATION=1 for live runs of failing policies and presets")
class LiveFailingPolicyTests(LiveProjectTestCase):
    """Real builds that a policy or preset blocks or passes only with advisories."""

    def settings_file(self, tables: str = "") -> Path:
        """Write a settings file outside the project that pins the PDF dates.

        Args:
            tables: Further settings text, written after the date.

        Returns:
            The path of the settings file.
        """
        path = self.base / "settings.toml"
        path.write_text(f"source_date_epoch = {LIVE_BUILD_EPOCH}\n{tables}", encoding="utf-8")
        return path

    def check_json(self, *arguments: str) -> dict[str, Any]:
        """Run ``fledge check ARGUMENTS --json --quiet`` and return its JSON report.

        Also asserts that the command exited with the code docs/reports.md gives its outcome,
        and fails the test when the command printed no JSON report.

        Args:
            *arguments: The arguments of ``fledge check``.

        Returns:
            The parsed JSON report.
        """
        result = _invoke("check", *arguments, "--json", "--quiet")
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail(f"No JSON report (exit code {result.exit_code}): {result.output}")
        self.assertEqual(result.exit_code, EXIT_CODES[report["outcome"]], blocking_summary(report))
        return report

    def assert_status(
        self, report: dict[str, Any], code: str, status: str, *, severity: str | None = None
    ) -> None:
        """Assert that ``code`` was reported, with ``status`` (and ``severity``) in every stage.

        Args:
            report: The JSON report.
            code: The finding code.
            status: The status every finding of ``code`` must have.
            severity: The severity every finding of ``code`` must have, or ``None`` to accept
                any.
        """
        found = _findings(report, code)
        self.assertTrue(found, f"{code} was not reported; problems: {blocking_summary(report)}")
        for item in found:
            self.assertEqual(item["status"], status, item)
            if severity is not None:
                self.assertEqual(item["severity"], severity, item)

    def prepare_misspelt_quickstart(self) -> _QuickstartRun:
        """Run the quick start's blocked run: ``prepare`` of the example citing ``knuth1948``.

        The run is isolated, prints the compact report and saves a JSON report and a
        diagnostics bundle in the base directory.

        Returns:
            The command's result and the paths it was given.
        """
        source = self.copy_nested_paper()
        results = source / "sections" / "results.tex"
        text = results.read_text(encoding="utf-8")
        self.assertIn(r"\cite{knuth1984}", text)
        results.write_text(text.replace(r"\cite{knuth1984}", r"\cite{knuth1948}"), encoding="utf-8")
        output = self.base / "submission"
        report = self.base / "report.json"
        diagnostics = self.base / "diagnostics.zip"
        result = _invoke(
            "prepare",
            str(source),
            "--main",
            "main.tex",
            "--output",
            str(output),
            "--output-format",
            "compact",
            "--quiet",
            "--report",
            str(report),
            "--diagnostics",
            str(diagnostics),
            "--isolated",
        )
        return _QuickstartRun(result, output, report, diagnostics)

    def assert_compact_blocked_report(self, lines: list[str]) -> None:
        """Assert that the compact report of the blocked run names its cause and what did not run.

        Args:
            lines: The lines of the compact report of the quick start's blocked run.
        """
        # skills/fledge/SKILL.md: the first line is `OUTCOME | counts | scope`.
        self.assertEqual(lines[0].split(" | ")[0], "blocked", lines)
        for prefix in ("BIB101 error/failed ", "BLD001 error/", "BLD002 error/", "BIB102 warning/"):
            self.assertTrue(any(line.startswith(prefix) for line in lines), (prefix, lines))
        not_run = [line for line in lines if line.startswith("not run:")]
        self.assertTrue(any("baseline build" in line for line in not_run), lines)

    def assert_unverified_bundle(self, path: Path) -> None:
        """Assert that a diagnostics ZIP holds one JSON report, labelled unverified, and no sources.

        The report is labelled unverified by an RPT103 finding.

        Args:
            path: The diagnostics ZIP.
        """
        self.assertTrue(zipfile.is_zipfile(path), path)
        with zipfile.ZipFile(path) as bundle:
            self.assertIsNone(bundle.testzip())
            names = bundle.namelist()
            documents = [json.loads(bundle.read(name)) for name in names if name.endswith(".json")]
        self.assertEqual([name for name in names if name.endswith((".tex", ".bib"))], [])
        reports = [item for item in documents if isinstance(item, dict) and "findings" in item]
        self.assertEqual(len(reports), 1, names)
        self.assertIn("RPT103", {item["code"] for item in reports[0]["findings"]})

    def test_quickstart_blocked_run_names_the_cause_and_publishes_no_bundle(self) -> None:
        """Check that the quick start's misspelt citation blocks prepare and publishes no bundle.

        docs/quickstart.md "3. Read the result", docs/reports.md and docs/workflow-reference.md
        "Report formats": the misspelt key blocks prepare with exit code 3. The compact report
        lists BIB101 as an error beside the BLD001 and BLD002 errors and the BIB102 warning it
        causes, and a ``not run:`` line names the failed baseline build. No output directory is
        written, while --report saves the blocked JSON report, whose BIB101 finding locates the
        key, and --diagnostics an unverified bundle without sources.
        """
        run = self.prepare_misspelt_quickstart()
        self.assertEqual(run.result.exit_code, 3, run.result.output)
        self.assert_compact_blocked_report(run.result.stdout.splitlines())
        self.assertFalse(run.output.exists())
        report = json.loads(run.report.read_text(encoding="utf-8"))
        self.assertEqual(report["command"], "prepare")
        self.assert_outcome(report, Outcome.BLOCKED)
        self.assertEqual(report["artifacts"], {})
        self.assert_status(report, "BIB101", "failed", severity="error")
        self.assertEqual(
            {
                (item["path"], item["line"], item["details"].get("key"))
                for item in _findings(report, "BIB101")
            },
            {("sections/results.tex", 5, "knuth1948")},
        )
        self.assert_unverified_bundle(run.diagnostics)

    def test_failed_baseline_build_carries_the_log_tail_when_latexmk_exits_zero(self) -> None:
        """Check that a failed baseline build carries the TeX log tail even when latexmk exits 0.

        docs/reports.md "The JSON report": a failed build's finding carries the last lines
        of the TeX log in details.log_tail. In the quick start's blocked run latexmk exits 0
        and the baseline build fails because the BLD001 and BLD002 log findings are errors.
        No build.failed finding exists to carry the tail, so the build.baseline finding does,
        and no other finding repeats it.
        """
        run = self.prepare_misspelt_quickstart()
        self.assertEqual(run.result.exit_code, 3, run.result.output)
        report = json.loads(run.report.read_text(encoding="utf-8"))
        baseline = [stage["status"] for stage in report["stages"] if stage["name"] == "baseline"]
        self.assertEqual(baseline, ["failed"], "the baseline build did not fail")
        rules = {item["rule"] for item in report["findings"]}
        self.assertNotIn("build.failed", rules, "latexmk exited with an error")
        failed = [item for item in report["findings"] if item["rule"] == "build.baseline"]
        self.assertEqual(
            [(item["severity"], item["status"]) for item in failed], [("error", "inconclusive")]
        )
        # The tail ends with the log's closing lines, written after the undefined citation.
        self.assertIn("Output written on", failed[0]["details"].get("log_tail", ""), failed[0])
        carrying = [item["rule"] for item in report["findings"] if "log_tail" in item["details"]]
        self.assertEqual(carrying, ["build.baseline"])

    def test_page_limit_blocks_preparation_and_keeps_the_passed_results(self) -> None:
        """Check that a failed page limit blocks ``prepare`` and the report keeps passed results.

        docs/checks.md PDF001 and docs/reports.md: a two-page PDF fails max_pages = 1, which
        blocks prepare, so no output directory is written; ``findings`` still records every
        result, including those that passed beside the failed page limit.
        """
        source = write_project(self.base / "paper", {"main.tex": TWO_PAGES})
        output = self.base / "submission"
        settings = live_settings(max_pages=1)
        report = run_job_sync(JobRequest("prepare", source, settings, output))
        problems = blocking_summary(report)
        self.assert_outcome(report, Outcome.BLOCKED)
        page_limit = [item for item in report.findings if item.code == "PDF001"]
        self.assertTrue(page_limit, problems)
        for item in page_limit:
            self.assertEqual((item.status, item.details.get("pages")), ("failed", 2), item)
        self.assertFalse(output.exists())
        self.assertEqual(report.artifacts, {})
        stages = {item.details.get("stage") for item in page_limit}
        beside = [
            item
            for item in report.findings
            if item.status == "passed" and item.details.get("stage") in stages
        ]
        self.assertTrue(beside, [(item.code or item.rule, item.status) for item in report.findings])

    def test_max_pages_option_blocks_check_and_is_recorded_in_the_settings(self) -> None:
        """Check that ``--max-pages`` blocks ``check`` and is recorded in the report's settings.

        docs/reports.md: --max-pages 1 on a two-page document blocks check with exit code 3,
        and the report's ``settings`` holds the effective value from the command line.
        """
        source = write_project(self.base / "paper", {"main.tex": TWO_PAGES})
        report = self.check_json(str(source), "--max-pages", "1", "--isolated")
        self.assert_outcome(report, Outcome.BLOCKED)
        self.assertEqual(report["settings"]["max_pages"], 1)
        self.assert_status(report, "PDF001", "failed", severity="error")

    def test_camera_ready_preset_passes_a_clean_article(self) -> None:
        """Check that the camera-ready preset passes a clean article with a title and an author.

        docs/configuration.md "Start from a preset": camera-ready switches on embedded fonts
        without Type 3, consistent page size, no encryption, JavaScript or attachments, title
        and author metadata, a PDF Title (PDF206) and the forbidden class options and packages
        (TEX102, TEX103); a clean article with a title, an author and a hyperref PDF Title
        passes each of them, and the report records the preset.
        """
        source = write_project(self.base / "paper", {"main.tex": CLEAN_ARTICLE})
        config = self.settings_file()
        report = self.check_json(
            str(source), "--main", "main.tex", "--preset", "camera-ready", "--config", str(config)
        )
        self.assert_outcome(report)
        self.assertEqual(report["execution"]["preset"], "camera-ready")
        for code in (
            "PDF102",
            "PDF104",
            "PDF105",
            "PDF106",
            "PDF108",
            "PDF109",
            "MAN008",
            "PDF206",
            "TEX102",
            "TEX103",
        ):
            with self.subTest(code=code):
                self.assert_status(report, code, "passed")

    def test_anonymous_review_preset_blocks_the_configured_identity_terms(self) -> None:
        """Check that the anonymous-review preset blocks a document naming an identity term.

        docs/configuration.md "Start from a preset" and docs/checks.md PRV001 to PRV005: with
        identity terms filled in, anonymous-review blocks a document that names its author, at
        each location in the source (PRV001) and in the extracted PDF text (PRV002), and runs
        its identity-hint, secret and private-comment scans (PRV003, PRV004, PRV005).
        """
        source = write_project(self.base / "paper", {"main.tex": NAMED_AUTHOR})
        config = self.settings_file(f'[submission_checks]\nidentity_terms = ["{IDENTITY_TERM}"]\n')
        report = self.check_json(
            str(source),
            "--main",
            "main.tex",
            "--preset",
            "anonymous-review",
            "--config",
            str(config),
        )
        self.assert_outcome(report, Outcome.BLOCKED)
        self.assertEqual(report["execution"]["preset"], "anonymous-review")
        self.assert_status(report, "PRV001", "failed")
        named = {
            ("main.tex", number)
            for number, line in enumerate(NAMED_AUTHOR.splitlines(), 1)
            if IDENTITY_TERM in line
        }
        located = {(match.get("path"), match.get("line")) for match in _matches(report, "PRV001")}
        self.assertEqual(located, named)
        self.assert_status(report, "PRV002", "failed")
        self.assertIn("text", {match.get("channel") for match in _matches(report, "PRV002")})
        for code in ("PRV003", "PRV004", "PRV005"):
            statuses = {item["status"] for item in _findings(report, code)}
            self.assertTrue(statuses, f"{code} did not run")
            self.assertLessEqual(statuses, {"passed", "failed"}, code)

    def test_arxiv_preset_checks_a_bbl_only_when_one_is_meant_for_arxiv(self) -> None:
        """Check that the arxiv preset needs no .bbl, and checks one that is meant to be uploaded.

        docs/configuration.md "Start from a preset" and docs/checks.md TEX009 and BIB109:
        arXiv runs BibTeX when no .bbl is uploaded, so with the arxiv preset a BibTeX project
        without a generated main.bbl gets only a not-applicable TEX009 note. A settings file
        that says arXiv should use an uploaded .bbl (venue_runs_bibliography_tools = false)
        makes the missing main.bbl a warning, and with check_bbl_coverage a main.bbl holding
        the cited key passes TEX009 and BIB109 while one that lacks it fails BIB109.
        """
        source = self.copy_nested_paper()
        arguments = (str(source), "--main", "main.tex", "--preset", "arxiv", "--config")
        with self.subTest(bbl="not needed"):
            report = self.check_json(*arguments, str(self.settings_file()))
            self.assert_status(report, "TEX009", "not_applicable", severity="info")
            self.assertEqual(_findings(report, "BIB109"), [])
            self.assert_status(report, "TEX013", "passed")
        uploaded = self.settings_file(
            "[submission_checks]\nvenue_runs_bibliography_tools = false\n"
            "check_bbl_coverage = true\n"
        )
        with self.subTest(bbl="missing"):
            report = self.check_json(*arguments, str(uploaded))
            self.assert_outcome(report, Outcome.PASSED_WITH_ADVISORIES)
            self.assert_status(report, "TEX009", "failed", severity="warning")
        with self.subTest(bbl="covering"):
            (source / "main.bbl").write_text(COVERING_BBL, encoding="utf-8")
            report = self.check_json(*arguments, str(uploaded))
            for code in ("TEX009", "BIB109"):
                self.assert_status(report, code, "passed")
        with self.subTest(bbl="stale"):
            (source / "main.bbl").write_text(STALE_BBL, encoding="utf-8")
            report = self.check_json(*arguments, str(uploaded))
            self.assert_status(report, "BIB109", "failed")

    def test_init_preset_file_is_discovered_until_a_run_is_isolated(self) -> None:
        """Check that the file ``fledge init`` writes is discovered until a run is isolated.

        docs/configuration.md "Start from a preset" and "Discovery and precedence": the
        fledge.toml that ``fledge init --preset arxiv`` writes is discovered by a later check
        without --preset (execution.config_path names it, execution.preset is null) and checks
        the engine against the ones arXiv offers (TEX013); --isolated skips discovery, so
        TEX013 is not reported.
        """
        source = self.copy_nested_paper()
        written = CliRunner().invoke(cli, ["init", str(source), "--preset", "arxiv"])
        self.assertEqual(written.exit_code, 0, written.output)
        self.assertTrue((source / "fledge.toml").is_file())
        discovered = self.check_json(str(source), "--main", "main.tex")
        config_path = discovered["execution"]["config_path"]
        self.assertTrue(str(config_path).endswith(f"/{source.name}/fledge.toml"), config_path)
        self.assertIsNone(discovered["execution"]["preset"])
        self.assert_status(discovered, "TEX013", "passed")
        isolated = self.check_json(str(source), "--main", "main.tex", "--isolated")
        self.assertIsNone(isolated["execution"]["config_path"])
        self.assertEqual(_findings(isolated, "TEX013"), [])


# A paper whose PDF figure names its author and a home folder in its information dictionary,
# which pdfTeX and LuaTeX copy into the built PDF unless told not to.
FIGURE_AUTHOR = "Jane Q. Secret"
FIGURE_HOME_FOLDER = "/Users/jsecret/"
FIGURE_PAPER = r"""\documentclass{article}
\usepackage{graphicx}
\begin{document}
Figure~\ref{fig:plot} shows the measurements.
\begin{figure}[ht]
\centering
\includegraphics[width=0.5\linewidth]{figures/plot}
\caption{The measurements.}\label{fig:plot}
\end{figure}
\end{document}
"""
_PDF_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)


def _named_figure() -> bytes:
    """Return a one-page vector figure, without fonts, whose metadata names its author."""
    info = (
        b"<< /Author ("
        + FIGURE_AUTHOR.encode()
        + b") /Title ("
        + FIGURE_HOME_FOLDER.encode()
        + b"Private/plot.pdf) /Creator (jsecret-laptop) >>"
    )
    return pdf_bytes(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 36] /Contents 4 0 R "
            b"/Resources << >> >>",
            4: pdf_stream(b"", b"0.2 0.4 0.8 rg 4 4 64 28 re f"),
            5: info,
        },
        b"<< /Root 1 0 R /Info 5 0 R >>",
    )


def _searchable(pdf: Path) -> bytes:
    """Return a PDF's bytes followed by every stream that inflates, for a plain search.

    The search does not use Fledge's PDF reader, whose findings the tests also check.
    """
    data = pdf.read_bytes()
    parts = [data]
    for match in _PDF_STREAM.finditer(data):
        try:
            parts.append(zlib.decompress(match[1]))
        except zlib.error:
            continue
    return b"\n".join(parts)


def _names_figure_author(data: bytes) -> bool:
    """Return whether ``data`` holds the figure's author or home folder, in either encoding."""
    return any(
        needle.encode(encoding) in data
        for needle in (FIGURE_AUTHOR, FIGURE_HOME_FOLDER)
        for encoding in ("latin-1", "utf-16-be")
    )


def _stage_statuses(report: dict[str, Any], code: str) -> dict[str, str]:
    """Return the status of each finding of ``code`` in a JSON report, by its stage."""
    return {str(item["details"].get("stage")): item["status"] for item in _findings(report, code)}


@live_test("Set FLEDGE_RUN_INTEGRATION=1 for live runs of figure-metadata privacy")
class LiveFigureMetadataPrivacyTests(LiveProjectTestCase):
    """Real pdfLaTeX and LuaLaTeX builds of a paper whose PDF figure names its author."""

    def paper(self, name: str) -> Path:
        """Write the paper and its figure as the project ``name`` in the scratch directory."""
        return write_project(
            self.base / name, {"main.tex": FIGURE_PAPER, "figures/plot.pdf": _named_figure()}
        )

    def prepare_with_preset(self, source: Path, engine: str) -> tuple[int, Path, dict[str, Any]]:
        """Run ``fledge prepare`` with the anonymous-review preset and the figure's author.

        Args:
            source: The project.
            engine: The TeX engine.

        Returns:
            The exit code, the output folder and the JSON report.
        """
        config = self.base / f"{engine}.toml"
        config.write_text(
            f"source_date_epoch = {LIVE_BUILD_EPOCH}\n"
            f'[submission_checks]\nidentity_terms = ["{FIGURE_AUTHOR}"]\n',
            encoding="utf-8",
        )
        output, saved = self.base / f"{engine}-output", self.base / f"{engine}.json"
        result = _invoke(
            "prepare",
            *(str(source), "--main", "main.tex", "--engine", engine),
            *("--preset", "anonymous-review", "--config", str(config)),
            *("--output", str(output), "--json-report", str(saved), "--quiet"),
        )
        self.assertTrue(saved.is_file(), (result.output, result.exception))
        return result.exit_code, output, json.loads(saved.read_text(encoding="utf-8"))

    def test_figure_metadata_reaches_the_prepared_pdf_without_the_setting(self) -> None:
        """Check that a PDF figure's author and home folder reach the PDF and are reported.

        docs/checks.md PRV104 and PRV105: without suppression, pdfTeX copies the figure's
        information dictionary into the built PDF; both checks warn, naming fields only.
        """
        report = self.prepare(self.paper("plain")).to_dict()
        published = _searchable(self.base / "prepared" / "manuscript.pdf")
        self.assertIn(b"/PTEX.InfoDict", published)
        self.assertTrue(_names_figure_author(published))
        for code in ("PRV104", "PRV105"):
            with self.subTest(code=code):
                self.assertIn("failed", _stage_statuses(report, code).values())
                self.assertNotIn("jsecret", json.dumps(_findings(report, code)))
        # prepare judges the PDF it publishes; its baseline is only information.
        severities = {
            item["details"].get("stage"): item["severity"] for item in _findings(report, "PRV105")
        }
        self.assertEqual(severities.get("final configured PDF checks"), "warning")
        self.assertEqual(severities.get("baseline configured PDF checks"), "info")
        self.assertEqual(_findings(report, "TEX204"), [])

    def test_anonymous_review_preset_keeps_figure_metadata_out_of_the_prepared_pdf(self) -> None:
        """Check that the preset's prepared PDF holds no /PTEX entries and no identity term.

        docs/checks.md TEX204: the anonymous-review preset adds the suppression line to the
        prepared main file only; pdfLaTeX and LuaLaTeX then omit the figure's information
        dictionary and file name, and the original project is unchanged.
        """
        for engine in ("pdflatex", "lualatex"):
            with self.subTest(engine=engine):
                source = self.paper(engine)
                before = directory_contents(source)
                code, output, report = self.prepare_with_preset(source, engine)
                self.assertIn(report["outcome"], {str(item) for item in PASSING_OUTCOMES})
                self.assertEqual(code, EXIT_CODES[report["outcome"]])
                published = _searchable(output / "manuscript.pdf")
                self.assertNotIn(b"PTEX.", published)
                self.assertFalse(_names_figure_author(published))
                self.assertEqual(directory_contents(source), before)
                self.assertEqual(set(_stage_statuses(report, "TEX204").values()), {"passed"})
                # The baseline is built from the unchanged sources and still carries the
                # figure's information, which is only noted; the published PDF is clean, so
                # PRV105 raises nothing and any advisory outcome has another cause.
                found = _stage_statuses(report, "PRV105")
                self.assertEqual(found.get("baseline configured PDF checks"), "failed")
                self.assertEqual(found.get("final configured PDF checks"), "passed")
                open_warnings = {
                    item["code"]
                    for item in report["findings"]
                    if item["severity"] == "warning" and item["status"] != "passed"
                }
                # Only the figure file itself, which preparation never rewrites, may still
                # warn; the added line must not leave the manuscript checks of the prepared
                # sources inconclusive.
                self.assertLessEqual(open_warnings, {"PRV104"})
                if report["outcome"] == "passed_with_advisories":
                    self.assertTrue(open_warnings)


if __name__ == "__main__":
    unittest.main()
