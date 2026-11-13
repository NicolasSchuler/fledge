r"""The optional chktex lint: its runs, its findings, its scheduling and its sandbox grant.

The lint runs on a ``FakeCommandRunner`` whose chktex answers in the format the lint asks
for, so these tests need no TeX; ``tests/test_integration_chktex.py`` runs the real chktex.
The configuration reader and the output parser have tests of their own in
``tests/test_chktex_configuration.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from latexprep.check_selection import CheckSelection
from latexprep.chktex_lint import (
    CHKTEX_MEMORY_MB,
    MAX_LISTED_WARNINGS,
    lint_with_chktex,
)
from latexprep.command_runner import ToolVersion
from latexprep.config import Settings
from latexprep.document_tasks import TaskName, source_analysis_tasks
from latexprep.errors import SandboxRefusedError
from latexprep.findings import Finding
from latexprep.job_context import JobContext, JobRequest
from latexprep.report import Report
from latexprep.resource_budget import ResourceBudget
from latexprep.runtime import ToolRunner
from latexprep.sandbox import BubblewrapSandbox, SeatbeltSandbox
from latexprep.scheduler import TaskResult, TaskStatus
from latexprep.task_reporting import TaskReporter
from latexprep.tool_launch import create_tool_environment
from latexprep.toolchain import Toolchain, install_hint
from tests.fakes import FakeCommandRunner, Reply, ToolCall
from tests.support import AsyncProjectTestCase, ProjectTestCase

_MAIN = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "Knuth describes the program in \\cite{knuth}.\n"
    "\\input{sections/method}\n"
    "\\end{document}\n"
)
_SECTION = "The method follows \\cite{knuth}.\nIt needs nothing else.\n"
_CLEAN = "\\documentclass{article}\n\\begin{document}\nNothing to say.\n\\end{document}\n"
_TIE_MESSAGE = "Non-breaking space (`~') should have been used."


def run_token(call: ToolCall) -> str:
    """Return the token that starts the ``-f`` format of a chktex command."""
    return call.argv[call.argv.index("-f") + 1].split("|", 1)[0]


def chktex_output(call: ToolCall, warnings: list[tuple[int, int, int, str]]) -> str:
    """Write chktex's output for ``warnings`` in the format of ``call``.

    Args:
        call: The chktex command, whose ``-f`` format names the token.
        warnings: The number, line, column and message of each warning.

    Returns:
        One warning line for each warning.
    """
    token = run_token(call)
    return "".join(
        f"{token}|{number}|{line}|{column}|Warning|{message}\n"
        for number, line, column, message in warnings
    )


def ties_before_citations(call: ToolCall) -> Reply:
    r"""Answer as chktex does for warning 2: each line of the copy with a space before \cite.

    Args:
        call: The chktex command, whose last argument is the frozen copy it checks.

    Returns:
        The warnings, with chktex's status for them.
    """
    lines = Path(call.argv[-1]).read_text(encoding="utf-8").splitlines()
    warnings = [
        (2, number, line.index(" \\cite") + 1, _TIE_MESSAGE)
        for number, line in enumerate(lines, start=1)
        if " \\cite" in line
    ]
    return Reply(chktex_output(call, warnings), returncode=2 if warnings else 0)


def chktex_finding_rows(findings: list[Finding]) -> list[tuple[object, ...]]:
    """Return the code, severity, status, file and line of each finding."""
    return [(item.code, item.severity, item.status, item.path, item.line) for item in findings]


class ChktexLintTests(AsyncProjectTestCase):
    """chktex runs once per file on frozen copies, and every result is a suggestion."""

    async def lint(
        self,
        files: dict[str, str | bytes],
        runner: FakeCommandRunner | ToolRunner | None = None,
        main: str = "main.tex",
    ) -> tuple[list[Finding], FakeCommandRunner | ToolRunner]:
        """Write ``files`` as a project and lint it.

        Args:
            files: The project's files by relative path.
            runner: The runner; a fake whose chktex reports spaces before citations by
                default.
            main: The main file.

        Returns:
            The findings and the runner.
        """
        project = self.write_project(files)
        used = runner or FakeCommandRunner({"chktex": ties_before_citations})
        return await lint_with_chktex(project, main, self.root / "work", used), used

    def runs(self, runner: FakeCommandRunner) -> list[ToolCall]:
        """Return the chktex runs of ``runner``, without its version probe."""
        return [call for call in runner.calls if not call.is_version_probe]

    async def test_each_warning_becomes_a_suggestion_at_its_file_and_line(self) -> None:
        findings, runner = await self.lint({"main.tex": _MAIN, "sections/method.tex": _SECTION})
        assert isinstance(runner, FakeCommandRunner)
        self.assertEqual(
            chktex_finding_rows(findings),
            [
                ("CHK001", "suggestion", "failed", "main.tex", 3),
                ("CHK001", "suggestion", "failed", "sections/method.tex", 1),
            ],
        )
        first = findings[0]
        self.assertEqual(first.details["chktex_number"], 2)
        self.assertEqual(first.details["chktex_kind"], "Warning")
        self.assertEqual(first.details["chktex_message"], _TIE_MESSAGE)
        self.assertEqual(first.details["column"], 31)
        self.assertEqual(first.message, f"{_TIE_MESSAGE} (chktex warning 2)")
        self.assertIn("% chktex 2", first.suggestion or "")
        self.assertEqual(runner.probes, ["chktex"])
        runs = self.runs(runner)
        self.assertEqual([Path(call.argv[-1]).name for call in runs], ["main.tex", "method.tex"])
        project = self.root / "project"
        for call in runs:
            copy = Path(call.argv[-1])
            self.assertEqual(call.argv[:5], ["chktex", "-q", "-v0", "-I0", "-f"])
            self.assertEqual(call.argv[-2], "--")
            self.assertEqual(call.readonly_inputs, (copy,))
            self.assertFalse(copy.is_relative_to(project))
            self.assertFalse(copy.is_relative_to(call.workspace))
            self.assertEqual([item.name for item in call.workspace.iterdir()], [])

    async def test_a_document_without_warnings_passes(self) -> None:
        findings, _ = await self.lint({"main.tex": _CLEAN})
        self.assertEqual(
            chktex_finding_rows(findings), [("CHK001", "suggestion", "passed", None, None)]
        )
        self.assertEqual(findings[0].details["checked_files"], 1)

    async def test_a_missing_chktex_is_reported_as_not_checked(self) -> None:
        runner = ToolRunner(source_date_epoch=0, toolchain=Toolchain(frozenset(), frozenset(), {}))
        with patch("latexprep.toolchain.shutil.which", return_value=None):
            findings, _ = await self.lint({"main.tex": _MAIN}, runner)
        self.assertEqual(
            chktex_finding_rows(findings), [("CHK001", "suggestion", "skipped", None, None)]
        )
        missing = findings[0]
        self.assertTrue(missing.incomplete)
        self.assertIn("chktex is not installed", missing.message)
        self.assertIn("sudo tlmgr install chktex", missing.suggestion or "")
        self.assertIn("sudo apt install chktex", missing.suggestion or "")

    async def test_an_unsafe_project_configuration_is_refused_before_chktex_runs(self) -> None:
        findings, runner = await self.lint(
            {
                "main.tex": _MAIN,
                ".chktexrc": "# Personal settings\nCmdLine { -n1 -l /etc/passwd }\n",
            }
        )
        assert isinstance(runner, FakeCommandRunner)
        self.assertEqual(
            chktex_finding_rows(findings),
            [("CHK001", "suggestion", "skipped", ".chktexrc", 2)],
        )
        self.assertIn("-l", findings[0].message)
        self.assertIn("outside the project", findings[0].message)
        self.assertIn("Remove it from CmdLine", findings[0].suggestion or "")
        self.assertEqual(runner.calls, [])

    async def test_warnings_beyond_the_bound_are_counted(self) -> None:
        extra = 7

        def many(call: ToolCall) -> Reply:
            """Report more warnings than a report lists."""
            warnings = [(1, line, 1, "Command terminated with space.") for line in range(1, 208)]
            return Reply(chktex_output(call, warnings[: MAX_LISTED_WARNINGS + extra]), returncode=2)

        findings, _ = await self.lint({"main.tex": _CLEAN}, FakeCommandRunner({"chktex": many}))
        listed = [item for item in findings if item.path == "main.tex"]
        self.assertEqual(len(listed), MAX_LISTED_WARNINGS)
        self.assertEqual([item.line for item in listed[:3]], [1, 2, 3])
        summary = findings[-1]
        self.assertEqual((summary.code, summary.status, summary.path), ("CHK001", "failed", None))
        self.assertTrue(summary.incomplete)
        self.assertEqual(summary.details["unlisted_warnings"], extra)
        self.assertIn(f"{extra} more warnings", summary.message)

    async def test_an_accepted_configuration_is_what_chktex_reads_in_its_folder(self) -> None:
        configuration = "CmdLine { -n8 -wall }\nTeXInputs { sections }\n"
        seen: list[str | None] = []

        class ObservingRunner(FakeCommandRunner):
            """A runner that notes the configuration in each command's working folder."""

            async def tool_version(self, argv: list[str], workspace: Path) -> ToolVersion:
                """Note the probe's folder, then probe as the fake does."""
                seen.append(_configuration_in(workspace))
                return await super().tool_version(argv, workspace)

        def observe(call: ToolCall) -> Reply:
            """Note the configuration beside the run and report nothing."""
            seen.append(_configuration_in(call.workspace))
            return Reply()

        findings, _ = await self.lint(
            {"main.tex": _CLEAN, ".chktexrc": configuration},
            ObservingRunner({"chktex": observe}),
        )
        self.assertEqual(seen, [None, configuration])
        self.assertEqual([item.status for item in findings], ["passed"])

    async def test_the_configuration_beside_a_nested_main_file_applies(self) -> None:
        findings, runner = await self.lint(
            {
                "paper/main.tex": _CLEAN,
                "paper/.chktexrc": "CmdLine { -o report.txt }\n",
                ".chktexrc": "CmdLine { -n2 }\n",
            },
            main="paper/main.tex",
        )
        assert isinstance(runner, FakeCommandRunner)
        self.assertEqual(
            chktex_finding_rows(findings),
            [("CHK001", "suggestion", "skipped", "paper/.chktexrc", 1)],
        )
        self.assertIn("writes chktex's report to a file", findings[0].message)
        self.assertEqual(runner.calls, [])

    async def test_a_run_stopped_by_a_limit_ends_the_lint(self) -> None:
        runner = FakeCommandRunner({"chktex": ties_before_citations})
        runner.failure = "timeout"
        findings, _ = await self.lint({"main.tex": _MAIN, "sections/method.tex": _SECTION}, runner)
        self.assertEqual(len(self.runs(runner)), 1)
        incomplete = findings[-1]
        self.assertEqual((incomplete.status, incomplete.severity), ("inconclusive", "suggestion"))
        self.assertIn("chktex did not complete for main.tex", incomplete.message)
        self.assertIn("ran too long", incomplete.message)
        self.assertIn("1 file was not checked", incomplete.message)
        self.assertEqual(incomplete.details["unchecked_files"], ["sections/method.tex"])
        self.assertNotIn("passed", [item.status for item in findings])

    async def test_messages_on_standard_error_and_unreadable_lines_leave_it_incomplete(
        self,
    ) -> None:
        def noisy(call: ToolCall) -> Reply:
            """Report one warning among lines that are not warnings, and a complaint."""
            warning = chktex_output(call, [(2, 3, 31, _TIE_MESSAGE)])
            forged = "0000|2|1|1|Warning|A line another token marks\n"
            return Reply(
                warning + "not a warning\n" + forged,
                stderr=f"chktex: WARNING -- Unable to open the TeX file `{call.argv[-1]}'.\n",
                returncode=2,
            )

        findings, _ = await self.lint({"main.tex": _MAIN}, FakeCommandRunner({"chktex": noisy}))
        self.assertEqual(
            chktex_finding_rows(findings),
            [
                ("CHK001", "suggestion", "failed", "main.tex", 3),
                ("CHK001", "suggestion", "inconclusive", None, None),
            ],
        )
        incomplete = findings[1]
        self.assertIn("Unable to open the TeX file `main.tex'", incomplete.message)
        self.assertNotIn(str(self.root / "work"), incomplete.message)
        self.assertIn("2 lines of chktex's output could not be read", incomplete.message)
        self.assertEqual(incomplete.details["unreadable_lines"], 2)

    async def test_an_unexpected_exit_status_leaves_the_lint_incomplete(self) -> None:
        failing = Reply(stderr="chktex: ERROR -- Illegal verbosity level.\n", returncode=1)
        findings, _ = await self.lint({"main.tex": _CLEAN}, FakeCommandRunner({"chktex": failing}))
        self.assertEqual([item.status for item in findings], ["inconclusive"])
        self.assertIn("Illegal verbosity level", findings[0].message)

    async def test_only_the_manuscript_files_that_the_main_file_uses_are_checked(self) -> None:
        main = (
            "\\documentclass{article}\n\\usepackage{localstyle}\n\\begin{document}\n"
            "\\input{part}\n\\end{document}\n"
        )
        findings, runner = await self.lint(
            {
                "main.tex": main,
                "part.tex": "A part.\n",
                "localstyle.sty": "\\ProvidesPackage{localstyle}\n",
                "unused.tex": "Never read \\cite{x}.\n",
            }
        )
        assert isinstance(runner, FakeCommandRunner)
        self.assertEqual(
            [Path(call.argv[-1]).name for call in self.runs(runner)], ["main.tex", "part.tex"]
        )
        self.assertEqual([item.status for item in findings], ["passed"])

    async def test_files_beyond_the_file_limit_are_counted_as_not_checked(self) -> None:
        with patch("latexprep.chktex_lint.MAX_CHECKED_FILES", 1):
            findings, runner = await self.lint({"main.tex": _MAIN, "sections/method.tex": _SECTION})
        assert isinstance(runner, FakeCommandRunner)
        self.assertEqual(len(self.runs(runner)), 1)
        self.assertEqual(findings[0].path, "main.tex")
        self.assertEqual(findings[-1].status, "inconclusive")
        self.assertEqual(findings[-1].details["unchecked_file_count"], 1)

    async def test_a_sandbox_refusal_ends_the_lint(self) -> None:
        def refused(call: ToolCall) -> Reply:
            """Refuse every command, as a sandbox without isolation does."""
            raise SandboxRefusedError("Restricted execution was refused")

        findings, runner = await self.lint(
            {"main.tex": _MAIN, "sections/method.tex": _SECTION},
            FakeCommandRunner({"chktex": refused}),
        )
        assert isinstance(runner, FakeCommandRunner)
        self.assertEqual(len(self.runs(runner)), 1)
        self.assertEqual([item.status for item in findings], ["inconclusive"])
        self.assertIn("Restricted execution was refused", findings[0].message)

    async def test_without_a_readable_main_file_chktex_is_not_run(self) -> None:
        findings, runner = await self.lint({"main.tex": _CLEAN}, main="missing.tex")
        assert isinstance(runner, FakeCommandRunner)
        self.assertEqual([item.status for item in findings], ["skipped"])
        self.assertEqual(runner.calls, [])


def _configuration_in(folder: Path) -> str | None:
    """Return the text of the ``.chktexrc`` in ``folder``, or ``None`` when there is none."""
    path = folder / ".chktexrc"
    return path.read_text(encoding="utf-8") if path.exists() else None


class ChktexSchedulingTests(AsyncProjectTestCase):
    """The lint is a task of inspect, check and prepare only when its suggestion is selected."""

    def context(self, *select: str, ignore: tuple[str, ...] = ()) -> JobContext:
        """Build a job context of an ``inspect`` job with the selection ``select``.

        Args:
            *select: The selectors of the job.
            ignore: The ignored selectors.

        Returns:
            The context, whose runner is a fake that reports spaces before citations.
        """
        settings = Settings(checks=CheckSelection(select=select, ignore=ignore))
        project = self.root / "imported"
        work = self.root / "work"
        work.mkdir(exist_ok=True)
        return JobContext(
            JobRequest("inspect", project, settings),
            settings,
            work,
            FakeCommandRunner({"chktex": ties_before_citations}),
            ResourceBudget(2, 2048),
            lambda _message: None,
        )

    def task_names(self, context: JobContext, main: str | None = "main.tex") -> list[str]:
        """Return the names of the source analysis tasks of ``context``."""
        return [task.name for task in source_analysis_tasks(context, lambda: self.root, main)]

    async def test_the_lint_is_scheduled_only_when_its_suggestion_is_selected(self) -> None:
        lint = TaskName.CHKTEX_LINT.value
        self.assertNotIn(lint, self.task_names(self.context("ALL")))
        self.assertIn(lint, self.task_names(self.context("ALL", "CHK001")))
        self.assertIn(lint, self.task_names(self.context("CHK")))
        self.assertIn(lint, self.task_names(self.context("chktex.warning")))
        # An ignore entry wins over a select entry as specific as it is.
        self.assertNotIn(lint, self.task_names(self.context("ALL", "CHK001", ignore=("CHK001",))))
        self.assertNotIn(lint, self.task_names(self.context("CHK", ignore=("CHK",))))
        self.assertNotIn(lint, self.task_names(self.context("CHK001"), main=None))

    async def test_the_lint_reads_the_imported_project_and_holds_a_small_lease(self) -> None:
        imported = self.write_project({"main.tex": _MAIN, "sections/method.tex": _SECTION})
        selected = self.write_project({"main.tex": _CLEAN}, name="selected")
        trees = iter((imported, selected))
        context = self.context("ALL", "CHK001")
        tasks = source_analysis_tasks(context, lambda: next(trees), "main.tex")
        task = next(item for item in tasks if item.name == TaskName.CHKTEX_LINT)
        self.assertEqual((task.cpu, task.memory_mb, task.kind), (1, CHKTEX_MEMORY_MB, None))
        findings = await task.run()
        self.assertEqual(
            [(item.path, item.line) for item in findings],
            [("main.tex", 3), ("sections/method.tex", 1)],
        )

    def test_a_chktex_suggestion_enters_its_stage_unless_a_comment_silences_it(self) -> None:
        project = self.write_project(
            {"main.tex": "First \\cite{a}.\nSecond \\cite{b}. % fledge: ignore CHK001\n"}
        )
        report = Report("inspect")
        reporter = TaskReporter(
            report, Settings(), source_root=project, main="main.tex", document_name=None
        )
        findings = [
            Finding(
                "chktex.warning",
                f"{_TIE_MESSAGE} (chktex warning 2)",
                severity="suggestion",
                path="main.tex",
                line=line,
            )
            for line in (1, 2)
        ]
        reporter.collect(TaskResult(TaskName.CHKTEX_LINT.value, TaskStatus.SUCCEEDED, findings))
        self.assertEqual(
            [(item.line, item.stage) for item in report.findings], [(1, "chktex lint")]
        )
        self.assertEqual(
            report.execution["silenced_suggestions"],
            [{"code": "CHK001", "path": "main.tex", "line": 2}],
        )


class ChktexToolGrantTests(ProjectTestCase):
    """chktex joins the declared toolchain, and starts with no user configuration."""

    def test_chktex_is_declared_with_the_other_tools_and_has_an_install_hint(self) -> None:
        def found(name: str) -> str | None:
            """Find chktex and pdflatex in one fixture directory, and nothing else."""
            return f"/test-tex/bin/{name}" if name in {"chktex", "pdflatex"} else None

        with patch("latexprep.toolchain.shutil.which", side_effect=found):
            toolchain = Toolchain.discover(SeatbeltSandbox())
        chktex = toolchain.resolve("chktex")
        self.assertEqual(chktex.name, "chktex")
        # Declared up front, as makeindex and Biber are: every command shares one grant set.
        self.assertIn(chktex, toolchain.context(toolchain.resolve("pdflatex")).executables)
        hint = install_hint("chktex") or ""
        self.assertIn("sudo tlmgr install chktex", hint)
        self.assertIn("sudo apt install chktex", hint)

    def test_the_linux_sandbox_may_read_the_debian_chktex_configuration(self) -> None:
        self.assertIn(Path("/etc/chktexrc"), BubblewrapSandbox().system_roots())

    def test_chktex_starts_without_any_user_configuration(self) -> None:
        workspace = self.root / "run"
        workspace.mkdir()
        toolchain = Toolchain(frozenset(), frozenset(), {})
        environment = create_tool_environment(
            workspace, toolchain.context(Path("/test-tex/bin/chktex")), source_date_epoch="0"
        )
        variables = environment.variables
        # chktex reads $HOME/.chktexrc, $LOGDIR/.chktexrc, $CHKTEXRC/.chktexrc and the file
        # CHKTEX_CONFIG names; none of these may reach it.
        for name in ("HOME", "LOGDIR", "CHKTEXRC", "CHKTEX_CONFIG"):
            self.assertNotIn(name, variables)
        configuration_home = Path(variables["XDG_CONFIG_HOME"])
        self.assertTrue(configuration_home.is_relative_to(environment.state_directory))
        self.assertEqual(list(configuration_home.iterdir()), [])
