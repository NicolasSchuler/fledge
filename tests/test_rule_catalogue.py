"""``fledge rules`` and ``fledge rule``: order, filters, activation, suggestions and completion."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from latexprep.build_checks import check_tex_distribution
from latexprep.build_result import BuildResult
from latexprep.cli import USAGE_EXIT_CODE, cli
from latexprep.cli_completion import complete_check_codes, complete_check_selectors
from latexprep.command_runner import CommandResult
from latexprep.config import Settings, load_settings
from latexprep.core import run_job
from latexprep.errors import PreparationError
from latexprep.formatting import format_project
from latexprep.job_context import JobCommand, JobRequest, pdf_check_settings
from latexprep.manuscript_checks import check_manuscript_policies
from latexprep.online_checks import check_online_references
from latexprep.online_http_wire import Response
from latexprep.pdf_artwork import inspect_artwork_inputs, inspect_pdf_artwork
from latexprep.pdf_artwork_options import configured_artwork_rules
from latexprep.pdf_check_options import configured_pdf_check_rules
from latexprep.pdf_check_pipeline import inspect_configured_pdf_checks
from latexprep.pdf_inspection import configured_policy_rules
from latexprep.resource_budget import ResourceBudget
from latexprep.rule_catalogue import ALWAYS, DEFAULT, OPT_IN, SUGGESTION, rule_activation
from latexprep.rules import RULES, get_rule
from latexprep.submission_readiness_checks import check_submission_readiness
from tests.fakes import fake_formatter
from tests.online_support import FakeTransport, public_resolver
from tests.pdf_fakes import ArtworkRunner, DetailRunner
from tests.support import controlled_backends, single_file_project


class RuleListTests(unittest.TestCase):
    """The list of checks: sorted by code, filtered by selector, marked by activation."""

    def run_rules(self, *words: str):
        """Run ``fledge rules`` with ``words`` at width 200 and return the Click result."""
        return CliRunner().invoke(cli, ["rules", *words], terminal_width=200)

    def test_the_list_is_sorted_by_code_and_marks_how_each_check_runs(self) -> None:
        result = self.run_rules()
        self.assertEqual(result.exit_code, 0, result.output)
        rows = [line.split() for line in result.stdout.splitlines()[2:] if line[:3].isupper()]
        codes = [row[0] for row in rows if row[0][:3].isalpha() and row[0][3:].isdigit()]
        self.assertEqual(codes, sorted(rule.code for rule in RULES))
        marks = {row[0]: row[1] for row in rows if row[0] in {"TEX001", "PDF001", "TEX005"}}
        self.assertEqual(marks, {"TEX001": DEFAULT, "PDF001": OPT_IN, "TEX005": ALWAYS})
        self.assertIn("opt-in: runs once configured", result.stdout)

    def test_selectors_filter_the_list_in_any_case(self) -> None:
        result = self.run_rules("tex", "BIB1")
        self.assertEqual(result.exit_code, 0, result.output)
        listed = json.loads(self.run_rules("tex", "BIB1", "--json").stdout)["rules"]
        codes = [rule["code"] for rule in listed]
        self.assertTrue(codes)
        self.assertTrue(all(code.startswith(("TEX", "BIB1")) for code in codes))
        self.assertEqual(codes, sorted(codes))
        self.assertIn(f"{len(codes)} registered checks", result.stdout)
        one = json.loads(self.run_rules("source.edit_marker", "--json").stdout)["rules"]
        self.assertEqual([rule["code"] for rule in one], ["TEX001"])

    def test_an_unknown_selector_is_a_usage_error_with_suggestions(self) -> None:
        result = self.run_rules("TEX99")
        self.assertEqual(result.exit_code, USAGE_EXIT_CODE, result.output)
        self.assertIn("did you mean", result.stderr)

    def test_json_carries_the_activation_and_the_settings_that_enable_a_check(self) -> None:
        listed = {
            rule["code"]: rule for rule in json.loads(self.run_rules("--json").stdout)["rules"]
        }
        self.assertEqual(set(listed), {rule.code for rule in RULES})
        self.assertEqual(listed["TEX001"]["activation"], DEFAULT)
        self.assertEqual(listed["BIB101"]["activation"], DEFAULT)
        self.assertEqual(listed["PDF001"]["activation"], OPT_IN)
        self.assertEqual(listed["PDF001"]["enabled_by"], ["max_pages"])
        self.assertEqual(listed["TEX005"]["enabled_by"], [])

    def test_json_pairs_each_activation_alternative_with_its_toml_table(self) -> None:
        result = self.run_rules("PDF207", "PDF308", "FMT001", "MAN201", "BIB109", "--json")
        self.assertEqual(result.exit_code, 0, result.output)
        listed = json.loads(result.stdout)["rules"]
        self.assertEqual(
            {rule["code"]: rule["enabled_by"] for rule in listed},
            {
                "PDF207": ["match_source_pdf_metadata", "expected_metadata in [pdf_checks]"],
                "PDF308": [
                    "classify_type3_glyphs in [rendered_pdf_artwork]",
                    "forbid_bitmap_type3_glyphs in [rendered_pdf_artwork]",
                    "classify_type3_glyphs in [included_figure_artwork]",
                    "forbid_bitmap_type3_glyphs in [included_figure_artwork]",
                ],
                "FMT001": ["fledge fmt"],
                "MAN201": ["required_author_fields in [structure_checks]"],
                "BIB109": ["check_bbl_coverage in [submission_checks]"],
            },
        )
        self.assertEqual({rule["activation"] for rule in listed}, {OPT_IN})

    def test_json_keeps_conjunctions_in_one_alternative_and_omits_tuning_fields(self) -> None:
        codes = ("MAN004", "PDF101", "PDF205", "PDF215", "PDF301", "PDF307", "PDF310")
        result = self.run_rules(*codes, "BLD105", "TEX013", "NET001", "--json")
        self.assertEqual(result.exit_code, 0, result.output)
        rules = {rule["code"]: rule for rule in json.loads(result.stdout)["rules"]}
        expected = {
            "MAN004": ["abstract_abbreviation_policy in [manuscript_checks]"],
            "PDF101": ["expected_page_width_pt and expected_page_height_pt"],
            "PDF205": ["min_page_ink_ratio in [pdf_checks]"],
            "PDF215": ["min_text_contrast and contrast_background_rgb in [pdf_checks]"],
            "PDF301": [
                f"detect_printer_marks and trim_regions and printer_mark_max_length_pt in [{table}]"
                for table in ("rendered_pdf_artwork", "included_figure_artwork")
            ],
            "PDF307": [
                f"artwork_category and category_min_dpi in [{table}]"
                for table in ("rendered_pdf_artwork", "included_figure_artwork")
            ],
            "PDF310": [
                f"raster_regions and min_raster_region_coverage in [{table}]"
                for table in ("rendered_pdf_artwork", "included_figure_artwork")
            ],
            "TEX013": ["venue_engines in [submission_checks]"],
            "NET001": ["online_doi_resolution and online in [online_checks]"],
            "BLD105": [],
        }
        self.assertEqual({code: rule["enabled_by"] for code, rule in rules.items()}, expected)
        self.assertEqual(rules["BLD105"]["activation"], DEFAULT)
        self.assertTrue(all(rules[code]["activation"] == OPT_IN for code in codes))


class RuleActivationTests(unittest.IsolatedAsyncioTestCase):
    """Compare the catalogue with producers using settings loaded from real TOML."""

    def setUp(self) -> None:
        """Create an isolated project and settings file for the checks to read."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        single_file_project(
            self.source,
            body=r"\begin{abstract}ABC and XYZ.\end{abstract}",
            preamble=r"\title{Paper}",
        )
        self.pdf = self.root / "paper.pdf"
        self.pdf.write_bytes(b"%PDF-fixture")

    def settings(self, toml: str) -> Settings:
        """Load a settings fragment through the real TOML loader and validators."""
        path = self.root / "fledge.toml"
        path.write_text(toml, encoding="utf-8")
        return load_settings(path, {})

    def assert_dispatch(self, code: str, settings: Settings, rules: Sequence[str]) -> None:
        """Check catalogue classification against the producer's rule registry."""
        rule = get_rule(code)
        self.assertEqual(
            rule_activation(rule, settings).kind, DEFAULT if rule.name in rules else OPT_IN
        )

    async def test_each_advertised_pdf207_setting_activates_the_check_on_its_own(self) -> None:
        for index, toml in enumerate(
            (
                'match_source_pdf_metadata = ["title"]\n',
                '[pdf_checks]\nexpected_metadata = [["Title", "Paper"]]\n',
            )
        ):
            with self.subTest(toml=toml):
                settings = self.settings(toml)
                findings = await inspect_configured_pdf_checks(
                    self.pdf,
                    self.source,
                    "main.tex",
                    self.root / f"work-{index}",
                    DetailRunner(),
                    pdf_check_settings(settings),
                    ResourceBudget(1, 1024),
                )
                self.assertIn(("PDF207", "passed"), {(f.code, f.status) for f in findings})
                self.assertEqual(rule_activation(get_rule("PDF207"), settings).kind, DEFAULT)

    def test_pdf101_requires_both_top_level_dimensions(self) -> None:
        width = "expected_page_width_pt = 612\n"
        height = "expected_page_height_pt = 792\n"
        for partial in (width, height):
            with (
                self.subTest(partial=partial),
                self.assertRaisesRegex(PreparationError, "together"),
            ):
                self.settings(partial)
        settings = self.settings(width + height)
        rules = configured_policy_rules(settings.pdf, settings.max_pages)
        self.assertIn(get_rule("PDF101").name, rules)
        self.assert_dispatch("PDF101", settings, rules)

    def test_pdf205_exemptions_and_pdf215_background_do_not_dispatch(self) -> None:
        for code, companion, threshold in (
            ("PDF205", "sparse_page_exemptions = [2]\n", "min_page_ink_ratio = 0.1\n"),
            ("PDF215", "contrast_background_rgb = [1, 1, 1]\n", "min_text_contrast = 4.5\n"),
        ):
            with self.subTest(code=code):
                table = "[pdf_checks]\n"
                off = self.settings(table + companion)
                rules = configured_pdf_check_rules(off.pdf_checks)
                self.assertNotIn(get_rule(code).name, rules)
                self.assert_dispatch(code, off, rules)
                on = self.settings(table + threshold + (companion if code == "PDF215" else ""))
                rules = configured_pdf_check_rules(on.pdf_checks)
                self.assertIn(get_rule(code).name, rules)
                self.assert_dispatch(code, on, rules)
        with self.assertRaisesRegex(
            PreparationError, "min_text_contrast requires contrast_background_rgb"
        ):
            self.settings("[pdf_checks]\nmin_text_contrast = 4.5\n")

    def test_artwork_combinations_are_local_to_each_table_and_aliases_normalize(self) -> None:
        region = (
            '{name = "Figure", page = 1, left_pt = 0, top_pt = 0, right_pt = 100, bottom_pt = 100}'
        )
        trim = f"trim_regions = [{region}]\nprinter_mark_max_length_pt = 4\n"
        category = 'artwork_category = "line"\n'
        dpi = 'category_min_dpi = [["line", 300]]\n'
        regions = f"raster_regions = [{region}]\n"
        coverage = "min_raster_region_coverage = 0.8\n"
        for group, alias in (
            ("rendered_pdf_artwork", "pdf_artwork"),
            ("included_figure_artwork", "figure_artwork"),
        ):
            with self.subTest(group=group):
                settings = self.settings(
                    f"[{alias}]\n" + trim + category + "printer_mark_exemptions = [1]\n"
                )
                rules = configured_artwork_rules(getattr(settings, group))
                self.assertEqual(rules, [])
                for code in ("PDF301", "PDF307"):
                    self.assert_dispatch(code, settings, rules)
                for partial in ("detect_printer_marks = true\n", dpi, regions, coverage):
                    with self.subTest(partial=partial), self.assertRaises(PreparationError):
                        self.settings(f"[{group}]\n" + partial)
                settings = self.settings(
                    f"[{group}]\n"
                    + "detect_printer_marks = true\n"
                    + trim
                    + category
                    + dpi
                    + regions
                    + coverage
                )
                rules = configured_artwork_rules(getattr(settings, group))
                self.assertEqual(
                    rules, [get_rule(code).name for code in ("PDF301", "PDF307", "PDF310")]
                )
                for code in ("PDF301", "PDF307", "PDF310"):
                    self.assert_dispatch(code, settings, rules)
        with self.assertRaisesRegex(PreparationError, "must name a category listed"):
            self.settings('[rendered_pdf_artwork]\nartwork_category = "photo"\n' + dpi)
        with self.assertRaisesRegex(PreparationError, "together"):
            self.settings(f"[rendered_pdf_artwork]\n{regions}[included_figure_artwork]\n{coverage}")

    def test_abbreviation_exceptions_tune_but_do_not_enable_man004(self) -> None:
        table = "[manuscript_checks]\n"
        settings = self.settings(table + 'abstract_abbreviation_exceptions = ["ABC"]\n')
        findings = check_manuscript_policies(self.source, "main.tex", settings.manuscript_checks)
        self.assertNotIn("MAN004", {f.code for f in findings})
        self.assertEqual(rule_activation(get_rule("MAN004"), settings).kind, OPT_IN)
        settings = self.settings(table + 'abstract_abbreviation_policy = "forbid"\n')
        findings = check_manuscript_policies(self.source, "main.tex", settings.manuscript_checks)
        self.assertIn("MAN004", {f.code for f in findings})
        self.assertEqual(rule_activation(get_rule("MAN004"), settings).kind, DEFAULT)

    def test_venue_engine_list_enables_tex013_but_build_year_only_tunes_bld105(self) -> None:
        for toml, venue_check in (
            ("", False),
            ('[submission_checks]\nvenue_engines = ["xelatex"]\n', True),
        ):
            with self.subTest(toml=toml):
                settings = self.settings(toml)
                findings = check_submission_readiness(
                    self.source, "main.tex", settings.bbl_checks, engine="pdflatex"
                )
                self.assertEqual("TEX013" in {f.code for f in findings}, venue_check)
                self.assert_dispatch("TEX013", settings, [f.rule for f in findings])
        build = BuildResult(success=True)
        for toml, severity in (
            ("", "info"),
            ("[build_checks]\nexpected_texlive_year = 2026\n", "warning"),
        ):
            with self.subTest(toml=toml):
                settings = self.settings(toml)
                finding = check_tex_distribution(build, settings.build_checks)
                self.assertEqual((finding.code, finding.severity), ("BLD105", severity))
                self.assertEqual(rule_activation(get_rule("BLD105"), settings).kind, DEFAULT)

    async def test_archive_limit_only_checks_an_archive_that_prepare_creates(self) -> None:
        settings = self.settings("[submission_checks]\nmax_archive_bytes = 10000\n")
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                request = JobRequest(
                    JobCommand.PREPARE, self.source, settings, self.root / "out", dry_run=dry_run
                )
                report = await run_job(request, backends=controlled_backends())
                self.assertEqual(
                    [f.status for f in report.findings if f.code == "PKG105"],
                    [] if dry_run else ["passed"],
                )

    async def test_rendered_region_checks_need_locations_but_input_figures_default_to_whole_pages(
        self,
    ) -> None:
        thresholds = "max_figure_whitespace_ratio = 0.5\nfigure_edge_band_pt = 2\n"
        settings = self.settings(
            "[rendered_pdf_artwork]\n" + thresholds + "[included_figure_artwork]\n" + thresholds
        )
        manuscript = await inspect_pdf_artwork(
            self.pdf, self.root / "rendered", ArtworkRunner(), options=settings.rendered_pdf_artwork
        )
        figures = await inspect_artwork_inputs(
            (self.pdf,),
            self.root / "figures",
            ArtworkRunner(),
            options=settings.included_figure_artwork,
        )
        for code in ("PDF302", "PDF303"):
            with self.subTest(code=code):
                self.assertEqual([f.status for f in manuscript if f.code == code], ["inconclusive"])
                self.assertIn(code, {f.code for f in figures if f.status != "inconclusive"})

    async def test_online_permission_and_the_rule_flag_have_separate_jobs(self) -> None:
        (self.source / "main.tex").write_text(
            r"\documentclass{article}\bibliography{refs}", encoding="utf-8"
        )
        (self.source / "refs.bib").write_text(
            "@article{key, doi={10.1234/example}}\n", encoding="utf-8"
        )
        for flags, status in (
            ("online = true\n", None),
            ("online_doi_resolution = true\n", "skipped"),
            ("online_doi_resolution = true\nonline = true\n", "passed"),
        ):
            with self.subTest(flags=flags):
                settings = self.settings("[online_checks]\n" + flags)
                transport = FakeTransport(Response(200))
                findings = await check_online_references(
                    self.source,
                    options=settings.online_checks,
                    resolver=public_resolver,
                    transport=transport,
                )
                self.assertEqual(
                    [f.status for f in findings if f.code == "NET001"], [status] if status else []
                )
                self.assertEqual(len(transport.requests), int(status == "passed"))

    async def test_fmt_reports_differences_but_prepare_applies_them_instead(self) -> None:
        output = CommandResult(0, "formatted\n", "", resolved_command=[])
        for command, toml in (("fmt", ""), ("prepare", "format = true\n")):
            with self.subTest(command=command):
                settings = self.settings(toml)
                self.assertEqual(settings.apply_formatting, command == "prepare")
                runner = fake_formatter([output, output])
                module = "standalone_checks" if command == "fmt" else "document_run"

                async def propose(source, work, _runner, options, *, runner=runner, **_kwargs):
                    return await format_project(source, work, runner, options)

                with (
                    patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"),
                    patch(f"latexprep.{module}.format_project", side_effect=propose) as formatter,
                ):
                    destination = self.root / "out" if command == "prepare" else None
                    report = await run_job(
                        JobRequest(JobCommand(command), self.source, settings, destination),
                        backends=controlled_backends(),
                    )
                formatter.assert_awaited_once()
                self.assertEqual("FMT001" in {f.code for f in report.findings}, command == "fmt")

    def test_falsy_enablers_still_count_as_configured(self) -> None:
        settings = self.settings(
            "[build_checks]\noverfull_tolerance_pt = 0\n"
            "[manuscript_checks]\nallowed_packages = []\n"
            "[figure_artwork]\nclassify_type3_glyphs = true\n"
        )
        for code in ("BLD101", "MAN001", "PDF308"):
            with self.subTest(code=code):
                self.assertEqual(rule_activation(get_rule(code), settings).kind, DEFAULT)


class RuleExplanationTests(unittest.TestCase):
    """Explaining one check: when it runs, and suggestions for a code that does not exist."""

    def test_the_explanation_says_when_the_check_runs(self) -> None:
        for code, expected in (
            ("TEX001", "Runs: by default"),
            ("PDF001", "Runs: only once configured; set max_pages."),
            ("PDF205", "set min_page_ink_ratio in [pdf_checks]."),
            ("PDF215", "set min_text_contrast and contrast_background_rgb in [pdf_checks]."),
            ("FMT001", "Runs: only with fledge fmt. No setting switches on this diagnostic."),
            (
                "NET001",
                "--online can grant permission instead of setting online in [online_checks].",
            ),
            ("PKG105", "Only measured when fledge prepare creates the archive."),
            ("TEX005", "Runs: always"),
        ):
            with self.subTest(code=code):
                result = CliRunner().invoke(cli, ["rule", code.lower()], terminal_width=200)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertIn(expected, " ".join(result.stdout.split()))
        payload = json.loads(CliRunner().invoke(cli, ["rule", "PDF001", "--json"]).stdout)
        self.assertEqual(payload["activation"], OPT_IN)

    def test_pdf207_explains_top_level_and_table_settings_separately(self) -> None:
        result = CliRunner().invoke(cli, ["rule", "PDF207"], terminal_width=200)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(
            "set match_source_pdf_metadata or set expected_metadata in [pdf_checks].",
            " ".join(result.stdout.split()),
        )

    def test_suggestions_are_marked_and_say_how_to_select_them(self) -> None:
        listed = {
            rule["code"]: rule
            for rule in json.loads(CliRunner().invoke(cli, ["rules", "--json"]).stdout)["rules"]
        }
        for code in ("STY101", "STY301", "BLD013"):
            with self.subTest(code=code):
                self.assertEqual(listed[code]["activation"], SUGGESTION)
                self.assertTrue(listed[code]["suggestion"])
                self.assertEqual(listed[code]["enabled_by"], [f"--select ALL --select {code}"])
        result = CliRunner().invoke(cli, ["rule", "STY301"], terminal_width=200)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Runs: only when selected", result.stdout)
        self.assertIn("--select ALL --select STY301", result.stdout)
        self.assertIn("Reference: https://ctan.org/pkg/lshort-english", result.stdout)

    def test_a_mistyped_code_suggests_the_closest_codes(self) -> None:
        result = CliRunner().invoke(cli, ["rule", "TEX01"])
        self.assertEqual(result.exit_code, USAGE_EXIT_CODE, result.output)
        # TEX01 starts several codes (TEX010 and up), and is also one zero short of TEX001.
        self.assertIn("run 'fledge rules TEX01' to list them", result.stderr)
        self.assertIn("did you mean TEX001", result.stderr)
        for typed, first in (("tex01", "TEX001"), ("TEX0001", "TEX001"), ("tx001", "TEX001")):
            with self.subTest(typed=typed):
                with self.assertRaisesRegex(ValueError, f"did you mean {first}"):
                    get_rule(typed)

    def test_a_family_points_to_the_list_of_its_checks(self) -> None:
        with self.assertRaisesRegex(ValueError, "run 'fledge rules BIB' to list them"):
            get_rule("bib")
        with self.assertRaisesRegex(ValueError, "did you mean source.edit_marker"):
            get_rule("source.edit_markr")


class CompletionTests(unittest.TestCase):
    """Shell completion of check codes and selectors."""

    def completions(self, *words: str) -> list[str]:
        """Return what bash completion offers after ``fledge`` and ``words``."""
        result = CliRunner().invoke(
            cli,
            [],
            prog_name="fledge",
            env={
                "_FLEDGE_COMPLETE": "bash_complete",
                "COMP_WORDS": " ".join(("fledge", *words)),
                "COMP_CWORD": str(len(words)),
            },
        )
        return [line.split(",", 1)[1] for line in result.output.splitlines() if "," in line]

    def test_selectors_and_codes_complete_in_any_case(self) -> None:
        selectors = [item.value for item in complete_check_selectors(None, None, "tex00")]
        self.assertIn("TEX00", selectors)
        self.assertIn("TEX001", selectors)
        codes = [item.value for item in complete_check_codes(None, None, "tex00")]
        self.assertNotIn("TEX00", codes)
        self.assertIn("TEX001", codes)
        [described] = complete_check_codes(None, None, "TEX001")
        self.assertEqual(described.help, get_rule("TEX001").title)

    def test_the_shell_receives_codes_for_select_ignore_rule_and_rules(self) -> None:
        self.assertIn("TEX001", self.completions("inspect", "paper", "--select", "TEX00"))
        self.assertIn("BIB", self.completions("check", "paper", "--ignore", "BI"))
        self.assertIn("TEX001", self.completions("rule", "tex00"))
        self.assertIn("PDF", self.completions("rules", "PD"))

    def test_main_completes_the_tex_files_inside_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("main.tex", "sections/intro.tex", ".hidden/x.tex", "figure.pdf"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("x\n")
            (root / "linked.tex").symlink_to(root / "main.tex")
            (root / "evil\x1b[31m.tex").write_text("x\n")
            (root / "two\nlines.tex").write_text("x\n")
            self.assertEqual(
                self.completions("check", str(root), "--main", ""),
                ["main.tex", "sections/intro.tex"],
            )
            self.assertEqual(
                self.completions("submission-text", str(root), "-m", "sec"),
                ["sections/intro.tex"],
            )
            self.assertEqual(self.completions("check", str(root / "main.tex"), "-m", ""), [])

    def test_main_never_offers_a_name_that_bash_would_run_or_split(self) -> None:
        # Click's bash script inserts a completion unquoted, so these would run a command
        # or split into several words once the user pressed Enter.
        hostile = ("x;touch pwned;.tex", "$(id).tex", "`id`.tex", "a b.tex", "o'q.tex", "x|y.tex")
        # Letters of any script stay, also when a combining mark spells the accent.
        plain = ("Einführung.tex", "Résumé.tex", "v1.2_final+notes-a.tex")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (*hostile, *plain):
                (root / name).write_text("x\n")
            (root / "$(id)").mkdir()
            (root / "$(id)" / "inside.tex").write_text("x\n")
            self.assertEqual(
                sorted(self.completions("check", str(root), "--main", "")), sorted(plain)
            )

    def test_commands_complete_in_workflow_order(self) -> None:
        self.assertEqual(self.completions("")[:3], ["inspect", "check", "prepare"])


if __name__ == "__main__":
    unittest.main()
