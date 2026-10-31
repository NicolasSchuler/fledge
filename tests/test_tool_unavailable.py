"""Missing tools: the error, the build finding and the explanation of a missing TeX toolchain."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from latexprep.defects import is_expected_failure
from latexprep.errors import PreparationError, ToolUnavailableError
from latexprep.findings import Finding
from latexprep.latexmk_build import build_project
from latexprep.setting_choices import ENGINES
from latexprep.toolchain import Toolchain, tool_path
from latexprep.toolchain_help import explain_missing_toolchain
from tests.fakes import FakeBuildRunner

_PACKAGES = {
    "Poppler": ("pdfinfo", "pdftotext", "pdftoppm", "pdffonts", "pdfimages", "pdftohtml"),
    "qpdf": ("qpdf",),
    "MuPDF": ("mutool",),
    "tex-fmt": ("tex-fmt",),
    "TeX Live": ("latexmk", "pdflatex", "xelatex", "lualatex", "biber", "bibtex"),
}


class MissingToolErrorTests(unittest.TestCase):
    """The error for a missing tool: the tool it names and the package that provides it."""

    def test_the_error_names_the_tool_without_message_parsing(self) -> None:
        with patch("latexprep.toolchain.shutil.which", return_value=None):
            with self.assertRaises(ToolUnavailableError) as raised:
                tool_path("pdftotext")
            with self.assertRaises(ToolUnavailableError) as resolved:
                Toolchain(frozenset(), frozenset(), {}).resolve("qpdf")
        self.assertEqual(raised.exception.tool, "pdftotext")
        self.assertEqual(resolved.exception.tool, "qpdf")
        # Still an expected failure that callers handling PreparationError report as before.
        self.assertIsInstance(raised.exception, PreparationError)
        self.assertTrue(is_expected_failure(raised.exception))

    def test_each_tool_names_the_package_that_provides_it(self) -> None:
        with patch("latexprep.toolchain.shutil.which", return_value=None):
            for package, tools in _PACKAGES.items():
                for tool in tools:
                    with self.subTest(tool=tool), self.assertRaises(ToolUnavailableError) as error:
                        tool_path(tool)
                    message = str(error.exception)
                    self.assertTrue(
                        message.startswith(f"Required tool is unavailable: {tool}. Install it "),
                        message,
                    )
                    self.assertIn(package, message)
                    self.assertIn("brew install", message)

    def test_a_tool_without_a_known_package_still_gets_the_plain_message(self) -> None:
        with patch("latexprep.toolchain.shutil.which", return_value=None):
            with self.assertRaises(ToolUnavailableError) as error:
                tool_path("unknown-tool")
        self.assertEqual(
            str(error.exception),
            "Required tool is unavailable: unknown-tool. Install it explicitly.",
        )
        self.assertEqual(error.exception.tool, "unknown-tool")


class BuildFindingTests(unittest.IsolatedAsyncioTestCase):
    """How a build records an unavailable execution, and which errors it lets through."""

    async def asyncSetUp(self) -> None:
        """Create a ``source`` folder with a placeholder ``main.tex``; reset the build count."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "main.tex").write_text("source content")
        self.builds = 0

    async def asyncTearDown(self) -> None:
        """Remove the temporary directory."""
        self.temporary.cleanup()

    async def build(self, error: Exception):
        """Build the test project with a runner whose every command raises ``error``.

        Each call uses a new build directory. Errors that the build does not turn into findings
        propagate.

        Args:
            error: The exception every command raises.

        Returns:
            The build result.
        """

        class RaisingRunner(FakeBuildRunner):
            """A fake build runner whose every command raises ``error``."""

            async def run(self, *args, **options):
                """Raise ``error`` whatever the command.

                The arguments are those of :meth:`CommandRunner.run`.
                """
                raise error

        self.builds += 1
        return await build_project(
            self.source, "main.tex", self.root / f"build-{self.builds}", "pdflatex", RaisingRunner()
        )

    async def test_a_missing_tool_is_recorded_in_the_finding_details(self) -> None:
        message = "Required tool is unavailable: latexmk. Install it explicitly."
        result = await self.build(ToolUnavailableError(message, "latexmk"))
        [finding] = [item for item in result.findings if item.rule == "build.execution_unavailable"]
        self.assertEqual(finding.message, message)
        self.assertEqual(finding.details, {"tool": "latexmk"})

    async def test_other_unavailable_executions_name_no_tool(self) -> None:
        result = await self.build(PreparationError("Restricted execution is unavailable"))
        [finding] = [item for item in result.findings if item.rule == "build.execution_unavailable"]
        self.assertEqual(finding.details, {})

    async def test_a_contract_violation_is_not_reported_as_an_unavailable_toolchain(self) -> None:
        for error in (ValueError("bad argument"), RuntimeError("overlapping workspaces")):
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                await self.build(error)


class MissingToolchainExplanationTests(unittest.TestCase):
    """Explaining a missing TeX toolchain from a finding's details, never from its message."""

    @staticmethod
    def unavailable(tool: str | None, message: str | None = None) -> Finding:
        """Return an inconclusive ``build.execution_unavailable`` error finding.

        Args:
            tool: The tool recorded in the details, or ``None`` for empty details.
            message: The message, or ``None`` for the standard message about ``tool``.

        Returns:
            The finding.
        """
        return Finding(
            "build.execution_unavailable",
            message or f"Required tool is unavailable: {tool}. Install it explicitly.",
            severity="error",
            status="inconclusive",
            details={} if tool is None else {"tool": tool},
        )

    def test_a_tex_tool_named_in_the_details_is_explained(self) -> None:
        with patch("latexprep.toolchain_help.shutil.which", return_value=None):
            for engine in ENGINES:
                with self.subTest(engine=engine):
                    [explained] = explain_missing_toolchain([self.unavailable("latexmk")], engine)
                    self.assertEqual(explained.details["missing_tools"], ["latexmk", engine])
                    self.assertEqual(explained.details["tool"], "latexmk")
                    self.assertIn("The TeX toolchain is unavailable", explained.message)

    def test_an_engine_found_on_the_path_is_not_reported_missing(self) -> None:
        with patch(
            "latexprep.toolchain_help.shutil.which",
            side_effect=lambda name: "/usr/bin/pdflatex" if name == "pdflatex" else None,
        ):
            [explained] = explain_missing_toolchain([self.unavailable("pdflatex")], "pdflatex")
        self.assertEqual(explained.details["missing_tools"], ["latexmk"])

    def test_every_finding_of_a_missing_tex_tool_is_explained(self) -> None:
        findings = [self.unavailable("latexmk"), self.unavailable("pdflatex")]
        with patch("latexprep.toolchain_help.shutil.which", return_value=None):
            explained = explain_missing_toolchain(findings, "pdflatex")
        self.assertEqual(
            [finding.details.get("missing_tools") for finding in explained],
            [["latexmk", "pdflatex"], ["latexmk", "pdflatex"]],
        )

    def test_the_message_wording_alone_is_never_parsed(self) -> None:
        finding = self.unavailable(None)
        other_tool = self.unavailable("pdftotext")
        other_rule = Finding("build.compile", "tool", severity="error", details={"tool": "latexmk"})
        with patch("latexprep.toolchain_help.shutil.which", return_value=None):
            explained = explain_missing_toolchain([finding, other_tool, other_rule], "pdflatex")
        self.assertEqual(explained, [finding, other_tool, other_rule])


if __name__ == "__main__":
    unittest.main()
