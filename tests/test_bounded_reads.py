"""Every reader of project, build and bibliography files uses the shared bounded read.

Each caller of ``file_safety.read_regular_file`` and ``read_file_prefix`` keeps its own
wording for a refusal; these tests pin what the shared reader changed for them: a link
or special file is refused at the read itself, a limit holds even if a file is larger
than a check saw, and no message names a path.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar
from unittest.mock import patch

from latexprep.bibliography_controls import read_bibliography_evidence
from latexprep.bibtex_syntax import BibFileReader, describe_read_failure
from latexprep.build_log import read_bounded
from latexprep.source_analysis import discover_roots
from latexprep.source_graph import SourceGraph

T = TypeVar("T")
ROOT_DOCUMENT = "\\documentclass{article}\n\\begin{document}Hi\\end{document}\n"


def returns_promptly(action: Callable[[], T]) -> T:
    """Run ``action`` and fail the test, rather than hang it, if it blocks on a pipe."""
    outcome: list[T] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            outcome.append(action())
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(10)
    if worker.is_alive():
        raise AssertionError("The read blocked instead of refusing a special file")
    if errors:
        raise errors[0]
    return outcome[0]


class ReadersTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def write(self, name: str, content: bytes | str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)
        return path


class BibFileReaderTests(ReadersTestCase):
    def test_a_file_within_the_allowance_is_parsed_and_counted(self) -> None:
        path = self.write("refs.bib", "@article{a, title={T}}\n")
        reader = BibFileReader(max_file_bytes=1024, max_total_bytes=1024)
        document = reader.read(path, "refs.bib")
        self.assertIsNotNone(document)
        self.assertEqual(reader.total_bytes, path.stat().st_size)

    def test_a_file_beyond_the_allowance_reads_as_none(self) -> None:
        path = self.write("refs.bib", "@article{a, title={T}}\n" * 10)
        self.assertIsNone(BibFileReader(max_file_bytes=16).read(path, "refs.bib"))
        reader = BibFileReader(max_file_bytes=1024, max_total_bytes=path.stat().st_size)
        self.assertIsNotNone(reader.read(path, "refs.bib"))
        self.assertIsNone(reader.read(path, "refs.bib"))

    def test_a_link_is_refused_at_the_read_with_a_path_free_reason(self) -> None:
        target = self.write("target.bib", "@article{a, title={T}}\n")
        link = self.root / "link.bib"
        link.symlink_to(target)
        with self.assertRaises(OSError) as raised:
            BibFileReader().read(link, "link.bib")
        reason = describe_read_failure(raised.exception)
        self.assertIn("could not be read", reason)
        self.assertNotIn(str(self.root), reason)

    def test_a_pipe_is_refused_instead_of_blocking(self) -> None:
        os.mkfifo(self.root / "pipe.bib")
        with self.assertRaises(OSError):
            returns_promptly(lambda: BibFileReader().read(self.root / "pipe.bib", "pipe.bib"))


class BoundedLogTests(ReadersTestCase):
    def test_a_log_within_the_limit_is_complete(self) -> None:
        path = self.write("main.log", "! Undefined control sequence.\n")
        self.assertEqual(read_bounded(path, 1024), ("! Undefined control sequence.\n", True))

    def test_a_log_at_the_limit_is_complete_and_one_byte_more_is_not(self) -> None:
        path = self.write("main.log", "abcd")
        self.assertEqual(read_bounded(path, 4), ("abcd", True))
        self.assertEqual(read_bounded(path, 3), ("abc", False))

    def test_invalid_utf8_is_replaced_rather_than_refused(self) -> None:
        path = self.write("main.log", b"ok \xff\xfe\n")
        text, complete = read_bounded(path, 1024)
        self.assertTrue(complete)
        self.assertTrue(text.startswith("ok "))

    def test_a_missing_linked_or_special_log_is_empty_and_incomplete(self) -> None:
        target = self.write("target.log", "real log")
        (self.root / "link.log").symlink_to(target)
        os.mkfifo(self.root / "pipe.log")
        for name in ("absent.log", "link.log", "pipe.log"):
            with self.subTest(name=name):
                self.assertEqual(
                    returns_promptly(lambda name=name: read_bounded(self.root / name, 1024)),
                    ("", False),
                )


class SourceDiscoveryTests(ReadersTestCase):
    def test_unreadable_linked_special_and_oversized_sources_are_skipped(self) -> None:
        self.write("main.tex", ROOT_DOCUMENT)
        target = self.write("elsewhere.txt", ROOT_DOCUMENT)
        (self.root / "linked.tex").symlink_to(target)
        os.mkfifo(self.root / "pipe.tex")
        self.write("binary.tex", b"\xff\xfe\\documentclass{article}")
        roots = returns_promptly(lambda: discover_roots(self.root))
        self.assertEqual(roots, ["main.tex"])


class SourceGraphLoadTests(ReadersTestCase):
    def build(self, extra: str) -> SourceGraph:
        self.write(
            "main.tex", f"\\documentclass{{article}}\n\\begin{{document}}{extra}\\end{{document}}\n"
        )
        return SourceGraph.build(self.root, "main.tex")

    def test_a_linked_source_is_reported_as_a_link(self) -> None:
        target = self.write("elsewhere.txt", "text")
        (self.root / "linked.tex").symlink_to(target)
        graph = self.build("\\input{linked}")
        self.assertIn("source.link", {finding.rule for finding in graph.findings})

    def test_a_source_replaced_by_a_directory_is_reported_unreadable_without_a_path(self) -> None:
        (self.root / "part.tex").mkdir()
        # A directory is not listed as a project file; load it directly, as a replacement
        # that appears between listing and reading would be.
        graph = self.build("")
        graph.files["part.tex"] = self.root / "part.tex"
        self.assertIsNone(graph._load("part.tex"))
        messages = [finding.message for finding in graph.findings]
        self.assertTrue(any("could not be read" in message for message in messages), messages)
        self.assertFalse(any(str(self.root) in message for message in messages))

    def test_a_source_over_the_limit_is_reported_as_too_large(self) -> None:
        graph = self.build("")
        self.write("big.tex", b"x" * 64)
        graph.files["big.tex"] = self.root / "big.tex"
        with patch("latexprep.source_loading.MAX_SOURCE_BYTES", 16):
            self.assertIsNone(graph._load("big.tex"))
        self.assertIn("source.size_limit", {finding.rule for finding in graph.findings})


class ControlFileEvidenceTests(ReadersTestCase):
    BCF = '<?xml version="1.0"?><bcf:controlfile xmlns:bcf="https://sourceforge.net/projects/biblatex"/>'

    def test_control_files_are_read_within_the_combined_limit(self) -> None:
        self.write("main.bcf", self.BCF)
        evidence = read_bibliography_evidence(self.root, 4096)
        self.assertEqual(evidence.observed_backends, ("biber",))
        self.assertEqual(evidence.uncertainty, ())

    def test_one_file_over_the_limit_names_the_file_limit(self) -> None:
        self.write("main.bcf", self.BCF)
        evidence = read_bibliography_evidence(self.root, 8)
        self.assertEqual(evidence.observed_backends, ())
        self.assertEqual(
            evidence.uncertainty, ("main.bcf: control file exceeds the diagnostic size limit",)
        )

    def test_a_second_file_that_exhausts_the_total_names_the_combined_limit(self) -> None:
        self.write("main.bcf", self.BCF)
        self.write("main.aux", "\\bibdata{refs}\n")
        evidence = read_bibliography_evidence(self.root, len(self.BCF) + 4)
        self.assertEqual(evidence.observed_backends, ("biber",))
        self.assertEqual(
            evidence.uncertainty,
            ("main.aux: combined control evidence exceeds the diagnostic size limit",),
        )

    def test_a_linked_control_file_is_not_an_owned_regular_file(self) -> None:
        target = self.write("elsewhere.txt", self.BCF)
        (self.root / "main.bcf").symlink_to(target)
        evidence = read_bibliography_evidence(self.root, 4096)
        self.assertEqual(evidence.observed_backends, ())
        self.assertEqual(
            evidence.uncertainty, ("main.bcf: control file is not an owned regular file",)
        )


if __name__ == "__main__":
    unittest.main()
