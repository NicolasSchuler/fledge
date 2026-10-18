from __future__ import annotations

import re
import unittest
from pathlib import Path

from latexprep.source_graph import (
    INCOMPLETE_GRAPH_RULES,
    SECOND_ARGUMENT_FILE_COMMANDS,
    UNPARSED_SOURCE_RULES,
    file_argument_index,
)

SOURCE_GRAPH = Path(__file__).resolve().parents[1] / "src" / "latexprep" / "source_graph.py"

# Rules the graph emits that do not leave it incomplete: how a reference was written, which
# directories are searched, and the choice of the main file, which callers report themselves.
ADVISORY_RULES = frozenset(
    {
        "source.path_option",
        "source.path_identity",
        "source.dynamic_search_path",
        "source.scoped_search_path",
        "source.search_path_syntax",
        "source.main_invalid",
        "source.root_ambiguous",
        "source.root_missing",
    }
)


class GraphRuleSetTests(unittest.TestCase):
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
        emitted = set(re.findall(r'"(source\.[a-z_]+)"', SOURCE_GRAPH.read_text(encoding="utf-8")))
        self.assertEqual(emitted - INCOMPLETE_GRAPH_RULES - ADVISORY_RULES, set())
        self.assertTrue(INCOMPLETE_GRAPH_RULES.isdisjoint(ADVISORY_RULES))


class FileArgumentIndexTests(unittest.TestCase):
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
