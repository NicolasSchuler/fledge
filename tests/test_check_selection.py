"""Public selector semantics and project TOML discovery through the CLI."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from latexprep.check_selection import CheckSelection
from latexprep.cli import cli
from latexprep.config import load_settings, resolve_config_path
from latexprep.models import PreparationError, Report
from latexprep.rules import RULES


class CheckSelectionTests(unittest.TestCase):
    def test_default_preserves_all_catalogued_checks(self) -> None:
        selection = CheckSelection()
        self.assertTrue(all(selection.enabled(rule.code) for rule in RULES))
        self.assertFalse(CheckSelection(select=()).enabled("TEX001"))

    def test_exact_family_and_numeric_prefixes(self) -> None:
        selection = CheckSelection(select=("TEX", "BIB1", "PDF104"))
        for code in ("TEX001", "TEX201", "BIB101", "BIB108", "PDF104"):
            self.assertTrue(selection.enabled(code), code)
        for code in ("BIB001", "PDF101", "BLD001"):
            self.assertFalse(selection.enabled(code), code)

    def test_specificity_and_ignore_ties_are_order_independent(self) -> None:
        for selected in (("TEX001", "ALL"), ("ALL", "TEX001")):
            selection = CheckSelection(select=selected, ignore=("TEX",))
            self.assertTrue(selection.enabled("TEX001"))
            self.assertFalse(selection.enabled("TEX002"))
            self.assertTrue(selection.enabled("BIB001"))
        self.assertFalse(CheckSelection(select=("TEX",), ignore=("TEX",)).enabled("TEX001"))
        self.assertFalse(CheckSelection(ignore=("TEX001",)).enabled("TEX001"))
        self.assertFalse(CheckSelection(ignore=("ALL",)).enabled("TEX001"))
        self.assertTrue(CheckSelection(select=("TEX",), ignore=("ALL",)).enabled("TEX001"))

    def test_unknown_and_partial_family_selectors_are_rejected(self) -> None:
        for selector in ("", " ", "tex", "TE", "TEX999", "BIB9", "TEX0010", "ALL1"):
            for name in ("select", "ignore"):
                with self.subTest(name=name, selector=selector):
                    with self.assertRaisesRegex(PreparationError, "Unknown checks"):
                        CheckSelection(**{name: (selector,)})

    def test_direct_api_is_immutable_and_rejects_invalid_container_types(self) -> None:
        selection = CheckSelection()
        with self.assertRaises(FrozenInstanceError):
            selection.select = ("TEX",)  # ty: ignore[invalid-assignment]
        for value in ("ALL", ["ALL"], (1,), None):
            with self.subTest(value=value), self.assertRaises(PreparationError):
                CheckSelection(select=cast(tuple[str, ...], value))
        with self.assertRaisesRegex(ValueError, "Unknown public check code"):
            selection.enabled("TEX999")

    def test_selection_does_not_enable_network_edits_or_invent_policy(self) -> None:
        settings = load_settings(None, {"checks": {"select": ["ALL"]}})
        self.assertFalse(settings.online_checks.online)
        self.assertFalse(settings.online_checks.online_metadata)
        self.assertFalse(settings.format)
        self.assertFalse(settings.normalize_doi)
        self.assertEqual(settings.submission_checks.identity_terms, ())
        self.assertIsNone(settings.max_pages)
        self.assertEqual(settings.manuscript_checks.required_metadata, ())
        self.assertEqual(load_settings(None, settings.to_dict()), settings)


class ConfigDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "project" / "paper"
        self.source.mkdir(parents=True)
        # Keep each fixture independent of the machine's ancestor configuration.
        (self.root / ".git").mkdir()

    def write(self, path: Path, text: str = "jobs = 3\n") -> Path:
        path.write_text(text, encoding="utf-8")
        return path

    def test_closest_directory_wins_without_merging(self) -> None:
        self.write(self.root / "latex-prep.toml", 'jobs = 4\nengine = "xelatex"\n')
        nearest = self.write(self.source.parent / "pyproject.toml", "[tool.latex-prep]\njobs = 1\n")
        selected = resolve_config_path(self.source)
        self.assertEqual(selected, nearest)
        settings = load_settings(selected, {})
        self.assertEqual(settings.jobs, 1)
        self.assertEqual(settings.engine, "pdflatex")

    def test_same_directory_hidden_then_plain_then_pyproject(self) -> None:
        pyproject = self.write(self.source / "pyproject.toml", "[tool.latex-prep]\njobs = 1\n")
        self.assertEqual(resolve_config_path(self.source), pyproject)
        plain = self.write(self.source / "latex-prep.toml")
        self.assertEqual(resolve_config_path(self.source), plain)
        hidden = self.write(self.source / ".latex-prep.toml")
        self.assertEqual(resolve_config_path(self.source), hidden)

    def test_unrelated_pyproject_is_skipped_and_other_tables_are_ignored(self) -> None:
        self.write(self.source / "pyproject.toml", '[project]\nname = "paper"\n')
        ancestor = self.write(self.source.parent / "latex-prep.toml")
        self.assertEqual(resolve_config_path(self.source), ancestor)
        pyproject = self.write(
            self.source / "pyproject.toml",
            '[project]\nname = "paper"\n[tool.other]\nunknown = true\n'
            '[tool.latex-prep]\njobs = 1\n[tool.latex-prep.checks]\nselect = ["TEX"]\n',
        )
        settings = load_settings(resolve_config_path(self.source), {})
        self.assertEqual(resolve_config_path(self.source), pyproject)
        self.assertEqual(settings.jobs, 1)
        self.assertEqual(settings.checks.select, ("TEX",))

    def test_explicit_config_and_isolated_take_precedence(self) -> None:
        self.write(self.source / "latex-prep.toml", "broken = [\n")
        explicit = self.write(self.root / "alternate.toml", "jobs = 1\n")
        self.assertEqual(resolve_config_path(self.source, explicit), explicit)
        self.assertIsNone(resolve_config_path(self.source, isolated=True))
        with self.assertRaisesRegex(PreparationError, "--isolated"):
            resolve_config_path(self.source, explicit, isolated=True)

    def test_repository_boundary_is_inclusive(self) -> None:
        self.write(self.root / "latex-prep.toml")
        (self.source.parent / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        self.assertIsNone(resolve_config_path(self.source))
        boundary = self.write(self.source.parent / ".latex-prep.toml")
        self.assertEqual(resolve_config_path(self.source), boundary)

    def test_home_ancestor_is_not_a_user_configuration_fallback(self) -> None:
        self.write(self.root / "latex-prep.toml")
        with patch("latexprep.config.Path.home", return_value=self.root):
            self.assertIsNone(resolve_config_path(self.source))
            # A deliberately selected home directory is still an input folder.
            self.assertEqual(resolve_config_path(self.root), self.root / "latex-prep.toml")

    def test_file_inputs_start_beside_the_file_and_never_inside_zip(self) -> None:
        archive = self.source / "paper.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("latex-prep.toml", 'jobs = 63\n[checks]\nselect = ["BAD"]\n')
        self.assertIsNone(resolve_config_path(archive))
        selected = self.write(self.source / "latex-prep.toml", "jobs = 1\n")
        self.assertEqual(resolve_config_path(archive), selected)
        self.assertEqual(resolve_config_path(self.source / "paper.pdf"), selected)

    def test_configuration_errors_are_actionable(self) -> None:
        for text, message in (
            ("[checks]\nselect = [\n", "Cannot read settings"),
            ('[checks]\nselect = ["BOGUS"]\n', "Unknown checks.select"),
            ('[checks]\nselect = "TEX"\n', "checks.select must be an array"),
            ("[checks]\nignore = [false]\n", "checks.ignore\\[0\\] must be a string"),
            ("[checks]\nunknown = true\n", "Unknown checks setting"),
        ):
            with self.subTest(text=text), self.assertRaisesRegex(PreparationError, message):
                load_settings(self.write(self.source / "latex-prep.toml", text), {})
        with self.assertRaisesRegex(PreparationError, "no \\[tool.latex-prep\\] table"):
            load_settings(self.write(self.source / "pyproject.toml", "[project]\n"), {})
        with self.assertRaisesRegex(PreparationError, "must be a settings table"):
            resolve_config_path(
                self.write(self.root / "pyproject.toml", '[tool]\nlatex-prep = "invalid"\n')
            )

    def test_pyproject_paths_remain_relative_to_the_configuration(self) -> None:
        path = self.write(
            self.root / "pyproject.toml",
            "[tool.latex-prep.submission_checks.template_references]\n"
            '"template.cls" = "reference/template.cls"\n',
        )
        settings = load_settings(path, {})
        self.assertEqual(
            settings.submission_checks.template_references,
            (("template.cls", str(self.root / "reference/template.cls")),),
        )

    def test_cli_discovery_overrides_and_reported_path(self) -> None:
        path = self.write(
            self.source / "latex-prep.toml", 'jobs = 3\n[checks]\nselect = ["BIB1"]\n'
        )

        async def record_settings(request, _progress, report: Report) -> Report:
            report.settings = request.settings.to_dict()
            report.outcome = "passed"
            return report

        with patch("latexprep.cli.run_job", new=AsyncMock(side_effect=record_settings)) as run:
            result = CliRunner().invoke(cli, ["inspect", str(self.source), "--jobs", "1", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        assert run.await_args is not None
        settings = run.await_args.args[0].settings
        self.assertEqual(settings.jobs, 1)
        self.assertEqual(settings.checks.select, ("BIB1",))
        self.assertEqual(json.loads(result.output)["execution"]["config_path"], str(path))

    def test_cli_explicit_and_isolated_override_discovery(self) -> None:
        self.write(self.source / "latex-prep.toml", "jobs = 3\n")
        explicit = self.write(self.root / "explicit.toml", "jobs = 4\n")
        for flags, expected in ((["--config", str(explicit)], 4), (["--isolated"], 2)):
            with self.subTest(flags=flags):
                with patch(
                    "latexprep.cli.run_job", new=AsyncMock(return_value=Report("inspect", "passed"))
                ) as run:
                    result = CliRunner().invoke(
                        cli, ["inspect", str(self.source), "--json", *flags]
                    )
                self.assertEqual(result.exit_code, 0, result.output)
                assert run.await_args is not None
                self.assertEqual(run.await_args.args[0].settings.jobs, expected)
        result = CliRunner().invoke(
            cli, ["inspect", str(self.source), "--config", str(explicit), "--isolated"]
        )
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("cannot be combined", result.output)

    def test_cli_invalid_discovered_selector_prevents_dispatch(self) -> None:
        self.write(self.source / "latex-prep.toml", '[checks]\nselect = ["TEX999"]\n')
        with patch("latexprep.cli.run_job", new=AsyncMock()) as run:
            result = CliRunner().invoke(cli, ["inspect", str(self.source), "--json"])
        self.assertEqual(result.exit_code, 4, result.output)
        run.assert_not_awaited()
        report = json.loads(result.output)
        self.assertIn("Unknown checks.select", report["findings"][0]["message"])
        self.assertIn("latex-prep rules", report["findings"][0]["message"])

    def test_cli_help_explains_configuration_controls(self) -> None:
        result = CliRunner().invoke(cli, ["inspect", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("--isolated", result.output)
        self.assertIn("discovering project configuration", result.output)


if __name__ == "__main__":
    unittest.main()
