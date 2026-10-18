"""Selection provenance and package measurements use real files and ZIP entries."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from latexprep.package_inputs import select_package_inputs
from latexprep.package_inventory import assemble_package_inventory
from latexprep.project import create_archive
from latexprep.report import Change, ChangeKind


class PackageInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()

    def write(self, name: str, data: str | bytes, *, root: Path | None = None) -> None:
        path = (root or self.source) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)

    def select(self, observed: set[str], explicit: set[str] | None = None, generated=()):
        changes, findings = select_package_inputs(
            self.source,
            self.base / "selected",
            "main.tex",
            observed,
            explicit or set(),
            set(generated),
        )
        finding = next(item for item in findings if item.status == "passed")
        return changes, finding.details["inventory"]

    def test_nested_selection_distinguishes_literal_recorded_explicit_generated_and_debris(self):
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\input{parts/body}\n"
            "\\begin{document}Body.\\end{document}\n",
        )
        self.write("parts/body.tex", "Body.\n")
        self.write("fonts/runtime.enc", b"recorded font input\n")
        self.write("deliverables/cover.pdf", b"%PDF-cover\n")
        self.write("main.bbl", "\\begin{thebibliography}{1}\\end{thebibliography}\n")
        self.write("main.aux", "rebuildable\n")
        self.write("draft.tex.bak", "backup\n")
        self.write("unused.csv", "a,b\n")
        observed = {"main.tex", "parts/body.tex", "fonts/runtime.enc"}
        changes, selection = self.select(observed, {"deliverables/cover.pdf"}, {"main.bbl"})
        rows = {item["source"]: item for item in selection}
        self.assertEqual(
            rows["parts/body.tex"]["reasons"],
            ["Supported literal dependency.", "Recorded build input."],
        )
        self.assertEqual(rows["fonts/runtime.enc"]["reasons"], ["Recorded build input."])
        self.assertEqual(rows["deliverables/cover.pdf"]["reasons"], ["Explicitly retained file."])
        self.assertEqual(
            rows["main.bbl"]["reasons"], ["Generated bibliography consumed by the build."]
        )
        self.assertIn("auxiliary", rows["main.aux"]["reasons"][0])
        self.assertEqual(rows["draft.tex.bak"]["reasons"], ["Editor backup or swap file."])
        self.assertIn("Not needed", rows["unused.csv"]["reasons"][0])
        self.assertEqual(
            {change.path for change in changes}, {"main.aux", "draft.tex.bak", "unused.csv"}
        )
        for name, row in rows.items():
            self.assertEqual(row["path"], name)
            self.assertEqual(row["size_bytes"], (self.source / name).stat().st_size)
            self.assertIsNone(row["compressed_bytes"])

    def test_ambiguous_literal_graph_does_not_invent_literal_dependency_reason(self):
        self.write("main.tex", "\\newcommand{\\chosen}{body}\n\\input{\\chosen}\n")
        self.write("body.tex", "Observed dynamically.\n")
        _, selection = self.select({"main.tex", "body.tex"})
        rows = {item["source"]: item for item in selection}
        self.assertEqual(rows["body.tex"]["reasons"], ["Recorded build input."])

    def test_inventory_preserves_selection_handling_of_unselected_links(self):
        self.write("main.tex", "Body.\n")
        outside = self.base / "outside.txt"
        outside.write_bytes(b"Not a source input.")
        (self.source / "unused.txt").symlink_to(outside)
        _, selection = self.select({"main.tex"})
        row = next(item for item in selection if item["source"] == "unused.txt")
        self.assertEqual(row["status"], "excluded")
        self.assertIsNone(row["size_bytes"])
        self.assertFalse((self.base / "selected" / "unused.txt").exists())

    def test_failed_selection_has_no_inventory_or_partial_selected_tree(self):
        self.write("main.tex", "Body.\n")
        destination = self.base / "selected"
        changes, findings = select_package_inputs(self.source, destination, "main.tex", None, set())
        self.assertEqual(changes, [])
        self.assertFalse(destination.exists())
        self.assertTrue(all("inventory" not in item.details for item in findings))

    def test_flat_mapping_keeps_collision_renames_and_excluded_original_identity(self):
        self.write("main.tex", "\\input{first/body}\n\\input{second/body}\n")
        self.write("first/body.tex", "First.\n")
        self.write("second/body.tex", "Second.\n")
        self.write("first_body.tex", "Unrelated original with final basename.\n")
        changes, selection = self.select({"main.tex", "first/body.tex", "second/body.tex"})
        final = self.base / "flat"
        mapping = {
            "main.tex": "main.tex",
            "first/body.tex": "first_body.tex",
            "second/body.tex": "second_body.tex",
        }
        self.write("main.tex", "rewritten source\n", root=final)
        self.write("first_body.tex", "First.\n", root=final)
        self.write("second_body.tex", "Second.\n", root=final)
        package = assemble_package_inventory(
            selections=[selection],
            changes=changes,
            final_root=final,
            path_mapping=mapping,
            status="prepared",
        )
        rows = {item["source"]: item for item in package["files"]}
        self.assertEqual(rows["first/body.tex"]["path"], "first_body.tex")
        self.assertEqual(rows["second/body.tex"]["path"], "second_body.tex")
        self.assertEqual(rows["first_body.tex"]["status"], "excluded")
        self.assertEqual(rows["first_body.tex"]["size_bytes"], 40)
        self.assertEqual(rows["main.tex"]["size_bytes"], len(b"rewritten source\n"))
        self.assertEqual(sum(item["status"] == "included" for item in rows.values()), 3)

    def test_consumed_input_and_import_cleanup_keep_sizes_and_explicit_exclusion_reasons(self):
        self.write("main.tex", "\\input{parts/body}\n")
        self.write("parts/body.tex", "Merged body.\n")
        self.write(".DS_Store", b"metadata")
        _, selection = self.select({"main.tex", "parts/body.tex"})
        final = self.base / "final"
        self.write("main.tex", "Merged body.\n", root=final)
        package = assemble_package_inventory(
            selections=[selection],
            source_root=self.source,
            final_root=final,
            changes=[
                Change(
                    "parts/body.tex",
                    kind=ChangeKind.REMOVE,
                    reason="Input contents were merged into the main source copy.",
                ),
                Change(
                    ".DS_Store",
                    kind=ChangeKind.EXCLUDE,
                    reason="Operating-system or version-control metadata.",
                ),
            ],
            status="incomplete",
        )
        rows = {item["source"]: item for item in package["files"]}
        self.assertEqual(package["status"], "incomplete")
        self.assertEqual(rows["parts/body.tex"]["status"], "excluded")
        self.assertIn(
            "Input contents were merged into the main source copy.",
            rows["parts/body.tex"]["reasons"],
        )
        self.assertEqual(rows["parts/body.tex"]["size_bytes"], len(b"Merged body.\n"))
        self.assertIn("Operating-system or version-control metadata.", rows[".DS_Store"]["reasons"])
        self.assertEqual(rows[".DS_Store"]["size_bytes"], len(b"metadata"))

    def test_exact_zip_measurements_include_directory_entries_in_overhead(self):
        self.write("main.tex", "\\input{parts/body}\n")
        self.write("parts/body.tex", "Body.\n" * 100)
        self.write("unused.txt", "Unused.\n")
        changes, selection = self.select({"main.tex", "parts/body.tex"})
        archive = self.base / "submission.zip"
        create_archive(self.base / "selected", archive)
        package = assemble_package_inventory(
            selections=[selection],
            changes=changes,
            archive=archive,
            document="paper",
            status="verified",
        )
        self.assertEqual(package["document"], "paper")
        self.assertEqual(package["status"], "verified")
        self.assertEqual(package["archive_bytes"], archive.stat().st_size)
        rows = {item["path"]: item for item in package["files"]}
        with zipfile.ZipFile(archive) as zipped:
            infos = {info.filename: info for info in zipped.infolist() if not info.is_dir()}
        for name, info in infos.items():
            self.assertEqual(rows[name]["size_bytes"], info.file_size)
            self.assertEqual(rows[name]["compressed_bytes"], info.compress_size)
        self.assertIsNone(rows["unused.txt"]["compressed_bytes"])
        self.assertEqual(
            package["overhead_bytes"],
            archive.stat().st_size - sum(info.compress_size for info in infos.values()),
        )
        self.assertGreater(package["overhead_bytes"], 0)

    def test_dry_run_retains_selection_evidence_and_labels_planned_removal(self):
        self.write("main.tex", "\\input{body}\n")
        self.write("body.tex", "Body.\n")
        _, selection = self.select({"main.tex", "body.tex"})
        package = assemble_package_inventory(
            selections=[selection],
            changes=[Change("body.tex", kind=ChangeKind.REMOVE, reason="Merged into main source.")],
        )
        row = next(item for item in package["files"] if item["source"] == "body.tex")
        self.assertEqual(package["status"], "proposed")
        self.assertEqual(row["status"], "included")
        self.assertIn("Planned removal: Merged into main source.", row["reasons"])
        self.assertIsNone(row["compressed_bytes"])
        self.assertIsNone(package["archive_bytes"])
        self.assertIsNone(package["overhead_bytes"])

    def test_latest_selection_supplies_generated_inputs_without_guessing_a_reason(self):
        self.write("main.tex", "Body.\n")
        _, initial = self.select({"main.tex"})
        transformed = [
            {
                "source": "main.bbl",
                "path": "main.bbl",
                "status": "included",
                "reasons": ["Generated bibliography consumed by the build."],
                "size_bytes": 3,
                "compressed_bytes": None,
            }
        ]
        final = self.base / "final"
        self.write("main.tex", "Body.\n", root=final)
        self.write("main.bbl", "bbl", root=final)
        self.write("unexpected.dat", "extra", root=final)
        package = assemble_package_inventory(
            selections=[initial, transformed], final_root=final, status="incomplete"
        )
        rows = {item["source"]: item for item in package["files"]}
        self.assertEqual(
            rows["main.bbl"]["reasons"], ["Generated bibliography consumed by the build."]
        )
        self.assertEqual(
            rows["unexpected.dat"]["reasons"],
            ["Present in the prepared package; selection reason unavailable."],
        )

    def test_missing_observations_never_claim_prepared_or_verified_and_sizes_stay_unknown(self):
        package = assemble_package_inventory(
            changes=[
                Change("excluded.log", kind=ChangeKind.EXCLUDE, reason="Rebuildable diagnostic.")
            ],
            status="incomplete",
        )
        self.assertIsNone(package["files"][0]["size_bytes"])
        self.assertIsNone(package["files"][0]["compressed_bytes"])
        for status in ("prepared", "verified"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                assemble_package_inventory(status=status)


if __name__ == "__main__":
    unittest.main()
