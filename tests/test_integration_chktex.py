r"""Live chktex lint: the real chktex in the sandbox, whose warnings never change the exit code.

The tests need chktex, which MacTeX and a full TeX Live include and Debian and Ubuntu
package as ``chktex``. They run ``fledge inspect`` and ``fledge check`` as a user types
them, on a document whose fourth line lacks the ``~`` before ``\cite`` (chktex's warning 2)
and whose fifth line keeps the same space with a ``% chktex 2`` comment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner, Result

from latexprep.cli import cli
from latexprep.report import Outcome
from tests.live_support import LiveProjectTestCase, blocking_summary
from tests.support import live_test, write_project

_DOCUMENT = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "\\section{Introduction}\\label{sec:intro}\n"
    "Knuth describes the program in \\cite{knuth1984}.\n"
    "This sentence keeps its space on purpose \\cite{knuth1984}. % chktex 2\n"
    "\\bibliographystyle{plain}\n"
    "\\bibliography{references/library}\n"
    "\\end{document}\n"
)
_LIBRARY = (
    "@book{knuth1984,\n  author = {Donald E. Knuth},\n  title = {The {TeXbook}},\n"
    "  year = {1984},\n  publisher = {Addison-Wesley}\n}\n"
)
_SELECT_LINT = ("--select", "ALL", "--select", "CHK001")


def chktex_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the findings of the chktex rule in a JSON report."""
    return [item for item in report["findings"] if item["code"] == "CHK001"]


@live_test("Set FLEDGE_RUN_INTEGRATION=1 for the live chktex lint")
class LiveChktexLintTests(LiveProjectTestCase):
    """The real chktex reports a missing tie, honours its comments and project settings."""

    def setUp(self) -> None:
        """Skip unless chktex is installed."""
        super().setUp()
        self.require_tools("chktex")

    def project(self, name: str = "paper", configuration: str | None = None) -> Path:
        """Write the document, its bibliography and, when given, a ``.chktexrc``.

        Args:
            name: The project folder in the scratch directory.
            configuration: The text of the project's ``.chktexrc``, or ``None`` for none.

        Returns:
            The project folder.
        """
        files = {"main.tex": _DOCUMENT, "references/library.bib": _LIBRARY}
        if configuration is not None:
            files[".chktexrc"] = configuration
        return write_project(self.base / name, files)

    def run_json(
        self, command: str, project: Path, *arguments: str
    ) -> tuple[Result, dict[str, Any]]:
        """Run ``fledge COMMAND PROJECT ARGUMENTS --json --quiet`` and read its report.

        Also asserts that the command exited with the code of its outcome.

        Args:
            command: ``inspect`` or ``check``.
            project: The project folder.
            *arguments: Further arguments.

        Returns:
            The Click result and the parsed JSON report.
        """
        result = CliRunner().invoke(
            cli,
            [
                command,
                str(project),
                *arguments,
                "--json",
                "--quiet",
                "--job-timeout-seconds",
                "600",
            ],
        )
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail(f"No JSON report (exit code {result.exit_code}): {result.output}")
        self.assertEqual(
            result.exit_code, Outcome(report["outcome"]).exit_code, blocking_summary(report)
        )
        return result, report

    def test_a_missing_tie_is_a_suggestion_that_a_chktex_comment_silences(self) -> None:
        project = self.project()
        plain_result, plain = self.run_json("inspect", project)
        linted_result, linted = self.run_json("inspect", project, *_SELECT_LINT)
        found = chktex_findings(linted)
        self.assertEqual(
            [
                (item["status"], item["path"], item["line"], item["details"]["chktex_number"])
                for item in found
            ],
            [("failed", "main.tex", 4, 2)],
        )
        self.assertEqual(found[0]["severity"], "suggestion")
        self.assertEqual(found[0]["details"]["stage"], "chktex lint")
        self.assertEqual(chktex_findings(plain), [])
        self.assertEqual(linted["outcome"], plain["outcome"])
        self.assertEqual(linted_result.exit_code, plain_result.exit_code)
        self.assertIn("ChkTeX", " ".join(linted["tools"]["chktex"]["version"]))

    def test_a_project_configuration_applies_and_an_unsafe_one_is_refused(self) -> None:
        quiet = self.project("quiet", "# Ties are a house-style decision here.\nCmdLine { -n2 }\n")
        _, report = self.run_json("inspect", quiet, "--select", "CHK001")
        self.assertEqual([item["status"] for item in chktex_findings(report)], ["passed"])
        unsafe = self.project("unsafe", "CmdLine { -l /etc/hosts }\n")
        plain_result, _ = self.run_json("inspect", unsafe)
        refused_result, refused = self.run_json("inspect", unsafe, *_SELECT_LINT)
        self.assertEqual(
            [(item["status"], item["path"], item["line"]) for item in chktex_findings(refused)],
            [("skipped", ".chktexrc", 1)],
        )
        self.assertNotIn("chktex", refused["tools"])
        self.assertEqual(refused_result.exit_code, plain_result.exit_code)

    def test_check_runs_the_lint_beside_the_build(self) -> None:
        self.require_tools("latexmk", "pdflatex", "bibtex")
        _, report = self.run_json("check", self.project(), *_SELECT_LINT)
        self.assert_outcome(report)
        self.assertEqual(
            [(item["status"], item["line"]) for item in chktex_findings(report)], [("failed", 4)]
        )
