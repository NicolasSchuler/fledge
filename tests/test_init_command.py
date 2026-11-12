"""``fledge init``: written files load, existing files are kept, and errors are clear."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from latexprep.cli import USAGE_EXIT_CODE, cli
from latexprep.config import load_settings
from latexprep.config_discovery import resolve_config_path
from latexprep.errors import PreparationError
from latexprep.init_command import STARTER
from latexprep.presets import PRESETS, preset_text
from latexprep.report import Outcome
from tests.support import ProjectTestCase


class InitCommandTests(ProjectTestCase):
    """The settings files that ``fledge init`` writes, refuses to overwrite or fails to write."""

    def setUp(self):
        """Create an empty ``paper`` project whose settings file would be ``fledge.toml``."""
        super().setUp()
        self.project = self.root / "paper"
        self.project.mkdir()
        self.target = self.project / "fledge.toml"
        self.runner = CliRunner()

    def init(self, *arguments: str):
        """Run ``fledge init`` with ``arguments`` and return the Click result."""
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
        self.assertEqual(result.exit_code, Outcome.ERROR.exit_code, result.output)
        self.assertIn("already exists; pass --force", result.output)
        self.assertEqual(self.target.read_text(), "jobs = 1\n")
        result = self.init(str(self.project), "--preset", "arxiv", "--force")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("comment_policy", self.target.read_text())

    def test_invalid_text_never_replaces_an_existing_file(self):
        self.target.write_text("jobs = 1\n")
        with patch("latexprep.init_command.preset_text", return_value="jobs = 0\n"):
            result = self.init(str(self.project), "--preset", "arxiv", "--force")
        self.assertEqual(result.exit_code, Outcome.ERROR.exit_code, result.output)
        self.assertIn("Cannot write a valid", result.output)
        self.assertEqual(self.target.read_text(), "jobs = 1\n")

    def test_symlinked_target_is_never_written_through(self):
        outside = self.root / "outside.toml"
        outside.write_text("jobs = 1\n")
        self.target.symlink_to(outside)
        result = self.init(str(self.project), "--force")
        self.assertEqual(result.exit_code, Outcome.ERROR.exit_code, result.output)
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


class InitMainFileTests(ProjectTestCase):
    """The main file that ``fledge init`` finds and writes into the new settings file."""

    def setUp(self):
        """Create the project folder ``paper`` and a Click runner."""
        super().setUp()
        self.project = self.root / "paper"
        self.project.mkdir()
        self.target = self.project / "fledge.toml"
        self.runner = CliRunner()

    def document(self, name: str) -> None:
        """Write a file ``name`` in the project that declares a document class."""
        self.write(
            f"paper/{name}", "\\documentclass{article}\n\\begin{document}\n\\end{document}\n"
        )

    def test_the_only_candidate_is_written_as_main(self):
        self.document("main.tex")
        self.write("paper/sections/intro.tex", "Intro.\n")
        result = self.runner.invoke(cli, ["init", str(self.project)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('\nmain = "main.tex"\n', self.target.read_text(encoding="utf-8"))
        self.assertEqual(load_settings(self.target, {}).main, "main.tex")
        self.assertIn("Main file: main.tex.", result.output)

    def test_several_candidates_are_listed_commented_out(self):
        for name in ("main.tex", "supplement.tex", "drafts/old.tex"):
            self.document(name)
        result = self.runner.invoke(cli, ["init", str(self.project)])
        self.assertEqual(result.exit_code, 0, result.output)
        text = self.target.read_text(encoding="utf-8")
        for name in ("main.tex", "supplement.tex", "drafts/old.tex"):
            self.assertIn(f'# main = "{name}"\n', text)
        self.assertIsNone(load_settings(self.target, {}).main)
        self.assertIn("Found 3 candidate main files", result.output)
        self.assertIn("uncomment one main line", result.output)

    def test_a_preset_file_also_names_the_main_file(self):
        self.document("paper.tex")
        result = self.runner.invoke(cli, ["init", str(self.project), "--preset", "arxiv"])
        self.assertEqual(result.exit_code, 0, result.output)
        text = self.target.read_text(encoding="utf-8")
        self.assertTrue(text.endswith(preset_text("arxiv")))
        settings = load_settings(self.target, {})
        self.assertEqual(settings.main, "paper.tex")
        self.assertEqual(settings.top_level_values()["require_embedded_fonts"], True)

    def test_unusual_file_names_are_written_as_valid_toml(self):
        self.document("Résumé du papier.tex")
        result = self.runner.invoke(cli, ["init", str(self.project)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(load_settings(self.target, {}).main, "Résumé du papier.tex")

    def test_a_project_that_cannot_be_read_gets_the_placeholder_and_a_reason(self):
        self.document("main.tex")
        with patch(
            "latexprep.init_command.candidate_mains",
            side_effect=PreparationError("too many files"),
        ):
            result = self.runner.invoke(cli, ["init", str(self.project)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.target.read_text(encoding="utf-8"), STARTER)
        self.assertIn("Could not look for the main file (too many files)", result.output)


if __name__ == "__main__":
    unittest.main()
