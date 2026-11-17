r"""Files of the TeX installation: the lookup, and bare ``\input`` names the project lacks.

``\input{glyphtounicode}`` reads a file of TeX Live, not of the project. The job asks the
installation about such names once; the source graph then treats a found name as a
toolchain reference and reports one the installation lacks as missing from both.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from latexprep.bibliography_integrity import check_bibliography_integrity
from latexprep.citation_graph import resource_declarations, selected_bibliography
from latexprep.command_runner import CommandResult, ToolVersion
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.errors import PreparationError, ToolUnavailableError
from latexprep.expanded_document import reachable_sources
from latexprep.findings import Finding
from latexprep.job_context import JobRequest
from latexprep.report import Report
from latexprep.source_graph import SourceGraph
from latexprep.source_resolution import (
    InstalledInputs,
    installation_candidates,
    installation_file_names,
    installed_inputs_known,
    package_code_candidates,
)
from latexprep.tex_installation import (
    MAX_INSTALLATION_NAMES,
    ask_installation,
    identical_copies,
    installed_files,
)
from tests.support import AsyncProjectTestCase, ProjectTestCase

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
            self.assertEqual(runner.calls, [["kpsewhich", *names]])
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

    async def test_a_failed_lookup_is_an_unanswered_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = KpsewhichRunner(2, "")
            answer = await ask_installation(["a.tex"], Path(directory) / "lookup", runner)
            self.assertEqual(answer, InstalledInputs(frozenset(), {}, answered=False))
            self.assertIsNone(await ask_installation([], Path(directory) / "none", runner))

    async def test_a_missing_kpsewhich_is_an_unanswered_question(self) -> None:
        class MissingKpsewhich(KpsewhichRunner):
            """A runner on a machine without TeX."""

            async def tool_version(self, argv: list[str], workspace: Path) -> ToolVersion:
                """Fail as the runner does for a tool that is not on the search path."""
                raise ToolUnavailableError("Required tool is unavailable: kpsewhich.", "kpsewhich")

        with tempfile.TemporaryDirectory() as directory:
            runner = MissingKpsewhich(0, "")
            answer = await ask_installation(["a.tex"], Path(directory) / "lookup", runner)
        assert answer is not None
        self.assertFalse(answer.answered)
        self.assertEqual(runner.calls, [])

    async def test_at_most_the_bound_is_asked(self) -> None:
        names = [f"n{index}.tex" for index in range(MAX_INSTALLATION_NAMES + 5)]
        with tempfile.TemporaryDirectory() as directory:
            runner = KpsewhichRunner(MAX_INSTALLATION_NAMES, "")
            answer = await ask_installation(names, Path(directory) / "lookup", runner)
        assert answer is not None
        self.assertEqual(len(answer.asked), MAX_INSTALLATION_NAMES)
        self.assertEqual(answer.found, {})


class InstallationNameTests(unittest.TestCase):
    """Only plain bare names are asked about."""

    def test_bibliography_names_get_the_bib_suffix_as_bibtex_reads_them(self) -> None:
        self.assertEqual(installation_file_names("IEEEabrv", "bibliography"), ("IEEEabrv.bib",))
        self.assertEqual(installation_file_names("IEEEabrv.bib", "bibliography"), ("IEEEabrv.bib",))
        # BibTeX appends .bib to any other name, even one with a dot.
        self.assertEqual(installation_file_names("refs.v2", "bibliography"), ("refs.v2.bib",))
        self.assertEqual(installation_file_names("bib/IEEEabrv", "bibliography"), ())

    def test_figure_names_are_asked_with_each_graphics_suffix(self) -> None:
        self.assertEqual(
            installation_file_names("example-image-a", "includegraphics"),
            (
                "example-image-a.pdf",
                "example-image-a.png",
                "example-image-a.jpg",
                "example-image-a.eps",
            ),
        )
        self.assertEqual(
            installation_file_names("example-image.PNG", "includegraphics"), ("example-image.PNG",)
        )

    def test_plain_names_get_the_tex_suffix_and_others_are_refused(self) -> None:
        self.assertEqual(installation_file_names("glyphtounicode"), ("glyphtounicode.tex",))
        self.assertEqual(installation_file_names(" pdfglyphlist.def "), ("pdfglyphlist.def",))
        for refused in ("sections/intro", "../x", "-rf", "a b", "x.", "\\macro", ""):
            with self.subTest(refused=refused):
                self.assertEqual(installation_file_names(refused), ())


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
            frozenset({"glyphtounicode.tex"}), {"glyphtounicode.tex": "/texlive/glyphtounicode.tex"}
        )
        with installed_inputs_known(answer):
            self.assertEqual(set(self.missing()), {"sections/intro", "chapter"})
            graph = SourceGraph.build(self.root, "main.tex")
        self.assertIn("glyphtounicode.tex", [item.basename for item in graph.system_references])
        # The answer applies only inside the block.
        self.assertIn("glyphtounicode", self.missing())

    def test_a_name_the_installation_lacks_is_missing_from_both(self) -> None:
        with installed_inputs_known(InstalledInputs(frozenset({"glyphtounicode.tex"}), {})):
            message = self.missing()["glyphtounicode"]
        self.assertIn("missing from the project and from the TeX installation", message)


class UnansweredInstallationTests(ProjectTestCase):
    r"""Without TeX on the search path, the installation cannot say which files it holds.

    A bare name that the installation may hold is then not a confirmed missing file, while a
    name with a directory and a chapter of ``\include``, which only the project can hold, are.
    """

    def setUp(self) -> None:
        super().setUp()
        figures = "\\includegraphics{example-image-a}\n\\includegraphics{figs/plot}"
        self.write("main.tex", MAIN.replace("Hello.", figures))

    def findings(self) -> dict[str, Finding]:
        """Return the missing-dependency finding of each reference, without an answer."""
        with installed_inputs_known(InstalledInputs(frozenset(), {}, answered=False)):
            graph = SourceGraph.build(self.root, "main.tex")
        return {
            str(item.details["reference"]): item
            for item in graph.findings
            if item.rule == "source.missing_dependency"
        }

    def test_a_bare_name_the_installation_may_hold_is_not_confirmed_missing(self) -> None:
        found = self.findings()
        for name in ("glyphtounicode", "example-image-a"):
            with self.subTest(name=name):
                item = found[name]
                self.assertEqual((item.status, item.severity), ("inconclusive", "warning"))
                self.assertIn("the TeX installation could not be asked", item.message)

    def test_a_name_with_a_directory_or_only_the_project_could_hold_is_still_missing(
        self,
    ) -> None:
        found = self.findings()
        for name in ("sections/intro", "chapter", "figs/plot"):
            with self.subTest(name=name):
                self.assertEqual((found[name].status, found[name].severity), ("failed", "error"))
                self.assertNotIn("TeX installation", found[name].message)


class InspectWithoutTexTests(AsyncProjectTestCase):
    """``inspect`` on a machine without TeX, whose ``kpsewhich`` cannot be found."""

    async def inspect(self, body: str) -> Report:
        """Return the report of ``inspect`` on a document with ``body``, without kpsewhich."""
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\usepackage{graphicx}\n\\begin{document}\n"
            + body
            + "\n\\end{document}\n",
        )
        real_which = shutil.which

        def without_kpsewhich(name: str, *args: object, **kwargs: object) -> str | None:
            return None if name == "kpsewhich" else real_which(name, *args, **kwargs)

        with patch("latexprep.toolchain.shutil.which", side_effect=without_kpsewhich):
            return await run_job(JobRequest("inspect", self.root, Settings(main="main.tex")))

    async def test_a_figure_the_installation_may_hold_is_an_advisory(self) -> None:
        report = await self.inspect("\\includegraphics{example-image-a}")
        [item] = [item for item in report.findings if item.code == "TEX005"]
        self.assertEqual((item.status, item.severity), ("inconclusive", "warning"))
        self.assertEqual(report.outcome, "passed_with_advisories")

    async def test_a_figure_missing_from_the_project_folder_still_blocks(self) -> None:
        report = await self.inspect(
            "\\includegraphics{example-image-a}\n\\includegraphics{figures/plot}"
        )
        statuses = {item.status for item in report.findings if item.code == "TEX005"}
        self.assertIn("failed", statuses)
        self.assertEqual(report.outcome, "blocked")


IEEE_MAIN = (
    "\\documentclass{article}\n\\begin{document}\nText~\\cite{a}.\n"
    "\\bibliographystyle{IEEEtran}\n\\bibliography{IEEEabrv, references}\n\\end{document}\n"
)
# The installed IEEEabrv.bib is Latin-1: BibTeX reads bytes, so the file is read anyway.
IEEE_ABBREVIATIONS = (
    b"% Nicol\xe1s Barabino and others\n"
    b'@STRING{IEEE_J_SE = "{IEEE} Trans. Softw. Eng."}\n'
    b"@string ( IEEE_O_CSE = {Comput. Sci. Eng.} )\n"
)


class InstalledBibliographyTests(ProjectTestCase):
    r"""``\bibliography{IEEEabrv}`` reads IEEE's abbreviations from the TeX installation."""

    def setUp(self) -> None:
        super().setUp()
        self.write("main.tex", IEEE_MAIN)
        self.write(
            "references.bib",
            "@article{a, title = {T}, author = {A. B}, journal = IEEE_J_SE, year = 2020}\n"
            "@article{b, title = {U}, author = {C. D}, journal = IEEE_J_TSE_X, year = 2020}\n",
        )
        installation = tempfile.TemporaryDirectory()
        self.addCleanup(installation.cleanup)
        self.installed = Path(installation.name) / "IEEEabrv.bib"
        self.installed.write_bytes(IEEE_ABBREVIATIONS)
        self.answer = InstalledInputs(
            frozenset({"IEEEabrv.bib"}), {"IEEEabrv.bib": str(self.installed)}
        )

    def test_each_name_of_a_bibliography_list_is_a_candidate(self) -> None:
        self.assertEqual(installation_candidates(self.root), ["IEEEabrv.bib"])

    def test_an_installed_bibliography_is_resolved_and_not_a_project_resource(self) -> None:
        with installed_inputs_known(self.answer):
            graph = SourceGraph.build(self.root, "main.tex")
            selection = selected_bibliography(self.root, "main.tex")
        self.assertNotIn("source.missing_dependency", [item.rule for item in graph.findings])
        resources, reasons = resource_declarations(graph, reachable_sources(graph))
        self.assertEqual((resources, reasons), ({"references.bib"}, []))
        self.assertEqual(selection.installed, (str(self.installed),))

    def test_its_string_macros_are_defined_and_others_still_reported(self) -> None:
        with installed_inputs_known(self.answer):
            findings = check_bibliography_integrity(self.root, "main.tex")
        undefined = [
            item.message for item in findings if item.rule == "bibliography.undefined_macro"
        ]
        self.assertEqual(len(undefined), 1)
        self.assertIn("IEEE_J_TSE_X", undefined[0])
        self.assertNotIn("bibliography.unreadable", [item.rule for item in findings])

    def test_an_installed_file_with_entries_leaves_citation_coverage_open(self) -> None:
        # IEEEexample.bib in TeX Live holds entries, which are not inspected.
        self.installed.write_bytes(IEEE_ABBREVIATIONS + b"@article{article-minimal, title={T}}\n")
        with installed_inputs_known(self.answer):
            graph = SourceGraph.build(self.root, "main.tex")
        _, reasons = resource_declarations(graph, reachable_sources(graph))
        self.assertEqual(len(reasons), 1)
        self.assertIn("IEEEabrv.bib from the TeX installation holds entries", reasons[0])

    def test_figure_names_are_asked_after_input_and_bibliography_names(self) -> None:
        figures = "".join(f"\\includegraphics{{fig{index}}}\n" for index in range(20))
        self.write("main.tex", figures + IEEE_MAIN)
        names = installation_candidates(self.root)
        self.assertEqual(names[0], "IEEEabrv.bib")
        self.assertEqual(len(names), 1 + 20 * 4)

    def test_an_unreadable_installed_file_leaves_its_macros_uncertain(self) -> None:
        self.installed.unlink()
        with installed_inputs_known(self.answer):
            findings = check_bibliography_integrity(self.root, "main.tex")
        undefined = [item for item in findings if item.rule == "bibliography.undefined_macro"]
        self.assertEqual({item.status for item in undefined}, {"inconclusive"})
        self.assertIn(
            "the bibliography style 'IEEEtran' or file 'IEEEabrv.bib' may define it",
            undefined[0].message,
        )


VENUE_CLASS = (
    "\\NeedsTeXFormat{LaTeX2e}\\ProvidesClass{venue}\n\\LoadClass{article}\n"
    "\\IfFileExists{venue.cfg}{\\input{venue.cfg}}{}\n\\catcode`\\@=11\n"
)


class InstalledCopyTests(ProjectTestCase):
    """A bundled class identical to the installation's copy is analysed as the installed one."""

    def setUp(self) -> None:
        super().setUp()
        self.write(
            "main.tex", "\\documentclass{venue}\n\\begin{document}\nText.\n\\end{document}\n"
        )
        self.write("venue.cls", VENUE_CLASS)
        installation = tempfile.TemporaryDirectory()
        self.addCleanup(installation.cleanup)
        self.installed = Path(installation.name) / "venue.cls"

    def test_only_byte_identical_copies_count(self) -> None:
        packages = package_code_candidates(self.root)
        self.assertEqual(packages, {"venue.cls": ["venue.cls"]})
        self.installed.write_text(VENUE_CLASS)
        found = {"venue.cls": str(self.installed)}
        self.assertEqual(identical_copies(self.root, packages, found), frozenset({"venue.cls"}))
        self.installed.write_text(VENUE_CLASS + "% a local change\n")
        self.assertEqual(identical_copies(self.root, packages, found), frozenset())
        self.assertEqual(identical_copies(self.root, packages, {}), frozenset())

    def test_an_identical_copy_is_a_dependency_but_not_walked(self) -> None:
        walked = SourceGraph.build(self.root, "main.tex")
        self.assertIn("venue.cls", reachable_sources(walked))
        self.assertTrue(any(item.path == "venue.cls" for item in walked.findings))
        answer = InstalledInputs(
            frozenset({"venue.cls"}),
            {"venue.cls": "/texlive/venue.cls"},
            identical=frozenset({"venue.cls"}),
        )
        with installed_inputs_known(answer):
            graph = SourceGraph.build(self.root, "main.tex")
        self.assertIn("venue.cls", graph.dependencies)
        self.assertNotIn("venue.cls", reachable_sources(graph))
        self.assertEqual([item for item in graph.findings if item.path == "venue.cls"], [])


if __name__ == "__main__":
    unittest.main()
