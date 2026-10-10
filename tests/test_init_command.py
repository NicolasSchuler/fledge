"""`fledge init`: written files load, existing files are kept, and errors are clear."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from click.testing import CliRunner

from latexprep.cli import USAGE_EXIT_CODE, cli
from latexprep.config import load_settings, resolve_config_path
from latexprep.init_command import STARTER, WRITE_ERROR_EXIT_CODE
from latexprep.presets import PRESETS, preset_text


class InitCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "paper"
        self.project.mkdir()
        self.target = self.project / "fledge.toml"
        self.runner = CliRunner()

    def init(self, *arguments: str):
        return self.runner.invoke(cli, ["init", *arguments])

    def test_each_preset_writes_a_file_the_normal_loader_discovers_and_accepts(self):
        for name in PRESETS:
            with self.subTest(preset=name):
                result = self.init(str(self.project), "--preset", name, "--force")
                self.assertEqual(result.exit_code, 0, result.output)
                text = self.target.read_text(encoding="utf-8")
                self.assertTrue(text.endswith(preset_text(name)))
                self.assertEqual(resolve_config_path(self.project), self.target)
                self.assertEqual(
                    load_settings(self.target, {}), load_settings(None, {}, preset=name)
                )
                self.assertIn(f"from the {name} preset", result.output)
                self.assertIn("does not certify", result.output)
                self.assertIn(f"fledge prepare {self.project} --output", result.output)

    def test_without_preset_writes_the_minimal_starter_in_the_current_directory(self):
        previous = Path.cwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, previous)
        result = self.init()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.target.read_text(encoding="utf-8"), STARTER)
        load_settings(self.target, {})
        self.assertIn("fledge check .", result.output)
        self.assertIn("fledge prepare . --output ../paper-submission", result.output)

    def test_existing_file_is_kept_unless_force_is_given(self):
        self.target.write_text("jobs = 1\n")
        result = self.init(str(self.project), "--preset", "arxiv")
        self.assertEqual(result.exit_code, WRITE_ERROR_EXIT_CODE, result.output)
        self.assertIn("already exists; pass --force", result.output)
        self.assertEqual(self.target.read_text(), "jobs = 1\n")
        result = self.init(str(self.project), "--preset", "arxiv", "--force")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("comment_policy", self.target.read_text())

    def test_symlinked_target_is_never_written_through(self):
        outside = self.root / "outside.toml"
        outside.write_text("jobs = 1\n")
        self.target.symlink_to(outside)
        result = self.init(str(self.project), "--force")
        self.assertEqual(result.exit_code, WRITE_ERROR_EXIT_CODE, result.output)
        self.assertEqual(outside.read_text(), "jobs = 1\n")

    def test_list_prints_every_preset_with_its_description(self):
        result = self.init("--list")
        self.assertEqual(result.exit_code, 0, result.output)
        for name, description in PRESETS.items():
            self.assertIn(f"{name}", result.output)
            self.assertIn(description, result.output)
        self.assertIn("not certifications", result.output)

    def test_usage_errors_exit_64_without_writing(self):
        for arguments in (
            (str(self.project), "--preset", "neurips"),
            ("--list", "--preset", "arxiv"),
            ("--list", "--force"),
            (str(self.root / "missing"),),
        ):
            with self.subTest(arguments=arguments):
                result = self.init(*arguments)
                self.assertEqual(result.exit_code, USAGE_EXIT_CODE, result.output)
                self.assertFalse(self.target.exists())
        result = self.init(str(self.project), "--preset", "neurips")
        self.assertIn("'anonymous-review', 'arxiv', 'camera-ready'", result.output)

    def test_warns_when_a_higher_priority_config_would_shadow_the_new_file(self):
        hidden = self.project / ".fledge.toml"
        hidden.write_text("jobs = 1\n")
        result = self.init(str(self.project), "--preset", "camera-ready")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(f"{hidden} takes precedence", result.output)
        legacy = self.root / "other"
        legacy.mkdir()
        (legacy / "latex-prep.toml").write_text("jobs = 1\n")
        result = self.init(str(legacy))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("now takes precedence over", result.output)
        self.assertEqual(resolve_config_path(legacy), legacy / "fledge.toml")


if __name__ == "__main__":
    unittest.main()
