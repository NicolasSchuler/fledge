r"""Files of the TeX installation: the lookup, and bare ``\input`` names the project lacks.

``\input{glyphtounicode}`` reads a file of TeX Live, not of the project. The job asks the
installation about such names once; the source graph then treats a found name as a
toolchain reference and reports one the installation lacks as missing from both.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from latexprep.command_runner import CommandResult, ToolVersion
from latexprep.errors import PreparationError
from latexprep.source_graph import SourceGraph
from latexprep.source_resolution import (
    InstalledInputs,
    installation_candidates,
    installation_file_name,
    installed_inputs_known,
)
from latexprep.tex_installation import MAX_INSTALLATION_NAMES, ask_installation, installed_files
from tests.support import ProjectTestCase

MAIN = (
    "\\documentclass{article}\n\\input{glyphtounicode}\n\\pdfgentounicode=1\n"
    "\\input{sections/intro}\n\\begin{document}\n\\include{chapter}\nHello.\n\\end{document}\n"
)


class KpsewhichRunner:
    """A runner that answers one kpsewhich lookup with a fixed status and output.

    kpsewhich exits with the number of names it finds no file for (kpathsea 6.4).

    Attributes:
        status: The exit status of the lookup.
        output: The standard output of the lookup.
        calls: The command line of every lookup.
    """

    def __init__(self, status: int, output: str) -> None:
        self.status, self.output = status, output
        self.calls: list[list[str]] = []

    async def tool_version(self, argv: list[str], workspace: Path) -> ToolVersion:
        """Return a fixed kpsewhich version."""
        return ToolVersion(("Kpathsea fixture",), "/tools/kpsewhich", tuple(argv[1:]))

    async def run(self, argv: list[str], cwd: Path, workspace: Path) -> CommandResult:
        """Record the lookup, check that it runs in an empty folder, and answer it."""
        self.calls.append(argv)
        assert cwd == workspace and not list(workspace.iterdir())
        return CommandResult(self.status, self.output, "", resolved_command=argv)


class InstalledFilesTests(unittest.IsolatedAsyncioTestCase):
    """One kpsewhich call for many names, and what its output proves."""

    async def lookup(self, names: list[str], status: int, output: str) -> dict[str, str]:
        """Return what ``installed_files`` finds when kpsewhich answers ``status``/``output``."""
        with tempfile.TemporaryDirectory() as directory:
            runner = KpsewhichRunner(status, output)
            found = await installed_files(names, Path(directory) / "lookup", runner)
            self.assertEqual(runner.calls, [["kpsewhich", "--format=tex", *names]])
            return found

    async def test_found_names_are_mapped_by_file_name(self) -> None:
        found = await self.lookup(
            ["glyphtounicode.tex", "missing.tex", "absent.tex"],
            2,
            "/texlive/tex/generic/pdftex/glyphtounicode.tex\n",
        )
        self.assertEqual(
            found, {"glyphtounicode.tex": "/texlive/tex/generic/pdftex/glyphtounicode.tex"}
        )

    async def test_contradictory_or_relative_output_is_ambiguous(self) -> None:
        cases = (
            # Status 0 says that both names were found, but the output names one.
            (["a.tex", "b.tex"], 0, "/texlive/a.tex\n"),
            # Status 2 says that both names are missing, but the output names one.
            (["a.tex", "b.tex"], 2, "/texlive/a.tex\n"),
            (["a.tex"], 0, "relative/a.tex\n"),
            (["a.tex"], 0, "/texlive/one/a.tex\n/texlive/two/a.tex\n"),
        )
        for names, status, output in cases:
            with self.subTest(output=output), self.assertRaises(PreparationError):
                await self.lookup(names, status, output)

    async def test_a_failed_lookup_is_no_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = KpsewhichRunner(2, "")
            answer = await ask_installation(["a.tex"], Path(directory) / "lookup", runner)
            self.assertIsNone(answer)
            self.assertIsNone(await ask_installation([], Path(directory) / "none", runner))

    async def test_at_most_the_bound_is_asked(self) -> None:
        names = [f"n{index}.tex" for index in range(MAX_INSTALLATION_NAMES + 5)]
        with tempfile.TemporaryDirectory() as directory:
            runner = KpsewhichRunner(MAX_INSTALLATION_NAMES, "")
            answer = await ask_installation(names, Path(directory) / "lookup", runner)
        assert answer is not None
        self.assertEqual(len(answer.asked), MAX_INSTALLATION_NAMES)
        self.assertEqual(answer.found, frozenset())


class InstallationNameTests(unittest.TestCase):
    """Only plain bare names are asked about."""

    def test_plain_names_get_the_tex_suffix_and_others_are_refused(self) -> None:
        self.assertEqual(installation_file_name("glyphtounicode"), "glyphtounicode.tex")
        self.assertEqual(installation_file_name(" pdfglyphlist.def "), "pdfglyphlist.def")
        for refused in ("sections/intro", "../x", "-rf", "a b", "x.", "\\macro", ""):
            with self.subTest(refused=refused):
                self.assertIsNone(installation_file_name(refused))


class InstallationResolutionTests(ProjectTestCase):
    """How the source graph uses the installation's answer."""

    def setUp(self) -> None:
        super().setUp()
        self.write("main.tex", MAIN)

    def missing(self) -> dict[str, str]:
        """Return the message of each missing-dependency finding, by its reference."""
        graph = SourceGraph.build(self.root, "main.tex")
        return {
            str(item.details["reference"]): item.message
            for item in graph.findings
            if item.rule == "source.missing_dependency"
        }

    def test_only_bare_input_names_that_no_project_file_has_are_candidates(self) -> None:
        self.write("macros.tex", "\\input{shared}\n\\input{glyphtounicode}\n")
        self.write("styles/shared.tex", "% found by the graph's own lookup\n")
        self.assertEqual(installation_candidates(self.root), ["glyphtounicode.tex"])

    def test_without_an_answer_every_missing_input_is_reported(self) -> None:
        missing = self.missing()
        self.assertEqual(set(missing), {"glyphtounicode", "sections/intro", "chapter"})
        self.assertNotIn("TeX installation", missing["glyphtounicode"])

    def test_a_name_the_installation_holds_is_a_toolchain_reference(self) -> None:
        answer = InstalledInputs(
            frozenset({"glyphtounicode.tex"}), frozenset({"glyphtounicode.tex"})
        )
        with installed_inputs_known(answer):
            self.assertEqual(set(self.missing()), {"sections/intro", "chapter"})
            graph = SourceGraph.build(self.root, "main.tex")
        self.assertIn("glyphtounicode.tex", [item.basename for item in graph.system_references])
        # The answer applies only inside the block.
        self.assertIn("glyphtounicode", self.missing())

    def test_a_name_the_installation_lacks_is_missing_from_both(self) -> None:
        with installed_inputs_known(
            InstalledInputs(frozenset({"glyphtounicode.tex"}), frozenset())
        ):
            message = self.missing()["glyphtounicode"]
        self.assertIn("missing from the project and from the TeX installation", message)


if __name__ == "__main__":
    unittest.main()
