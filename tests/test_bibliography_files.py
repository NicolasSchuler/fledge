"""Finding and reading .bib files within budgets, with every failure typed.

The checks of one job that read the same project share each parse through the project's
analysis (``AnalysedSource.bib_files``); the last classes show that every file is parsed
once, that each read keeps its own budget, and that whole jobs parse once per tree.
"""

from __future__ import annotations

import threading
import time
import unittest
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import latexprep.bibtex_syntax as bibtex_syntax
from latexprep.analysed_source import AnalysedSource
from latexprep.bibliography_checks import check_bibliography_coverage_and_fields
from latexprep.bibliography_files import (
    BibliographyBudget,
    ProblemKind,
    discover_bibliographies,
    read_declared_bibliographies,
)
from latexprep.bibliography_integrity import check_bibliography_integrity
from latexprep.bibtex_syntax import ParsedBibFiles
from latexprep.cancellation import AnalysisCancelled
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.job_context import JobRequest
from latexprep.online_inputs import collect_inputs
from latexprep.online_options import OnlineOptions
from tests.support import AsyncProjectTestCase, ProjectTestCase

_PAPER = {
    "main.tex": (
        "\\documentclass{article}\n\\begin{document}\n\\cite{a}\\cite{missing}\n"
        "\\bibliography{one,two}\n\\end{document}\n"
    ),
    "one.bib": (
        "@article{a, title={A reference}, author={Smith, Ada}, year={2024}, doi={10.1234/a}}\n"
        "@article{a, title={Repeated key}}\n"
    ),
    "two.bib": "@misc{b, title={Spare}, url={https://example.org/b}}\n",
}


@contextmanager
def counted_parses() -> Iterator[Counter[str]]:
    """Count the parses of each .bib file, by the name each is parsed under.

    Yields:
        The counter, which grows while the context is open.
    """
    parses: Counter[str] = Counter()
    original = bibtex_syntax.parse_bibtex

    def counting(name: str, text: str, *, bom: bool = False) -> bibtex_syntax.BibDocument:
        """Count one parse of ``name`` and parse it as usual."""
        parses[name] += 1
        return original(name, text, bom=bom)

    with patch.object(bibtex_syntax, "parse_bibtex", counting):
        yield parses


class BibliographyFilesTests(ProjectTestCase):
    """Tests of discovering and reading ``.bib`` files within budgets, with typed problems."""

    def write(self, name: str, content: str | bytes = "@misc{k}\n") -> Path:
        """Write a file below ``root``, by default holding one ``@misc`` entry."""
        return super().write(name, content)

    def read(self, *names: str, **budget):
        """Read the declared bibliographies below ``root``.

        Args:
            *names: The declared bibliography files.
            **budget: The fields of the ``BibliographyBudget``.

        Returns:
            What ``read_declared_bibliographies`` loaded.
        """
        return read_declared_bibliographies(self.root, names, BibliographyBudget(**budget))

    def kinds(self, load) -> list[tuple[str | None, ProblemKind]]:
        """Return the file name and the kind of every problem of ``load``."""
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

    def test_bytes_that_are_not_utf8_outside_every_record_are_read_as_bibtex_does(self):
        # TeX Live's IEEEabrv.bib has one Latin-1 byte in its opening comment.
        self.write(
            "abbrev.bib",
            b"% Nicol\xe1s Barabino\n@string{J = {Journal}}\n"
            b"@comment{caf\xe9}\nbetween \xff entries\n@misc{k, title = {T}}\n",
        )
        load = self.read("abbrev.bib")
        self.assertEqual(load.problems, [])
        [document] = load.documents
        self.assertTrue(document.valid)
        self.assertEqual(document.foreign_bytes, 3)
        self.assertEqual([entry.key for entry in document.entries], ["k"])
        self.assertEqual(document.line(document.entries[0].start), 5)

    def test_a_byte_that_is_not_utf8_inside_a_record_or_unparsed_text_is_refused(self):
        cases = {
            "value.bib": b"@misc{k, title = {caf\xe9}}\n",
            "key.bib": b"@misc{k\xe9, title = {T}}\n",
            "string.bib": b"@string{J\xe9 = {Journal}}\n",
            "closing.bib": b"@misc{k, title = {T} \xe9}\n",
            "after-error.bib": b"@misc{k, title = {T}\n\xe9\n",
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                self.write(name, content)
                self.assertEqual(self.kinds(self.read(name)), [(name, ProblemKind.NOT_UTF8)])

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


class SharedParseTests(ProjectTestCase):
    """The checks that share a project's analysis parse each of its .bib files once."""

    def checks(self, analysed: AnalysedSource | None):
        """Run the integrity, coverage and online input checks of ``main.tex``.

        Args:
            analysed: The analysis they share, or ``None`` for each to read on its own.

        Returns:
            The findings of the two offline checks and the online inputs.
        """
        online = OnlineOptions(online=True, online_metadata=True)
        return (
            check_bibliography_integrity(self.root, "main.tex", analysed=analysed),
            check_bibliography_coverage_and_fields(self.root, "main.tex", analysed=analysed),
            collect_inputs(self.root, "main.tex", online, analysed=analysed),
        )

    def test_checks_sharing_an_analysis_parse_each_file_once(self) -> None:
        for name, content in _PAPER.items():
            self.write(name, content)
        with counted_parses() as separate:
            alone = self.checks(None)
        with counted_parses() as shared:
            together = self.checks(AnalysedSource(self.root, "main.tex"))
        self.assertEqual(separate, {"one.bib": 3, "two.bib": 3})
        self.assertEqual(shared, {"one.bib": 1, "two.bib": 1})
        self.assertEqual(together, alone)
        self.assertTrue(alone[0] and alone[1] and alone[2].references)

    def test_each_read_keeps_its_own_budget_over_a_shared_parse(self) -> None:
        self.write("a.bib", "@misc{a}\n" + "%" * 60)
        self.write("b.bib", "@misc{b}\n" + "%" * 60)
        shared = ParsedBibFiles()

        def read(**budget: int | None):
            """Read both files through ``shared`` within ``budget``."""
            load = read_declared_bibliographies(
                self.root, ["a.bib", "b.bib"], BibliographyBudget(**budget), shared=shared
            )
            return [document.path for document in load.documents], [
                (problem.name, problem.kind) for problem in load.problems
            ]

        with counted_parses() as parses:
            self.assertEqual(
                read(max_file_bytes=32),
                ([], [("a.bib", ProblemKind.TOO_LARGE), ("b.bib", ProblemKind.TOO_LARGE)]),
            )
            self.assertEqual(read(), (["a.bib", "b.bib"], []))
            self.assertEqual(
                read(max_file_bytes=32),
                ([], [("a.bib", ProblemKind.TOO_LARGE), ("b.bib", ProblemKind.TOO_LARGE)]),
            )
            self.assertEqual(
                read(max_total_bytes=100), (["a.bib"], [("b.bib", ProblemKind.TOO_LARGE)])
            )
        self.assertEqual(parses, {"a.bib": 1, "b.bib": 1})

    def test_threads_that_read_one_file_at_once_wait_for_one_parse(self) -> None:
        self.write("refs.bib")
        shared = ParsedBibFiles()
        original = bibtex_syntax.parse_bibtex
        calls: list[str] = []
        lock = threading.Lock()

        def slow(name: str, text: str, *, bom: bool = False) -> bibtex_syntax.BibDocument:
            """Parse ``name`` slowly, so that the other threads ask while it runs."""
            with lock:
                calls.append(name)
            time.sleep(0.05)
            return original(name, text, bom=bom)

        def read(_: int):
            """Read the file through ``shared`` and return the document."""
            load = read_declared_bibliographies(
                self.root, ["refs.bib"], BibliographyBudget(), shared=shared
            )
            return load.documents[0]

        with patch.object(bibtex_syntax, "parse_bibtex", slow), ThreadPoolExecutor(8) as pool:
            documents = list(pool.map(read, range(8)))
        self.assertEqual(calls, ["refs.bib"])
        self.assertTrue(all(document is documents[0] for document in documents))

    def test_a_parse_that_was_cancelled_is_not_remembered(self) -> None:
        self.write("refs.bib")
        shared = ParsedBibFiles()
        original = bibtex_syntax.parse_bibtex
        outcomes = iter([AnalysisCancelled("stop"), None])

        def cancelled_once(name: str, text: str, *, bom: bool = False):
            """Raise the first time, as a cancelled job would, then parse."""
            if (error := next(outcomes)) is not None:
                raise error
            return original(name, text, bom=bom)

        with patch.object(bibtex_syntax, "parse_bibtex", cancelled_once):
            with self.assertRaises(AnalysisCancelled):
                read_declared_bibliographies(
                    self.root, ["refs.bib"], BibliographyBudget(), shared=shared
                )
            load = read_declared_bibliographies(
                self.root, ["refs.bib"], BibliographyBudget(), shared=shared
            )
        self.assertEqual([document.path for document in load.documents], ["refs.bib"])


class JobParseTests(AsyncProjectTestCase):
    """Whole jobs parse each .bib file once for every tree they check."""

    async def test_the_bibliography_and_inspect_commands_parse_each_file_once(self) -> None:
        project = self.write_project(_PAPER)
        for command in ("bib", "inspect"):
            with self.subTest(command=command), counted_parses() as parses:
                report = await run_job(JobRequest(command, project, Settings(main="main.tex")))
                self.assertEqual(parses, {"one.bib": 1, "two.bib": 1})
                rules = {item.rule for item in report.findings}
                self.assertIn("bibliography.duplicate_key", rules)
                self.assertIn("bibliography.citation_coverage", rules)


if __name__ == "__main__":
    unittest.main()
