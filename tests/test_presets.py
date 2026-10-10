"""Shipped presets: packaging, schema validity, non-certifying content and precedence."""

from __future__ import annotations

import fnmatch
import json
import tempfile
import tomllib
import unittest
from importlib import resources
from pathlib import Path

from click.testing import CliRunner

from latexprep.cli import cli
from latexprep.config import load_settings
from latexprep.models import PreparationError
from latexprep.presets import PRESETS, preset_names, preset_text, preset_values

ROOT = Path(__file__).resolve().parents[1]


class PresetPackagingTests(unittest.TestCase):
    def test_every_preset_is_package_data_readable_through_importlib_resources(self):
        directory = resources.files("latexprep").joinpath("presets")
        shipped = {item.name for item in directory.iterdir() if item.name.endswith(".toml")}
        self.assertEqual(shipped, {f"{name}.toml" for name in PRESETS})
        for name in preset_names():
            with self.subTest(preset=name):
                text = directory.joinpath(f"{name}.toml").read_text(encoding="utf-8")
                self.assertEqual(text, preset_text(name))
                tomllib.loads(text)

    def test_wheel_and_sdist_manifests_include_every_preset(self):
        with (ROOT / "pyproject.toml").open("rb") as stream:
            setuptools = tomllib.load(stream)["tool"]["setuptools"]
        patterns = setuptools["package-data"]["latexprep"]
        manifest = (ROOT / "MANIFEST.in").read_text().splitlines()
        self.assertIn("recursive-include src/latexprep/presets *.toml", manifest)
        for name in PRESETS:
            with self.subTest(preset=name):
                relative = f"presets/{name}.toml"
                self.assertTrue(any(fnmatch.fnmatch(relative, item) for item in patterns))


class PresetContentTests(unittest.TestCase):
    def test_every_preset_validates_against_the_settings_schema(self):
        for name in preset_names():
            with self.subTest(preset=name):
                load_settings(None, {}, preset=name)

    def test_presets_supply_no_venue_values_and_say_they_do_not_certify(self):
        for name in preset_names():
            with self.subTest(preset=name):
                settings = load_settings(None, {}, preset=name)
                self.assertIsNone(settings.max_pages)
                self.assertIsNone(settings.expected_page_width_pt)
                self.assertIsNone(settings.submission_checks.max_archive_bytes)
                self.assertEqual(settings.submission_checks.identity_terms, ())
                self.assertEqual(settings.metadata_privacy.image_identity_terms, ())
                self.assertFalse(settings.online_checks.online)
                text = preset_text(name)
                self.assertIn(f'Fledge preset "{name}"', text.splitlines()[0])
                self.assertIn("not certify", text)
                self.assertIn("Venue-specific additions", text)
                self.assertIn("Venue-specific top-level settings", text)

    def test_presets_enable_their_documented_generic_checks(self):
        arxiv = load_settings(None, {}, preset="arxiv")
        self.assertTrue(arxiv.require_embedded_fonts)
        self.assertTrue(arxiv.submission_checks.scan_private_comments)
        self.assertTrue(arxiv.submission_checks.report_unused_assets)
        self.assertEqual(arxiv.source_transforms.comment_policy, "private")
        self.assertTrue(arxiv.submission_checks.require_bbl)
        self.assertTrue(arxiv.submission_checks.check_bbl_coverage)
        self.assertIsNone(arxiv.build_checks.expected_texlive_year)
        review = load_settings(None, {}, preset="anonymous-review")
        self.assertTrue(review.submission_checks.scan_identity_hints)
        self.assertTrue(review.forbid_attachments)
        self.assertTrue(review.manuscript_checks.require_float_references)
        self.assertEqual(review.source_transforms.comment_policy, "retain")
        self.assertFalse(review.manuscript_checks.require_line_numbers)
        self.assertFalse(review.submission_checks.require_bbl)
        final = load_settings(None, {}, preset="camera-ready")
        self.assertTrue(final.forbid_type3_fonts and final.require_consistent_page_size)
        self.assertEqual(final.manuscript_checks.required_metadata, ("title", "author"))
        self.assertTrue(final.manuscript_checks.check_float_reference_order)
        self.assertFalse(final.manuscript_checks.forbid_line_numbers)
        self.assertIsNone(final.figure_artwork.required_color_space)
        self.assertIsNone(final.pdf_artwork.required_color_space)
        self.assertIsNone(final.build_checks.expected_texlive_year)

    def test_venue_value_placeholders_are_commented_out_with_their_codes(self):
        expected = {
            "arxiv": ("# expected_texlive_year = ", "BLD105"),
            "anonymous-review": ("# require_line_numbers = true", "MAN017"),
            "camera-ready": (
                "# forbid_line_numbers = true",
                "MAN017",
                "# expected_texlive_year = ",
                "BLD105",
                "PDF313",
            ),
        }
        for name, fragments in expected.items():
            with self.subTest(preset=name):
                text = preset_text(name)
                for fragment in fragments:
                    self.assertIn(fragment, text)
        camera = preset_text("camera-ready")
        for table in ("[figure_artwork]", "[pdf_artwork]"):
            section = camera.split(table, 1)[1].split("\n[", 1)[0]
            self.assertIn('# required_color_space = ""', section)
        self.assertIn("require_bbl = true", preset_text("arxiv"))

    def test_unknown_preset_error_lists_valid_names(self):
        with self.assertRaisesRegex(PreparationError, "anonymous-review, arxiv, camera-ready"):
            load_settings(None, {}, preset="neurips")
        with self.assertRaises(PreparationError):
            preset_values("../arxiv")


class PresetPrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def config(self, text: str) -> Path:
        path = self.root / "fledge.toml"
        path.write_text(text)
        return path

    def test_settings_file_overrides_preset_values_key_by_key(self):
        path = self.config(
            "require_embedded_fonts = false\n"
            "[submission_checks]\n"
            'identity_terms = ["Ada Lovelace"]\n'
            "scan_identity_hints = false\n"
        )
        settings = load_settings(path, {}, preset="anonymous-review")
        self.assertFalse(settings.require_embedded_fonts)
        self.assertFalse(settings.submission_checks.scan_identity_hints)
        self.assertEqual(settings.submission_checks.identity_terms, ("Ada Lovelace",))
        # Keys the file does not mention keep the preset's value.
        self.assertTrue(settings.submission_checks.scan_private_comments)
        self.assertTrue(settings.forbid_attachments)

    def test_command_line_overrides_beat_preset_and_settings_file(self):
        path = self.config('layout = "preserve"\n')
        settings = load_settings(
            path, {"layout": "flat", "forbid_type3_fonts": False}, preset="camera-ready"
        )
        self.assertEqual(settings.layout, "flat")
        self.assertFalse(settings.forbid_type3_fonts)
        self.assertTrue(settings.require_embedded_fonts)

    def test_settings_file_errors_are_still_reported_with_a_preset(self):
        path = self.config("[submission_checks]\nscan_secrets = 1\n")
        with self.assertRaises(PreparationError):
            load_settings(path, {}, preset="arxiv")

    def test_cli_applies_preset_beneath_config_and_records_it(self):
        project = self.root / "paper"
        project.mkdir()
        (project / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\nHello. % TODO private\n\\end{document}\n"
        )
        config = self.config('[source_transforms]\ncomment_policy = "all"\n')
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["inspect", str(project), "--preset", "arxiv", "--config", str(config)]
            + ["--json", "--quiet"],
        )
        self.assertEqual(result.exit_code, 1, result.output)
        report = json.loads(result.stdout)
        self.assertEqual(report["execution"]["preset"], "arxiv")
        self.assertEqual(report["settings"]["source_transforms"]["comment_policy"], "all")
        self.assertTrue(report["settings"]["submission_checks"]["scan_private_comments"])
        self.assertIn("PRV005", {finding["code"] for finding in report["findings"]})

        isolated = runner.invoke(
            cli, ["inspect", str(project), "--preset", "arxiv", "--isolated", "--json", "--quiet"]
        )
        report = json.loads(isolated.stdout)
        self.assertIsNone(report["execution"]["config_path"])
        self.assertEqual(report["settings"]["source_transforms"]["comment_policy"], "private")

        plain = runner.invoke(cli, ["inspect", str(project), "--isolated", "--json", "--quiet"])
        report = json.loads(plain.stdout)
        self.assertIsNone(report["execution"]["preset"])
        self.assertFalse(report["settings"]["submission_checks"]["scan_private_comments"])

    def test_cli_rejects_unknown_preset_with_valid_names_and_usage_exit_code(self):
        for command in ("inspect", "check", "prepare"):
            with self.subTest(command=command):
                result = CliRunner().invoke(cli, [command, str(self.root), "--preset", "nope"])
                self.assertEqual(result.exit_code, 64, result.output)
                self.assertIn("'anonymous-review', 'arxiv', 'camera-ready'", result.output)


if __name__ == "__main__":
    unittest.main()
