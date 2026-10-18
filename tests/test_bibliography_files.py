"""Finding and reading .bib files within budgets, with every failure typed."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from latexprep.bibliography_files import (
    BibliographyBudget,
    ProblemKind,
    discover_bibliographies,
    read_declared_bibliographies,
)


class BibliographyFilesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, name: str, text: str | bytes = "@misc{k}\n") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode() if isinstance(text, str) else text)
        return path

    def read(self, *names: str, **budget):
        return read_declared_bibliographies(self.root, names, BibliographyBudget(**budget))

    def kinds(self, load) -> list[tuple[str | None, ProblemKind]]:
        return [(problem.name, problem.kind) for problem in load.problems]

    def test_files_are_read_in_name_order_and_an_invalid_one_is_still_returned(self):
        self.write("b.bib", "@misc{open")
        self.write("a.bib")
        load = self.read("b.bib", "a.bib")
        self.assertEqual(
            [(d.path, d.valid) for d in load.documents], [("a.bib", True), ("b.bib", False)]
        )
        self.assertEqual(load.problems, [])

    def test_each_unusable_file_is_a_typed_problem_and_the_rest_are_read(self):
        self.write("target.txt")
        (self.root / "linked.bib").symlink_to(self.root / "target.txt")
        self.write("real/inner.bib")
        (self.root / "dir-link").symlink_to(self.root / "real")
        self.write("legacy.bib", b"@misc{k,title={\xff}}")
        self.write("big.bib", "%" * 100)
        (self.root / "folder.bib").mkdir()
        load = self.read(
            "linked.bib",
            "dir-link/inner.bib",
            "legacy.bib",
            "big.bib",
            "folder.bib",
            "missing.bib",
            "real/inner.bib",
            max_file_bytes=64,
        )
        self.assertEqual(
            self.kinds(load),
            [
                ("big.bib", ProblemKind.TOO_LARGE),
                ("dir-link/inner.bib", ProblemKind.LINKED),
                ("folder.bib", ProblemKind.NOT_A_FILE),
                ("legacy.bib", ProblemKind.NOT_UTF8),
                ("linked.bib", ProblemKind.LINKED),
                ("missing.bib", ProblemKind.NOT_A_FILE),
            ],
        )
        self.assertEqual([d.path for d in load.documents], ["real/inner.bib"])

    def test_problems_read_as_a_phrase_after_the_file_name(self):
        self.write("legacy.bib", b"@misc{k,title={\xff}}")
        self.write("big.bib", "%" * 100)
        load = self.read("legacy.bib", "big.bib", max_file_bytes=64, max_total_bytes=1024 * 1024)
        self.assertEqual(
            [problem.message for problem in load.problems],
            [
                "big.bib exceeds the bibliography byte budget (0 MiB per file, 1 MiB in total)",
                "legacy.bib is not valid UTF-8 (first invalid byte at offset 15)",
            ],
        )

    def test_a_project_root_reached_through_a_link_is_still_readable(self):
        self.write("real/refs.bib")
        link = self.root / "project"
        link.symlink_to(self.root / "real")
        load = read_declared_bibliographies(link, ["refs.bib"], BibliographyBudget())
        self.assertEqual([d.path for d in load.documents], ["refs.bib"])

    def test_the_total_byte_budget_is_shared_by_all_files(self):
        self.write("a.bib", "%" * 60)
        self.write("b.bib", "%" * 60)
        load = self.read("a.bib", "b.bib", max_total_bytes=100)
        self.assertEqual([d.path for d in load.documents], ["a.bib"])
        self.assertEqual(self.kinds(load), [("b.bib", ProblemKind.TOO_LARGE)])

    def test_the_file_and_record_limits_stop_reading_with_a_problem_of_the_whole_set(self):
        for name in ("a.bib", "b.bib", "c.bib"):
            self.write(name)
        load = self.read("a.bib", "b.bib", "c.bib", max_files=2)
        self.assertEqual([d.path for d in load.documents], ["a.bib", "b.bib"])
        (problem,) = load.problems
        self.assertEqual((problem.kind, problem.name), (ProblemKind.TOO_MANY_FILES, None))
        self.assertEqual(problem.message, "Bibliography resource count exceeds 2")
        load = self.read("a.bib", "b.bib", "c.bib", max_records=2)
        self.assertEqual([d.path for d in load.documents], ["a.bib", "b.bib"])
        self.assertEqual(
            [problem.message for problem in load.problems], ["Bibliography entry count exceeds 2"]
        )

    def test_discovery_lists_bib_files_and_links_but_not_directories(self):
        self.write("a.bib")
        self.write("sub/b.BIB")
        self.write("notes.txt")
        (self.root / "folder.bib").mkdir()
        (self.root / "linked.bib").symlink_to(self.root / "a.bib")
        self.assertEqual(discover_bibliographies(self.root), ["a.bib", "linked.bib", "sub/b.BIB"])


if __name__ == "__main__":
    unittest.main()
