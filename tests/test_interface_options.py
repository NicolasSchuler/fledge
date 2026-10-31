"""Settings tables and offline outputs reach the same workflow from the interfaces."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from click.testing import CliRunner

from latexprep.cli import cli
from latexprep.config import load_settings
from tests.support import minimal_project


class InterfaceExtensionTests(unittest.TestCase):
    """Settings-table edits, offline outputs and nested configuration through the interfaces."""

    def test_toml_field_edit_tables_can_add_or_remove_with_explicit_preconditions(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.toml"
            config.write_text("""[[bibliography_transform.field_edits]]
key = "paper"
field = "doi"
expected_absent = true
replacement = "10.1234/example"
[[bibliography_transform.field_edits]]
key = "paper"
field = "note"
expected = "private"
remove = true
""")
            options = load_settings(config, {}).bibliography_transform
            self.assertEqual(
                options.field_edits,
                (("paper", "doi", None, "10.1234/example"), ("paper", "note", "private", None)),
            )
            config.write_text("""[[bibliography_transform.field_edits]]
key = "paper"
field = "doi"
replacement = "10.1234/example"
""")
            from latexprep.errors import PreparationError

            with self.assertRaises(PreparationError):
                load_settings(config, {})

    def test_offline_html_ci_and_diagnostics_are_accessible_from_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            minimal_project(base / "input")
            with (base / "input/main.tex").open("a") as stream:
                stream.write("\n% comment only\n")
            result = CliRunner().invoke(
                cli,
                [
                    "inspect",
                    str(base / "input"),
                    "--quiet",
                    "--output-format",
                    "html",
                    "--html-report",
                    str(base / "report.html"),
                    "--diagnostics",
                    str(base / "debug.zip"),
                ],
            )
            self.assertIn(result.exit_code, {0, 1}, result.output)
            self.assertIn("<!doctype html>", result.output.lower())
            self.assertTrue((base / "report.html").is_file())
            with zipfile.ZipFile(base / "debug.zip") as archive:
                self.assertNotIn("submission.zip", archive.namelist())
                self.assertTrue(
                    any(
                        "unverified" in archive.read(name).decode().lower()
                        for name in archive.namelist()
                        if name.endswith(".json")
                    )
                )
            ci = CliRunner().invoke(
                cli, ["inspect", str(base / "input"), "--quiet", "--output-format", "ci"]
            )
            self.assertIn(ci.exit_code, {0, 1}, ci.output)

    def test_nested_transform_document_and_review_configuration_round_trips(self):
        settings = load_settings(
            None,
            {
                "workflow": {
                    "documents": [
                        {"name": "paper", "main": "main.tex", "include": ["*.tex", "*.bib"]}
                    ],
                    "baseline_runs": 2,
                    "comparison": {"channel_tolerance": 1},
                },
                "bibliography_transform": {
                    "remove_fields": ["file"],
                    "key_renames": {"old": "new"},
                },
                "source_transforms": {"comment_policy": "private"},
                "reporting": {
                    "accepted_exceptions": [
                        {"code": "PDF001", "reason": "Approved", "document": "paper"}
                    ]
                },
                "formatting_options": {"exclude": ["generated/*.tex"]},
            },
        )
        self.assertEqual(load_settings(None, settings.to_dict()), settings)
        self.assertEqual(settings.bibliography_transform.key_renames, (("old", "new"),))


if __name__ == "__main__":
    unittest.main()
