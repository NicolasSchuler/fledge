"""Resolving file references: candidate paths, project lookup, path helpers and graph findings."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock

from latexprep.bibliography_checks import check_bibliography_coverage_and_fields
from latexprep.citation_graph import resource_declarations
from latexprep.expanded_document import reachable_sources
from latexprep.findings import Finding, has_blockers
from latexprep.project_files import is_parsed_source, known_path_index, path_comparison_key
from latexprep.source_file_commands import DEFAULT_SUFFIXES_BY_FILE_COMMAND, candidate_paths
from latexprep.source_findings import MAX_LISTED_PATHS, GraphFindings, listed_names
from latexprep.source_graph import SourceGraph
from latexprep.source_resolution import (
    InstalledInputs,
    LookupScope,
    ProjectTree,
    Reference,
    existing_files,
    installation_candidates,
    installation_file_names,
    installed_input_path,
    installed_inputs_known,
    resolve_reference,
)
from latexprep.tex_lexer import scan_source_commands
from tests.support import ProjectTestCase, clean_case


def reference(text: str) -> Reference:
    """Return the reference that the only file command in ``text`` makes from ``main.tex``.

    The referenced name is the command's first argument.
    """
    (command,) = scan_source_commands(text)
    return Reference(command.arguments[0].value, "main.tex", command)


class CandidatePathTests(unittest.TestCase):
    """The candidate paths a file command tries for a name, and their order."""

    def test_a_suffixless_name_tries_each_default_suffix_before_the_bare_name(self) -> None:
        suffixes = DEFAULT_SUFFIXES_BY_FILE_COMMAND["includegraphics"]
        candidates = candidate_paths("includegraphics", ["fig/a"], suffixes)
        self.assertEqual(candidates[0], "fig/a.pdf")
        self.assertEqual(candidates[-1], "fig/a")
        self.assertEqual(len(candidates), len(suffixes) + 1)

    def test_suffix_handling_depends_on_the_command(self) -> None:
        self.assertEqual(
            candidate_paths("input", ["a.tex", "b/a.tex"], (".tex",)), ["a.tex", "b/a.tex"]
        )
        self.assertEqual(
            candidate_paths("include", ["a.tex", "b.final", "c.tex.tex"], (".tex",)),
            ["a.tex", "b.final.tex", "c.tex.tex"],
        )
        self.assertEqual(
            candidate_paths("bibliography", ["a.final", "b.bib"], (".bib",)),
            ["a.final.bib", "b.bib"],
        )

    def test_paths_keep_their_order_so_the_first_base_wins(self) -> None:
        candidates = candidate_paths("input", ["inner/x", "x"], (".tex",))
        self.assertEqual(candidates, ["inner/x.tex", "inner/x", "x.tex", "x"])


class ProjectLookupTests(unittest.TestCase):
    """Looking up references: links, missing files, case mismatches and toolchain files."""

    def tree(self, names: dict[str, Path], on_link=None) -> ProjectTree:
        """Return a project tree of ``names`` whose compilation directory is the project root.

        Args:
            names: The project files by project-relative name.
            on_link: Called with each linked file the lookup skips, or ``None`` for a :class:`Mock`.

        Returns:
            The project tree.
        """
        return ProjectTree(names, "", on_link or Mock())

    def test_a_link_is_skipped_and_reported_to_the_graph(self) -> None:
        link = Mock(spec=Path)
        link.is_symlink.return_value = True
        plain = Mock(spec=Path)
        plain.is_symlink.return_value = False
        on_link = Mock()
        tree = self.tree({"a.tex": link, "b.tex": plain}, on_link)
        self.assertEqual(existing_files(["a.tex", "b.tex", "b.tex"], tree), ["b.tex"])
        on_link.assert_called_once_with("a.tex")

    def test_a_missing_file_lists_what_was_tried_and_a_case_mismatch_names_the_file(self) -> None:
        plain = Mock(spec=Path)
        plain.is_symlink.return_value = False
        findings, uncertainties = [], []
        sink = GraphFindings(findings, uncertainties)
        tree = self.tree({"Body.TEX": plain})
        missing = resolve_reference(reference(r"\input{nothing}"), LookupScope(), tree, sink)
        self.assertIsNone(missing.target)
        case = resolve_reference(reference(r"\input{body.TEX}"), LookupScope(), tree, sink)
        self.assertIsNone(case.target)
        self.assertEqual(
            [item.rule for item in findings], ["source.missing_dependency", "source.case_mismatch"]
        )
        self.assertIn("looked for nothing.tex, nothing", findings[0].message)
        self.assertIn("Body.TEX", findings[1].suggestion)
        self.assertEqual(uncertainties, [])

    def test_a_toolchain_class_outside_the_project_is_left_to_the_toolchain(self) -> None:
        findings: list[Finding] = []
        sink = GraphFindings(findings, [])
        result = resolve_reference(
            reference(r"\usepackage{amsmath}"), LookupScope(), self.tree({}), sink
        )
        self.assertIsNone(result.target)
        assert result.system is not None
        self.assertEqual(result.system.basename, "amsmath.sty")
        self.assertEqual(findings, [])


def missing_dependencies(graph: SourceGraph) -> dict[str, Finding]:
    """Return the missing-dependency finding of each reference of ``graph``, by its name."""
    return {
        str(item.details["reference"]): item
        for item in graph.findings
        if item.rule == "source.missing_dependency"
    }


CITING_MAIN = (
    "\\documentclass{article}\n\\begin{document}\nText \\cite{k1}.\n"
    "\\bibliographystyle{plain}\n\\bibliography{refs}\n\\end{document}\n"
)
BUILT_BIBLIOGRAPHY = (
    "\\begin{thebibliography}{1}\n\\bibitem{k1} A. Author. \\newblock Title. 2020.\n"
    "\\end{thebibliography}\n"
)


class ShippedBibliographyTests(ProjectTestCase):
    r"""An upload that ships the built ``<main>.bbl`` instead of the ``.bib`` database.

    The build reads the ``.bbl`` named after the main file, beside it, and runs no BibTeX
    without the database, so ``\bibliography{refs}`` needs no ``refs.bib``; that is the
    usual form of an arXiv upload.
    """

    @clean_case("TEX005")
    def test_the_main_files_bbl_stands_in_for_a_missing_database(self) -> None:
        self.write("main.tex", CITING_MAIN)
        self.write("main.bbl", BUILT_BIBLIOGRAPHY)
        graph = SourceGraph.build(self.root, "main.tex")
        note = missing_dependencies(graph)["refs"]
        self.assertEqual((note.status, note.severity), ("passed", "info"))
        self.assertIn("main.bbl", note.message)
        self.assertFalse(has_blockers(graph.findings))
        self.assertTrue(graph.is_complete)

    def test_the_bibliography_checks_say_that_the_entries_were_not_inspected(self) -> None:
        self.write("main.tex", CITING_MAIN)
        self.write("main.bbl", BUILT_BIBLIOGRAPHY)
        graph = SourceGraph.build(self.root, "main.tex")
        resources, reasons = resource_declarations(graph, reachable_sources(graph))
        self.assertEqual(resources, set())
        self.assertEqual(len(reasons), 1)
        self.assertIn("refs.bib is not in the project", reasons[0])
        self.assertIn("main.bbl", reasons[0])
        coverage = [
            item
            for item in check_bibliography_coverage_and_fields(self.root, "main.tex")
            if item.code == "BIB101"
        ]
        self.assertTrue(coverage)
        self.assertNotIn("passed", {item.status for item in coverage})
        self.assertFalse(has_blockers(coverage))

    def test_the_bbl_of_a_main_file_in_a_folder_is_read_beside_it(self) -> None:
        self.write("paper/main.tex", CITING_MAIN)
        self.write("paper/main.bbl", BUILT_BIBLIOGRAPHY)
        graph = SourceGraph.build(self.root, "paper/main.tex")
        self.assertEqual(missing_dependencies(graph)["refs"].status, "passed")

    def test_without_a_bbl_the_missing_database_still_fails(self) -> None:
        self.write("main.tex", CITING_MAIN)
        graph = SourceGraph.build(self.root, "main.tex")
        error = missing_dependencies(graph)["refs"]
        self.assertEqual((error.status, error.severity), ("failed", "error"))

    def test_a_bbl_without_entries_or_of_the_wrong_format_does_not_stand_in(self) -> None:
        # Review: an empty main.bbl, or a BibTeX one beside a biblatex resource, excused
        # the missing database although the build can print no bibliography from it.
        biblatex_main = (
            "\\documentclass{article}\n\\usepackage[backend=biber]{biblatex}\n"
            "\\addbibresource{refs.bib}\n\\begin{document}\nText \\cite{k1}.\n"
            "\\printbibliography\n\\end{document}\n"
        )
        biblatex_bbl = (
            "\\refsection{0}\n  \\datalist[entry]{nty/global//global/global}\n"
            "    \\entry{k1}{article}{}\n    \\endentry\n  \\enddatalist\n\\endrefsection\n"
        )
        for main, bbl, name in (
            (CITING_MAIN, "", "refs"),
            (CITING_MAIN, "% no entries yet\n\\bibliographystyle{plain}\n", "refs"),
            (biblatex_main, BUILT_BIBLIOGRAPHY, "refs.bib"),
        ):
            with self.subTest(bbl=bbl):
                self.write("main.tex", main)
                self.write("main.bbl", bbl)
                error = missing_dependencies(SourceGraph.build(self.root, "main.tex"))[name]
                self.assertEqual((error.status, error.severity), ("failed", "error"))
        # biblatex's own format stands in for its resource.
        self.write("main.tex", biblatex_main)
        self.write("main.bbl", biblatex_bbl)
        note = missing_dependencies(SourceGraph.build(self.root, "main.tex"))["refs.bib"]
        self.assertEqual(note.status, "passed")

    def test_the_bbl_of_another_main_file_does_not_stand_in(self) -> None:
        self.write("main.tex", CITING_MAIN)
        for other in ("paper.bbl", "build/main.bbl", "refs.bbl"):
            with self.subTest(other=other):
                self.write(other, BUILT_BIBLIOGRAPHY)
                graph = SourceGraph.build(self.root, "main.tex")
                error = missing_dependencies(graph)["refs"]
                self.assertEqual((error.status, error.severity), ("failed", "error"))
                (self.root / other).unlink()

    def test_a_main_file_in_a_folder_is_not_excused_by_a_bbl_at_the_root(self) -> None:
        self.write("paper/main.tex", CITING_MAIN)
        self.write("main.bbl", BUILT_BIBLIOGRAPHY)
        graph = SourceGraph.build(self.root, "paper/main.tex")
        self.assertEqual(missing_dependencies(graph)["refs"].status, "failed")

    def test_a_missing_input_is_not_excused_by_the_bbl(self) -> None:
        with_input = CITING_MAIN.replace("\\begin{document}", "\\input{body}\n\\begin{document}")
        self.write("main.tex", with_input)
        self.write("main.bbl", BUILT_BIBLIOGRAPHY)
        graph = SourceGraph.build(self.root, "main.tex")
        self.assertEqual(missing_dependencies(graph)["body"].status, "failed")


VENUE_CLASS_WITH_LOGOS = (
    "\\NeedsTeXFormat{LaTeX2e}\\ProvidesClass{venue}\n\\LoadClass{article}\n"
    "\\RequirePackage{graphicx}\n\\newif\\if@venue@journal\n"
    "\\DeclareOption{journal}{\\@venue@journaltrue}\\ProcessOptions\n"
    "\\def\\venue@logobox{\\setbox0=\\vbox{\\includegraphics{venue-logo}}}\n"
    "\\newcommand\\venue@badge{\\includegraphics[width=1cm]{venue-badge}}\n"
    "\\if@venue@journal\n  \\AtBeginDocument{\\includegraphics{journal-logo}}\n\\fi\n"
)


class PackageCodeReferenceTests(ProjectTestCase):
    r"""A file named in a bundled class runs only when the code around it runs.

    In package code, a reference inside a macro definition runs only if the macro is used,
    and one inside a conditional only if the condition holds, as for ``acmart.cls``'s
    ``\includegraphics{acm-jdslogo}`` under its ``acmcp`` format. The scan cannot decide
    either, so a missing file there is not a confirmed missing dependency.
    """

    def build(
        self, main_body: str = "Text.\n", class_text: str = VENUE_CLASS_WITH_LOGOS
    ) -> SourceGraph:
        """Return the graph of a document of the bundled ``venue`` class.

        Args:
            main_body: The text of the document environment of ``main.tex``.
            class_text: The source of the bundled ``venue.cls``.

        Returns:
            The graph built from ``main.tex``.
        """
        self.write(
            "main.tex",
            "\\documentclass{venue}\n\\begin{document}\n" + main_body + "\\end{document}\n",
        )
        self.write("venue.cls", class_text)
        return SourceGraph.build(self.root, "main.tex")

    def test_a_missing_figure_in_a_definition_or_conditional_of_a_bundled_class_is_open(
        self,
    ) -> None:
        graph = self.build()
        found = missing_dependencies(graph)
        merged = found["venue-logo"]
        self.assertEqual((merged.status, merged.severity), ("inconclusive", "warning"))
        self.assertEqual(merged.path, "venue.cls")
        self.assertIn("runs only if", merged.message)
        self.assertEqual(merged.details["occurrences"], 3)
        self.assertFalse(has_blockers(graph.findings))

    def test_a_missing_figure_the_class_always_runs_still_fails(self) -> None:
        graph = self.build(class_text=VENUE_CLASS_WITH_LOGOS + "\\includegraphics{always-logo}\n")
        error = missing_dependencies(graph)["always-logo"]
        self.assertEqual((error.status, error.severity), ("failed", "error"))

    def test_a_missing_figure_of_the_documents_own_files_still_fails(self) -> None:
        main_body = (
            "\\newcommand{\\logo}{\\includegraphics{own-logo}}\n"
            "\\ifdefined\\final\\includegraphics{final-figure}\\fi\n\\includegraphics{figure}\n"
        )
        graph = self.build(main_body)
        found = missing_dependencies(graph)
        for name in ("own-logo", "final-figure", "figure"):
            with self.subTest(name=name):
                self.assertEqual((found[name].status, found[name].severity), ("failed", "error"))
        self.assertEqual(found["venue-logo"].status, "inconclusive")


class InstalledStyleTests(ProjectTestCase):
    r"""A ``\bibliographystyle`` name the project lacks may name a .bst of the TeX installation.

    The installation is asked about it as about a bare ``\bibliography`` name, so that the
    bibliography checks can read the macros an installed style such as
    ``ACM-Reference-Format.bst`` defines.
    """

    def test_a_style_name_is_asked_with_the_suffix_bibtex_adds(self) -> None:
        for name in ("ACM-Reference-Format", "ACM-Reference-Format.bst"):
            with self.subTest(name=name):
                self.assertEqual(
                    installation_file_names(name, "bibliographystyle"),
                    ("ACM-Reference-Format.bst",),
                )
        # BibTeX adds .bst even after another dot, and finds no style in a directory.
        self.assertEqual(installation_file_names("acm.v2", "bibliographystyle"), ("acm.v2.bst",))
        self.assertEqual(installation_file_names("styles/acm", "bibliographystyle"), ())

    def test_only_a_style_that_no_project_file_has_is_a_candidate(self) -> None:
        self.write("main.tex", "\\bibliographystyle{ACM-Reference-Format}\n\\bibliography{refs}\n")
        self.write("refs.bib", "")
        self.assertEqual(installation_candidates(self.root), ["ACM-Reference-Format.bst"])
        self.write("ACM-Reference-Format.bst", "")
        self.assertEqual(installation_candidates(self.root), [])

    def test_a_standard_style_is_not_asked_about(self) -> None:
        # Its macros are known without its file, so it costs the job no lookup.
        self.write("main.tex", "\\bibliographystyle{Plain}\n\\bibliography{refs}\n")
        self.write("refs.bib", "")
        self.assertEqual(installation_candidates(self.root), [])

    def test_style_names_are_asked_after_input_and_figure_names(self) -> None:
        # An unasked style leaves its macros uncertain; an unasked input or figure is missing.
        self.write(
            "main.tex",
            "\\bibliographystyle{acmstyle}\n\\includegraphics{example-image}\n"
            "\\input{glyphtounicode}\n",
        )
        names = installation_candidates(self.root)
        self.assertEqual(names[0], "glyphtounicode.tex")
        self.assertEqual(names[-1], "acmstyle.bst")
        self.assertEqual(len(names), 1 + 4 + 1)

    def test_the_installation_path_is_known_only_while_its_answer_applies(self) -> None:
        name = "ACM-Reference-Format.bst"
        answer = InstalledInputs(frozenset({name, "missing.bst"}), {name: "/texlive/" + name})
        with installed_inputs_known(answer):
            self.assertEqual(installed_input_path(name), Path("/texlive/" + name))
            self.assertIsNone(installed_input_path("missing.bst"))
        self.assertIsNone(installed_input_path(name))


class ProjectFilesTests(unittest.TestCase):
    """Project file helpers: the known-path index, which files are parsed and path comparison."""

    def test_the_index_holds_every_suffix_and_directory_prefix(self) -> None:
        self.assertEqual(
            known_path_index(["a/b/c.tex"]),
            {"a/b/c.tex", "b/c.tex", "c.tex", "a/", "a/b/"},
        )

    def test_tex_sources_generated_tex_and_links_are_parsed(self) -> None:
        for name, expected in (
            ("a/main.tex", True),
            ("fig.pdf_tex", True),
            ("main.bbl", True),
            ("data.csv", False),
        ):
            with self.subTest(name=name):
                self.assertEqual(is_parsed_source(name, Path(name)), expected)

    def test_comparison_ignores_case_and_unicode_form(self) -> None:
        self.assertEqual(path_comparison_key("Café.TEX"), path_comparison_key("café.tex"))


class ListedNamesTests(unittest.TestCase):
    """Shortening a long list of file names in a message."""

    def test_many_names_are_cut_short_and_counted(self) -> None:
        names = [f"f{index}.tex" for index in range(MAX_LISTED_PATHS + 3)]
        text = listed_names(names)
        self.assertEqual(text, "f0.tex, f1.tex, f2.tex, f3.tex, f4.tex (+3 more)")
        self.assertEqual(listed_names(names[:2]), "f0.tex, f1.tex")


class GraphFindingsTests(unittest.TestCase):
    """Recording graph findings and uncertainties without duplicates."""

    def test_an_identical_uncertainty_or_error_is_recorded_once(self) -> None:
        findings, uncertainties = [], []
        sink = GraphFindings(findings, uncertainties)
        for _ in range(2):
            sink.uncertain("source.dynamic_path", "m", "a.tex", 3)
            sink.error("source.missing_dependency", "m", "a.tex", 3)
        self.assertEqual(len(uncertainties), 1)
        self.assertEqual(len(findings), 2)
        self.assertEqual(uncertainties[0].status, "inconclusive")
        self.assertEqual(findings[1].severity, "error")


if __name__ == "__main__":
    unittest.main()
