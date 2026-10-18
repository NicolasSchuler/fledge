from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from latexprep.config import Settings
from latexprep.errors import PreparationError
from latexprep.findings import Finding
from latexprep.staging import apply_contents, flatten_sources, stage_document, transform_sources
from latexprep.workflow_options import DocumentOptions


class StagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def project(self) -> Path:
        snapshot = self.base / "snapshot"
        (snapshot / "paper").mkdir(parents=True)
        (snapshot / "paper" / "main.tex").write_text("\\documentclass{article}\n")
        return snapshot

    def test_an_unsafe_document_input_is_named_with_the_way_out(self) -> None:
        snapshot = self.project()
        (snapshot / "paper" / "figure.pdf").symlink_to(self.base)
        document = DocumentOptions("paper", "main.tex", "paper", ("main.tex", "*.pdf"))
        with self.assertRaises(PreparationError) as raised:
            stage_document(snapshot, self.base / "selected", document)
        self.assertIn("'figure.pdf'", str(raised.exception))
        self.assertIn("link", str(raised.exception))
        self.assertFalse((self.base / "selected").exists())

    def test_contents_are_written_only_after_every_precondition_holds(self) -> None:
        root = self.base / "tree"
        root.mkdir()
        (root / "a.tex").write_bytes(b"old a")
        (root / "b.tex").write_bytes(b"old b")
        contents = {"a.tex": b"new a", "b.tex": b"new b"}
        with self.assertRaisesRegex(PreparationError, "original bytes"):
            apply_contents(root, contents, originals={"a.tex": b"old a"})
        with self.assertRaisesRegex(PreparationError, "changed after its edit was planned: b.tex"):
            apply_contents(root, contents, originals={"a.tex": b"old a", "b.tex": b"stale"})
        self.assertEqual((root / "a.tex").read_bytes(), b"old a")
        apply_contents(root, contents, originals={"a.tex": b"old a", "b.tex": b"old b"})
        self.assertEqual((root / "b.tex").read_bytes(), b"new b")

    def test_a_blocked_edit_is_reported_but_not_applied_and_later_edits_are_not_planned(
        self,
    ) -> None:
        snapshot = self.project()
        applied: list[str] = []
        blocking = Finding("source.merge_inputs", "blocked", severity="error")

        def edit(name: str, findings: list[Finding]):
            def plan(*_args):
                return SimpleNamespace(
                    changes=[], findings=findings, apply=lambda: applied.append(name)
                )

            return plan

        with (
            patch("latexprep.staging._metadata_edit", edit("metadata", [])),
            patch("latexprep.staging._bibliography_edit", edit("bibliography", [blocking])),
            patch("latexprep.staging._source_edit", side_effect=AssertionError("planned")),
        ):
            outcome = transform_sources(
                snapshot, self.base / "work", "paper/main.tex", Settings(), set(), set()
            )
        self.assertEqual(applied, ["metadata"])
        self.assertEqual(outcome.findings, [blocking])

    def test_every_edit_applies_in_order_when_none_blocks(self) -> None:
        snapshot = self.project()
        applied: list[str] = []

        def edit(name: str):
            return lambda *_args: SimpleNamespace(
                changes=[], findings=[], apply=lambda: applied.append(name)
            )

        with (
            patch("latexprep.staging._metadata_edit", edit("metadata")),
            patch("latexprep.staging._bibliography_edit", edit("bibliography")),
            patch("latexprep.staging._doi_edit", edit("doi")),
            patch("latexprep.staging._source_edit", edit("source")),
        ):
            transform_sources(
                snapshot, self.base / "work", "paper/main.tex", Settings(), set(), set()
            )
            self.assertEqual(applied, ["metadata", "bibliography", "source"])
            applied.clear()
            settings = Settings(normalize_doi=True)
            transform_sources(
                snapshot, self.base / "work2", "paper/main.tex", settings, set(), set()
            )
        self.assertEqual(applied, ["metadata", "bibliography", "doi", "source"])

    def test_preserve_layout_ships_the_selected_sources_as_they_are(self) -> None:
        shipment = self.base / "shipment"
        outcome = flatten_sources(shipment, self.base / "work", "main.tex", Settings())
        self.assertEqual(
            (outcome.root, outcome.main, outcome.path_mapping), (shipment, "main.tex", {})
        )


if __name__ == "__main__":
    unittest.main()
