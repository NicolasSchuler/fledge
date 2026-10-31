"""Resolving file references: candidate paths, project lookup, path helpers and graph findings."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock

from latexprep.findings import Finding
from latexprep.project_files import is_parsed_source, known_path_index, path_comparison_key
from latexprep.source_file_commands import DEFAULT_SUFFIXES_BY_FILE_COMMAND
from latexprep.source_findings import MAX_LISTED_PATHS, GraphFindings, listed_names
from latexprep.source_resolution import (
    LookupScope,
    ProjectTree,
    Reference,
    candidate_paths,
    existing_files,
    resolve_reference,
)
from latexprep.tex_lexer import scan_source_commands


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

    def test_a_name_with_a_suffix_is_tried_as_written_except_by_include(self) -> None:
        self.assertEqual(
            candidate_paths("input", ["a.tex", "b/a.tex"], (".tex",)), ["a.tex", "b/a.tex"]
        )
        self.assertEqual(candidate_paths("include", ["a.tex"], (".tex",)), ["a.tex.tex"])

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
