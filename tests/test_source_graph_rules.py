"""How source-graph rules are classified, and which argument of a file command names the file."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from latexprep.source_file_commands import SECOND_ARGUMENT_FILE_COMMANDS, file_argument_index
from latexprep.source_graph import INCOMPLETE_GRAPH_RULES, UNPARSED_SOURCE_RULES
from tests.support import SOURCE_GRAPH_ADVISORY_RULES

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "latexprep"
# The modules that make up the source graph and emit its findings.
GRAPH_MODULES = (
    "source_graph.py",
    "source_loading.py",
    "source_command_checks.py",
    "source_resolution.py",
)


class GraphRuleSetTests(unittest.TestCase):
    """The rule sets that classify source-graph findings as unparsed, incomplete or advisory."""

    def test_unparsed_files_are_a_subset_of_the_rules_that_leave_the_graph_incomplete(self) -> None:
        self.assertLess(UNPARSED_SOURCE_RULES, INCOMPLETE_GRAPH_RULES)
        self.assertEqual(
            UNPARSED_SOURCE_RULES,
            {
                "source.encoding",
                "source.size_limit",
                "source.argument_limit",
                "source.command_limit",
                "source.link",
            },
        )

    def test_a_symbolic_link_and_an_ambiguous_path_context_leave_the_graph_incomplete(self) -> None:
        self.assertLessEqual({"source.link", "source.context_ambiguity"}, INCOMPLETE_GRAPH_RULES)

    def test_every_rule_named_in_the_graph_is_classified_as_incomplete_or_advisory(
        self,
    ) -> None:
        emitted = {
            rule
            for module in GRAPH_MODULES
            for rule in re.findall(r'"(source\.[a-z_]+)"', (PACKAGE / module).read_text("utf-8"))
        }
        self.assertEqual(emitted - INCOMPLETE_GRAPH_RULES - SOURCE_GRAPH_ADVISORY_RULES, set())
        self.assertTrue(INCOMPLETE_GRAPH_RULES.isdisjoint(SOURCE_GRAPH_ADVISORY_RULES))


class FileArgumentIndexTests(unittest.TestCase):
    """Which argument of a file-reading command names the file."""

    def test_the_file_follows_the_directory_for_import_style_commands(self) -> None:
        for name in ("import", "subimport", "inputfrom", "DTLloaddb", "inputminted"):
            with self.subTest(name=name):
                self.assertEqual(file_argument_index(name), 1)

    def test_every_other_command_names_its_file_first(self) -> None:
        for name in ("input", "include", "includegraphics", "usepackage", "unknown"):
            with self.subTest(name=name):
                self.assertEqual(file_argument_index(name), 0)

    def test_the_index_is_one_exactly_for_the_second_argument_commands(self) -> None:
        for name in SECOND_ARGUMENT_FILE_COMMANDS:
            self.assertEqual(file_argument_index(name), 1)


if __name__ == "__main__":
    unittest.main()
