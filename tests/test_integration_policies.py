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

import asyncio
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any, NamedTuple

from click.testing import CliRunner, Result

from latexprep.cli import cli
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.job_context import JobRequest
from tests.support import live_test, write_project

EPOCH = 1_700_000_000
# The quick start's example: nested sources that cite knuth1984 from a BibTeX database.
NESTED_PAPER = Path(__file__).resolve().parents[1] / "examples" / "nested-paper"
# The exit code of each outcome, as docs/reports.md "Exit codes" lists them.
EXIT_CODES = {
    "passed": 0,
    "passed_with_advisories": 1,
    "accepted_exceptions": 2,
    "blocked": 3,
    "error": 4,
}

TWO_PAGES = r"""\documentclass{article}
\begin{document}
First page.
\newpage
Second page.
\end{document}
"""

CLEAN_ARTICLE = r"""\documentclass{article}
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
    """The quick start's blocked ``prepare``: the command's result and the paths it was given."""

    result: Result
    output: Path
    report: Path
    diagnostics: Path


def _invoke(command: str, *arguments: str) -> Result:
    """Run the job ``command`` in this process, as a user types it.

    The whole-run limit is given explicitly because other live builds may share the host; no
    test here asserts a duration.
    """
    return CliRunner().invoke(cli, [command, *arguments, "--job-timeout-seconds", "600"])


def _findings(report: dict[str, Any], code: str) -> list[dict[str, Any]]:
    """Every finding of ``code`` in a JSON report, one per stage that ran the check."""
    return [item for item in report["findings"] if item["code"] == code]


def _matches(report: dict[str, Any], code: str) -> list[dict[str, Any]]:
    """The matches that the findings of a scan with ``code`` record in ``details.matches``."""
    return [
        match for item in _findings(report, code) for match in item["details"].get("matches", ())
    ]


def _problems(report: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    """The unresolved errors and warnings of a JSON report, to explain a failed assertion."""
    return [
        (item["code"] or item["rule"], item["severity"], item["status"], item["message"])
        for item in report["findings"]
        if item["severity"] in {"error", "warning"} and item["status"] != "passed"
    ]


@live_test("Set FLEDGE_RUN_INTEGRATION=1 for live runs of failing policies and presets")
class LiveFailingPolicyTests(unittest.TestCase):
    """Real builds that a policy or preset blocks or passes only with advisories."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="fledge-policies-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def nested_paper(self) -> Path:
        """A private copy of the quick start's example project."""
        return Path(shutil.copytree(NESTED_PAPER, self.base / "paper"))

    def settings_file(self, tables: str = "") -> Path:
        """A settings file outside the project that pins the PDF dates, followed by ``tables``."""
        path = self.base / "settings.toml"
        path.write_text(f"source_date_epoch = {EPOCH}\n{tables}", encoding="utf-8")
        return path

    def check_json(self, *arguments: str) -> dict[str, Any]:
        """The JSON report of ``fledge check ARGUMENTS --json --quiet``.

        Also asserts that the command exited with the code docs/reports.md gives its outcome.
        """
        result = _invoke("check", *arguments, "--json", "--quiet")
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail(f"No JSON report (exit code {result.exit_code}): {result.output}")
        self.assertEqual(result.exit_code, EXIT_CODES[report["outcome"]], _problems(report))
        return report

    def assert_status(
        self, report: dict[str, Any], code: str, status: str, *, severity: str | None = None
    ) -> None:
        """``code`` was reported, with ``status`` (and ``severity``) in every stage."""
        found = _findings(report, code)
        self.assertTrue(found, f"{code} was not reported; problems: {_problems(report)}")
        for item in found:
            self.assertEqual(item["status"], status, item)
            if severity is not None:
                self.assertEqual(item["severity"], severity, item)

    def prepare_misspelt_quickstart(self) -> _QuickstartRun:
        """The quick start's blocked run: ``prepare`` of the example citing ``knuth1948``."""
        source = self.nested_paper()
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
        """The compact report of the blocked quick start names the cause and what did not run."""
        # skills/fledge/SKILL.md: the first line is `OUTCOME | counts | scope`.
        self.assertEqual(lines[0].split(" | ")[0], "blocked", lines)
        for prefix in ("BIB101 error/failed ", "BLD001 error/", "BLD002 error/", "BIB102 warning/"):
            self.assertTrue(any(line.startswith(prefix) for line in lines), (prefix, lines))
        not_run = [line for line in lines if line.startswith("not run:")]
        self.assertTrue(any("baseline build" in line for line in not_run), lines)

    def assert_unverified_bundle(self, path: Path) -> None:
        """A diagnostics ZIP holds one JSON report, labelled unverified (RPT103), and no sources."""
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
        """docs/quickstart.md "3. Read the result", docs/reports.md and docs/workflow-reference.md
        "Report formats": the misspelt key blocks prepare with exit code 3. The compact report
        lists BIB101 as an error beside the BLD001 and BLD002 errors and the BIB102 warning it
        causes, and a `not run:` line names the failed baseline build. No output directory is
        written, while --report saves the blocked JSON report, whose BIB101 finding locates the
        key, and --diagnostics an unverified bundle without sources.
        """
        run = self.prepare_misspelt_quickstart()
        self.assertEqual(run.result.exit_code, 3, run.result.output)
        self.assert_compact_blocked_report(run.result.stdout.splitlines())
        self.assertFalse(run.output.exists())
        report = json.loads(run.report.read_text(encoding="utf-8"))
        self.assertEqual((report["command"], report["outcome"]), ("prepare", "blocked"))
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
        """docs/reports.md "The JSON report": a failed build's finding carries the last lines
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
        """docs/checks.md PDF001 and docs/reports.md: a two-page PDF fails max_pages = 1, which
        blocks prepare, so no output directory is written; `findings` still records every
        result, including those that passed beside the failed page limit.
        """
        source = write_project(self.base / "paper", {"main.tex": TWO_PAGES})
        output = self.base / "submission"
        settings = Settings(
            main="main.tex", max_pages=1, source_date_epoch=EPOCH, job_timeout_seconds=600
        )
        report = asyncio.run(run_job(JobRequest("prepare", source, settings, output)))
        problems = _problems(report.to_dict())
        self.assertEqual(report.outcome, "blocked", problems)
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
        """docs/reports.md: --max-pages 1 on a two-page document blocks check with exit code 3,
        and the report's `settings` holds the effective value from the command line.
        """
        source = write_project(self.base / "paper", {"main.tex": TWO_PAGES})
        report = self.check_json(str(source), "--max-pages", "1", "--isolated")
        self.assertEqual(report["outcome"], "blocked", _problems(report))
        self.assertEqual(report["settings"]["max_pages"], 1)
        self.assert_status(report, "PDF001", "failed", severity="error")

    def test_camera_ready_preset_passes_a_clean_article(self) -> None:
        """docs/configuration.md "Start from a preset": camera-ready switches on embedded fonts
        without Type 3, consistent page size, no encryption, JavaScript or attachments, title
        and author metadata and the forbidden class options and packages (TEX102, TEX103); a
        clean article with a title and an author passes each of them, and the report records
        the preset.
        """
        source = write_project(self.base / "paper", {"main.tex": CLEAN_ARTICLE})
        config = self.settings_file()
        report = self.check_json(
            str(source), "--main", "main.tex", "--preset", "camera-ready", "--config", str(config)
        )
        self.assertIn(report["outcome"], {"passed", "passed_with_advisories"}, _problems(report))
        self.assertEqual(report["execution"]["preset"], "camera-ready")
        for code in (
            "PDF102",
            "PDF104",
            "PDF105",
            "PDF106",
            "PDF108",
            "PDF109",
            "MAN008",
            "TEX102",
            "TEX103",
        ):
            with self.subTest(code=code):
                self.assert_status(report, code, "passed")

    def test_anonymous_review_preset_blocks_the_configured_identity_terms(self) -> None:
        """docs/configuration.md "Start from a preset" and docs/checks.md PRV001 to PRV005: with
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
        self.assertEqual(report["outcome"], "blocked", _problems(report))
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

    def test_arxiv_preset_requires_a_shipped_bbl_that_covers_every_citation(self) -> None:
        """docs/configuration.md "Start from a preset" and docs/checks.md TEX009 and BIB109: with
        the arxiv preset a BibTeX project without a generated main.bbl passes only with
        advisories (TEX009 is a warning, exit code 1); a main.bbl holding the cited key passes
        TEX009 and BIB109, and one that lacks it fails BIB109.
        """
        source = self.nested_paper()
        config = self.settings_file()
        arguments = (
            str(source),
            "--main",
            "main.tex",
            "--preset",
            "arxiv",
            "--config",
            str(config),
        )
        with self.subTest(bbl="missing"):
            report = self.check_json(*arguments)
            self.assertEqual(report["outcome"], "passed_with_advisories", _problems(report))
            self.assert_status(report, "TEX009", "failed", severity="warning")
        with self.subTest(bbl="covering"):
            (source / "main.bbl").write_text(COVERING_BBL, encoding="utf-8")
            report = self.check_json(*arguments)
            for code in ("TEX009", "BIB109"):
                self.assert_status(report, code, "passed")
        with self.subTest(bbl="stale"):
            (source / "main.bbl").write_text(STALE_BBL, encoding="utf-8")
            report = self.check_json(*arguments)
            self.assert_status(report, "BIB109", "failed")

    def test_init_preset_file_is_discovered_until_a_run_is_isolated(self) -> None:
        """docs/configuration.md "Start from a preset" and "Discovery and precedence": the
        fledge.toml that `fledge init --preset arxiv` writes is discovered by a later check
        without --preset (execution.config_path names it, execution.preset is null) and makes
        TEX009 a warning; --isolated skips discovery, so TEX009 is again a not-applicable note.
        """
        source = self.nested_paper()
        written = CliRunner().invoke(cli, ["init", str(source), "--preset", "arxiv"])
        self.assertEqual(written.exit_code, 0, written.output)
        self.assertTrue((source / "fledge.toml").is_file())
        discovered = self.check_json(str(source), "--main", "main.tex")
        config_path = discovered["execution"]["config_path"]
        self.assertTrue(str(config_path).endswith(f"/{source.name}/fledge.toml"), config_path)
        self.assertIsNone(discovered["execution"]["preset"])
        self.assert_status(discovered, "TEX009", "failed", severity="warning")
        isolated = self.check_json(str(source), "--main", "main.tex", "--isolated")
        self.assertIsNone(isolated["execution"]["config_path"])
        self.assert_status(isolated, "TEX009", "not_applicable")
        self.assertNotIn("warning", {item["severity"] for item in _findings(isolated, "TEX009")})


if __name__ == "__main__":
    unittest.main()
