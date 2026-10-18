from __future__ import annotations

import re
import tempfile
import tomllib
import unittest
from pathlib import Path

from latexprep.author_records import AuthorRecordOptions
from latexprep.bbl_checks import BblOptions
from latexprep.command_runner import RuntimeLimits
from latexprep.config import SETTING_NAMES, Settings, load_settings, toml_table
from latexprep.errors import PreparationError, SettingsError
from latexprep.import_budget import ImportLimits
from latexprep.job_execution import JobExecution
from latexprep.pdf_structure_checks import StructureOptions
from latexprep.submission_options import SubmissionOptions


class ConfigTests(unittest.TestCase):
    def test_explicit_file_then_command_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            path.write_text('layout = "flat"\njobs = 4\napply_formatting = true\n')
            settings = load_settings(path, {"jobs": 1, "apply_formatting": False, "main": None})
            self.assertEqual(settings.jobs, 1)
            self.assertEqual(settings.layout, "flat")
            self.assertFalse(settings.apply_formatting)

    def test_former_setting_names_are_accepted_and_reported_under_current_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            path.write_text(
                "format = true\n"
                "[pdf_artwork]\ngrayscale_preview = true\n"
                "[figure_artwork]\nclassify_artwork = true\n"
            )
            settings = load_settings(path, {})
            self.assertTrue(settings.apply_formatting)
            self.assertTrue(settings.rendered_pdf_artwork.grayscale_preview)
            self.assertTrue(settings.included_figure_artwork.classify_artwork)
            serialized = settings.to_dict()
            self.assertTrue(serialized["apply_formatting"])
            self.assertNotIn("format", serialized)
            self.assertNotIn("pdf_artwork", serialized)
            self.assertFalse(load_settings(path, {"format": False}).apply_formatting)
            with self.assertRaisesRegex(PreparationError, "apply_formatting"):
                load_settings(path, {"apply_formatting": False, "format": True})
            path.write_text("format = true\napply_formatting = false\n")
            with self.assertRaisesRegex(PreparationError, "former name"):
                load_settings(path, {})

    def test_line_format_settings_stay_top_level_keys(self):
        settings = load_settings(None, {"wrap_length": 100, "tab_size": 4})
        self.assertEqual(settings.formatting_options.wrap_length, 100)
        self.assertEqual(settings.formatting_options.tab_size, 4)
        serialized = settings.to_dict()
        self.assertEqual((serialized["wrap_length"], serialized["tab_size"]), (100, 4))
        self.assertNotIn("wrap_length", serialized["formatting_options"])
        for overrides in ({"wrap_length": 19}, {"tab_size": 17}, {"wrap_length": True}):
            with self.subTest(overrides=overrides), self.assertRaises(PreparationError):
                load_settings(None, overrides)
        with self.assertRaisesRegex(PreparationError, "at the top level"):
            load_settings(None, {"formatting_options": {"wrap_length": 90}})
        self.assertTrue(
            load_settings(None, {"format": None, "apply_formatting": True}).apply_formatting
        )

    def test_unknown_type_and_contradictory_settings_rejected(self):
        for overrides in [
            {"publisher": "example"},
            {"jobs": True},
            {"jobs": 0},
            {"jobs": 65},
            {"layout": "publisher"},
            {"apply_formatting": "true"},
            {"engine": "custom.sh"},
            {"memory_mb": 512, "build_memory_mb": 1024},
            {"max_pages": -1},
        ]:
            with self.subTest(overrides=overrides), self.assertRaises(PreparationError):
                load_settings(None, overrides)

    def test_generic_constraints_load_into_typed_options(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            path.write_text(
                'expected_document_class = "article"\n'
                'required_class_options = ["review"]\n'
                'forbidden_packages = ["draftwatermark"]\n'
                'required_sections = ["Data Availability"]\n'
                "require_abstract = true\n"
                "abstract_min_words = 100\nabstract_max_words = 250\n"
                "keywords_min_count = 3\nkeywords_max_count = 6\n"
                "expected_page_width_pt = 612\nexpected_page_height_pt = 792.0\n"
                "require_embedded_fonts = true\nmin_image_dpi = 300\n"
            )
            settings = load_settings(path, {"require_abstract": False, "abstract_min_words": 0})
            manuscript = settings.manuscript
            self.assertEqual(manuscript.expected_document_class, "article")
            self.assertEqual(manuscript.required_class_options, ("review",))
            self.assertEqual(manuscript.required_sections, ("Data Availability",))
            self.assertFalse(manuscript.require_abstract)
            self.assertEqual(manuscript.abstract_min_words, 0)
            pdf = settings.pdf
            self.assertEqual((pdf.expected_page_width_pt, pdf.expected_page_height_pt), (612, 792))
            self.assertTrue(pdf.require_embedded_fonts)
            self.assertEqual(pdf.min_image_dpi, 300)

    def test_invalid_constraint_types_and_combinations_are_rejected(self):
        for overrides in (
            {"expected_document_class": ""},
            {"required_class_options": "review"},
            {"required_class_options": ["review", "review"]},
            {"required_sections": [" "]},
            {"forbidden_packages": [False]},
            {"required_class_options": ["review"], "forbidden_class_options": ["review"]},
            {"abstract_min_words": 20, "abstract_max_words": 10},
            {"abstract_min_words": True},
            {"keywords_max_count": -1},
            {"require_abstract": 1},
            {"expected_page_width_pt": 612},
            {"page_size_tolerance_pt": -0.1},
            {"min_image_dpi": float("nan")},
            {"min_image_dpi": float("inf")},
            {"min_image_dpi": True},
            {"forbid_forms": "false"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(PreparationError):
                load_settings(None, overrides)


class ByteLimitTests(unittest.TestCase):
    """max_bytes is the default of the import total and of the size of one tool file."""

    def test_max_bytes_is_the_fallback_of_both_specific_limits(self) -> None:
        settings = load_settings(None, {"max_bytes": 4096})
        self.assertEqual(settings.import_byte_limit, 4096)
        self.assertEqual(settings.generated_file_byte_limit, 4096)
        self.assertIsNone(settings.max_import_bytes)
        self.assertIsNone(settings.max_generated_file_bytes)
        defaults = Settings()
        self.assertEqual(defaults.import_byte_limit, defaults.max_bytes)
        self.assertEqual(defaults.generated_file_byte_limit, defaults.max_bytes)

    def test_an_explicit_limit_wins_and_the_other_keeps_falling_back(self) -> None:
        both = load_settings(
            None,
            {"max_bytes": 8192, "max_import_bytes": 4096, "max_generated_file_bytes": 1024},
        )
        self.assertEqual((both.import_byte_limit, both.generated_file_byte_limit), (4096, 1024))
        only_import = load_settings(None, {"max_bytes": 4096, "max_import_bytes": 8192})
        self.assertEqual(only_import.import_byte_limit, 8192)
        self.assertEqual(only_import.generated_file_byte_limit, 4096)
        only_file = load_settings(None, {"max_bytes": 4096, "max_generated_file_bytes": 1024})
        self.assertEqual(only_file.import_byte_limit, 4096)
        self.assertEqual(only_file.generated_file_byte_limit, 1024)

    def test_the_import_total_must_fit_in_the_workspace_of_a_build(self) -> None:
        """The documented 1 GiB import with 64 MiB per file could never be built."""
        mebibyte = 1024 * 1024
        for overrides in (
            {"max_import_bytes": 1024 * mebibyte, "max_generated_file_bytes": 64 * mebibyte},
            # The default import total (256 MiB) against a small per-file limit.
            {"max_generated_file_bytes": 7000},
            {"max_bytes": 4097, "max_generated_file_bytes": 1024},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(SettingsError) as raised:
                    load_settings(None, overrides)
                self.assertEqual(
                    raised.exception.settings, ("max_import_bytes", "max_generated_file_bytes")
                )
                message = str(raised.exception)
                self.assertIn("max_import_bytes", message)
                self.assertIn("max_generated_file_bytes", message)
                self.assertIn("4 times max_generated_file_bytes", message)

    def test_the_documented_byte_limit_example_is_a_valid_configuration(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "docs" / "configuration.md").read_text(
            encoding="utf-8"
        )
        [example] = [
            block
            for block in re.findall(r"```toml\n(.*?)```", text, re.DOTALL)
            if "max_generated_file_bytes" in block
        ]
        settings = load_settings(None, tomllib.loads(example))
        self.assertEqual(settings.import_byte_limit, 1024**3)
        self.assertLessEqual(settings.import_byte_limit, 4 * settings.generated_file_byte_limit)

    def test_a_workspace_sized_import_total_and_the_defaults_are_valid(self) -> None:
        mebibyte = 1024 * 1024
        defaults = Settings()
        self.assertLessEqual(defaults.import_byte_limit, 4 * defaults.generated_file_byte_limit)
        limits = load_settings(
            None, {"max_import_bytes": 256 * mebibyte, "max_generated_file_bytes": 64 * mebibyte}
        )
        self.assertEqual(limits.import_byte_limit, 4 * limits.generated_file_byte_limit)
        load_settings(None, {"max_bytes": 1024 * mebibyte})

    def test_settings_files_and_reports_use_the_new_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            path.write_text("max_bytes = 4096\nmax_import_bytes = 8192\n")
            settings = load_settings(path, {})
        self.assertEqual(settings.import_byte_limit, 8192)
        self.assertIn("max_import_bytes", SETTING_NAMES)
        self.assertIn("max_generated_file_bytes", SETTING_NAMES)
        recorded = settings.to_dict()
        self.assertEqual((recorded["max_bytes"], recorded["max_import_bytes"]), (4096, 8192))
        self.assertIsNone(recorded["max_generated_file_bytes"])

    def test_the_limits_must_be_positive_integers(self) -> None:
        for name in ("max_import_bytes", "max_generated_file_bytes"):
            for value in (0, -1, True, 1.5, "1"):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(
                        PreparationError, f"{name} must be a positive integer"
                    ):
                        load_settings(None, {name: value})

    def test_each_consumer_reads_its_own_limit(self) -> None:
        settings = load_settings(
            None,
            {"max_bytes": 8192, "max_import_bytes": 4096, "max_generated_file_bytes": 1024},
        )
        limits = RuntimeLimits.from_settings(settings)
        self.assertEqual(ImportLimits.from_settings(settings).max_bytes, 4096)
        self.assertEqual(limits.max_copy_bytes, 4096)
        self.assertEqual(limits.max_file_bytes, 1024)
        fallback = load_settings(None, {"max_bytes": 4096})
        limits = RuntimeLimits.from_settings(fallback)
        self.assertEqual(ImportLimits.from_settings(fallback).max_bytes, 4096)
        self.assertEqual((limits.max_copy_bytes, limits.max_file_bytes), (4096, 4096))


class ToolLimitTests(unittest.TestCase):
    def test_runtime_limits_have_one_default_with_the_settings(self) -> None:
        self.assertEqual(RuntimeLimits.from_settings(Settings()), RuntimeLimits())

    def test_limits_follow_the_settings_of_the_job(self) -> None:
        settings = load_settings(
            None,
            {
                "timeout_seconds": 33,
                "build_memory_mb": 512,
                "max_import_bytes": 7000,
                "max_generated_file_bytes": 7000,
            },
        )
        limits = RuntimeLimits.from_settings(settings)
        self.assertEqual(
            (limits.timeout_seconds, limits.max_memory_mb, limits.max_file_bytes),
            (33, 512, 7000),
        )
        self.assertEqual(limits.max_copy_bytes, settings.import_byte_limit)

    def test_every_limit_must_be_a_positive_integer(self) -> None:
        for name in ("timeout_seconds", "max_memory_mb", "max_file_bytes", "max_copy_bytes"):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(PreparationError, f"{name} must be a positive integer"),
            ):
                RuntimeLimits(**{name: 0})


class JobToolLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_job_runs_its_tools_under_limits_from_its_settings(self) -> None:
        settings = Settings(timeout_seconds=33, build_memory_mb=512, max_bytes=7000)
        self.assertEqual(
            JobExecution(settings).runner.limits, RuntimeLimits.from_settings(settings)
        )


class SharedTableTests(unittest.TestCase):
    """The author-record and .bbl options keep their TOML tables while their modules own them."""

    def test_each_table_fills_both_option_groups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            path.write_text(
                "[structure_checks]\n"
                'author_command = "person"\n'
                'required_author_fields = ["email"]\n'
                "require_figure_alt = true\n"
                "[submission_checks]\n"
                "scan_secrets = true\n"
                "require_bbl = true\n"
            )
            settings = load_settings(path, {})
        self.assertEqual(
            settings.author_records,
            AuthorRecordOptions(author_command="person", required_author_fields=("email",)),
        )
        self.assertEqual(settings.structure_checks, StructureOptions(require_figure_alt=True))
        self.assertEqual(settings.submission_checks, SubmissionOptions(scan_secrets=True))
        self.assertEqual(settings.bbl_checks, BblOptions(require_bbl=True))

    def test_reports_record_the_tables_as_written(self) -> None:
        settings = Settings(
            author_records=AuthorRecordOptions(required_author_fields=("email",)),
            bbl_checks=BblOptions(check_bbl_coverage=True),
        )
        recorded = settings.to_dict()
        structure, submission = recorded["structure_checks"], recorded["submission_checks"]
        assert isinstance(structure, dict) and isinstance(submission, dict)
        self.assertEqual(structure["required_author_fields"], ("email",))
        self.assertEqual(structure["author_command"], "author")
        self.assertTrue(submission["check_bbl_coverage"])
        self.assertEqual(
            set(structure),
            {"author_command", "required_author_fields"}
            | set(StructureOptions.__dataclass_fields__),
        )
        for group in ("author_records", "bbl_checks"):
            self.assertNotIn(group, recorded)
            self.assertNotIn(group, SETTING_NAMES)

    def test_the_groups_cannot_be_written_as_tables_of_their_own(self) -> None:
        for name in ("author_records", "bbl_checks"):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(PreparationError, f"Unknown setting\\(s\\): {name}"),
            ):
                load_settings(None, {name: {}})

    def test_a_table_names_the_group_that_holds_its_keys(self) -> None:
        self.assertEqual(toml_table("author_records"), "structure_checks")
        self.assertEqual(toml_table("bbl_checks"), "submission_checks")
        self.assertEqual(toml_table("structure_checks"), "structure_checks")
        self.assertEqual(toml_table("pdf_checks"), "pdf_checks")

    def test_errors_name_the_table_the_value_was_written_in(self) -> None:
        for table, key, value in (
            ("structure_checks", "required_author_fields", '["author"]'),
            ("submission_checks", "biblatex_bbl_format_version", '"x"'),
        ):
            with self.subTest(key=key), self.assertRaises(SettingsError) as raised:
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "settings.toml"
                    path.write_text(f"[{table}]\n{key} = {value}\n")
                    load_settings(path, {})
            self.assertEqual(raised.exception.settings, (table,))


class BuildSettingsTests(unittest.TestCase):
    def test_loading_settings_leaves_the_given_values_unchanged(self) -> None:
        from copy import deepcopy

        values = {
            "jobs": 3,
            "wrap_length": 90,
            "abstract_min_words": 50,
            "formatting_options": {},
            "match_source_pdf_metadata": ["title"],
            "checks": {"select": ["TEX"]},
        }
        original = deepcopy(values)
        settings = load_settings(None, values)
        self.assertEqual(values, original)
        self.assertEqual(settings.jobs, 3)
        self.assertEqual(settings.formatting_options.wrap_length, 90)
        self.assertEqual(settings.manuscript.abstract_min_words, 50)
        self.assertEqual(settings.match_source_pdf_metadata, ("title",))


class SettingsErrorTests(unittest.TestCase):
    """An invalid value names the top-level settings at fault, in the order its message does."""

    def test_invalid_values_name_the_settings_at_fault(self):
        cases = (
            ({"jobs": 0}, ("jobs",)),
            ({"max_pages": 0}, ("max_pages",)),
            ({"engine": "word"}, ("engine",)),
            ({"apply_formatting": 1}, ("apply_formatting",)),
            ({"source_date_epoch": -1}, ("source_date_epoch",)),
            ({"build_jobs": 100}, ("build_jobs",)),
            ({"memory_mb": 300, "build_memory_mb": 1024}, ("memory_mb", "build_memory_mb")),
            ({"wrap_length": 5}, ("wrap_length",)),
            ({"tab_size": 0}, ("tab_size",)),
            ({"required_sections": "intro"}, ("required_sections",)),
            ({"required_class_options": ["a", "a"]}, ("required_class_options",)),
            (
                {"abstract_min_words": 5, "abstract_max_words": 2},
                ("abstract_min_words", "abstract_max_words"),
            ),
            ({"format": True, "apply_formatting": True}, ("apply_formatting",)),
            (
                {"formatting_options": {"wrap_length": 90}},
                ("wrap_length", "formatting_options"),
            ),
            (
                {
                    "layout": "preserve",
                    "source_transforms": {"filename_overrides": [["a.tex", "b.tex"]]},
                },
                ("source_transforms", "layout"),
            ),
        )
        for values, names in cases:
            with self.subTest(values=values):
                with self.assertRaises(SettingsError) as raised:
                    load_settings(None, values)
                self.assertEqual(raised.exception.settings, names)
                self.assertLessEqual(set(raised.exception.settings), SETTING_NAMES)

    def test_a_problem_inside_a_table_names_the_table(self):
        cases = (
            {"online_checks": {"online_timeout_seconds": 0}},
            {"online_checks": {"unknown_option": 1}},
            {"online_checks": 3},
            {"formatting_options": {"blank_lines": "everything"}},
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(SettingsError) as raised:
                    load_settings(None, values)
                [table] = values
                self.assertEqual(raised.exception.settings, (table,))

    def test_failures_that_no_setting_explains_carry_no_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            path.write_text("jobs = [")
            with self.assertRaises(PreparationError) as raised:
                load_settings(path, {})
        self.assertNotIsInstance(raised.exception, SettingsError)


if __name__ == "__main__":
    unittest.main()
