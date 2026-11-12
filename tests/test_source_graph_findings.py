"""Source-graph findings that the catalogue lists, each produced by a small project.

The graph reports what it cannot read or follow. Every such finding has a check code, and
check selection treats it as the rule's kind says: a gap in the graph is mandatory, an
advisory about how a reference was written can be ignored.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from latexprep.check_policy import selected_findings
from latexprep.check_selection import CheckSelection
from latexprep.findings import Finding
from latexprep.flat_layout import plan_flatten
from latexprep.rule_definition import Rule
from latexprep.rules import MANDATORY_CODES, RULES
from latexprep.source_analysis import analyze_sources
from tests.support import SOURCE_GRAPH_ADVISORY_RULES, ProjectTestCase

DOCUMENT = "\\documentclass{article}\n"
# The source-graph rules whose findings leave the main file selected, or are errors by
# themselves: mandatory although the graph classifies them as advisory about completeness.
SELECTION_ERRORS = frozenset(
    {"source.main_invalid", "source.root_ambiguous", "source.root_missing"}
)


class SourceGraphFindingTests(ProjectTestCase):
    """Each source-graph finding in the catalogue, produced by a small project."""

    def report(self, rule: str, main: str | None = "main.tex") -> Finding:
        """Return the one finding of ``rule`` that the analysis of the project reports.

        The test fails unless there is exactly one such finding and it has a check code.

        Args:
            rule: The rule whose finding is expected.
            main: The main file to analyse, or ``None`` to select the single document root.

        Returns:
            The finding.
        """
        found = [item for item in analyze_sources(self.root, main).findings if item.rule == rule]
        self.assertEqual(len(found), 1, found)
        self.assertIsNotNone(found[0].code, rule)
        return found[0]

    def test_an_unclosed_literal_region_is_reported_with_its_line(self) -> None:
        self.write("main.tex", DOCUMENT + "\\verb|never closed\n")
        finding = self.report("source.literal_unclosed")
        self.assertEqual(
            (finding.path, finding.line, finding.status), ("main.tex", 2, "inconclusive")
        )

    def test_a_comment_inside_a_path_argument_is_reported(self) -> None:
        self.write("main.tex", DOCUMENT + "\\input{bo% hidden\ndy}\n")
        self.write("body.tex", "Body.\n")
        self.assertEqual(self.report("source.path_literal_context").line, 2)

    def test_a_file_command_inside_a_group_or_macro_is_reported(self) -> None:
        self.write("body.tex", "Body.\n")
        for text in ("{\\input{body}}\n", "\\newcommand{\\chapterbody}{\\input{body}}\n"):
            with self.subTest(text=text):
                self.write("main.tex", DOCUMENT + text)
                self.assertEqual(self.report("source.nested_file_command").line, 2)

    def test_the_subfiles_v1_preamble_mode_is_reported_as_unsupported_structure(self) -> None:
        self.write("main.tex", DOCUMENT + "\\usepackage[v1]{subfiles}\n")
        self.assertEqual(self.report("source.subfile_structure").line, 2)

    def test_font_options_outside_the_supported_set_are_reported(self) -> None:
        self.write("body.ttf", b"font")
        self.write("main.tex", DOCUMENT + "\\setmainfont{body.ttf}[Contextuals=Alternate]\n")
        self.assertEqual(self.report("source.font_options").line, 2)

    def test_a_declared_name_with_a_directory_is_an_advisory(self) -> None:
        self.write("main.tex", DOCUMENT + "\\ProvidesPackage{styles/name}\n")
        finding = self.report("source.path_identity")
        self.assertEqual(finding.line, 2)
        self.assertNotIn(finding.code, MANDATORY_CODES)

    def test_graphics_search_paths_outside_the_supported_form_are_advisories(self) -> None:
        for rule, text in (
            ("source.scoped_search_path", "{\\graphicspath{{figures/}}}\n"),
            ("source.dynamic_search_path", "\\graphicspath{\\figures}\n"),
            ("source.search_path_syntax", "\\graphicspath{{figures}}\n"),
        ):
            with self.subTest(rule=rule):
                self.write("main.tex", DOCUMENT + text)
                finding = self.report(rule)
                self.assertEqual(finding.line, 2)
                self.assertNotIn(finding.code, MANDATORY_CODES)

    def test_a_cycle_of_inputs_is_reported_once(self) -> None:
        self.write("main.tex", DOCUMENT + "\\input{first}\n")
        self.write("first.tex", "\\input{second}\n")
        self.write("second.tex", "\\input{first}\n")
        cycle = self.report("source.input_cycle")
        self.assertEqual(
            cycle.details["cycle"], ["main.tex", "first.tex", "second.tex", "first.tex"]
        )

    def test_a_traversal_over_its_command_budget_is_reported(self) -> None:
        self.write("main.tex", DOCUMENT + "\\input{body}\n" * 3)
        self.write("body.tex", "Body.\n")
        with patch("latexprep.source_graph.MAX_COMMANDS", 2):
            self.assertEqual(self.report("source.expansion_limit").path, "main.tex")

    def test_a_project_without_a_document_root_reports_it(self) -> None:
        self.write("notes.tex", "Notes without a document class.\n")
        finding = self.report("source.root_missing", main=None)
        self.assertEqual((finding.severity, finding.status), ("error", "inconclusive"))

    def test_an_unvisited_source_with_file_references_blocks_a_flat_layout(self) -> None:
        self.write("main.tex", DOCUMENT)
        self.write("orphan.tex", "\\input{other}\n")
        self.write("other.tex", "Other.\n")
        found = [
            item
            for item in plan_flatten(self.root, "main.tex").findings
            if item.rule == "flatten.unresolved_context"
        ]
        self.assertEqual([item.path for item in found], ["orphan.tex"])
        self.assertEqual((found[0].severity, found[0].status), ("error", "inconclusive"))
        self.assertIn("PKG008", MANDATORY_CODES)


class SourceGraphSelectionTests(unittest.TestCase):
    """Check selection for the source-graph rules, whose codes start with ``TEX3``.

    Only advisories about how a reference was written can be ignored; ignoring the whole family
    keeps every gap in the graph.
    """

    def graph_rules(self) -> list[Rule]:
        """Return the catalogued source-graph rules: those whose code starts with ``TEX3``."""
        return [rule for rule in RULES if rule.code.startswith("TEX3")]

    def test_only_advisories_about_how_a_reference_was_written_are_selectable(self) -> None:
        optional = {rule.name for rule in self.graph_rules() if not rule.mandatory}
        self.assertEqual(optional, SOURCE_GRAPH_ADVISORY_RULES - SELECTION_ERRORS)

    def test_ignoring_the_family_keeps_every_gap_in_the_graph(self) -> None:
        findings = [Finding(rule.name, rule.title) for rule in self.graph_rules()]
        kept = {item.rule for item in selected_findings(findings, CheckSelection(ignore=("TEX",)))}
        self.assertEqual(kept, {rule.name for rule in self.graph_rules() if rule.mandatory})
        self.assertLessEqual(SELECTION_ERRORS | {"source.encoding", "source.input_cycle"}, kept)
        self.assertFalse(kept & SOURCE_GRAPH_ADVISORY_RULES - SELECTION_ERRORS)


if __name__ == "__main__":
    unittest.main()


class FontLookupTests(ProjectTestCase):
    """Font commands without a ``Path`` look fonts up outside the project unless it has them.

    A family name and a font file of the TeX distribution are neither dependencies nor gaps
    in the graph, so they leave the source and citation checks conclusive; a project font
    file and a ``Path`` stay project lookups.
    """

    def findings_and_dependencies(self, command: str) -> tuple[set[str], set[str]]:
        """Analyse a document whose preamble holds ``command``.

        Args:
            command: The font command, on the second line of ``main.tex``.

        Returns:
            The rules of the findings and the project dependencies of the analysis.
        """
        self.write("main.tex", DOCUMENT + "\\usepackage{fontspec}\n" + command + "\n")
        analysis = analyze_sources(self.root, "main.tex")
        return {item.rule for item in analysis.findings}, set(analysis.dependencies)

    def test_family_names_and_distribution_font_files_are_outside_the_project(self) -> None:
        for command in (
            "\\setmainfont{Times New Roman}",
            "\\setsansfont{Helvetica Neue}[Scale=MatchLowercase, BoldFont={Helvetica Neue Bold}]",
            "\\setmonofont[Contextuals=Alternate]{Fira Code}",
            "\\setmainfont{texgyretermes-regular.otf}",
            "\\fontspec{texgyreheros}[Extension=.otf, UprightFont=*-regular]",
        ):
            with self.subTest(command=command):
                rules, dependencies = self.findings_and_dependencies(command)
                self.assertEqual(rules, set())
                self.assertEqual(dependencies, {"main.tex"})

    def test_project_fonts_stay_project_lookups(self) -> None:
        self.write("fonts/Body-Regular.otf", b"font")
        self.write("Local.ttf", b"font")
        rules, dependencies = self.findings_and_dependencies("\\setmainfont{Local.ttf}")
        self.assertEqual((rules, dependencies), (set(), {"main.tex", "Local.ttf"}))
        rules, dependencies = self.findings_and_dependencies(
            "\\setmainfont{Body-Regular.otf}[Path=fonts/]"
        )
        self.assertEqual((rules, dependencies), (set(), {"main.tex", "fonts/Body-Regular.otf"}))
        # A family whose files the project holds may load them, so the graph stays uncertain.
        self.write("Body-Bold.otf", b"font")
        rules, _ = self.findings_and_dependencies("\\setmainfont{Body}[Extension=.otf]")
        self.assertIn("source.unsupported_command", rules)
        rules, _ = self.findings_and_dependencies("\\setmainfont{Body}[Path=fonts/]")
        self.assertIn("source.unsupported_command", rules)
        rules, _ = self.findings_and_dependencies("\\setmainfont{fonts/Missing.otf}")
        self.assertIn("source.missing_dependency", rules)
